"""Play a search result, queue songs, and stop."""

from __future__ import annotations

import asyncio
import logging

from music import source
from music.lavalink import _channel_num, _join, _replaced, joining, joining_channel
from music.playback.arrange import _keep_current, _keep_earlier
from music.playback.control import _begin
from music.playback.remember import (
    QUEUE_SONGS, _CHANGED, _Line, _Song, _drop, _end_saved, _forget, _guild_id,
    _hold, _line, _playlist_skips, _recall, _saved_generation, _store, _stored,
    _title, _voice_player, _whole, member_in_voice, paint_soon, release,
)

log = logging.getLogger(__name__)

_NO_SERVER = "Use this command in a server."
_FULL = "The queue is full."


def _who(member: object) -> int | None:
    return _whole(getattr(member, "id", None))


def _songs(member: object, tracks: list[tuple[object, str]]) -> list[_Song]:
    who = _who(member)
    return [
        _Song(track, who, source.normalize_platform(platform))
        for track, platform in tracks
    ]


async def load_line(member: object, guild: object | None, tracks: list[tuple[object, str]]) -> str:
    """Replace the line with these songs and start the first."""
    return await _start_songs(member, guild, _songs(member, tracks), replace=True)


async def play_now(member: object, guild: object | None, track: object, platform: str) -> str:
    """Start this song now. Songs already lined up stay after it."""
    return await _start_songs(member, guild, _songs(member, [(track, platform)]), replace=False)


async def queue_opened(
    member: object,
    guild: object | None,
    tracks: list[tuple[object, str]],
    *,
    whole: bool = False,
    token: int | None = None,
) -> str:
    """Add these songs in order. Each keeps its own platform.

    A playlist passes whole, so every song joins even past the waiting cap.
    A token drops the song when a newer playlist or a stop already won.
    """
    if guild is None:
        return _NO_SERVER
    if not tracks:
        return "That playlist has no songs."
    return await _enqueue(member, guild, _songs(member, tracks), whole=whole, token=token)


async def queue_tracks(
    member: object,
    guild: object | None,
    tracks: list,
    platform: str = "youtube",
    videos: tuple[str, ...] = (),
    video_at: int = 0,
    station: str = "",
) -> str:
    """Join the member's voice channel when this is what starts playback."""
    if guild is None:
        return _NO_SERVER
    platform = source.normalize_platform(platform)
    if not tracks:
        return f"Nothing on {source.platform_label('', platform)} matched that."
    songs = _songs(member, [(track, platform) for track in tracks])
    if videos:
        songs[0].videos = videos
        songs[0].video_at = video_at
        songs[0].station = station
    return await _enqueue(member, guild, songs)


async def stop_track(guild: object | None) -> str:
    if guild is None:
        return _NO_SERVER
    try:
        async with _hold(guild):
            guild_id = _guild_id(guild)
            if guild_id is not None:
                _recall(guild_id)
                _end_saved(guild_id)
                _playlist_skips.pop(guild_id, None)
            _keep_current(guild)
            _drop(guild)
            _forget(guild)
            _store(guild)
            return await _disconnect(getattr(guild, "voice_client", None))
    finally:
        paint_soon(guild)


def warm_voice(member: object, guild: object | None) -> None:
    """Start joining while a search is still running. The play waits for that join."""
    if guild is None or getattr(guild, "voice_client", None) is not None:
        return
    channel = _room(member, guild)
    if isinstance(channel, str):
        return
    asyncio.create_task(_join(guild, channel), name="music-warm")


async def leave_if_idle(guild: object | None) -> None:
    """Drop a join that a failed search started, once nothing is playing."""
    if guild is None:
        return
    pending = joining(guild)
    if pending is not None:
        try:
            await pending
        except Exception:
            log.warning("music could not finish the early voice join", exc_info=True)
    async with _hold(guild):
        player = _voice_player(guild)
        if player is None or getattr(player, "current", None) is not None:
            return
        # A song already on the line keeps the channel. A saved queue joins again later.
        line = _stored(guild)
        if line is not None and line.current is not None:
            return
        try:
            await player.disconnect()
        except Exception:
            log.warning("music could not leave an unused voice channel", exc_info=True)


