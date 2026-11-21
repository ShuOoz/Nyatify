"""The songs lined up for one server, and the helpers the player shares."""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import wavelink

from config import BASE_DIR
from music import source
from music.player.radio import _words_in

log = logging.getLogger(__name__)

T = TypeVar("T")

def _keep_loaded(name: str, default: T, namespace: dict[str, Any]) -> T:
    """Keep a value a reload would otherwise replace."""
    return cast(T, namespace[name]) if name in namespace else default

# Waiting songs only. The one already playing sits in front of this list.
QUEUE_SONGS = 15
HISTORY_SONGS = 50
_CHANGED = "The playlist changed."
_MODES = ("repeat", "whole", "shuffle", "radio", "paused")

@dataclass(frozen=True)
class _Kept:
    title: str; author: str
    uri: str | None; artwork: str | None
    length: int; is_stream: bool

@dataclass
class _Song:
    track: object; requester_id: int | None; platform: str
    videos: tuple[str, ...] = (); video_at: int = 0
    drops: int = 0; station: str = ""

@dataclass
class _Line:
    # repeat replays the current song. whole keeps the playlist. held plays when a waiting slot opens.
    # shuffle mixes the waiting songs; order is that sequence. radio leaves the queue until it is off.
    # paused and position are a saved pause, in milliseconds. resting is not on a player yet. handoff set the current song aside.
    earlier: list[_Song] = field(default_factory=list)
    current: _Song | None = None
    later: list[_Song] = field(default_factory=list)
    held: list[_Song] = field(default_factory=list)
    volume: int = 100
    repeat: bool = False; whole: bool = False; shuffle: bool = False; radio: bool = False
    radio_next: tuple[str, str] | None = None
    # Names still to search, then songs already searched. A skip plays the first ready one. radio_miss would not start.
    radio_plan: list[tuple[str, str]] = field(default_factory=list)
    radio_ready: list = field(default_factory=list)
    radio_miss: list[tuple[str, str]] = field(default_factory=list)
    # The song radio was opened on. Suggestions stay about this one.
    radio_from: _Song | None = None
    order: list[_Song] = field(default_factory=list)
    paused: bool = False; position: int = 0
    resting: bool = False; handoff: bool = False

@dataclass
class PlayState:
    title: str; author: str
    uri: str | None; artwork: str | None
    position: int; length: int; paused: bool
    requester_id: int | None; channel_id: int | None; volume: int
    upcoming: str | None; has_track: bool; stream: bool
    repeating: bool = False; shuffling: bool = False
    radio: bool = False; radio_next: str | None = None

@dataclass
class Queued:
    title: str; platform: str; url: str | None
    playing: bool; stream: bool
    author: str = ""; length: int = 0; requester_id: int | None = None

# A cog reload runs this file again. These stay, or the next save would replace the live queue.
_keep = _keep_loaded
_requesters: dict[int, int] = _keep("_requesters", {}, globals())
_heard: dict[int, list[_Song]] = _keep("_heard", {}, globals())
_missing: set[int] = _keep("_missing", set(), globals())
# A newer playlist or a stop ends the one still opening. Placement stays in list order.
_saved_generation: dict[int, int] = _keep("_saved_generation", {}, globals())
_saved_lock: dict[int, asyncio.Lock] = _keep("_saved_lock", {}, globals())
_saved_task: dict[int, asyncio.Task] = _keep("_saved_task", {}, globals())
_lines: dict[int, _Line] = _keep("_lines", {}, globals())
_gates: dict[int, asyncio.Lock] = _keep("_gates", {}, globals())
# The prepare task for a station. A reload keeps it, so two prepares do not run.
_radio_epoch: dict[int, int] = _keep("_radio_epoch", {}, globals())
_radio_fills: dict[int, tuple[int, asyncio.Task]] = _keep("_radio_fills", {}, globals())
# The task inside _hold. A player refresh must not change the line while that task is.
_holders: dict[int, asyncio.Task] = _keep("_holders", {}, globals())
_line_watchers: list[Callable[[object], Awaitable[None]]] = _keep("_line_watchers", [], globals())
_paint_pending: set[int] = _keep("_paint_pending", set(), globals())
_paint_tasks: dict[int, asyncio.Task] = _keep("_paint_tasks", {}, globals())
# The bot has joined this server. Cleared when that leave is stored. One id, every guild copy.
_bots_in: set[int] = _keep("_bots_in", set(), globals())

