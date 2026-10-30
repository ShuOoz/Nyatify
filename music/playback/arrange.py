"""Queue, history, and a saved playlist for one server."""

from __future__ import annotations

import asyncio
import importlib
import logging
from contextlib import asynccontextmanager

from music import source
from music.lavalink import joining
from music.playback.remember import (
    HISTORY_SONGS, QUEUE_SONGS, PlayState, Queued, _CHANGED, _Line, _OPEN_AT_ONCE, _Song,
    _active, _coming, _end_saved, _fill_held, _guild_id, _heard, _hold, _http,
    _on_air, _order_drop, _playlist_skips, _promote_waiting, _queued_index, _recall, _requesters,
    _saved_generation, _saved_lock_for, _saved_task, _song_name, _store, _stored,
    _stream, _text, _voice_player, _number, paint_soon, playing_note,
)

log = logging.getLogger(__name__)

# A playlist that stopped because the waiting line was full. Cleared once it has room.
_over_cap: set[int] = set()

_NO_SERVER = "Use this command in a server."
_GONE = "That song is gone."
_SILENT = "Nothing is playing."
_OPEN_FAILED = "Nothing on YouTube matched that."
_START_FAILED = "Something went wrong while starting the song."
_FULL = "The queue is full."


def now_playing(guild: object | None) -> PlayState | None:
    """The song on now. A restart still shows the one that was playing or paused.

    Empty when the bot is out of voice and no song was left on.
    """
    if guild is None:
        return None
    guild_id = _guild_id(guild)
    if guild_id is not None:
        _recall(guild_id)
    player = _voice_player(guild)
    line = _stored(guild)
    in_voice = getattr(guild, "voice_client", None) is not None
    live = None if player is None else player.current
    # Skip of the last song stops the audio. A late track object is not that song.
    if player is not None and getattr(player, "_idle", False):
        if line is None or (line.current is None and not line.later):
            live = None
        else:
            player._idle = False
    if line is not None and line.current is None and live is not None:
        _claim_current(line, live, guild)
    if live is None and _promote_waiting(line):
        _store(guild)
    # A leave, a dropped link, or a reload can leave the song up with nobody in voice.
    # Nothing is playing it, so it is the saved one. Otherwise the card says nothing is on.
    if (
        line is not None and line.current is not None and not line.resting
        and live is None and not in_voice and joining(guild) is None
    ):
        line.resting = True
    if player is not None and line is not None and line.resting and live is not None:
        line.resting = False
        line.paused = player.paused is True
    saved = None
    if line is not None and line.resting and line.current is not None:
        saved = line.current.track
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
    return PlayState(
        _text(getattr(track, "title", None), "Unknown") if shown else "",
        _text(getattr(track, "author", None)) if shown else "",
        _http(getattr(track, "uri", None)) if shown else None,
        _http(getattr(track, "artwork", None)) if shown else None,
        position,
        length,
        paused,
        _who_asked(guild, line, track) if shown else None,
        _number(getattr(channel, "id", None)),
        100 if line is None else line.volume,
        None if coming is None else _song_name(coming),
        shown,
        shown and _on_air(line, track),
        line is not None and line.repeat,
        line is not None and line.shuffle,
        line is not None and line.radio,
    )


def _who_asked(guild: object, line: _Line | None, track: object) -> int | None:
    active = _active(line, track)
    if active is not None and active.requester_id is not None:
        return active.requester_id
    guild_id = _guild_id(guild)
    return None if guild_id is None else _requesters.get(guild_id)


def queued(guild: object | None) -> list[Queued] | None:
    """The current song, then the rest. A saved line still reads when voice is gone."""
    state = now_playing(guild)
    line = _stored(guild)
    if state is None:
        return [_queued_row(song, False) for song in _saved_songs(line)] or None
    rows: list[Queued] = []
    if line is not None and line.current is not None:
        rows.append(_queued_row(line.current, True))
    elif state.has_track:
        rows.append(Queued(
            state.title or "Unknown",
            source.platform_key(state.uri or "", "youtube"),
            state.uri,
            True,
            state.stream,
            state.author,
            state.length,
            state.requester_id,
        ))
    if line is not None:
        rows.extend(_queued_row(song, False) for song in line.later)
    return rows


