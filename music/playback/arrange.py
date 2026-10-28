"""Queue, history, and a saved playlist for one server."""

from __future__ import annotations

import asyncio
import importlib
import logging
from contextlib import asynccontextmanager

from music import source

from music.playback.remember import (
    HISTORY_SONGS, PlayState, Queued, _CHANGED, _Line, _OPEN_AT_ONCE, _Song,
    _active, _coming, _end_saved, _guild_id, _heard, _hold, _http,
    _on_air, _playlist_skips, _queued_index, _recall, _requesters,
    _saved_generation, _saved_lock_for, _saved_task, _song_name, _store, _stored,
    _stream, _text, _voice_player, _whole, paint_soon, playing_note,
)

log = logging.getLogger(__name__)

_NO_SERVER = "Use this command in a server."
_GONE = "That song is gone."
_SILENT = "Nothing is playing."
_OPEN_FAILED = "Nothing on YouTube matched that."
_START_FAILED = "Something went wrong while starting the song."


def now_playing(guild: object | None) -> PlayState | None:
    """Empty while the bot is out of voice, even when a queue is saved."""
    if guild is None or getattr(guild, "voice_client", None) is None:
        return None
    player = _voice_player(guild)
    line = _stored(guild)
    track = None if player is None else player.current
    channel = None if player is None else getattr(player, "channel", None)
    coming = _coming(line, track)
    length = _whole(getattr(track, "length", None)) or 0
    position = (_whole(getattr(player, "position", None)) or 0) if track else 0
    if length:
        position = min(max(0, position), length)
    return PlayState(
        _text(getattr(track, "title", None), "Unknown") if track else "",
        _text(getattr(track, "author", None)) if track else "",
        _http(getattr(track, "uri", None)) if track else None,
        _http(getattr(track, "artwork", None)) if track else None,
        position,
        length,
        track is not None and player.paused is True,
        _who_asked(guild, line, track) if track else None,
        _whole(getattr(channel, "id", None)),
        100 if line is None else line.volume,
        None if coming is None else _song_name(coming),
        track is not None,
        track is not None and _on_air(line, track),
        line is not None and line.repeat,
    )


def _who_asked(guild: object, line: _Line | None, track: object) -> int | None:
    active = _active(line, track)
    if active is not None and active.requester_id is not None:
        return active.requester_id
    guild_id = _guild_id(guild)
    return None if guild_id is None else _requesters.get(guild_id)


def queued(guild: object | None) -> list[Queued] | None:
    """The current song, then the rest. A saved line still reads when voice is gone."""
    guild_id = _guild_id(guild)
    if guild_id is not None:
        _recall(guild_id)
    state = now_playing(guild)
    line = _stored(guild)
    if state is None:
        songs = _saved_songs(line)
        return [_queued_row(song, False) for song in songs] or None
    player = _voice_player(guild)
    if line is not None and line.current is None and player is not None and player.current is not None:
        _claim_current(line, player.current, guild)
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
    songs = [line.current] if line.current is not None else []
    return songs + list(line.later)


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
        _http(getattr(track, "uri", None)),
        playing,
        _stream(track) or bool(song.videos),
        _text(getattr(track, "author", None)),
        _whole(getattr(track, "length", None)) or 0,
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


async def drop_queued(guild: object | None, index: int) -> str | None:
    async with _editing(guild) as touch:
        rows = queued(guild)
        missing = _listed(rows, index)
        if missing or rows is None:
            return missing
        if rows[index].playing:
            note = await _module("music.playback.control")._advance_line(guild)
            touch()
            return note
        line = _stored(guild)
        slot = _slot(rows, index)
        if line is None or not 0 <= slot < len(line.later):
            return _GONE
        line.later.pop(slot)
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
        queued(guild)
        guild_id = _guild_id(guild)
        if guild_id is not None:
            _end_saved(guild_id)
            if _playlist_skips.pop(guild_id, None):
                touch()
        line = _stored(guild)
        if line is not None and (line.later or line.whole):
            line.later.clear()
            line.whole = False
            _store(guild)
            touch()


