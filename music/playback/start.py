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
    QUEUE_SONGS, _CHANGED, _Line, _Song, _drop, _end_saved, _fill_held, _forget, _guild_id, _hold, _line,
    _occupied_channel, _order_add, _playlist_skips, _pull, _recall, _saved_generation, _seen_channel,
    _spread, _store, _stored, _title, _voice_player, _number, paint_soon, refresh_line, save_and_clear,
)

log = logging.getLogger(__name__)

_NO_SERVER = "Use this command in a server."
_FULL = "The queue is full."
_STOPPED = "Stopped."
_JOIN = "Join a voice channel first."
_JOIN_THIS = "Join the voice channel first."

def _who(member: object) -> int | None:
    return _number(getattr(member, "id", None))
def _songs(member: object, tracks: list[tuple[object, str]]) -> list[_Song]:
    who = _who(member)
    return [_Song(track, who, source.normalize_platform(platform)) for track, platform in tracks]

async def load_line(member: object, guild: object | None, tracks: list[tuple[object, str]]) -> str:
    """Replace the line with these songs and start the first."""
    return await _start_songs(member, guild, _songs(member, tracks), replace=True)
async def play_now(member: object, guild: object | None, track: object, platform: str) -> str:
    """Start this song now. Songs already lined up stay after it."""
    return await _start_songs(member, guild, _songs(member, [(track, platform)]), replace=False)
async def queue_opened(
    member: object, guild: object | None, tracks: list[tuple[object, str]], *,
    whole: bool = False, token: int | None = None,
) -> str:
    """Add these songs in order. Each keeps its own platform.

    A playlist passes whole and keeps every song. The ones past the cap play when a slot opens.
    A token drops the song when a newer playlist or a stop already won.
    """
    if guild is None:
        return _NO_SERVER
    if not tracks:
        return "That playlist has no songs."
    return await _enqueue(member, guild, _songs(member, tracks), whole=whole, token=token)
async def queue_tracks(
    member: object, guild: object | None, tracks: list, platform: str = "youtube",
    videos: tuple[str, ...] = (), video_at: int = 0, station: str = "",
) -> str:
    """Join the member's voice channel when this is what starts playback."""
    if guild is None:
        return _NO_SERVER
    platform = source.normalize_platform(platform)
    if not tracks:
        return f"Nothing on {source.platform_label('', platform)} matched that."
    songs = _songs(member, [(track, platform) for track in tracks])
    if videos:
        songs[0].videos, songs[0].video_at, songs[0].station = videos, video_at, station
    return await _enqueue(member, guild, songs)

def _end_opening(guild: object) -> int | None:
    """End a playlist that is still opening. The id is used if the leave fails."""
    guild_id = _guild_id(guild)
    if guild_id is None:
        return None
    _recall(guild_id)
    _end_saved(guild_id)
    _playlist_skips.pop(guild_id, None)
    return guild_id
async def stop_track(guild: object | None) -> str:
    if guild is None:
        return _NO_SERVER
    try:
        async with _hold(guild):
            _end_opening(guild)
            _cancel_pause_leave(guild)
            _keep_current(guild)
            _drop(guild)
            _forget(guild)
            _store(guild)
            return await _disconnect(getattr(guild, "voice_client", None))
    finally:
        paint_soon(guild)
async def leave_track(guild: object | None) -> str:
    """Leave voice and keep the queue. The song that was on stays with the rest.

    A failed disconnect puts that line back, so a bot still in the room keeps it.
    """
    if guild is None:
        return _NO_SERVER
    try:
        async with _hold(guild):
            guild_id = _end_opening(guild)
            save_and_clear(guild)
            note = await _disconnect(getattr(guild, "voice_client", None))
            if note != _STOPPED and getattr(guild, "voice_client", None) is not None and guild_id is not None:
                _recall(guild_id)
            return note
    finally:
        paint_soon(guild)