def _saved_songs(line: _Line | None) -> list[_Song]:
    if line is None:
        return []
    return ([line.current] if line.current is not None else []) + list(line.later)


def _claim_current(line: _Line, track: object, guild: object | None = None) -> None:
    """A reload parks the playing song on the queue. Put that song back."""
    if line.current is not None or not line.later:
        return
    index = _queued_index(line.later, track)
    if index is None:
        return
    line.current = line.later.pop(index)
    requester = line.current.requester_id
    guild_id = _guild_id(guild)
    if requester is not None and guild_id is not None and guild_id not in _requesters:
        _requesters[guild_id] = requester


def _queued_row(song: _Song, playing: bool) -> Queued:
    track = song.track
    return Queued(
        _text(getattr(track, "title", None), "Unknown"),
        song.platform,
        source.remembered_share(track) or _http(getattr(track, "uri", None)),
        playing,
        _stream(track) or bool(song.videos),
        _text(getattr(track, "author", None)),
        _number(getattr(track, "length", None)) or 0,
        song.requester_id,
    )


def played(guild: object | None) -> list[Queued] | None:
    """Songs already heard, newest first. One on now or waiting stays hidden."""
    songs = _past(guild)
    return None if songs is None else [_queued_row(song, False) for song in reversed(songs)]


def earlier_at(guild: object | None, index: int) -> tuple[object, str] | None:
    songs = _past(guild)
    if songs is None or not 0 <= index < len(songs):
        return None
    song = songs[-1 - index]
    return song.track, song.platform


@asynccontextmanager
async def _editing(guild: object | None):
    """Hold the line, then paint once if the body actually changed it."""
    changed = False

    def touch() -> None:
        nonlocal changed
        changed = True

    async with _hold(guild):
        yield touch
    if changed:
        paint_soon(guild)


def _listed(rows: list[Queued] | None, *indexes: int) -> str | None:
    if rows is None:
        return _SILENT
    if any(not 0 <= index < len(rows) for index in indexes):
        return _GONE
    return None


def _slot(rows: list[Queued], index: int) -> int:
    return index - bool(rows[0].playing)


async def drop_queued(guild: object | None, index: int, member: object | None = None) -> str | None:
    async with _editing(guild) as touch:
        rows = queued(guild)
        missing = _listed(rows, index)
        if missing or rows is None:
            return missing
        if rows[index].playing:
            note = await _module("music.playback.control")._advance_line(guild, member)
            touch()
            return note
        line = _stored(guild)
        slot = _slot(rows, index)
        if line is None or not 0 <= slot < len(line.later):
            return _GONE
        _order_drop(line, line.later.pop(slot))
        _fill_held(line)
        if not line.later:
            line.whole = False
        _store(guild)
        touch()
        return None


async def move_queued(guild: object | None, source: int, dest: int) -> str | None:
    """Exchange two waiting songs. The one on now stays where it is."""
    async with _editing(guild) as touch:
        rows = queued(guild)
        missing = _listed(rows, source, dest)
        if missing or rows is None:
            return missing
        if rows[source].playing or rows[dest].playing or source == dest:
            return None
        src, dst = _slot(rows, source), _slot(rows, dest)
        line = _stored(guild)
        if line is None or not 0 <= src < len(line.later) or not 0 <= dst < len(line.later):
            return _GONE
        line.later[src], line.later[dst] = line.later[dst], line.later[src]
        _store(guild)
        touch()
        return None


async def clear_queued(guild: object | None) -> None:
    """Drop every waiting song. The one on now keeps playing, and an opening playlist stops."""
    async with _editing(guild) as touch:
        queued(guild)  # load the line and settle the song on it
        guild_id = _guild_id(guild)
        if guild_id is not None:
            _end_saved(guild_id)
            full = guild_id in _over_cap
            _over_cap.discard(guild_id)
            if _playlist_skips.pop(guild_id, None) or full:
                touch()
        line = _stored(guild)
        if line is not None and (line.later or line.held or line.whole):
            line.later.clear()
            line.held.clear()
            line.order.clear()
            line.whole = False
            _store(guild)
            touch()