def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None

def _text(value: object, fallback: str = "") -> str:
    return (" ".join(value.split()) if isinstance(value, str) else "") or fallback

def _http(value: object) -> str | None:
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None

_COVER_PX = 300
# Apple puts the pixel size in the last segment, including the {w}x{h} template.
_APPLE_SIZE = re.compile(
    r"^(?:\{[wh]\}|\d+)x(?:\{[wh]\}|\d+)[a-z]{0,4}(?:-\d+)?\.(?:jpe?g|png|webp)$",
    re.IGNORECASE,
)

def cover_url(value: object) -> str | None:
    """A picture the card can show. A cover is asked for small, so Discord can load it."""
    url = _http(value)
    if url is None:
        return None
    parts = urlsplit(url)
    host = parts.netloc.lower().split(":", 1)[0]
    if host == "music.126.net" or host.endswith(".music.126.net"):
        query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True) if key != "param"]
        query.append(("param", f"{_COVER_PX}y{_COVER_PX}"))
        return urlunsplit(("https", parts.netloc, parts.path, urlencode(query), ""))
    if host == "mzstatic.com" or host.endswith(".mzstatic.com"):
        return urlunsplit(("https", parts.netloc, _apple_cover(parts.path), parts.query, ""))
    return url

def _apple_cover(path: str) -> str:
    """The full Apple file is thousands of pixels. That size does not load on the card."""
    head, _, tail = path.rpartition("/")
    if not head or _APPLE_SIZE.match(tail) is None:
        return path
    return f"{head}/{_COVER_PX}x{_COVER_PX}bb.jpg"

def _guild_id(guild: object | None) -> int | None: return _number(getattr(guild, "id", None))

def _state_get(guild: object | None, name: str) -> object | None:
    # A stand-in method is callable too. Only a real function may look up the cache.
    guild_id = _guild_id(guild)
    getter = getattr(getattr(guild, "_state", None), name, None)
    if guild_id is None or not (inspect.isfunction(getter) or inspect.ismethod(getter)):
        return None
    try:
        return getter(guild_id)
    except Exception:
        return None

def note_bot_in(guild: object | None) -> None:
    if (guild_id := _guild_id(guild)) is not None:
        _bots_in.add(guild_id)

def forget_bot(guild: object | None) -> None:
    if (guild_id := _guild_id(guild)) is not None:
        _bots_in.discard(guild_id)

def bot_was_in(guild: object | None) -> bool:
    guild_id = _guild_id(guild)
    return guild_id is not None and guild_id in _bots_in

def cached_guild(guild: object | None) -> object | None:
    """The client's guild for this id. A copy from a click is left out."""
    found = _state_get(guild, "get_guild")
    return None if found is None or found is guild or _guild_id(found) != _guild_id(guild) else found

def _voice_client(guild: object | None) -> object | None:
    """The client on this guild. A stand-in grows a fake one the moment it is read."""
    if guild is None or (type(guild).__module__ == "unittest.mock" and "voice_client" not in guild.__dict__):
        return None
    return getattr(guild, "voice_client", None)

def bot_connected(guild: object | None, other: object | None = None) -> bool:
    guild_id = _guild_id(guild)
    for item in (guild, other, cached_guild(guild)):
        if item is not None and _guild_id(item) == guild_id and _voice_client(item) is not None: return True
    return _state_get(guild, "_get_voice_client") is not None

def _title(track: object) -> str: return _text(getattr(track, "title", None), "Unknown")

def _voice_player(guild: object | None) -> wavelink.Player | None:
    return player if isinstance(player := getattr(guild, "voice_client", None), wavelink.Player) else None

def _lock(locks: dict[int, asyncio.Lock], guild_id: int) -> asyncio.Lock:
    return locks.setdefault(guild_id, asyncio.Lock())

