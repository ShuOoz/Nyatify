"""Pause, skip, volume, and the moment a song ends."""

from __future__ import annotations

import importlib
import logging

import wavelink

from music import source
from music.lavalink import _play_track
from music.playback.arrange import _claim_current, _keep_earlier
from music.playback.remember import (
    NEAR_START_MS,
    QUEUE_SONGS,
    _Kept,
    _Line,
    _Song,
    _clear_track,
    _drop,
    _each,
    _hold,
    _line,
    _movable,
    _on_air,
    _place,
    _remember_id,
    _same_track,
    _store,
    _stored,
    _text,
    _voice_player,
    _whole,
    paint_soon,
)

log = logging.getLogger(__name__)

# A livestream that ends this fast was dropped by the YouTube loader, not by the station.
_SHORT_DROP_MS = 20_000
_CHANGE = "Something went wrong while changing the song."
_QUIET = "Nothing is playing."
_STUCK = "That song cannot move."


async def _attempt(call, problem: str) -> bool:
    try:
        await call()
    except wavelink.WavelinkException:
        log.exception(problem)
        return False
    return True


def _attach(guild: object | None, player: object) -> _Line | None:
    """The line in memory. A reload's parked song is put back on now."""
    line = _stored(guild)
    track = getattr(player, "current", None)
    if line is not None and line.current is None and track is not None:
        _claim_current(line, track, guild)
    return line


async def _find(guild: object, query: str, platform: str):
    """Call music.playback.find_songs after the package has loaded.

    Importing it above would cycle. Tests replace that package attribute.
    """
    playback = importlib.import_module("music.playback")
    return await playback.find_songs(guild, query, platform)


async def _try_song(
    player, guild, line: _Line, song: _Song, undo,
) -> tuple[str | None, _Song | None]:
    """Start this song. A failure puts it back and leaves the line on what was playing."""
    playing = line.current
    note = await _begin(player, guild, song)
    if note:
        undo()
        return note, None
    return None, playing


async def toggle_pause(guild: object | None) -> str | None:
    if guild is None:
        return "Use this command in a server."
    async with _hold(guild):
        player = _voice_player(guild)
        if player is None or player.current is None:
            return _QUIET
        paused = not player.paused
        if not await _attempt(lambda: player.pause(paused), "Could not pause the player"):
            return "I could not pause that."
        return None


async def previous_track(guild: object | None) -> str | None:
    async with _hold(guild):
        player = _voice_player(guild)
        if player is None:
            return _QUIET
        line = _attach(guild, player)
        position = _whole(getattr(player, "position", None)) or 0
        # Past the opening, Previous restarts this song instead of leaving it.
        if player.current is not None and _movable(player.current) and position > NEAR_START_MS:
            return await _restart(player)
        if line is not None and line.earlier:
            song = line.earlier.pop()
            note, playing = await _try_song(
                player, guild, line, song, lambda: line.earlier.append(song),
            )
            if note:
                return note
            if playing is not None:
                line.later.insert(0, playing)
                # A normal queue stays at its cap. A playlist keeps every song.
                if not line.whole and len(line.later) > QUEUE_SONGS:
                    del line.later[QUEUE_SONGS:]
            _store(guild)
            paint_soon(guild)
            return None
        if player.current is None:
            return _QUIET
        if not _movable(player.current):
            return "Nothing is before this."
        return await _restart(player)


async def nudge(guild: object | None, delta: int) -> str | None:
    async with _hold(guild):
        player = _voice_player(guild)
        if player is None or player.current is None:
            return _QUIET
        if not _movable(player.current):
            return _STUCK
        length = _whole(getattr(player.current, "length", None)) or 0
        position = _whole(getattr(player, "position", None)) or 0
        target = min(max(0, position + delta), length)
        if not await _attempt(lambda: player.seek(target), "Could not move through the song"):
            return _STUCK
        _place(player, target)
        return None


async def _restart(player: wavelink.Player) -> str | None:
    if player.current is None or not _movable(player.current):
        return _STUCK
    if not await _attempt(lambda: player.seek(0), "Could not restart the song"):
        return _STUCK
    _place(player, 0)
    return None


async def repeat_track(guild: object | None) -> str | None:
    """Play this song again when it ends. The waiting queue is left alone."""
    async with _hold(guild):
        player = _voice_player(guild)
        if player is None or player.current is None:
            return _QUIET
        line = _attach(guild, player)
        if line is None or line.current is None:
            return _QUIET
        if _on_air(line, player.current) or not _movable(player.current):
            return "That song cannot repeat."
        line.repeat = not line.repeat
        _store(guild)
        return None


async def next_track(guild: object | None) -> str | None:
    async with _hold(guild):
        note = await _advance_line(guild)
    paint_soon(guild)
    return note


async def _advance_line(guild: object | None) -> str | None:
    player = _voice_player(guild)
    if player is None:
        return _QUIET
    line = _attach(guild, player)
    if line is not None and line.later:
        song = line.later.pop(0)
        note, playing = await _try_song(
            player, guild, line, song, lambda: line.later.insert(0, song),
        )
        if note:
            return note
        if playing is not None:
            _keep_earlier(guild, line, playing)
        if not line.later:
            line.whole = False
        _store(guild)
        return None
    if player.current is None and (line is None or line.current is None):
        return _QUIET
    if not await _attempt(player.stop, "Could not end the song"):
        # The song is still going, so the line stays with it.
        return _CHANGE
    _clear_track(player)
    if line is not None and line.current is not None:
        _keep_earlier(guild, line, line.current)
        line.current = None
    if line is not None:
        # Nothing is left on now, so this song's flags do not carry to the next play.
        line.repeat = False
        line.whole = False
        _store(guild)
    _remember_id(guild, None)
    return None


