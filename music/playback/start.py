"""Play a search result, queue songs, and stop."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager

import wavelink

from music import source
from music.lavalink import (
    _channel_num, _join, _play_track, _replaced, joining_channel,
    keep_track, load_link, ready_track,
)
from music.playback.arrange import _keep_current, _keep_earlier
from music.playback.remember import (
    QUEUE_SONGS, _CHANGED, _Line, _Song, _drop, _end_saved, _fill_held, _forget, _guild_id, _hold, _line,
    _occupied_channel, _order_add, _place, _pull, _recall, _remember_id, _saved_generation, _seen_channel,
    _spread, _store, _stored, _title, _voice_player, _number, forget_bot, note_bot_in, paint_soon,
    save_and_clear,
)

log = logging.getLogger(__name__)

_NO_SERVER = "Use this command in a server."
_FULL = "The queue is full."
_STOPPED = "Stopped."
_JOIN = "Join a voice channel first."
_JOIN_THIS = "Join the voice channel first."
_BLOCKED_ONE = "That song is not playable."
_BLOCKED_MANY = "Those songs are not playable."


def _accepted(member: object, tracks: list[tuple[object, str]]) -> list[_Song] | str:
    kept = [item for item in tracks if not source.unplayable(item[0])]
    if not kept:
        return _BLOCKED_ONE if len(tracks) == 1 else _BLOCKED_MANY
    who = _number(getattr(member, "id", None))
    return [_Song(track, who, source.normalize_platform(platform)) for track, platform in kept]


async def load_line(member: object, guild: object | None, tracks: list[tuple[object, str]]) -> str:
    if not tracks:
        return await _start_songs(member, guild, [], replace=True)
    ready = _accepted(member, tracks)
    if isinstance(ready, str):
        return ready
    return await _start_songs(member, guild, ready, replace=True)


async def play_now(member: object, guild: object | None, track: object, platform: str) -> str:
    ready = _accepted(member, [(track, platform)])
    if isinstance(ready, str):
        return ready
    return await _start_songs(member, guild, ready, replace=False)


async def queue_opened(
    member: object, guild: object | None, tracks: list[tuple[object, str]], *,
    whole: bool = False, token: int | None = None,
) -> str:
    if guild is None:
        return _NO_SERVER
    if not tracks:
        return "That playlist has no songs."
    ready = _accepted(member, tracks)
    if isinstance(ready, str):
        return ready
    return await _enqueue(member, guild, ready, whole=whole, token=token)


async def queue_tracks(
    member: object, guild: object | None, tracks: list, platform: str = "youtube",
    videos: tuple[str, ...] = (), video_at: int = 0, station: str = "",
    *,
    whole: bool = False,
) -> str:
    if guild is None:
        return _NO_SERVER
    platform = source.normalize_platform(platform)
    if not tracks:
        return f"Nothing on {source.platform_label('', platform)} matched that."
    ready = _accepted(member, [(track, platform) for track in tracks])
    if isinstance(ready, str):
        return ready
    if videos:
        ready[0].videos = videos
        ready[0].video_at = video_at
        ready[0].station = station
    return await _enqueue(member, guild, ready, whole=whole)


def _end_opening(guild: object) -> int | None:
    guild_id = _guild_id(guild)
    if guild_id is None:
        return None
    _recall(guild_id)
    _end_saved(guild_id)
    return guild_id


async def stop_track(guild: object | None) -> str:
    from music.playback.prepare import _retire_fill
    from music.playback.idle import _cancel_pause_leave

    if guild is None:
        return _NO_SERVER
    _retire_fill(guild)
    try:
        async with _hold(guild):
            _end_opening(guild)
            _cancel_pause_leave(guild)
            _keep_current(guild)
            _drop(guild)
            _forget(guild)
            _store(guild)
            note = await _disconnect(getattr(guild, "voice_client", None))
            if note == _STOPPED or getattr(guild, "voice_client", None) is None:
                forget_bot(guild)
            return note
    finally:
        paint_soon(guild)


async def leave_voice(guild: object | None) -> str:
    if guild is None:
        return _NO_SERVER
    from music.playback.prepare import _retire_fill
    _retire_fill(guild)
    try:
        async with _hold(guild):
            guild_id = _end_opening(guild)
            save_and_clear(guild)
            note = await _disconnect(getattr(guild, "voice_client", None))
            # A failed disconnect puts that line back. A bot still in the room keeps it.
            if note != _STOPPED and getattr(guild, "voice_client", None) is not None and guild_id is not None:
                note_bot_in(guild)
                _recall(guild_id)
            return note
    finally:
        paint_soon(guild)


def _on_player(player: object | None) -> object | None:
    return None if player is None else getattr(player, "current", None)


def _player_paused(player: object | None) -> bool:
    return player is not None and getattr(player, "paused", None) is True


def _line_holding(line: _Line | None) -> bool:
    return line is not None and line.current is not None


def warm_voice(member: object, guild: object | None) -> None:
    if guild is None or getattr(guild, "voice_client", None) is not None:
        return
    channel = _room(member, guild)
    if not isinstance(channel, str):
        asyncio.create_task(_join(guild, channel), name="music-warm")


def _awake(guild: object) -> tuple[object, _Line | None]:
    guild_id = _guild_id(guild)
    if guild_id is not None:
        _recall(guild_id)
    return _voice_player(guild), _stored(guild)


@contextmanager
def _handoff(line: _Line | None):
    # A refresh during the join must not take the song this start is about to fill.
    if line is not None:
        line.handoff = True
    try:
        yield
    finally:
        if line is not None:
            line.handoff = False


def _paused_away(player: object, line: _Line | None) -> bool:
    # The bot is out of voice and a paused song is still the current one.
    return _on_player(player) is None and _line_holding(line) and line.paused


def paused_away(guild: object | None) -> bool:
    if guild is None or getattr(guild, "voice_client", None) is not None:
        return False
    return _paused_away(*_awake(guild))


def _to_front(line: _Line, song: _Song) -> None:
    line.handoff = True
    line.later.insert(0, song)
    line.current = None


def _park_resting(player: object, line: _Line | None) -> None:
    # A saved song that is not on the air waits at the front until playback starts.
    if not _line_holding(line) or not line.resting:
        return
    if _on_player(player) is None:
        _to_front(line, line.current)
    line.resting = False


def _shift_reloaded(player: object, line: _Line | None) -> _Song | None:
    # A reloaded player is silent. Its song waits at the front with the rest.
    if player is None or not _replaced(player) or not _line_holding(line):
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
    # A reloaded player is stale. Radio keeps its song through the gap, so a new one waits.
    if player is None:
        return False
    playing = _on_player(player) is not None
    if not _line_holding(line):
        return playing and not _replaced(player)
    return (playing or line.radio) if _replaced(player) else True


def _fit(songs: list[_Song], used: int = 0) -> tuple[list[_Song], list[_Song]]:
    room = QUEUE_SONGS - used
    if room <= 0:
        return [], list(songs)
    return songs[:room], songs[room:]


def _past_cap(line: _Line, overflow: list[_Song], prior: list, *, whole: bool, was_whole: bool) -> bool:
    # A playlist keeps songs past the cap. Cutting any other add turns the cap on.
    if whole:
        line.held = [*overflow, *prior]
        line.whole = not line.held
        return False
    line.held = list(prior)
    line.whole = False if overflow else was_whole
    return bool(overflow)


def _take(line: _Line, songs: list[_Song], whole: bool) -> list[_Song]:
    # Songs already waiting past the cap take an open slot before this add.
    _fill_held(line)
    fitting, overflow = _fit(songs, len(line.later))
    if whole:
        if fitting:
            _spread(line.later, fitting, mix=False)
            _order_add(line, fitting)
        if overflow:
            line.held.extend(overflow)
        line.whole = not line.held
        return list(songs)
    if fitting:
        _spread(line.later, fitting, mix=line.shuffle)
        _order_add(line, fitting)
        line.whole = False
    return fitting


def _add_waiting(guild: object, songs: list[_Song], whole: bool) -> tuple[str, bool]:
    kept = _line(guild)
    if kept is None:
        return _NO_SERVER, False
    taken = _take(kept, songs, whole)
    if taken:
        _store(guild)
    return _added(taken, len(songs)), bool(taken)


async def _open_waiting(member, guild, channel, songs, player, line, whole) -> tuple[str, bool]:
    moved = _shift_reloaded(player, line)
    parked = line is not None and line.current is None and bool(line.later)
    if not parked and player is None:
        # A kick leaves a line behind. The next play starts clean.
        save_and_clear(guild)
    kept = _line(guild) if parked else None
    if parked and (kept is None or not songs):
        if moved is not None:
            _restore_current(guild, moved)
        return _NO_SERVER, False
    snap = None if kept is None else (
        list(kept.later), list(kept.order), kept.whole, kept.shuffle, kept.repeat,
        kept.current, list(kept.held),
    )
    requester = _number(getattr(member, "id", None)) if parked else None
    skipped: list[str] = []
    missed: str | None = None
    for index, song in enumerate(songs):
        rest = songs[index + 1:]
        if snap is not None and kept is not None:
            saved, order, was_whole, prior = snap[0], snap[1], snap[2], snap[6]
            fitting, overflow = _fit([*rest, *saved])
            kept.later = fitting
            kept.order = list(order)
            for dropped in overflow:
                _pull(kept.order, dropped)
            _order_add(kept, fitting[: len(rest)])
            full = _past_cap(kept, overflow, prior, whole=whole, was_whole=was_whole)
        missed = await _play_song(guild, channel, song, failed=_failed(song), requester=requester)
        if missed:
            if snap is not None and kept is not None:
                kept.later, kept.order, kept.whole, kept.shuffle, kept.repeat, kept.current, kept.held = snap
                _store(guild)
            skipped.append(_title(song.track))
            continue
        if kept is None:
            made = _line(guild)
            fitting, overflow = _fit(rest)
            if made is None:
                full = bool(overflow) and not whole
            else:
                made.repeat = False
                made.shuffle = False
                made.order.clear()
                made.later = fitting
                full = _past_cap(made, overflow, [], whole=whole, was_whole=False)
        _store(guild)
        note = _playing(song, full=full)
        if skipped:
            note = f"{note} Skipped {', '.join(skipped)}."
        return note, True
    if moved is not None:
        _restore_current(guild, moved)
    return missed or _failed(songs[-1]), False


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
        with _handoff(line):
            if away or _still_on(player, line):
                note, changed = _add_waiting(guild, songs, whole)
            else:
                note, changed = await _open_waiting(member, guild, channel, songs, player, line, whole)
    if changed:
        paint_soon(guild)
    return note


async def _start_songs(member: object, guild: object | None, songs: list[_Song], *, replace: bool) -> str:
    if not songs:
        return "Nothing on YouTube matched that."
    async with _hold(guild):
        channel = _room(member, guild)
        if isinstance(channel, str):
            return channel
        player, line = _awake(guild)
        _park_resting(player, line)
        with _handoff(line):
            if not replace and _line_holding(line) and line.radio:
                # This song keeps playing. The new ones wait with the rest.
                note, changed = _add_waiting(guild, songs, False)
                if changed:
                    paint_soon(guild)
                return note
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
            full = False
            if replace:
                fitting, overflow = _fit(songs[1:])
                full = bool(overflow)
                if started is not None:
                    started.later = fitting
                    started.held = list(overflow)
                    started.whole = False
                    started.radio = False
                    started.radio_next = None
                    started.radio_plan = []
                    started.radio_ready = []
                    started.radio_from = None
                    from music.playback.prepare import _retire_fill
                    _retire_fill(guild)
            _store(guild)
            paint_soon(guild)
            return _playing(songs[0], full=full)


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


async def _begin(player, guild, song, *, failed=None, at=0, replaced=False):
    from music.playback.control import (
        _CHANGE, _attempt, _fresh_mark, _pause_wait, _show_paused, _snapshot,
        repeat_after_replacement,
    )

    missed = await _resolve_kept(guild, song)
    if missed:
        return missed
    had_line, kept_line = _stored(guild) is not None, _line(guild)
    kept, mark = _snapshot(kept_line), _fresh_mark(getattr(player, "current", None), song.track)
    if mark:
        player._replaced_mark = mark
        player._started_at = time.monotonic()
    try:
        # A dead session returns the new player. Volume and position belong to that one.
        live = await _play_track(player, song.track, start=at)
    except wavelink.WavelinkException:
        log.exception("Could not play the next song")
        if mark:
            player._replaced_mark = None
            player._started_at = None
        if not had_line:
            _drop(guild)
        return failed or _CHANGE
    if mark and live is not player:
        live._replaced_mark = mark
        live._started_at = player._started_at
    # A replace can clear the pointer before this line. The song that started is this one.
    if getattr(live, "current", None) is not song.track:
        for name in ("_current", "_track"):
            if hasattr(live, name):
                setattr(live, name, song.track)
    live._idle = False
    # play() on a real player already did this. A stand-in player needs the same mark.
    _show_paused(live, False)
    line = _line(guild)
    if line is not None and kept is not None and line is not kept_line:
        for name, value in kept.items():
            setattr(line, name, value)
    if line is not None:
        line.current, line.resting, line.paused, line.position = song, False, False, 0
        line.repeat = repeat_after_replacement(line.repeat, replaced=replaced)
        if line.volume != 100:
            await _attempt(lambda: live.set_volume(line.volume), "Could not keep the volume")
    _place(live, at)
    _remember_id(guild, song.requester_id)
    _store(guild)
    _pause_wait(guild, False)
    return None


async def _resolve_kept(guild, song):
    from music.playback.control import _lookup

    # A reload builds _Kept again, so the type name is what matches the song just loaded.
    if type(song.track).__name__ != "_Kept":
        return None
    uri = getattr(song.track, "uri", None)
    cached = await ready_track(uri)
    if cached is not None:
        song.track = cached
        return None
    if not uri:
        return "That song has no link to play."
    failed = "Something went wrong while starting the song."

    async def fetch():
        opened = await _lookup(guild, uri, song.platform, "music could not open a saved song", failed=failed)
        if isinstance(opened, str):
            return opened
        if not opened.tracks:
            return failed
        keep_track(uri, opened.tracks[0])
        return opened.tracks[0], song.platform

    opened = await load_link(uri, fetch)
    if isinstance(opened, str):
        return opened
    song.track = opened[0]
    return None


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