async def _enqueue(
    member: object,
    guild: object,
    songs: list[_Song],
    *,
    whole: bool = False,
    token: int | None = None,
) -> str:
    channel = _room(member, guild)
    if isinstance(channel, str):
        return channel
    note, changed = _NO_SERVER, False
    async with _hold(guild):
        if token is not None and _saved_generation.get(_guild_id(guild), 0) != token:
            return _CHANGED
        # The room can fill while this waited, including a join started early.
        channel = _room(member, guild)
        if isinstance(channel, str):
            return channel
        guild_id = _guild_id(guild)
        if guild_id is not None:
            _recall(guild_id)
        player = _voice_player(guild)
        line = _stored(guild)
        moved = _shift_reloaded(player, line)
        if _still_on(player, line):
            note, changed = _add_waiting(guild, songs, whole)
        elif _waiting(line):
            note, changed = await _start_parked(member, guild, channel, songs, whole)
            if moved is not None and not changed:
                _restore_current(guild, moved)
        else:
            # A kick leaves a line behind. The next play starts clean.
            if player is None:
                release(guild)
            note, changed = await _start_fresh(guild, channel, songs, whole)
    if changed:
        paint_soon(guild)
    return note


def _shift_reloaded(player: object, line: _Line | None) -> _Song | None:
    """A reloaded player is silent. Its song waits at the front with the rest."""
    if player is None or not _replaced(player) or line is None or line.current is None:
        return None
    song = line.current
    line.later.insert(0, song)
    line.current = None
    return song


def _restore_current(guild: object, song: _Song) -> None:
    held = _stored(guild)
    if held is None or held.current is not None or not held.later or held.later[0] is not song:
        return
    held.current = held.later.pop(0)
    _store(guild)


def _still_on(player: object, line: _Line | None) -> bool:
    """True while this player is on a song, including the gap before the next one."""
    if player is None or _replaced(player):
        return False
    if getattr(player, "current", None) is not None:
        return True
    return line is not None and line.current is not None


def _waiting(line: _Line | None) -> bool:
    return line is not None and line.current is None and bool(line.later)


def _add_waiting(guild: object, songs: list[_Song], whole: bool) -> tuple[str, bool]:
    kept = _line(guild)
    if kept is None:
        return _NO_SERVER, False
    taken = _take(kept, songs, whole)
    if taken:
        _store(guild)
    return _added(taken, len(songs)), bool(taken)


async def _start_parked(
    member: object,
    guild: object,
    channel: object,
    songs: list[_Song],
    whole: bool,
) -> tuple[str, bool]:
    """A saved queue is waiting. Start its first song and put the new ones behind it."""
    kept = _line(guild)
    if kept is None:
        return _NO_SERVER, False
    was_whole = kept.whole
    before = len(kept.later)
    taken = _take(kept, songs, whole)
    first = kept.later.pop(0)

    def undo() -> None:
        kept.whole = was_whole
        kept.later.insert(0, first)
        # New songs were appended at `before`. The head is back in front of them.
        del kept.later[before:]
        _store(guild)

    missed = await _play_song(
        guild, channel, first, failed=_failed(first), requester=_who(member),
    )
    if missed:
        undo()
        return missed, False
    _store(guild)
    return _playing(first, full=not whole and len(taken) < len(songs)), True