@asynccontextmanager
async def _hold(guild: object | None) -> AsyncIterator[None]:
    guild_id = _guild_id(guild)
    if guild_id is None:
        yield
        return
    async with _lock(_gates, guild_id):
        _holders[guild_id] = asyncio.current_task()
        try:
            yield
        finally:
            if _holders.get(guild_id) is asyncio.current_task():
                _holders.pop(guild_id, None)

def _held_by_other(guild: object | None) -> bool:
    holder = None if (guild_id := _guild_id(guild)) is None else _holders.get(guild_id)
    return holder is not None and not holder.done() and holder is not asyncio.current_task()

def _saved_lock_for(guild_id: int) -> asyncio.Lock: return _lock(_saved_lock, guild_id)

def _end_saved(guild_id: int) -> int:
    _saved_generation[guild_id] = token = _saved_generation.get(guild_id, 0) + 1
    return token

def _stored(guild: object | None) -> _Line | None:
    return None if (guild_id := _guild_id(guild)) is None else _lines.get(guild_id)

def _line(guild: object) -> _Line | None:
    if (guild_id := _guild_id(guild)) is None:
        return None
    return _lines.setdefault(guild_id, _Line())

def _drop(guild: object) -> None:
    if (guild_id := _guild_id(guild)) is not None:
        _lines.pop(guild_id, None)

def _remember_id(guild: object, user_id: int | None) -> None:
    if (guild_id := _guild_id(guild)) is None: return
    if user_id is None: _requesters.pop(guild_id, None)
    else: _requesters[guild_id] = user_id

def _forget(guild: object) -> None: _remember_id(guild, None)

def watch_line(callback: Callable[[object], Awaitable[None]]) -> None: _line_watchers.append(callback)

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

def _channel_id(value: object) -> int | None:
    number = _number(getattr(value, "id", None))
    return number if number is not None and number > 0 else None


def _first(items, ok):
    return next((item for item in items if ok(item)), None)


@dataclass(frozen=True, slots=True)
class _ChannelId:
    """A channel Discord named by id after the channel object was cleared."""
    id: int


def _voice_channel(member: object | None) -> object | None:
    return getattr(getattr(member, "voice", None), "channel", None)


def _open(value: object | None) -> object | None:
    return value if _channel_id(value) is not None else None


def _saved_channel(player: object) -> object | None:
    """Channel Lavalink kept after the channel object was cleared."""
    state = getattr(player, "_voice_state", None)
    raw = state.get("channel_id") if isinstance(state, dict) else None
    number = int(raw) if isinstance(raw, str) and raw.isdecimal() else _number(raw)
    return _ChannelId(number) if isinstance(number, int) and number > 0 else None


def _seen_channel(member: object, guild: object | None) -> object | None:
    # A click can hand a member from a guild copy with no voice cache.
    direct = _voice_channel(member)
    if direct is not None or guild is None or (user_id := _channel_id(member)) is None: return direct
    getter = getattr(guild, "get_member", None)
    cached = getter(user_id) if callable(getter) else None
    voice = None if cached in (None, member) else _voice_channel(cached)
    states = getattr(guild, "_voice_states", None)
    state = states.get(user_id) if isinstance(states, dict) else None
    return _first((voice, getattr(state, "channel", None)), lambda item: _channel_id(item) is not None)


def _occupied_channel(guild: object | None) -> object | None:
    # This copy wins when it has an answer. A click's guild with no voice uses the cached server.
    found = _room_on(guild)
    if found is not None or _voice_client(guild) is not None: return found
    cached = cached_guild(guild)
    return None if cached is None else _room_on(cached)


def _room_on(guild: object | None) -> object | None:
    # The bot's Discord voice state wins. A player channel after the link dropped is not a room.
    player = _voice_client(guild)
    if player is None: return None
    channel = _open(getattr(player, "channel", None))
    me = getattr(guild, "me", None)
    if _channel_id(me) is not None:
        if (heard := _open(_voice_channel(me))) is not None: return heard
        if channel is not None:
            # The link is up. The member cache can have no channel while the player stays.
            return channel if getattr(player, "_connected", None) is True else None
    return channel or _saved_channel(player)

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

