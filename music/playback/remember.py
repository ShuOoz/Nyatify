"""The songs lined up for one server, and the helpers the player shares."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

import wavelink

from music import archive, source
from music.player.radio import _words_in

log = logging.getLogger(__name__)

VOLUME_STEP = 10
# Waiting songs only. The one already playing sits in front of this list.
QUEUE_SONGS = 15
# A saved playlist opens a few links at once. The first song starts before the rest finish.
_OPEN_AT_ONCE = 4
HISTORY_SONGS = 50
_CHANGED = "The playlist changed."
_MODES = ("repeat", "whole", "shuffle", "radio", "paused")

@dataclass(frozen=True)
class _Kept:
    title: str
    author: str
    uri: str | None
    artwork: str | None
    length: int
    is_stream: bool

@dataclass
class _Song:
    track: object
    requester_id: int | None
    platform: str
    videos: tuple[str, ...] = ()
    video_at: int = 0
    drops: int = 0
    station: str = ""

@dataclass
class _Line:
    # repeat replays the current song. whole means the visible queue is the whole playlist.
    # held is what did not fit, in order, and plays when a waiting slot opens.
    # shuffle mixes the waiting songs; order is that sequence. radio leaves the queue until it is off.
    # paused and position are a saved pause, in milliseconds. resting is not on a player yet. handoff set the current song aside.
    earlier: list[_Song] = field(default_factory=list)
    current: _Song | None = None
    later: list[_Song] = field(default_factory=list)
    held: list[_Song] = field(default_factory=list)
    volume: int = 100
    repeat: bool = False
    whole: bool = False
    shuffle: bool = False
    radio: bool = False
    order: list[_Song] = field(default_factory=list)
    paused: bool = False
    position: int = 0
    resting: bool = False
    handoff: bool = False

@dataclass
class PlayState:
    title: str
    author: str
    uri: str | None
    artwork: str | None
    position: int
    length: int
    paused: bool
    requester_id: int | None
    channel_id: int | None
    volume: int
    upcoming: str | None
    has_track: bool
    stream: bool
    repeating: bool = False
    shuffling: bool = False
    radio: bool = False

@dataclass
class Queued:
    title: str
    platform: str
    url: str | None
    playing: bool
    stream: bool
    author: str = ""
    length: int = 0
    requester_id: int | None = None

# A cog reload runs this file again. These stay, or the next save would replace the live queue.
_keep = archive._keep_loaded
_requesters: dict[int, int] = _keep("_requesters", {}, globals())
_heard: dict[int, list[_Song]] = _keep("_heard", {}, globals())
_missing: set[int] = _keep("_missing", set(), globals())
# A newer playlist or a stop ends the one still opening. Placement stays in list order.
_saved_generation: dict[int, int] = _keep("_saved_generation", {}, globals())
_saved_lock: dict[int, asyncio.Lock] = _keep("_saved_lock", {}, globals())
_saved_task: dict[int, asyncio.Task] = _keep("_saved_task", {}, globals())
_playlist_skips: dict[int, list[str]] = _keep("_playlist_skips", {}, globals())
_lines: dict[int, _Line] = _keep("_lines", {}, globals())
_gates: dict[int, asyncio.Lock] = _keep("_gates", {}, globals())
_line_watchers: list[Callable[[object], Awaitable[None]]] = _keep("_line_watchers", [], globals())
_paint_pending: set[int] = _keep("_paint_pending", set(), globals())
_paint_tasks: dict[int, asyncio.Task] = _keep("_paint_tasks", {}, globals())

def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
def _text(value: object, fallback: str = "") -> str:
    cleaned = " ".join(value.split()) if isinstance(value, str) else ""
    return cleaned or fallback
def _http(value: object) -> str | None:
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None
def _guild_id(guild: object | None) -> int | None:
    return _number(getattr(guild, "id", None))
def _title(track: object) -> str:
    return _text(getattr(track, "title", None), "Unknown")
def _voice_player(guild: object | None) -> wavelink.Player | None:
    player = getattr(guild, "voice_client", None)
    return player if isinstance(player, wavelink.Player) else None
def _lock(locks: dict[int, asyncio.Lock], guild_id: int) -> asyncio.Lock:
    if guild_id not in locks:
        locks[guild_id] = asyncio.Lock()
    return locks[guild_id]

@asynccontextmanager
async def _hold(guild: object | None) -> AsyncIterator[None]:
    guild_id = _guild_id(guild)
    if guild_id is None:
        yield
        return
    async with _lock(_gates, guild_id):
        yield
def _saved_lock_for(guild_id: int) -> asyncio.Lock:
    return _lock(_saved_lock, guild_id)
def _end_saved(guild_id: int) -> int:
    _saved_generation[guild_id] = token = _saved_generation.get(guild_id, 0) + 1
    return token
def _stored(guild: object | None) -> _Line | None:
    return None if (guild_id := _guild_id(guild)) is None else _lines.get(guild_id)
def _line(guild: object) -> _Line | None:
    if (guild_id := _guild_id(guild)) is None:
        return None
    if guild_id not in _lines:
        _lines[guild_id] = _Line()
    return _lines[guild_id]
def _drop(guild: object) -> None:
    if (guild_id := _guild_id(guild)) is not None:
        _lines.pop(guild_id, None)
def _remember_id(guild: object, user_id: int | None) -> None:
    if (guild_id := _guild_id(guild)) is None:
        return
    if user_id is None:
        _requesters.pop(guild_id, None)
    else:
        _requesters[guild_id] = user_id
def _forget(guild: object) -> None:
    _remember_id(guild, None)

def watch_line(callback: Callable[[object], Awaitable[None]]) -> None:
    _line_watchers.append(callback)
def unwatch_line(callback: Callable[[object], Awaitable[None]]) -> None:
    with suppress(ValueError):
        _line_watchers.remove(callback)

async def refresh_line(guild: object | None) -> None:
    if guild is None:
        return
    for callback in list(_line_watchers):
        try:
            await callback(guild)
        except Exception:
            log.exception("music queue could not refresh")

def paint_soon(guild: object | None) -> None:
    guild_id = _guild_id(guild)
    if guild_id is None or not _line_watchers:
        return
    _paint_pending.add(guild_id)
    task = _paint_tasks.get(guild_id)
    if task is None or task.done():
        _paint_tasks[guild_id] = asyncio.create_task(_paint_latest(guild, guild_id), name="music-line")
async def _paint_latest(guild: object, guild_id: int) -> None:
    try:
        while guild_id in _paint_pending:
            _paint_pending.discard(guild_id)
            await refresh_line(guild)
    except Exception:
        log.exception("music queue could not refresh")
    finally:
        if _paint_tasks.get(guild_id) is asyncio.current_task():
            _paint_tasks.pop(guild_id, None)
            if guild_id in _paint_pending:
                _paint_tasks[guild_id] = asyncio.create_task(_paint_latest(guild, guild_id), name="music-line")
def playing_note(line: str) -> bool:
    return line.startswith(("Playing ", "Added "))

def _channel_id(value: object) -> int | None:
    number = _number(getattr(value, "id", None))
    return number if number is not None and number > 0 else None
@dataclass(frozen=True, slots=True)
class _ChannelId:
    """A channel Discord named by id after the channel object was cleared."""
    id: int
def _voice_channel(member: object | None) -> object | None:
    return getattr(getattr(member, "voice", None), "channel", None)
def _with_channel_id(value: object | None) -> object | None:
    return value if _channel_id(value) is not None else None
def _saved_channel(player: object) -> object | None:
    """Channel Lavalink kept after the channel object was cleared."""
    state = getattr(player, "_voice_state", None)
    raw = state.get("channel_id") if isinstance(state, dict) else None
    if isinstance(raw, str) and raw.isdecimal():
        raw = int(raw)
    number = _number(raw)
    return _ChannelId(number) if number is not None and number > 0 else None

def _seen_channel(member: object, guild: object | None) -> object | None:
    # A click can hand a member from a guild copy with no voice cache.
    direct = _voice_channel(member)
    if direct is not None:
        return direct
    user_id = _channel_id(member)
    if guild is None or user_id is None:
        return None
    getter = getattr(guild, "get_member", None)
    cached = getter(user_id) if callable(getter) else None
    if cached is not None and cached is not member:
        found = _with_channel_id(_voice_channel(cached))
        if found is not None:
            return found
    states = getattr(guild, "_voice_states", None)
    state = states.get(user_id) if isinstance(states, dict) else None
    return _with_channel_id(getattr(state, "channel", None))
def _room_from_player(player: object) -> object | None:
    channel = _with_channel_id(getattr(player, "channel", None))
    return channel if channel is not None else _saved_channel(player)

def _occupied_channel(guild: object | None) -> object | None:
    # The bot's Discord voice state wins. A player channel after the link dropped is not a room.
    player = getattr(guild, "voice_client", None)
    if player is None:
        return None
    me = getattr(guild, "me", None)
    if _channel_id(me) is not None:
        heard = _with_channel_id(_voice_channel(me))
        if heard is not None:
            return heard
        channel = getattr(player, "channel", None)
        if _channel_id(channel) is not None:
            # The link is up. The member cache can have no channel while the player stays.
            return channel if getattr(player, "_connected", None) is True else None
    return _room_from_player(player)

def can_edit_queue(member: object, guild: object | None) -> bool:
    room = _channel_id(_occupied_channel(guild))
    return room is None or _channel_id(_seen_channel(member, guild)) == room
def member_in_voice(member: object, guild: object | None) -> bool:
    # A live room only answers from inside it. After a pause, anyone in voice can bring the song back.
    return _channel_id(_seen_channel(member, guild)) is not None and can_edit_queue(member, guild)

def _clear_track(player: object) -> None:
    if hasattr(player, "_track"):
        player._track = None
    player._current = None
def _place(player: object, position: int) -> None:
    player._last_position = position
    player._last_update = time.monotonic_ns()
    if hasattr(player, "_position"):
        player._position = position
def _stream(track: object) -> bool:
    return getattr(track, "is_stream", None) is True
def _movable(track: object) -> bool:
    length = _number(getattr(track, "length", None)) or 0
    return getattr(track, "is_seekable", None) is not False and length > 0
def _each(results: object) -> list:
    tracks = getattr(results, "tracks", None)
    return tracks if isinstance(tracks, list) else results if isinstance(results, list) else []
def _same_track(left: object, right: object) -> bool:
    one, other = getattr(left, "identifier", None), getattr(right, "identifier", None)
    if isinstance(one, str) and isinstance(other, str) and one and other:
        return one == other
    return _text(getattr(left, "title", None)).casefold() == _text(getattr(right, "title", None)).casefold()
def _label(track: object) -> tuple[str, str]:
    return _text(getattr(track, "title", None)).casefold(), _text(getattr(track, "author", None)).casefold()

def _find(songs: list[_Song], match: Callable[[_Song], bool]) -> int | None:
    return next((index for index, song in enumerate(songs) if match(song)), None)
def _queued_index(songs: list[_Song], track: object) -> int | None:
    uri = _http(getattr(track, "uri", None))
    if uri:
        return _find(songs, lambda song: _same_url(song.track, uri))
    title, author = _label(track)
    if not title:
        return None
    return _find(songs, lambda song: _same_name(song.track, title, author))
def _same_url(track: object, uri: str) -> bool:
    song_uri = _http(getattr(track, "uri", None))
    return bool(song_uri and source.same_url(song_uri, uri))
def _same_name(track: object, title: str, author: str) -> bool:
    song_title, song_author = _label(track)
    return song_title == title and (not author or not song_author or song_author == author)
def _active(line: _Line | None, track: object) -> _Song | None:
    if line is None:
        return None
    if line.current is not None:
        return line.current
    index = _queued_index(line.later, track) if track is not None and line.later else None
    return None if index is None else line.later[index]
def _coming(line: _Line | None, track: object) -> _Song | None:
    if line is None or not line.later:
        return None
    index = 1 if line.current is None and track is not None and _queued_index(line.later, track) == 0 else 0
    return line.later[index] if index < len(line.later) else None

def _on_air(line: _Line | None, track: object) -> bool:
    if _stream(track):
        return True
    song = _active(line, track)
    if song is None or not song.videos:
        return False
    # Still parked on the queue after a reload. The live track was already matched.
    if song is not line.current or song.track is track:
        return True
    left, right = _http(getattr(song.track, "uri", None)), _http(getattr(track, "uri", None))
    return left == right if left and right else _same_track(song.track, track)

def _spread(later: list[_Song], songs: list[_Song], *, mix: bool) -> None:
    if not mix:
        later.extend(songs)
        return
    for song in songs:
        later.insert(random.randrange(len(later) + 1), song)
def _pull(songs: list[_Song], song: _Song) -> None:
    if (index := _find(songs, lambda item: item is song)) is not None:
        del songs[index]
def _order_add(line: _Line, songs: list[_Song]) -> None:
    if line.shuffle and songs:
        line.order.extend(songs)
def _order_drop(line: _Line, song: _Song) -> None:
    _pull(line.order, song)
def _fill_held(line: _Line | None) -> None:
    """Move parked songs into the waiting line until the cap is full again."""
    if line is None or not line.held:
        return
    room = QUEUE_SONGS - len(line.later)
    if room <= 0:
        return
    moved = line.held[:room]
    del line.held[:room]
    _spread(line.later, moved, mix=False)
    _order_add(line, moved)

def _promote_waiting(line: _Line | None) -> bool:
    if line is None or line.handoff or line.current is not None:
        return False
    _fill_held(line)
    if not line.later:
        return False
    line.current = line.later.pop(0)
    _fill_held(line)
    line.resting = True
    line.paused = False
    line.position = 0
    return True
def _unshuffle(line: _Line) -> None:
    if line.order:
        waiting = {id(song) for song in line.later}
        restored = [song for song in line.order if id(song) in waiting]
        known = {id(song) for song in restored}
        line.later = restored + [song for song in line.later if id(song) not in known]
    line.order.clear()
    line.shuffle = False
def _bind_order(wanted: list[_Song], waiting: list[_Song], *, playing: _Song | None = None) -> list[_Song]:
    pool, held = list(waiting), [] if playing is None else [playing]
    bound: list[_Song] = []
    for song in wanted:
        for group in (pool, held):
            index = _queued_index(group, song.track)
            if index is not None:
                bound.append(group.pop(index))
                break
    return bound + pool
def _song_name(song: _Song) -> str:
    title, author = _title(song.track), _text(getattr(song.track, "author", None))
    if not author or _words_in(author, title):
        return title
    return f"{title} - {author}"

def save_and_clear(guild: object | None) -> None:
    """Save the queue, then drop it from memory. The bot has left voice."""
    guild_id = _guild_id(guild)
    if guild_id is None:
        return
    from music.playback.start import _cancel_pause_leave

    _cancel_pause_leave(guild)
    # A second leave already cleared the live line. Writing again would drop the waiting songs.
    if guild_id not in _lines and guild_id not in _requesters:
        return
    _store(guild)
    _drop(guild)
    _forget(guild)

def load_saved_lines() -> None:
    # A line already in memory stays. History alone does not: leaving voice drops the line.
    for guild_id, raw in archive.read_all().items():
        if guild_id in _lines and guild_id in _heard:
            continue
        _install(guild_id, raw)
        _missing.discard(guild_id)
def save_open_lines() -> None:
    for guild_id in set(_lines) | set(_heard):
        archive.write_guild(guild_id, _unload_record(guild_id))
def _unload_record(guild_id: int) -> dict | None:
    # Leaving voice stores the queue, then drops it. History alone must not replace that queue.
    record = _record(guild_id)
    if guild_id in _lines:
        return record
    saved = archive.read_guild(guild_id)
    if not isinstance(saved, dict):
        return record
    if record is None:
        return saved
    kept = dict(saved)
    kept["history"] = record.get("history", [])
    return kept
def _store(guild: object | None) -> None:
    guild_id = _guild_id(guild)
    if guild_id is None:
        return
    _missing.discard(guild_id)
    archive.write_guild(guild_id, _record(guild_id))

def _record(guild_id: int) -> dict | None:
    line, heard = _lines.get(guild_id), _heard.get(guild_id, [])
    current = None if line is None else line.current
    later = () if line is None else line.later
    held = () if line is None else line.held
    if current is None and not later and not held and not heard:
        return None
    payload: dict[str, object] = {
        "current": None if current is None else _dump_song(current),
        "later": [_dump_song(song) for song in later],
        "history": [_dump_song(song) for song in heard],
    }
    if held:
        payload["held"] = [_dump_song(song) for song in held]
    if line is None:
        return payload
    if line.volume != 100:
        payload["volume"] = line.volume
    payload.update({name: True for name in _MODES if getattr(line, name)})
    if line.paused and line.position > 0:
        # Saved files still call this spot, so a restart finds the same place.
        payload["spot"] = line.position
    if line.shuffle and line.order:
        payload["order"] = [_dump_song(song) for song in line.order]
    return payload
def _dump_song(song: _Song) -> dict[str, object]:
    track = song.track
    payload: dict[str, object] = {
        "title": _title(track),
        "author": _text(getattr(track, "author", None)),
        "url": source.remembered_share(track) or _http(getattr(track, "uri", None)) or "",
        "platform": song.platform,
        "length": _number(getattr(track, "length", None)) or 0,
        "stream": _stream(track) or bool(song.videos),
        "artwork": _http(getattr(track, "artwork", None)),
        "requester_id": song.requester_id,
    }
    if song.videos:
        payload.update(videos=list(song.videos), video_at=song.video_at, station=song.station)
    return {key: value for key, value in payload.items() if value is not None}
def _recall(guild_id: int) -> None:
    if guild_id in _missing or (guild_id in _lines and guild_id in _heard):
        return
    raw = archive.read_guild(guild_id)
    if raw is None:
        _missing.add(guild_id)
        return
    _install(guild_id, raw, line=guild_id not in _lines, heard=guild_id not in _heard)

def _install(guild_id: int, raw: dict, *, line: bool = True, heard: bool = True) -> None:
    if heard and guild_id not in _heard and (history := _load_rows(raw.get("history"))):
        _heard[guild_id] = history[-HISTORY_SONGS:]
    if not line or guild_id in _lines:
        return
    later = _load_rows(raw.get("later"))
    parked = _load_rows(raw.get("held"))
    current = _load_song(raw.get("current"))
    # The song that was on can be missing. The next queued one takes that place.
    took_next = current is None and bool(later)
    if took_next:
        current = later.pop(0)
    elif current is None and parked:
        current = parked.pop(0)
        took_next = True
    elif current is None:
        return
    paused = not took_next and raw.get("paused") is True
    position = _number(raw.get("spot")) or 0
    shuffled = raw.get("shuffle") is True
    volume = _number(raw.get("volume"))
    line = _Line(
        current=current, later=later, held=parked, volume=100 if volume is None else volume,
        repeat=raw.get("repeat") is True, whole=raw.get("whole") is True,
        shuffle=shuffled, radio=raw.get("radio") is True,
        order=_bind_order(_load_rows(raw.get("order")), later, playing=current) if shuffled else [],
        paused=paused, position=position if paused and position > 0 else 0, resting=current is not None,
    )
    _fill_held(line)
    if line.held:
        line.whole = False
    _lines[guild_id] = line
def _load_rows(raw: object) -> list[_Song]:
    rows = raw if isinstance(raw, list) else []
    return [song for item in rows if (song := _load_song(item)) is not None]
def _load_song(raw: object) -> _Song | None:
    if not isinstance(raw, dict):
        return None
    title = _text(raw.get("title"), "Unknown")
    url = raw.get("url") if isinstance(raw.get("url"), str) else ""
    if title == "Unknown" and not _text(url):
        return None
    listed = raw.get("videos", raw.get("lives"))
    platform = raw.get("platform")
    return _Song(
        _Kept(
            title, _text(raw.get("author")), url or None, _http(raw.get("artwork")),
            _number(raw.get("length")) or 0, raw.get("stream") is True,
        ),
        _number(raw.get("requester_id")),
        source.normalize_platform(platform if isinstance(platform, str) else "youtube"),
        videos=tuple(item for item in listed if isinstance(item, str)) if isinstance(listed, list) else (),
        video_at=_number(raw.get("video_at", raw.get("live_at"))) or 0,
        station=_text(raw.get("station")),
    )

# A link Lavalink has already opened. The next play uses this track.
_opened: dict[str, object] = {}
_opening: dict[str, asyncio.Task] = {}

def keep_track(url: object, track: object) -> None:
    if (link := _http(url)) is not None:
        _opened[link] = track
def peek_track(url: object) -> object | None:
    return None if (link := _http(url)) is None else _opened.get(link)
def warm_saved(guild: object | None, song: object) -> None:
    url = getattr(song, "url", None)
    live = getattr(getattr(guild, "voice_client", None), "current", None)
    if isinstance(url, str) and getattr(live, "uri", None) == url:
        keep_track(url, live)
        return
    platform = getattr(song, "platform", None)
    warm_link(guild, url, platform if isinstance(platform, str) and platform else "youtube")
def warm_link(guild: object | None, url: object, platform: str) -> None:
    link = _http(url)
    if link is not None and link not in _opened and link not in _opening:
        _opening[link] = asyncio.create_task(_warm(guild, link, platform), name="music-warm-link")

async def ready_track(url: object) -> object | None:
    found = peek_track(url)
    if found is not None:
        return found
    link = _http(url)
    task = None if link is None else _opening.get(link)
    if task is None:
        return None
    try:
        await task
    except Exception:
        log.warning("music could not keep a saved song ready", exc_info=True)
    return peek_track(url)
async def _warm(guild: object | None, url: str, platform: str) -> None:
    try:
        from music.lavalink import find_songs

        found = await find_songs(guild, url, platform, mode="track")
        if not isinstance(found, str) and found.tracks:
            keep_track(url, found.tracks[0])
    except Exception:
        log.warning("music could not open a saved song early", exc_info=True)
    finally:
        _opening.pop(url, None)