def warm_voice(member: object, guild: object | None) -> None:
    """Start joining while a search is still running. The play waits for that join."""
    if guild is None or getattr(guild, "voice_client", None) is not None:
        return
    channel = _room(member, guild)
    if not isinstance(channel, str):
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
        player, line = _voice_player(guild), _stored(guild)
        # A song on the air stays. A saved one joins again later.
        playing = player is not None and getattr(player, "current", None) is not None
        saved = line is not None and line.current is not None and not line.resting
        if player is None or playing or saved:
            return
        try:
            await player.disconnect()
        except Exception:
            log.warning("music could not leave an unused voice channel", exc_info=True)

# A reload runs this file again. The wait already counting has to stay.
_pause_waits: dict[int, asyncio.Task] = _keep_loaded("_pause_waits", {}, globals())
_pause_token: dict[int, int] = _keep_loaded("_pause_token", {}, globals())
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
    # The task that is leaving must not cancel itself.
    if task is None or task is asyncio.current_task():
        return
    _pause_waits.pop(guild_id, None)
    if not task.done():
        task.cancel()
async def _wait(seconds: float) -> None:
    await asyncio.sleep(seconds)
async def _leave_after_pause(guild: object, guild_id: int, token: int) -> None:
    await _wait(IDLE_SECONDS)
    if _pause_token.get(guild_id) != token or _pause_waits.get(guild_id) is not asyncio.current_task():
        return
    _pause_waits.pop(guild_id, None)
    player = _voice_player(guild)
    paused = player is not None and getattr(player, "paused", None) is True
    if not paused or getattr(player, "current", None) is None:
        return
    # The quiet-player event leaves and takes the card, the same as no song.
    _pause_ready.add(guild_id)
    dispatch = getattr(getattr(player, "client", None), "dispatch", None)
    try:
        if dispatch is not None:
            dispatch("wavelink_inactive_player", player)
            return
        if await leave_if_quiet(guild):
            await refresh_line(guild)
    except Exception:
        log.exception("music could not leave after a pause")
async def leave_if_quiet(guild: object | None) -> bool:
    """Leave when nothing is on, or a pause has lasted the quiet wait."""
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
        save_and_clear(guild)
        return True

def _awake(guild: object) -> tuple[object, _Line | None]:
    guild_id = _guild_id(guild)
    if guild_id is not None:
        _recall(guild_id)
    return _voice_player(guild), _stored(guild)
async def _enqueue(
    member: object, guild: object, songs: list[_Song], *, whole: bool = False, token: int | None = None,
) -> str:
    channel = _room(member, guild)
    if isinstance(channel, str):
        return channel
    note, changed = _NO_SERVER, False
    async with _hold(guild):
        if token is not None and _saved_generation.get(_guild_id(guild), 0) != token:
            return _CHANGED
        channel = _room(member, guild)
        if isinstance(channel, str):
            return channel
        player, line = _awake(guild)
        away = _paused_away(player, line)
        if not away:
            _park_resting(player, line)
        try:
            if away or _still_on(player, line):
                note, changed = _add_waiting(guild, songs, whole)
                if away and line is not None and line.current is not None:
                    note = _still_paused(note, line.current.track)
            else:
                note, changed = await _open_waiting(member, guild, channel, songs, player, line, whole)
        finally:
            if line is not None:
                line.handoff = False
    if changed:
        paint_soon(guild)
    return note
async def _open_waiting(member, guild, channel, songs, player, line, whole) -> tuple[str, bool]:
    """Nothing is on. Start these songs, or put them in front of a parked line."""
    moved = _shift_reloaded(player, line)
    parked = _waiting(line)
    if not parked and player is None:
        # A kick leaves a line behind. The next play starts clean.
        save_and_clear(guild)
    note, changed = await _open_songs(
        guild, channel, songs, whole=whole, parked=parked,
        requester=_who(member) if parked else None,
    )
    if moved is not None and not changed:
        _restore_current(guild, moved)
    return note, changed
def paused_away(guild: object | None) -> bool:
    """The bot is out of voice and a paused song is still the current one."""
    if guild is None or getattr(guild, "voice_client", None) is not None:
        return False
    player, line = _awake(guild)
    return _paused_away(player, line)
