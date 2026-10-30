"""Play a search result, queue songs, and stop."""

from __future__ import annotations

import asyncio
import logging

from music import source
from music.archive import _keep_loaded
from music.lavalink import IDLE_SECONDS, _channel_num, _join, _replaced, joining, joining_channel
from music.playback.arrange import _keep_current, _keep_earlier
from music.playback.control import _begin
from music.playback.remember import (
    QUEUE_SONGS, _CHANGED, _Line, _Song, _drop, _end_saved, _forget, _guild_id,
    _hold, _line, _order_add, _order_drop, _playlist_skips, _pull, _recall,
    _saved_generation, _spread,
    _store, _stored, _unshuffle,
    _occupied_channel, _seen_channel, _title, _voice_player, _whole, paint_soon, release,
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
                _cancel_pause_leave(guild)
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
        # A song already on the air keeps the channel. A saved one joins again later.
        line = _stored(guild)
        if line is not None and line.current is not None and not line.resting:
            return
        try:
            await player.disconnect()
        except Exception:
            log.warning("music could not leave an unused voice channel", exc_info=True)


# A reload runs this file again. The wait that is already counting has to stay,
# or a paused song would lose its leave timer.
_pause_waits: dict[int, asyncio.Task] = _keep_loaded("_pause_waits", {}, globals())
# Bumped when a wait starts or is dropped, so a late one cannot leave after a new pause.
_pause_token: dict[int, int] = _keep_loaded("_pause_token", {}, globals())
# The wait finished and the leave has not run yet.
_pause_ready: set[int] = _keep_loaded("_pause_ready", set(), globals())


def _arm_pause_leave(guild: object | None) -> None:
    """A paused song gets the same wait as a player with nothing on."""
    guild_id = _guild_id(guild)
    if guild_id is None:
        return
    _cancel_pause_leave(guild)
    token = _pause_token.get(guild_id, 0) + 1
    _pause_token[guild_id] = token
    _pause_waits[guild_id] = asyncio.create_task(
        _leave_after_pause(guild, guild_id, token), name="music-pause-leave",
    )


def _cancel_pause_leave(guild: object | None) -> None:
    guild_id = _guild_id(guild)
    if guild_id is None:
        return
    if guild_id in _pause_token:
        _pause_token[guild_id] += 1
    _pause_ready.discard(guild_id)
    task = _pause_waits.get(guild_id)
    if task is None or task is asyncio.current_task():
        return
    _pause_waits.pop(guild_id, None)
    if not task.done():
        task.cancel()


async def _wait(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _leave_after_pause(guild: object, guild_id: int, token: int) -> None:
    await _wait(IDLE_SECONDS)
    if _pause_token.get(guild_id) != token:
        return
    if _pause_waits.get(guild_id) is not asyncio.current_task():
        return
    _pause_waits.pop(guild_id, None)
    player = _voice_player(guild)
    paused = player is not None and getattr(player, "paused", None) is True
    if not paused or getattr(player, "current", None) is None:
        return
    # The quiet-player event leaves and takes the card, the same as no song.
    _pause_ready.add(guild_id)
    client = getattr(player, "client", None)
    dispatch = getattr(client, "dispatch", None)
    try:
        if dispatch is not None:
            dispatch("wavelink_inactive_player", player)
            return
        if await leave_if_quiet(guild):
            await refresh_line(guild)
    except Exception:
        log.exception("music could not leave after a pause")


async def leave_if_quiet(guild: object | None) -> bool:
    """Leave when no song is on, or a pause has lasted the quiet wait. A song on stays."""
    if guild is None:
        return False
    async with _hold(guild):
        player = _voice_player(guild)
        if player is None:
            return False
        current = getattr(player, "current", None)
        paused = getattr(player, "paused", None) is True
        if current is not None and not paused:
            return False
        guild_id = _guild_id(guild)
        if paused and current is not None:
            # A newer pause replaced this wait. Stay until that one finishes.
            if guild_id not in _pause_ready:
                return False
            _pause_ready.discard(guild_id)
            line = _stored(guild)
            if line is not None:
                line.paused = True
        try:
            await player.disconnect()
        except Exception:
            log.warning("music could not leave an idle voice channel", exc_info=True)
            # Discord can drop the link before Lavalink answers. The bot is out
            # either way, so the saved song is stored instead of left half on.
            if getattr(guild, "voice_client", None) is not None:
                return False
        release(guild)
        return True


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
        # A pause keeps this song current. Anything saved without a pause waits up front.
        away = _paused_away(player, line)
        if not away:
            _park_resting(player, line)
        try:
            # Already on a song, so this one waits. A reload must not restart it or drop the queue.
            if away or _still_on(player, line) or _radio_holds(player, line):
                note, changed = _add_waiting(guild, songs, whole)
            else:
                moved = _shift_reloaded(player, line)
                if _waiting(line):
                    note, changed = await _start_parked(member, guild, channel, songs, whole)
                    if moved is not None and not changed:
                        _restore_current(guild, moved)
                else:
                    # A kick leaves a line behind. The next play starts clean.
                    if player is None:
                        release(guild)
                    note, changed = await _start_fresh(guild, channel, songs, whole)
        finally:
            if line is not None:
                line.handoff = False
    if changed:
        paint_soon(guild)
    return note


def _paused_away(player: object, line: _Line | None) -> bool:
    """The bot left on a pause. That song is still the current one."""
    if line is None or not line.paused or line.current is None:
        return False
    return player is None or getattr(player, "current", None) is None


def _park_resting(player: object, line: _Line | None) -> None:
    """The saved song is not on the air. It waits at the front until playback starts."""
    if line is None or not line.resting or line.current is None:
        return
    if player is not None and getattr(player, "current", None) is not None:
        line.resting = False
        return
    line.handoff = True
    line.later.insert(0, line.current)
    line.current = None
    line.resting = False


def _shift_reloaded(player: object, line: _Line | None) -> _Song | None:
    """A reloaded player is silent. Its song waits at the front with the rest."""
    if player is None or not _replaced(player) or line is None or line.current is None:
        return None
    line.handoff = True
    song = line.current
    line.later.insert(0, song)
    if line.shuffle:
        line.order.insert(0, song)
    line.current = None
    return song


def _restore_current(guild: object, song: _Song) -> None:
    held = _stored(guild)
    if held is None or held.current is not None or not held.later or held.later[0] is not song:
        return
    held.current = held.later.pop(0)
    _order_drop(held, held.current)
    _store(guild)


def _radio_holds(player: object, line: _Line | None) -> bool:
    """Radio keeps its song. A new one waits even in the gap before the next."""
    return player is not None and line is not None and line.radio and line.current is not None


def _still_on(player: object, line: _Line | None) -> bool:
    """True while a song is on, including the gap before the next one.

    A reload marks the player stale. The song is still going when that player
    and the line both have it, so a new song waits instead of starting over.
    """
    if player is None:
        return False
    playing = getattr(player, "current", None) is not None
    if _replaced(player):
        return playing and line is not None and line.current is not None
    if playing:
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
    first = kept.later.pop(0)
    taken = _take(kept, songs, whole)

    def undo() -> None:
        kept.whole = was_whole
        for song in taken:
            _pull(kept.later, song)
            _pull(kept.order, song)
        kept.later.insert(0, first)
        _store(guild)

    missed = await _play_song(
        guild, channel, first, failed=_failed(first), requester=_who(member),
    )
    if missed:
        undo()
        return missed, False
    _order_drop(kept, first)
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
        line.shuffle = False
        line.order.clear()
        # A card may have loaded the queue a kick just saved. This play replaces it.
        line.later = list(rest)
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
        _park_resting(_voice_player(guild), line)
        parked = line
        try:
            if line is not None and line.radio and line.current is not None:
                # This song keeps playing. The new ones wait with the rest.
                taken = _take(line, songs, False)
                if taken:
                    _store(guild)
                    paint_soon(guild)
                return _added(taken, len(songs))
            old = None if line is None else line.current
            missed = await _play_song(guild, channel, songs[0])
            if missed:
                return missed
            line = _stored(guild)
            if line is not None and old is not None and old is not line.current:
                _keep_earlier(guild, line, old)
            if line is not None:
                line.repeat = False
                if line.shuffle and not replace:
                    _unshuffle(line)
                else:
                    line.shuffle = False
                    line.order.clear()
                if replace:
                    line.later = songs[1:QUEUE_SONGS + 1]
                    line.whole = False
            _store(guild)
            paint_soon(guild)
            return _playing(songs[0], full=replace and len(songs) > QUEUE_SONGS + 1)
        finally:
            if parked is not None:
                parked.handoff = False


async def _play_song(
    guild: object,
    channel: object,
    song: _Song,
    *,
    failed: str | None = None,
    requester: int | None = None,
    at: int = 0,
) -> str | None:
    player = await _join(guild, channel)
    if isinstance(player, str):
        return player
    if requester is not None and song.requester_id is None:
        song.requester_id = requester
    return await _begin(player, guild, song, failed=failed, at=at)


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
    channel = _seen_channel(member, guild)
    if channel is None:
        return "Join a voice channel first."
    # Discord's voice state, not a player channel left behind after a dropped link.
    here = _occupied_channel(guild)
    if here is None:
        here = joining_channel(guild)
    if here is not None and _other_room(here, channel):
        return "Join the voice channel first."
    return channel


def _other_room(left: object, right: object) -> bool:
    left_id = _channel_num(left)
    right_id = _channel_num(right)
    if left_id is not None and right_id is not None:
        return left_id != right_id
    return left is not None and right is not None and left != right


def _take(line: _Line, songs: list[_Song], whole: bool) -> list[_Song]:
    taken = _fit(line.later, songs, whole=whole, mix=line.shuffle)
    _order_add(line, taken)
    if whole:
        line.whole = True
    return taken


def _fit(later: list[_Song], songs: list[_Song], *, whole: bool, mix: bool = False) -> list[_Song]:
    """A playlist keeps every song. Anything else stops at the waiting cap."""
    if whole:
        _spread(later, songs, mix=mix)
        return list(songs)
    room = QUEUE_SONGS - len(later)
    if room <= 0:
        return []
    taken = songs[:room]
    _spread(later, taken, mix=mix)
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
