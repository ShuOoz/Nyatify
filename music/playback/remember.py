"""The songs lined up for one server, and the helpers the player shares."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import wavelink

from music import archive, source

log = logging.getLogger(__name__)

# Past this, Previous restarts the song. Before it, Previous plays the one before.
NEAR_START_MS = 3_000
JUMP_MS = 15_000
VOLUME_STEP = 10
# Waiting songs only. The one already playing sits in front of this list.
QUEUE_SONGS = 15
# A saved playlist opens a few links at once. The first song starts before the rest finish.
_OPEN_AT_ONCE = 4
HISTORY_SONGS = 50
_CHANGED = "The playlist changed."


@dataclass(frozen=True)
class _Kept:
    """A song read back from disk, shown before Lavalink plays it again."""

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
    earlier: list[_Song] = field(default_factory=list)
    current: _Song | None = None
    later: list[_Song] = field(default_factory=list)
    volume: int = 100
    # Replay this song when it ends. The waiting queue stays put.
    repeat: bool = False
    # A playlist keeps every waiting song. Previous does not cut it back to the cap.
    whole: bool = False


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


# Who asked for the current song, so the player card can name them.
_requesters: dict[int, int] = {}
# Finished songs kept after the live line is dropped. Newest last.
_heard: dict[int, list[_Song]] = {}
# Servers already checked on disk. A miss stays a miss until the line changes.
_missing: set[int] = set()
# A newer playlist or a stop ends the one still opening. Placement stays in list order.
_saved_generation: dict[int, int] = {}
_saved_lock: dict[int, asyncio.Lock] = {}
_saved_task: dict[int, asyncio.Task] = {}
# Songs a finished playlist could not open. The queue card names them.
_playlist_skips: dict[int, list[str]] = {}
_lines: dict[int, _Line] = {}
_gates: dict[int, asyncio.Lock] = {}
_line_watchers: list[Callable[[object], Awaitable[None]]] = []
# One refresh per server. A play during a refresh asks for another pass.
_paint_pending: set[int] = set()
_paint_tasks: dict[int, asyncio.Task] = {}


def _whole(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: object, fallback: str = "") -> str:
    if isinstance(value, str):
        cleaned = " ".join(value.split())
        if cleaned:
            return cleaned
    return fallback


def _http(value: object) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def _guild_id(guild: object | None) -> int | None:
    return _whole(getattr(guild, "id", None))


def _title(track: object) -> str:
    return _text(getattr(track, "title", None), "that song")


def _voice_player(guild: object | None) -> wavelink.Player | None:
    player = getattr(guild, "voice_client", None)
    return player if isinstance(player, wavelink.Player) else None


def _lock(locks: dict[int, asyncio.Lock], guild_id: int) -> asyncio.Lock:
    lock = locks.get(guild_id)
    if lock is None:
        lock = locks[guild_id] = asyncio.Lock()
    return lock


@asynccontextmanager
async def _hold(guild: object | None) -> AsyncIterator[None]:
    """One handoff at a time. A play during a song change waits, then lines up."""
    guild_id = _guild_id(guild)
    if guild_id is None:
        yield
        return
    async with _lock(_gates, guild_id):
        yield


def _saved_lock_for(guild_id: int) -> asyncio.Lock:
    return _lock(_saved_lock, guild_id)


def _end_saved(guild_id: int) -> int:
    token = _saved_generation.get(guild_id, 0) + 1
    _saved_generation[guild_id] = token
    return token


def _stored(guild: object | None) -> _Line | None:
    guild_id = _guild_id(guild)
    return None if guild_id is None else _lines.get(guild_id)


def _line(guild: object) -> _Line | None:
    guild_id = _guild_id(guild)
    if guild_id is None:
        return None
    line = _lines.get(guild_id)
    if line is None:
        line = _lines[guild_id] = _Line()
    return line


def _drop(guild: object) -> None:
    guild_id = _guild_id(guild)
    if guild_id is not None:
        _lines.pop(guild_id, None)


def _remember_id(guild: object, user_id: int | None) -> None:
    guild_id = _guild_id(guild)
    if guild_id is None:
        return
    if user_id is None:
        _requesters.pop(guild_id, None)
        return
    _requesters[guild_id] = user_id


def _forget(guild: object) -> None:
    _remember_id(guild, None)


def watch_line(callback: Callable[[object], Awaitable[None]]) -> None:
    """Call this when the line for a server changes."""
    _line_watchers.append(callback)


def unwatch_line(callback: Callable[[object], Awaitable[None]]) -> None:
    try:
        _line_watchers.remove(callback)
    except ValueError:
        return


async def refresh_line(guild: object | None) -> None:
    """Open queue cards paint again. A watcher that fails stays out of the way."""
    if guild is None:
        return
    for callback in list(_line_watchers):
        try:
            await callback(guild)
        except Exception:
            log.exception("music queue could not refresh")


def paint_soon(guild: object | None) -> None:
    """Refresh open cards after the command returns.

    The reply, and the moment a song starts, used to wait on those Discord edits.
    """
    guild_id = _guild_id(guild)
    if guild is None or guild_id is None or not _line_watchers:
        return
    _paint_pending.add(guild_id)
    task = _paint_tasks.get(guild_id)
    if task is not None and not task.done():
        return
    _paint_tasks[guild_id] = asyncio.create_task(
        _paint_latest(guild, guild_id), name="music-line",
    )


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
                _paint_tasks[guild_id] = asyncio.create_task(
                    _paint_latest(guild, guild_id), name="music-line",
                )


def playing_note(line: str) -> bool:
    return line.startswith(("Playing ", "Added "))


def _channel_id(value: object) -> int | None:
    return _whole(getattr(value, "id", None))


def member_in_voice(member: object, guild: object | None) -> bool:
    """True when this member is in the voice channel the bot is using."""
    player = getattr(guild, "voice_client", None)
    channel_id = _channel_id(getattr(player, "channel", None))
    voice = getattr(member, "voice", None)
    here = _channel_id(getattr(voice, "channel", None))
    return channel_id is not None and channel_id == here


def edits_queue(member: object, guild: object | None) -> bool:
    """A live room only changes from inside it. A saved queue can change while the bot is out."""
    player = getattr(guild, "voice_client", None) if guild is not None else None
    channel = getattr(player, "channel", None) if player is not None else None
    if _channel_id(channel) is None:
        return True
    return member_in_voice(member, guild)


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
    if getattr(track, "is_seekable", None) is False:
        return False
    return (_whole(getattr(track, "length", None)) or 0) > 0


def _each(results: object) -> list:
    tracks = getattr(results, "tracks", None)
    if isinstance(tracks, list):
        return tracks
    return results if isinstance(results, list) else []


def _same_track(left: object, right: object) -> bool:
    one = getattr(left, "identifier", None)
    other = getattr(right, "identifier", None)
    if isinstance(one, str) and isinstance(other, str) and one and other:
        return one == other
    left_title = _text(getattr(left, "title", None)).casefold()
    right_title = _text(getattr(right, "title", None)).casefold()
    return left_title == right_title


def _queued_index(songs: list[_Song], track: object) -> int | None:
    """Where this track sits in a waiting line. A link wins over a title."""
    uri = _http(getattr(track, "uri", None))
    if uri:
        for index, song in enumerate(songs):
            if _http(getattr(song.track, "uri", None)) == uri:
                return index
        return None
    title = _text(getattr(track, "title", None)).casefold()
    if not title:
        return None
    author = _text(getattr(track, "author", None)).casefold()
    for index, song in enumerate(songs):
        song_title = _text(getattr(song.track, "title", None)).casefold()
        song_author = _text(getattr(song.track, "author", None)).casefold()
        if song_title == title and (not author or not song_author or song_author == author):
            return index
    return None


def _active(line: _Line | None, track: object) -> _Song | None:
    """The song on now. A reload leaves it at the front of the queue until it is claimed."""
    if line is None:
        return None
    if line.current is not None:
        return line.current
    if track is None or not line.later:
        return None
    index = _queued_index(line.later, track)
    return None if index is None else line.later[index]


def _coming(line: _Line | None, track: object) -> _Song | None:
    """The song that plays next. The one already on now is not listed twice."""
    if line is None or not line.later:
        return None
    playing_is_first = (
        line.current is None
        and track is not None
        and _queued_index(line.later, track) == 0
    )
    index = 1 if playing_is_first else 0
    return line.later[index] if index < len(line.later) else None


def _on_air(line: _Line | None, track: object) -> bool:
    """A livestream, or a long station, has no time bar."""
    if _stream(track):
        return True
    song = _active(line, track)
    if song is None or not song.videos:
        return False
    # Still parked on the queue after a reload. The live track was already matched.
    if line is None or song is not line.current:
        return True
    if song.track is track:
        return True
    left = _http(getattr(song.track, "uri", None))
    right = _http(getattr(track, "uri", None))
    if left and right:
        return left == right
    return _same_track(song.track, track)


def _song_name(song: _Song) -> str:
    title = _text(getattr(song.track, "title", None), "Unknown")
    author = _text(getattr(song.track, "author", None))
    if not author or author.casefold() in title.casefold():
        return title or "Unknown"
    return f"{title} - {author}"


def release(guild: object | None) -> None:
    """The bot left voice. This server's queue stays on disk."""
    if guild is None:
        return
    _store(guild)
    _drop(guild)
    _forget(guild)