def _paused_away(player: object, line: _Line | None) -> bool:
    quiet = player is None or getattr(player, "current", None) is None
    return quiet and line is not None and line.paused and line.current is not None
def _still_paused(note: str, track: object) -> str:
    title = _title(track)
    if note == _FULL:
        return f"{title} is still paused. {_FULL}"
    return f"{note} {title} is still paused."
def _to_front(line: _Line, song: _Song) -> None:
    line.handoff = True
    line.later.insert(0, song)
    line.current = None
def _park_resting(player: object, line: _Line | None) -> None:
    """A saved song that is not on the air waits at the front until playback starts."""
    if line is None or not line.resting or line.current is None:
        return
    if player is not None and getattr(player, "current", None) is not None:
        line.resting = False
        return
    _to_front(line, line.current)
    line.resting = False
def _shift_reloaded(player: object, line: _Line | None) -> _Song | None:
    """A reloaded player is silent. Its song waits at the front with the rest."""
    if player is None or not _replaced(player) or line is None or line.current is None:
        return None
    song = line.current
    _to_front(line, song)
    return song
def _restore_current(guild: object, song: _Song) -> None:
    held = _stored(guild)
    if held is None or held.current is not None or not held.later or held.later[0] is not song:
        return
    held.current = held.later.pop(0)
    _store(guild)
def _still_on(player: object, line: _Line | None) -> bool:
    """True while a song is on, including the gap before the next one.

    A reloaded player is stale. The song still counts when the line has it,
    and radio keeps it through that gap, so a new song waits.
    """
    if player is None:
        return False
    playing = getattr(player, "current", None) is not None
    if line is None or line.current is None:
        return playing and not _replaced(player)
    return (playing or line.radio) if _replaced(player) else True
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

async def _open_songs(
    guild: object, channel: object, songs: list[_Song], *,
    whole: bool, parked: bool = False, requester: int | None = None,
) -> tuple[str, bool]:
    """Play the first song that starts. A dead one is named, and the rest stay.

    A parked line keeps saved songs behind the request and drops past the cap.
    A miss puts that line back. A fresh start replaces whatever a kick left.
    """
    kept = _line(guild) if parked else None
    if parked and (kept is None or not songs):
        return _NO_SERVER, False
    snap = None if kept is None else (
        list(kept.later), list(kept.order), kept.whole, kept.shuffle, kept.repeat, kept.current,
        list(kept.held),
    )
    skipped: list[str] = []
    missed: str | None = None
    full = False
    for index, song in enumerate(songs):
        rest = songs[index + 1:]
        if kept is not None and snap is not None:
            full = _stage(kept, rest, snap, whole)
        missed = await _play_song(guild, channel, song, failed=_failed(song), requester=requester)
        if missed:
            if kept is not None and snap is not None:
                kept.later, kept.order, kept.whole, kept.shuffle, kept.repeat, kept.current, kept.held = snap
                _store(guild)
            skipped.append(_title(song.track))
            continue
        if kept is None:
            full = _fresh_line(guild, rest, whole)
        _store(guild)
        note = _playing(song, full=full)
        if skipped:
            note = f"{note} Skipped {', '.join(skipped)}."
        return note, True
    return missed or _failed(songs[-1]), False
def _stage(line: _Line, rest: list[_Song], snap: tuple, whole: bool) -> bool:
    """Put the rest of this request in front of the saved songs, then cap it."""
    saved, order, was_whole = snap[0], snap[1], snap[2]
    prior = snap[6]
    combined = [*rest, *saved]
    line.later = combined[:QUEUE_SONGS]
    line.order = list(order)
    overflow = combined[QUEUE_SONGS:]
    for dropped in overflow:
        _pull(line.order, dropped)
    _order_add(line, line.later[: len(rest)])
    if whole:
        # A playlist keeps the songs that did not fit. They play when a slot opens.
        line.held = [*overflow, *prior]
        line.whole = not line.held
        return False
    line.held = list(prior)
    overflowed = bool(overflow)
    # Cutting the line turns the cap on. An add that fits leaves the old flag.
    line.whole = (not overflowed) if overflowed else was_whole
    return overflowed