async def _start_fresh(
    guild: object,
    channel: object,
    songs: list[_Song],
    whole: bool,
) -> tuple[str, bool]:
    missed = await _play_song(guild, channel, songs[0], failed=_failed(songs[0]))
    if missed:
        return missed, False
    line = _line(guild)
    rest = songs[1:] if whole else songs[1:QUEUE_SONGS + 1]
    if line is not None:
        line.repeat = False
        line.later.extend(rest)
        line.whole = whole
    _store(guild)
    capped = not whole and len(songs) > QUEUE_SONGS + 1
    return _playing(songs[0], full=capped), True


async def _start_songs(
    member: object,
    guild: object | None,
    songs: list[_Song],
    *,
    replace: bool,
) -> str:
    if not songs:
        return "Nothing on YouTube matched that."
    async with _hold(guild):
        channel = _room(member, guild)
        if isinstance(channel, str):
            return channel
        guild_id = _guild_id(guild)
        if guild_id is not None:
            _recall(guild_id)
        line = _stored(guild)
        old = None if line is None else line.current
        missed = await _play_song(guild, channel, songs[0])
        if missed:
            return missed
        line = _stored(guild)
        if line is not None and old is not None and old is not line.current:
            _keep_earlier(guild, line, old)
        if line is not None:
            line.repeat = False
            if replace:
                line.later = songs[1:QUEUE_SONGS + 1]
                line.whole = False
        _store(guild)
        paint_soon(guild)
        return _playing(songs[0], full=replace and len(songs) > QUEUE_SONGS + 1)


async def _play_song(
    guild: object,
    channel: object,
    song: _Song,
    *,
    failed: str | None = None,
    requester: int | None = None,
) -> str | None:
    player = await _join(guild, channel)
    if isinstance(player, str):
        return player
    if requester is not None and song.requester_id is None:
        song.requester_id = requester
    return await _begin(player, guild, song, failed=failed)


async def _disconnect(player: object | None) -> str:
    if player is None:
        return "Nothing is playing."
    try:
        await player.stop()
    except Exception:
        log.exception("Could not stop the player")
    try:
        await player.disconnect()
    except Exception:
        log.exception("Could not leave the voice channel")
        return "The music stopped, but I am still in the voice channel."
    return "Stopped."


def _room(member: object, guild: object | None) -> object | str:
    """The voice channel to use. Someone outside an open room stays outside."""
    if guild is None:
        return _NO_SERVER
    channel = getattr(getattr(member, "voice", None), "channel", None)
    if channel is None:
        return "Join a voice channel first."
    player = _voice_player(guild)
    here = getattr(player, "channel", None) if player is not None else None
    if here is None:
        here = joining_channel(guild)
    if here is not None and not member_in_voice(member, guild) and _other_room(here, channel):
        return "Join the voice channel first."
    return channel


def _other_room(left: object, right: object) -> bool:
    left_id = _channel_num(left)
    right_id = _channel_num(right)
    if left_id is not None and right_id is not None:
        return left_id != right_id
    return left is not None and right is not None and left != right


def _take(line: _Line, songs: list[_Song], whole: bool) -> list[_Song]:
    taken = _fit(line.later, songs, whole=whole)
    if whole:
        line.whole = True
    return taken


def _fit(later: list[_Song], songs: list[_Song], *, whole: bool) -> list[_Song]:
    """A playlist keeps every song. Anything else stops at the waiting cap."""
    if whole:
        later.extend(songs)
        return list(songs)
    room = QUEUE_SONGS - len(later)
    if room <= 0:
        return []
    taken = songs[:room]
    later.extend(taken)
    return taken


def _failed(song: _Song) -> str:
    return f"{source.platform_label('', song.platform)} did not start that song."


def _playing(song: _Song, *, full: bool) -> str:
    note = f"Playing {_title(song.track)}."
    return f"{note} {_FULL}" if full else note


def _added(taken: list[_Song], offered: int) -> str:
    if not taken:
        return _FULL
    if len(taken) == 1:
        note = f"Added {_title(taken[0].track)}."
    else:
        note = f"Added {len(taken)} songs."
    return f"{note} {_FULL}" if len(taken) < offered else note