def load_saved_lines() -> None:
    for guild_id, raw in archive.read_all().items():
        if guild_id in _lines or guild_id in _heard:
            continue
        _install(guild_id, raw)
        _missing.discard(guild_id)


def save_open_lines() -> None:
    for guild_id in set(_lines) | set(_heard):
        archive.write_guild(guild_id, _record(guild_id))


def _store(guild: object | None) -> None:
    guild_id = _guild_id(guild)
    if guild_id is None:
        return
    _missing.discard(guild_id)
    archive.write_guild(guild_id, _record(guild_id))


def _record(guild_id: int) -> dict | None:
    line = _lines.get(guild_id)
    heard = _heard.get(guild_id, [])
    current = None if line is None else line.current
    later = () if line is None else line.later
    if current is None and not later and not heard:
        return None
    payload: dict[str, object] = {
        "current": None if current is None else _dump_song(current),
        "later": [_dump_song(song) for song in later],
        "history": [_dump_song(song) for song in heard],
    }
    if line is None:
        return payload
    if line.volume != 100:
        payload["volume"] = line.volume
    payload.update({name: True for name in ("repeat", "whole") if getattr(line, name)})
    return payload


def _dump_song(song: _Song) -> dict[str, object]:
    track = song.track
    payload: dict[str, object] = {
        "title": _text(getattr(track, "title", None), "Unknown"),
        "author": _text(getattr(track, "author", None)),
        "url": _http(getattr(track, "uri", None)) or "",
        "platform": song.platform,
        "length": _whole(getattr(track, "length", None)) or 0,
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
    if heard and guild_id not in _heard:
        history = _load_rows(raw.get("history"))
        if history:
            _heard[guild_id] = history[-HISTORY_SONGS:]
    if not line or guild_id in _lines:
        return
    later = _load_rows(raw.get("later"))
    current = _load_song(raw.get("current"))
    # A restart is not on a song, so the unfinished one waits at the front.
    if current is not None:
        later.insert(0, current)
    if not later:
        return
    volume = raw.get("volume")
    _lines[guild_id] = _Line(
        later=later,
        volume=volume if isinstance(volume, int) and not isinstance(volume, bool) else 100,
        repeat=raw.get("repeat") is True,
        whole=raw.get("whole") is True,
    )


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
    videos = tuple(item for item in listed if isinstance(item, str)) if isinstance(listed, list) else ()
    platform = raw.get("platform") if isinstance(raw.get("platform"), str) else "youtube"
    return _Song(
        _Kept(
            title,
            _text(raw.get("author")),
            url or None,
            _http(raw.get("artwork")),
            _whole(raw.get("length")) or 0,
            raw.get("stream") is True,
        ),
        _whole(raw.get("requester_id")),
        source.normalize_platform(platform),
        videos=videos,
        video_at=_whole(raw.get("video_at", raw.get("live_at"))) or 0,
        station=_text(raw.get("station")),
    )