async def clear_played(guild: object | None, requester_id: int | None = None) -> None:
    """Drop the history panel. One person drops only the songs they asked for."""
    async with _editing(guild) as touch:
        guild_id = _guild_id(guild)
        if guild_id is None:
            return
        _recall(guild_id)
        heard = _heard.get(guild_id)
        if not heard:
            return
        if requester_id is None:
            heard.clear()
        else:
            kept = [song for song in heard if song.requester_id != requester_id]
            if len(kept) == len(heard):
                return
            heard[:] = kept
        _store(guild)
        touch()


def _past(guild: object | None) -> list[_Song] | None:
    """History, minus a song on now or waiting. Removing it before it plays shows it again."""
    guild_id = _guild_id(guild)
    if guild_id is None:
        return None
    _recall(guild_id)
    songs = _heard.get(guild_id, [])
    line = _stored(guild)
    if line is None:
        return songs
    lined = _saved_songs(line)
    return [song for song in songs if not any(_same_song(song, item) for item in lined)]


def _keep_current(guild: object) -> None:
    """The song on now counts as played, even when playback stops before it ends."""
    line = _stored(guild)
    if line is None or line.current is None:
        return
    song = line.current
    # Stop drops the queue, so another copy waiting there does not hide this play.
    line.later = [item for item in line.later if not _same_song(item, song)]
    line.order = [item for item in line.order if not _same_song(item, song)]
    _keep_earlier(guild, line, song)


def _keep_earlier(guild: object | None, line: _Line, song: _Song) -> None:
    _push(line.earlier, song, newest=False)
    if any(_same_song(song, item) for item in _saved_songs(line) if item is not song):
        return
    guild_id = _guild_id(guild)
    if guild_id is not None:
        _push(_heard.setdefault(guild_id, []), song, newest=True)


def _push(songs: list[_Song], song: _Song, *, newest: bool) -> None:
    if newest:
        songs[:] = [item for item in songs if not _same_song(item, song)]
    songs.append(song)
    del songs[:-HISTORY_SONGS]


def _same_song(song: _Song, other: _Song) -> bool:
    """The same recording. A link matches even when the saved title differs."""
    left, right = song.track, other.track
    one, two = _http(getattr(left, "uri", None)), _http(getattr(right, "uri", None))
    if one and two:
        return source.same_url(one, two)
    ident, other_id = getattr(left, "identifier", None), getattr(right, "identifier", None)
    if isinstance(ident, str) and isinstance(other_id, str) and ident and other_id:
        return ident == other_id
    title, other_title = (_text(getattr(item, "title", None)).casefold() for item in (left, right))
    if not title or title != other_title:
        return False
    author, other_author = (_text(getattr(item, "author", None)).casefold() for item in (left, right))
    return not author or not other_author or author == other_author


def _module(name: str):
    return importlib.import_module(name)


def _token_holds(guild_id: int | None, token: int) -> bool:
    return guild_id is not None and _saved_generation.get(guild_id, 0) == token


async def _pull(job: asyncio.Task, guild_id: int | None, token: int, missed: list[str]):
    """The next opened song, why it failed, and whether that job was awaited.

    A lost playlist comes back as `_CHANGED` and does not count as a skip.
    """
    if not _token_holds(guild_id, token):
        return None, _CHANGED, False
    opened, title, reason = await job
    if not _token_holds(guild_id, token):
        return None, _CHANGED, True
    if opened is None:
        missed.append(title)
    return opened, None if opened is not None else reason, True