async def clear_played(guild: object | None) -> None:
    """Drop the history panel. The song on now and the queue stay."""
    async with _editing(guild) as touch:
        guild_id = _guild_id(guild)
        if guild_id is None:
            return
        _recall(guild_id)
        heard = _heard.get(guild_id)
        if not heard:
            return
        heard.clear()
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
    _keep_earlier(guild, line, song)


def _keep_earlier(guild: object | None, line: _Line, song: _Song) -> None:
    _push(line.earlier, song, newest=False)
    waiting = [item for item in _saved_songs(line) if item is not song]
    if any(_same_song(song, item) for item in waiting):
        return
    guild_id = _guild_id(guild)
    if guild_id is None:
        return
    _push(_heard.setdefault(guild_id, []), song, newest=True)


def _push(songs: list[_Song], song: _Song, *, newest: bool) -> None:
    if newest:
        songs[:] = [item for item in songs if not _same_song(item, song)]
    songs.append(song)
    del songs[:-HISTORY_SONGS]


def _same_song(song: _Song, other: _Song) -> bool:
    """The same recording. A link matches even when the saved title differs."""
    left, right = _http(getattr(song.track, "uri", None)), _http(getattr(other.track, "uri", None))
    if left and right:
        return left == right
    one, another = getattr(song.track, "identifier", None), getattr(other.track, "identifier", None)
    if isinstance(one, str) and isinstance(another, str) and one and another:
        return one == another
    title = _text(getattr(song.track, "title", None)).casefold()
    other_title = _text(getattr(other.track, "title", None)).casefold()
    if not title or title != other_title:
        return False
    author = _text(getattr(song.track, "author", None)).casefold()
    other_author = _text(getattr(other.track, "author", None)).casefold()
    return not author or not other_author or author == other_author


def _module(name: str):
    return importlib.import_module(name)


async def _pull(job: asyncio.Task, guild_id: int | None, token: int, missed: list[str]):
    """The next opened song. The third value says the job was awaited.

    A lost playlist comes back as `_CHANGED` and does not count as a skip.
    """
    if not _token_holds(guild_id, token):
        return None, _CHANGED, False
    opened, title, reason = await job
    if not _token_holds(guild_id, token):
        return None, _CHANGED, True
    if opened is not None:
        return opened, None, True
    missed.append(title)
    return None, reason, True


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
        token = _end_saved(guild_id) if replace else _saved_generation.get(guild_id, 0)
    gate = asyncio.Semaphore(_OPEN_AT_ONCE)
    jobs = [asyncio.create_task(_open_saved(guild, song, gate), name="music-open") for song in songs]
    lock = _saved_lock_for(guild_id)
    await lock.acquire()
    missed: list[str] = []
    error = _OPEN_FAILED
    at = 0
    # Return before the rest join, so the caller can attach voice first.
    handed = False
    try:
        while at < len(jobs):
            opened, reason, took = await _pull(jobs[at], guild_id, token, missed)
            if took:
                at += 1
            if reason == _CHANGED:
                return _CHANGED
            if opened is None:
                if reason:
                    error = reason
                continue
            note = await _place(start, member, guild, opened, token, replace=replace)
            if not playing_note(note):
                return note
            if missed:
                note = f"{note} Skipped {', '.join(missed)}."
            rest = jobs[at:]
            if not rest:
                await _name_skips(guild, guild_id, token, missed)
                return note
            _saved_task[guild_id] = asyncio.create_task(
                _play_rest(start, member, guild, rest, token, lock, missed),
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
    task = None if guild_id is None else _saved_task.get(guild_id)
    if task is not None:
        await task


def _token_holds(guild_id: int | None, token: int) -> bool:
    return guild_id is not None and _saved_generation.get(guild_id, 0) == token


async def _open_saved(guild: object, song: object, gate: asyncio.Semaphore):
    async with gate:
        try:
            return await _module("music.playback").open_one(guild, song)
        except Exception:
            log.exception("music playlist could not open a song")
            title = getattr(song, "title", None)
            name = title.strip() if isinstance(title, str) and title.strip() else "that song"
            return None, name, _START_FAILED


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
            if not playing_note(note):
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
    names = None if guild_id is None else _playlist_skips.get(guild_id)
    return None if not names else f"Skipped {', '.join(names)}."


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