def _fresh_line(guild: object, rest: list[_Song], whole: bool) -> bool:
    line = _line(guild)
    if line is None:
        return len(rest) > QUEUE_SONGS and not whole
    line.repeat = False
    line.shuffle = False
    line.order.clear()
    line.later = rest[:QUEUE_SONGS]
    overflow = rest[QUEUE_SONGS:]
    if whole:
        line.held = list(overflow)
        line.whole = not line.held
        return False
    line.held = []
    line.whole = False
    return bool(overflow)

async def _start_songs(member: object, guild: object | None, songs: list[_Song], *, replace: bool) -> str:
    if not songs:
        return "Nothing on YouTube matched that."
    async with _hold(guild):
        channel = _room(member, guild)
        if isinstance(channel, str):
            return channel
        player, line = _awake(guild)
        _park_resting(player, line)
        try:
            if not replace and line is not None and line.radio and line.current is not None:
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
            started = _stored(guild)
            if started is not None and old is not None and old is not started.current:
                _keep_earlier(guild, started, old)
            if started is not None:
                started.repeat = False
                # Play now keeps a shuffled queue. Replacing the line starts in order.
                if replace or not started.shuffle:
                    started.shuffle = False
                    started.order.clear()
                if replace:
                    started.later = songs[1:QUEUE_SONGS + 1]
                    started.held = list(songs[QUEUE_SONGS + 1:])
                    started.whole = False
                    started.radio = False
            _store(guild)
            paint_soon(guild)
            return _playing(songs[0], full=replace and len(songs) > QUEUE_SONGS + 1)
        finally:
            if line is not None:
                line.handoff = False
async def _play_song(
    guild: object, channel: object, song: _Song, *,
    failed: str | None = None, requester: int | None = None, at: int = 0,
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
    return _STOPPED

def _room(member: object, guild: object | None) -> object | str:
    """The voice channel to use. Someone outside an open room stays outside."""
    if guild is None:
        return _NO_SERVER
    channel = _seen_channel(member, guild)
    if channel is None:
        return _JOIN
    # Discord's voice state, not a player channel left behind after a dropped link.
    here = _occupied_channel(guild) or joining_channel(guild)
    if here is not None and _other_room(here, channel):
        return _JOIN_THIS
    return channel
def _other_room(left: object, right: object) -> bool:
    left_id, right_id = _channel_num(left), _channel_num(right)
    if left_id is not None and right_id is not None:
        return left_id != right_id
    return left is not None and right is not None and left != right
def _take(line: _Line, songs: list[_Song], whole: bool) -> list[_Song]:
    """Stop at the waiting cap. A playlist keeps the rest until a slot opens.

    Shuffle mixes a song added on its own, and that add clears the playlist
    flag so Previous keeps the cap. A playlist stays in its own order.
    """
    # Songs already waiting past the cap take an open slot before this add.
    _fill_held(line)
    room = QUEUE_SONGS - len(line.later)
    if whole:
        fitting = songs[:room] if room > 0 else []
        rest = songs[len(fitting):]
        if fitting:
            _spread(line.later, fitting, mix=False)
            _order_add(line, fitting)
        if rest:
            line.held.extend(rest)
        line.whole = not rest and not line.held
        return list(songs)
    taken = songs[:room] if room > 0 else []
    if taken:
        _spread(line.later, taken, mix=line.shuffle)
        _order_add(line, taken)
        line.whole = False
    return taken
def _failed(song: _Song) -> str:
    return f"{source.platform_label('', song.platform)} did not start that song."
def _playing(song: _Song, *, full: bool) -> str:
    note = f"Playing {_title(song.track)}."
    return f"{note} {_FULL}" if full else note
def _added(taken: list[_Song], offered: int) -> str:
    if not taken:
        return _FULL
    note = f"Added {_title(taken[0].track)}." if len(taken) == 1 else f"Added {len(taken)} songs."
    return f"{note} {_FULL}" if len(taken) < offered else note