async def play_saved(member: object, guild: object | None, songs: list, *, replace: bool) -> str:
    """The first open song starts. The rest join in order. Play replaces; queue appends."""
    if guild is None:
        return _NO_SERVER
    if not songs:
        return "That playlist has no songs."
    start = _module("music.playback.start")
    room = start._room(member, guild)
    if isinstance(room, str):
        return room
    guild_id = _guild_id(guild)
    if guild_id is None:
        return _NO_SERVER
    async with _hold(guild):
        _playlist_skips.pop(guild_id, None)
        _over_cap.discard(guild_id)
        token = _end_saved(guild_id) if replace else _saved_generation.get(guild_id, 0)
    gate = asyncio.Semaphore(_OPEN_AT_ONCE)
    jobs = [asyncio.create_task(_open_saved(guild, song, gate), name="music-open") for song in songs]
    lock = _saved_lock_for(guild_id)
    await lock.acquire()
    missed: list[str] = []
    at, handed, error = 0, False, _OPEN_FAILED
    try:
        while at < len(jobs):
            opened, reason, took = await _pull(jobs[at], guild_id, token, missed)
            if took:
                at += 1
            if reason == _CHANGED:
                return _CHANGED
            if opened is None:
                error = reason or error
                continue
            note = await _place(start, member, guild, opened, token, replace=replace)
            if not playing_note(note):
                return note
            if missed:
                note = f"{note} Skipped {', '.join(missed)}."
            # The caller attaches voice before the rest of the playlist joins.
            if at == len(jobs):
                await _name_skips(guild, guild_id, token, missed)
                return note
            _saved_task[guild_id] = asyncio.create_task(
                _play_rest(start, member, guild, jobs[at:], token, lock, missed),
                name="music-playlist",
            )
            handed = True
            return note
        return error
    finally:
        if not handed:
            await _drop_jobs(jobs[at:])
            lock.release()


async def finish_saved(guild: object | None) -> None:
    guild_id = _guild_id(guild)
    if guild_id is not None and (task := _saved_task.get(guild_id)) is not None:
        await task


async def _open_saved(guild: object, song: object, gate: asyncio.Semaphore):
    async with gate:
        try:
            return await _module("music.playback").open_one(guild, song)
        except Exception:
            log.exception("music playlist could not open a song")
            title = getattr(song, "title", None)
            name = title.strip() if isinstance(title, str) else ""
            return None, name or "Unknown", _START_FAILED


async def _play_rest(start, member, guild, jobs, token: int, lock: asyncio.Lock, missed: list[str]) -> None:
    guild_id = _guild_id(guild)
    name_them = True
    try:
        for job in jobs:
            opened, reason, _took = await _pull(job, guild_id, token, missed)
            if reason == _CHANGED:
                name_them = False
                return
            if opened is None:
                continue
            note = await _place(start, member, guild, opened, token, replace=False)
            if playing_note(note):
                continue
            if isinstance(note, str) and note.endswith(_FULL) and _token_holds(guild_id, token):
                _over_cap.add(guild_id)
                paint_soon(guild)
            return
    except Exception:
        log.exception("music playlist could not be queued")
    finally:
        try:
            if name_them:
                await _name_skips(guild, guild_id, token, missed)
        finally:
            await _drop_jobs(jobs)
            lock.release()


async def _place(start, member, guild, opened, token, *, replace: bool) -> str:
    if replace:
        return await start.load_line(member, guild, [opened])
    return await start.queue_opened(member, guild, [opened], whole=True, token=token)


def skipped_songs(guild: object | None) -> str | None:
    guild_id = _guild_id(guild)
    if guild_id is None:
        return None
    names = _playlist_skips.get(guild_id)
    line = _stored(guild)
    if guild_id in _over_cap and (line is None or len(line.later) < QUEUE_SONGS):
        _over_cap.discard(guild_id)
    parts: list[str] = []
    if names:
        parts.append(f"Skipped {', '.join(names)}.")
    if guild_id in _over_cap:
        parts.append(_FULL)
    return " ".join(parts) or None


async def _name_skips(guild: object, guild_id: int | None, token: int, missed: list[str]) -> None:
    if not missed or not _token_holds(guild_id, token):
        return
    _playlist_skips[guild_id] = list(missed)
    paint_soon(guild)


async def _drop_jobs(jobs: list[asyncio.Task]) -> None:
    pending = [job for job in jobs if not job.done()]
    for job in pending:
        job.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