def _stream(track: object) -> bool: return getattr(track, "is_stream", None) is True

def _movable(track: object) -> bool:
    length = _number(getattr(track, "length", None)) or 0
    return getattr(track, "is_seekable", None) is not False and length > 0

def _each(results: object) -> list:
    tracks = getattr(results, "tracks", None)
    return tracks if isinstance(tracks, list) else results if isinstance(results, list) else []

def _names(track: object) -> tuple[str, str]:
    return _text(getattr(track, "title", None)).casefold(), _text(getattr(track, "author", None)).casefold()

def _same_track(left: object, right: object) -> bool:
    one, other = getattr(left, "identifier", None), getattr(right, "identifier", None)
    if isinstance(one, str) and isinstance(other, str) and one and other: return one == other
    return _names(left)[0] == _names(right)[0]

def _find(songs: list[_Song], match: Callable[[_Song], bool]) -> int | None:
    found = _first(enumerate(songs), lambda pair: match(pair[1]))
    return None if found is None else found[0]


def _queued_index(songs: list[_Song], track: object) -> int | None:
    uri, (title, author) = _http(getattr(track, "uri", None)), _names(track)
    if not uri and not title: return None

    def hit(song: _Song) -> bool:
        song_uri = _http(getattr(song.track, "uri", None))
        if uri: return bool(song_uri and source.same_url(song_uri, uri))
        other, who = _names(song.track)
        return other == title and (not author or not who or who == author)

    return _find(songs, hit)


def _placed(line: _Line | None, track: object) -> tuple[_Song | None, _Song | None]:
    """The song on, and the next one. A live track still first in line is not up next."""
    if line is None: return None, None
    later, current = line.later, line.current
    index = None if current is not None or track is None or not later else _queued_index(later, track)
    song = current if current is not None else None if index is None else later[index]
    skip = 1 if index == 0 else 0
    return song, later[skip] if skip < len(later) else None


def _coming(line: _Line | None, track: object) -> _Song | None:
    return _placed(line, track)[1]

def _same_recording(left: object, right: object) -> bool:
    """The same video. A different spelling of the address still counts."""
    one, two = _http(getattr(left, "uri", None)), _http(getattr(right, "uri", None))
    if one and two:
        return source.same_url(one, two)
    return _same_track(left, right)

def _on_air(line: _Line | None, track: object) -> bool:
    if _stream(track): return True
    song = _placed(line, track)[0]
    if song is not None:
        _note_station(song)
    if song is None or not song.videos: return False
    # Still parked on the queue after a reload. The live track was already matched.
    if song is not line.current or song.track is track: return True
    return _same_recording(song.track, track)

def _spread(later: list[_Song], songs: list[_Song], *, mix: bool) -> None:
    if not mix:
        later.extend(songs)
        return
    for song in songs:
        later.insert(random.randrange(len(later) + 1), song)

def _pull(songs: list[_Song], song: _Song) -> None:
    if (index := _find(songs, lambda item: item is song)) is not None: songs.pop(index)

def _order_add(line: _Line, songs: list[_Song]) -> None:
    if line.shuffle and songs: line.order.extend(songs)

def _order_drop(line: _Line, song: _Song) -> None: _pull(line.order, song)

def _fill_held(line: _Line | None) -> None:
    """Move parked songs into the waiting line until the cap is full again."""
    if line is None or not line.held or len(line.later) >= QUEUE_SONGS: return
    moved = line.held[:QUEUE_SONGS - len(line.later)]
    del line.held[:len(moved)]
    line.later.extend(moved)
    _order_add(line, moved)

def _promote_waiting(line: _Line | None) -> bool:
    if line is None or line.handoff or line.current is not None:
        return False
    _fill_held(line)
    if not line.later:
        return False
    line.current = line.later.pop(0)
    _fill_held(line)
    line.resting, line.paused, line.position = True, False, 0
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
    pool, held, bound = list(waiting), ([] if playing is None else [playing]), []
    for song in wanted:
        for group in (pool, held):
            if (index := _queued_index(group, song.track)) is not None:
                bound.append(group.pop(index))
                break
    return bound + pool