async def change_volume(guild: object | None, delta: int) -> str | None:
    async with _hold(guild):
        player = _voice_player(guild)
        line = None if player is None else _stored(guild)
        if player is None or line is None:
            return _QUIET
        previous = line.volume
        line.volume = min(100, max(0, previous + delta))
        if await _attempt(lambda: player.set_volume(line.volume), "Could not change the volume"):
            _store(guild)
            return None
        # The card already showed this step. Put the old level back.
        line.volume = previous
        return "Something went wrong while changing the volume."


async def suggest_next(guild: object | None, member: object) -> str | None:
    """Search this song's platform and put a different match on next."""
    async with _hold(guild):
        player = _voice_player(guild)
        if player is None or player.current is None:
            return _QUIET
        line = _attach(guild, player)
        if line is None or line.current is None:
            return _QUIET
        match = await _other_track(line.current)
        if match is None:
            return "Nothing similar came up."
        if len(line.later) >= QUEUE_SONGS:
            return "The queue is full."
        line.later.append(_Song(match, _whole(getattr(member, "id", None)), line.current.platform))
        _store(guild)
        paint_soon(guild)
        return None


async def _other_track(song: _Song):
    title = _text(getattr(song.track, "title", None), "Unknown")
    author = _text(getattr(song.track, "author", None))
    if not author or author.casefold() in title.casefold():
        query = title
    else:
        query = f"{title} {author}"
    try:
        found = await source.search_track(query, song.platform)
    except wavelink.WavelinkException:
        log.exception("Similar song search failed")
        return None
    tracks = _each(found)
    if song.platform == "soundcloud":
        tracks = source.drop_previews(tracks)
    return next((track for track in tracks if not _same_track(track, song.track)), None)


async def advance_from_end(payload: object) -> None:
    """Play the next song after one finishes. Replaced and stopped are already handled."""
    reason = getattr(payload, "reason", None)
    if reason not in ("finished", "loadFailed"):
        return
    player = getattr(payload, "player", None)
    guild = getattr(player, "guild", None)
    if player is None or guild is None:
        return
    async with _hold(guild):
        # Lavalink has already dropped the current track. Keep the line in step.
        if hasattr(player, "_track"):
            player._track = None
        line = _stored(guild)
        song = None if line is None else line.current
        # A short drop is the loader failing. A real ending gives the queue its turn.
        short = (_whole(getattr(player, "_last_position", None)) or 0) < _SHORT_DROP_MS
        waiting = line is not None and bool(line.later)
        if song is not None and _repeats(reason, line, song):
            # The same song starts again. It is not copied onto the queue.
            if await _begin(player, guild, song):
                await _advance_line(guild)
        elif song is not None and song.videos and (short or not waiting):
            if not await _continue_station(player, guild, song, short):
                await _advance_line(guild)
        else:
            await _advance_line(guild)
    paint_soon(guild)


def _repeats(reason: object, line: _Line | None, song: _Song) -> bool:
    if reason != "finished" or line is None or not line.repeat or song.videos:
        return False
    return (_whole(getattr(song.track, "length", None)) or 0) > 0


async def _continue_station(player: wavelink.Player, guild: object, song: _Song, short: bool) -> bool:
    """Keep a station on the air. The first short drop reloads it, then the other addresses play."""
    drops = song.drops + 1 if short else 0
    if drops > len(song.videos):
        log.warning("radio station ended before it could hold")
        return False
    count = len(song.videos)
    # One reload of the address that just died. After that, only the backups are tried.
    indexes = [song.video_at] if short and song.drops == 0 else []
    indexes.extend((song.video_at + step) % count for step in range(1, count))
    for index in indexes:
        try:
            found = await _find(guild, song.videos[index], "youtube")
        except Exception:
            log.exception("music radio failed")
            continue
        if isinstance(found, str) or not found.tracks:
            continue
        note = await _begin(
            player,
            guild,
            _Song(
                found.tracks[0],
                song.requester_id,
                "youtube",
                videos=song.videos,
                video_at=index,
                drops=drops,
                station=song.station,
            ),
        )
        if note:
            continue
        log.info("radio continued on address %s", index + 1)
        return True
    return False


async def _begin(
    player: wavelink.Player,
    guild: object,
    song: _Song,
    *,
    failed: str | None = None,
) -> str | None:
    missed = await _open_saved(guild, song)
    if missed:
        return missed
    had_line = _stored(guild) is not None
    line = _line(guild)
    try:
        # A dead session returns the new player. Volume and position belong to that one.
        live = await _play_track(player, song.track)
    except wavelink.WavelinkException:
        log.exception("Could not play the next song")
        if not had_line:
            _drop(guild)
        return failed or _CHANGE
    if getattr(live, "current", None) is not song.track and hasattr(live, "_track"):
        live._track = song.track
    if live.paused is True:
        await _attempt(lambda: live.pause(False), "Could not resume the next song")
        if hasattr(live, "_paused"):
            live._paused = False
    if line is not None:
        line.current = song
        if line.volume != 100:
            await _attempt(lambda: live.set_volume(line.volume), "Could not keep the volume")
    _place(live, 0)
    _remember_id(guild, song.requester_id)
    _store(guild)
    return None


async def _open_saved(guild: object, song: _Song) -> str | None:
    """Turn a saved song back into a track Lavalink can play."""
    if not isinstance(song.track, _Kept):
        return None
    if not song.track.uri:
        return "That song has no link to play."
    try:
        found = await _find(guild, song.track.uri, song.platform)
    except Exception:
        log.exception("music could not open a saved song")
        return "Something went wrong while starting the song."
    if isinstance(found, str):
        return found
    if not found.tracks:
        label = source.platform_label("", song.platform)
        return f"Nothing on {label} matched that."
    song.track = found.tracks[0]
    return None