def _song_name(song: _Song) -> str:
    title, author = _title(song.track), _text(getattr(song.track, "author", None))
    return title if not author or _words_in(author, title) else f"{title} - {author}"

def _who_asked(guild: object, line: _Line | None, track: object) -> int | None:
    active = _placed(line, track)[0]
    if active is not None and active.requester_id is not None:
        return active.requester_id
    guild_id = _guild_id(guild)
    return None if guild_id is None else _requesters.get(guild_id)

from music.playback.arrange import _adopt_previous, _note_station, _recall, _store

def now_playing(guild: object | None) -> PlayState | None:
    """The song on now. A restart still shows one left playing or paused, and nothing when voice is empty."""
    from music.lavalink import joining
    from music.playback.arrange import _claim_current

    if guild is None:
        return None
    if (guild_id := _guild_id(guild)) is not None:
        _recall(guild_id)
    player, line = _voice_player(guild), _stored(guild)
    in_voice = getattr(guild, "voice_client", None) is not None
    live = None if player is None else player.current
    # Skip of the last song stops the audio. A late track object is not that song.
    if player is not None and getattr(player, "_idle", False):
        if line is None or (line.current is None and not line.later):
            live = None
        else:
            player._idle = False
    # A refresh while a song is starting must not take a waiting song out of the line.
    if not _held_by_other(guild):
        if line is not None and line.current is None and live is not None:
            _claim_current(line, live, guild)
        if live is None and _promote_waiting(line):
            _store(guild)
        # A leave, a dropped link, or a reload leaves the song up with nobody in voice.
        parked = line is not None and line.current is not None and not line.resting and live is None
        if parked and not in_voice and joining(guild) is None:
            line.resting = True
        if player is not None and line is not None and line.resting and live is not None:
            line.resting = False
            line.paused = player.paused is True
    saved = None if line is None or not line.resting or line.current is None else line.current.track
    track = live if live is not None else saved
    if track is None and not in_voice:
        return None
    length = _number(getattr(track, "length", None)) or 0
    position = (_number(getattr(player, "position", None)) or 0) if live is not None else 0
    paused = player.paused is True if live is not None else bool(line is not None and line.paused)
    # A pause report can sit at the start. The position saved then is where the song was.
    if paused and line is not None and line.position > position:
        position = line.position
    if length:
        position = min(max(0, position), length)
    coming = _coming(line, track)
    channel = None if player is None else getattr(player, "channel", None)
    shown = track is not None
    radio = None if line is None or line.radio_next is None else line.radio_next[1]
    return PlayState(
        _title(track) if shown else "",
        _text(getattr(track, "author", None)) if shown else "",
        _http(getattr(track, "uri", None)) if shown else None,
        cover_url(getattr(track, "artwork", None)) if shown else None,
        position, length, paused,
        _who_asked(guild, line, track) if shown else None,
        _number(getattr(channel, "id", None)),
        100 if line is None else line.volume,
        None if coming is None else _song_name(coming),
        shown, shown and _on_air(line, track),
        line is not None and line.repeat,
        line is not None and line.shuffle,
        line is not None and line.radio,
        radio,
    )

def save_and_clear(guild: object | None) -> None:
    """Save the queue, then drop it from memory. The bot has left voice."""
    guild_id = _guild_id(guild)
    if guild_id is None:
        return
    forget_bot(guild)
    from music.playback.idle import _cancel_pause_leave

    _cancel_pause_leave(guild)
    # A second leave already cleared the live line. Writing again would drop the waiting songs.
    if guild_id in _lines or guild_id in _requesters:
        _store(guild)
        _drop(guild)
        _forget(guild)


# Line record. The queue and the history, stored in data/music_session.json.

PATH = _keep("PATH", BASE_DIR / "data" / "music_session.json", globals())
_PREVIOUS_NAME = "music_line.json"
_cache_path: Path | None = _keep("_cache_path", None, globals())
_book: dict[str, dict] | None = _keep("_book", None, globals())
# A failed read stays failed until the path changes, so a save cannot replace it.
_failed_path: Path | None = None
_adopt_previous(PATH, _PREVIOUS_NAME)
