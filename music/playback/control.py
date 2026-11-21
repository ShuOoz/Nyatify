"""Pause, skip, volume, and the moment a song ends."""

from __future__ import annotations

import importlib
import logging
import random
import time

import wavelink

from music import source
from music.lavalink import _join, joining, restore_player
from music.playback.prepare import (
    _arm_next, _clear_station, _drop_plan, _play_radio, _point_radio, _retire_fill,
)
from music.playback.arrange import _claim_current, _keep_earlier
from music.playback.remember import (
    QUEUE_SONGS, _Line, _Song, _clear_track, _fill_held, _guild_id, _hold,
    _movable, _note_station, _on_air, _order_drop, _place, _promote_waiting, _recall,
    _remember_id, _seen_channel, _store, _stored, _text, _unshuffle, _voice_player,
    _number, bot_connected, paint_soon,
)
from music.player.rule import line_modes, skip_step, track_ending
from music.player.radio import radio_seed, saved_key

log = logging.getLogger(__name__)

# A livestream that dies this fast was dropped, so the same address is tried once.
_SHORT_DROP_MS = 20_000
# The end of a song a skip just replaced can arrive while the new one is opening.
_OPENING_S = 1.5
_OPENING_MS = 1_500
_CHANGE = "Something went wrong while changing the song."
_QUIET = "Nothing is playing."
_KEY = "You need a Last.fm key to do this."
_STATION = "A station cannot start radio."
_STUCK = "That song cannot move."
_JOIN = "Join a voice channel first."
_VOLUME = "Something went wrong while changing the volume."
_CARRIED = (
    "later", "order", "earlier", "held", "repeat", "whole", "shuffle", "radio",
    "radio_next", "radio_plan", "radio_ready", "radio_miss", "radio_from", "volume",
)
_LISTS = {"later", "order", "earlier", "held", "radio_plan", "radio_ready", "radio_miss"}

def waiting_kept(count: int, *, cap: int) -> int:
    """How many waiting songs stay. Previous puts one back, and the cap still holds."""
    if count < cap:
        return count
    return cap

def repeat_after_replacement(repeating: bool, *, replaced: bool) -> bool:
    """Repeat turns off when another site's copy replaces the song. Skip keeps it."""
    if replaced:
        return False
    return repeating

def back_keeps(radio: bool, repeating: bool) -> str:
    """On Radio parks the song as the next radio song. Repeat parks it on the queue."""
    if radio and not repeating:
        return "radio"
    return "queue"

def station_ended(found: bool) -> str:
    """A resolved song keeps the station. When every name misses, the queue plays."""
    if found:
        return "play"
    return "release"

def previous_step(*, movable: bool, earlier: bool, playing: bool) -> str:
    """An earlier song plays when there is one. Otherwise a movable song starts again."""
    if earlier:
        return "back"
    if not playing:
        return "resume"
    if not movable:
        return "stay"
    return "restart"

def pause_position(reported: int, saved: int) -> int:
    """The place to keep. A paused player can report the start, so the saved position wins when it is ahead."""
    if saved > reported:
        return saved
    return reported

def _recalled(guild):
    if (guild_id := _guild_id(guild)) is not None:
        _recall(guild_id)
    return _stored(guild)

def _attach(guild, player):
    line, track = _stored(guild), getattr(player, "current", None)
    if line is not None and line.current is None and track is not None:
        _claim_current(line, track, guild)
    return line

def _holding(line):
    return line is not None and (line.current is not None or bool(line.later))

def _show_paused(player, paused):
    if hasattr(player, "_paused"):
        player._paused = paused

def _room(member, guild):
    channel = _seen_channel(member, guild)
    return channel if channel is not None else _JOIN

async def _play_at(guild, member, song, at):
    channel = _room(member, guild)
    if isinstance(channel, str): return channel
    return await _play_song(guild, channel, song, at=at)

async def _attempt(call, problem):
    try:
        await call()
    except wavelink.WavelinkException:
        log.exception(problem)
        return False
    return True

def _pause_wait(guild, paused):
    from music.playback.idle import _arm_pause_leave, _cancel_pause_leave

    (_arm_pause_leave if paused else _cancel_pause_leave)(guild)

async def _set_pause(player, paused, was):
    changed = False
    _show_paused(player, paused)
    try:
        try:
            await player.pause(paused)
            changed = True
        except wavelink.LavalinkException as exc:
            if exc.status == 404 and await restore_player(player):
                await player.pause(paused)
                changed = True
            else:
                log.exception("Could not pause the player")
    except wavelink.WavelinkException:
        log.exception("Could not pause the player")
    finally:
        if not changed:
            _show_paused(player, was)
    return changed

async def _try_song(player, guild, line, song, undo):
    playing, note = line.current, await _begin(player, guild, song)
    if note: undo()
    return (note, None) if note else (None, playing)

def _snapshot(line):
    if line is None: return None
    carried = {}
    for name in _CARRIED:
        if name in _LISTS:
            found = getattr(line, name, None)
            carried[name] = list(found) if isinstance(found, list) else []
        else:
            carried[name] = getattr(line, name)
    return carried

def _counted(value, *, high=None):
    number = _number(value)
    if number is None or number < 0 or (high is not None and number > high): return None
    return number

async def toggle_pause(guild: object | None, member: object | None = None) -> str | None:
    if guild is None: return "Use this command in a server."
    async with _hold(guild):
        player = _voice_player(guild)
        if player is None or player.current is None:
            if member is None: return _QUIET
            line = _recalled(guild)
            if line is None or line.current is None: return _QUIET
            return await _play_at(guild, member, line.current, line.position)
        line = _stored(guild)
        was_paused, position = bool(player.paused), _number(getattr(player, "position", None)) or 0
        if was_paused:
            await _keep_position(player, line)
            position = pause_position(position, 0 if line is None else line.position)
        _place(player, position)
        paused = not was_paused
        if not await _set_pause(player, paused, was_paused):
            return "I could not pause that."
        if line is not None:
            line.paused, line.position, line.resting = paused, 0 if was_paused else position, False
            _store(guild)
        _pause_wait(guild, paused)
        return None

async def previous_track(guild: object | None, member: object | None = None) -> str | None:
    async with _hold(guild):
        player = await _connect(guild, member, advance=False)
        if isinstance(player, str): return player
        line = _attach(guild, player)
        playing = player.current is not None
        step = previous_step(
            movable=playing and _movable(player.current),
            earlier=line is not None and bool(line.earlier), playing=playing,
        )
        if step == "restart": return await _seek_at(player, 0, "Could not restart the song")
        if step == "back": return await _step_back(player, guild, line)
        if step == "resume":
            if line is None or line.current is None: return _QUIET
            note = await _begin(player, guild, line.current, at=line.position if line.paused else 0)
            if note is None: paint_soon(guild)
            return note
        return "Nothing is before this."

async def _step_back(player, guild, line):
    song = line.earlier.pop()
    note, playing = await _try_song(player, guild, line, song, lambda: line.earlier.append(song))
    if note: return note
    if playing is not None and back_keeps(line.radio, line.repeat) == "radio":
        track = playing.track
        _point_radio(guild, line, radio_seed(_text(getattr(track, "title", None)), _text(getattr(track, "author", None))))
    elif playing is not None:
        line.later.insert(0, playing)
        keep = waiting_kept(len(line.later), cap=QUEUE_SONGS)
        for item in line.later[keep:]: _order_drop(line, item)
        line.held[:0], line.later[keep:] = line.later[keep:], []
    _store(guild)
    paint_soon(guild)
    return None

async def _commit_mode(guild, player, line, kind, *, place):
    updated = line_modes(line.repeat, line.shuffle, line.radio, kind)
    if updated is None: return None
    repeating, shuffling, radio = updated
    if line.shuffle and not shuffling: _unshuffle(line)
    elif kind == "shuffle" and shuffling:
        line.order = list(line.later)
        random.shuffle(line.later)
    line.repeat, line.shuffle, line.radio = repeating, shuffling, radio
    if not radio:
        _drop_plan(line)
        _clear_station(line)
        _retire_fill(guild)
    _store(guild)
    if place: await _keep_position(player, line)
    paint_soon(guild)
    return None

def _now(guild, *, recall):
    player = _voice_player(guild)
    track = None if player is None else getattr(player, "current", None)
    line = _attach(guild, player) if track is not None else (_recalled if recall else _stored)(guild)
    song = None if line is None else line.current
    return player, line, song, track if track is not None else (None if song is None else song.track)

async def _press_mode(guild, kind):
    async with _hold(guild):
        player, line, _song, track = _now(guild, recall=False)
        if line is None: return _QUIET
        stuck = track is not None and (_on_air(line, track) or not _movable(track))
        if kind == "repeat" and not line.repeat and stuck: return "That song cannot repeat."
        return await _commit_mode(guild, player, line, kind, place=True)

async def repeat_track(guild: object | None) -> str | None:
    """Play this song again when it ends. Skip still plays a song that is waiting."""
    return await _press_mode(guild, "repeat")

async def shuffle_queue(guild: object | None) -> str | None:
    """Mix the waiting songs. The one on now stays, and repeat turns off."""
    return await _press_mode(guild, "shuffle")

async def next_track(guild: object | None, member: object | None = None) -> str | None:
    async with _hold(guild):
        player, line, song, track = _now(guild, recall=True)
        waiting = line is not None and bool(line.later or line.held)
        station = (
            line is not None and line.radio and not waiting
            and song is not None and not song.videos
        )
        again = song is not None and not station and skip_step(
            repeating=line.repeat, videos=bool(song.videos), length=_length(track), waiting=waiting,
        ) == "repeat"
        if again: note = await _replay(guild, member, player, line, song)
        elif station: note = await _skip_radio(guild, member, player, line)
        else: note = await _advance_line(guild, member)
    paint_soon(guild)
    return note

async def _station_song(player, guild, line):
    if station_ended(await _play_radio(player, guild, line)) == "play": return True
    line.radio = False
    _drop_plan(line)
    _clear_station(line)
    _retire_fill(guild)
    return False

async def _skip_radio(guild, member, player, line):
    """Play the station song. Repeat stays on. A miss leaves the queue."""
    if player is None or getattr(player, "current", None) is None:
        opened = await _connect(guild, member, advance=True)
        if isinstance(opened, str) or opened is None: return opened
        player = opened
    if await _station_song(player, guild, line): return _saved(guild)
    return await _advance_line(guild, member)

async def _replay(guild, member, player, line: _Line, song):
    if player is None or getattr(player, "current", None) is None:
        opened = None if member is None else await _connect(guild, member, advance=False)
        if isinstance(opened, str) and opened not in (_JOIN, _QUIET): return opened
        if not isinstance(opened, str) and opened is not None: player = opened
        if player is None:
            line.position, line.paused = 0, False
            return _saved(guild)
        if getattr(player, "current", None) is None: return await _begin(player, guild, song, at=0)
    if player.current is song.track and _movable(player.current):
        note = await _seek_at(player, 0, "Could not restart the song")
        if note is None:
            if getattr(player, "paused", None) is True:
                if not await _set_pause(player, False, True): return "I could not pause that."
                line.paused = False
                _pause_wait(guild, False)
            line.position = 0
            return _saved(guild)
    return await _begin(player, guild, song, at=0)

def _saved(guild):
    _store(guild)
    return None

async def _connect(guild, member, *, advance):
    player = _voice_player(guild)
    if player is not None: return player
    opened = None
    if guild is not None and member is not None and _holding(_recalled(guild)):
        channel = _room(member, guild)
        opened = channel if isinstance(channel, str) else await _join(guild, channel)
    if opened is not None and not isinstance(opened, str): return opened
    if advance and guild is not None and opened in (None, _JOIN): return _advance_saved(guild)
    return opened if isinstance(opened, str) else _QUIET

async def _advance_line(guild: object | None, member: object | None = None) -> str | None:
    player = await _connect(guild, member, advance=True)
    if isinstance(player, str) or player is None: return player
    line = _attach(guild, player)
    if _radio_instead(line) and await _station_song(player, guild, line): return _saved(guild)
    song = _next_up(line)
    if song is not None:
        note, playing = await _try_song(player, guild, line, song, lambda: line.later.insert(0, song))
        if note: return note
        if playing is not None: _keep_earlier(guild, line, playing)
        _taken(line)
        if line is not None and line.radio: await _arm_next(guild, line)
        return _saved(guild)
    if player.current is None and (line is None or line.current is None): return _QUIET
    if not await _attempt(player.stop, "Could not end the song"): return _CHANGE
    _clear_track(player)
    _close_line(guild, line)
    player._idle = True
    _pause_wait(guild, False)
    return None

async def _ready(guild, member, *, playing, start=True):
    """(player, line) or a reply. No player means the saved song changes in place."""
    if start and member is not None and not bot_connected(guild) and joining(guild) is None and _seen_channel(member, guild) is None:
        line = _recalled(guild)
        if line is None or line.current is None: return _QUIET
        return None, line
    player = _voice_player(guild)
    if start and guild is not None and (player is None or getattr(player, "current", None) is None):
        line = _recalled(guild)
        if _promote_waiting(line): _store(guild)
        if line is None or line.current is None:
            if not (member is None and player is not None): return _QUIET
        elif member is not None:
            note = await _play_at(guild, member, line.current, line.position if line.paused else 0)
            if note: return note
            player = _voice_player(guild)
    if playing:
        if player is None: return _QUIET
        if getattr(player, "current", None) is None:
            await restore_player(player)
        line = _attach(guild, player)
        if line is None or line.current is None: return _QUIET
        if getattr(player, "current", None) is None and not bot_connected(guild): return _QUIET
    elif player is None or (line := _stored(guild)) is None: return _QUIET
    return player, line

async def toggle_radio(guild: object | None, member: object | None = None) -> str | None:
    """After this song, a similar one plays. This song and the waiting queue stay."""
    async with _hold(guild):
        found = await _ready(guild, member, playing=True)
        if isinstance(found, str): return found
        player, line = found
        # No player means the saved song changes in place. A station still cannot start radio.
        track = None if player is None else getattr(player, "current", None)
        if track is None and line.current is not None:
            track = line.current.track
        if not line.radio and track is not None and _on_air(line, track):
            return _STATION
        if not saved_key(): return _KEY
        note = await _commit_mode(guild, player, line, "radio", place=False)
        if line.radio: await _arm_next(guild, line)
        return note

async def _write_volume(guild, player, line, level):
    previous, line.volume = line.volume, level
    if await _attempt(lambda: player.set_volume(level), "Could not change the volume"): return _saved(guild)
    line.volume = previous
    return _VOLUME

async def change_volume(guild: object | None, delta: int) -> str | None:
    async with _hold(guild):
        found = await _ready(guild, None, playing=False, start=False)
        if isinstance(found, str): return found
        player, line = found
        level = min(100, max(0, line.volume + delta))
        return await _write_volume(guild, player, line, level)

def _miss(platform):
    return f"Nothing on {source.platform_label('', platform)} matched that."

def _length(track):
    return _number(getattr(track, "length", None)) or 0

async def _seek_at(player, position, problem):
    if not await _attempt(lambda: player.seek(position), problem): return _STUCK
    _place(player, position)
    player._jumped = True
    if getattr(player, "paused", None) is True and (line := _stored(getattr(player, "guild", None))) is not None:
        line.position = position
    return None

async def _lookup(guild, query, platform, problem, empty=None, failed=_CHANGE):
    empty = _miss(platform) if empty is None else empty
    try:
        found = await importlib.import_module("music.playback").find_songs(guild, query, platform)
    except Exception:
        log.exception(problem)
        found = None
    if found is None or isinstance(found, str): return failed if found is None else found
    return found if found.tracks else empty

def _marks(track):
    return tuple(
        f"{kind}:{value}" for kind, attr in (("id", "identifier"), ("uri", "uri"))
        if isinstance(value := getattr(track, attr, None), str) and value
    )

def _overlap(left, right):
    """True when both name one song. None when either has no id or uri."""
    one, two = set(_marks(left)), set(_marks(right))
    if not one or not two: return None
    return bool(one & two)

def _fresh_mark(previous, following):
    if previous is None or following is None or previous is following: return None
    if _overlap(previous, following): return None
    return _marks(previous) or ("played",)

def _ignore_end(player, guild, payload, reason):
    # A finish here belongs to the song a skip just replaced, or to a different track.
    ended = getattr(payload, "track", None)
    line = _stored(guild)
    current = None if line is None or line.current is None else line.current.track
    mark = getattr(player, "_replaced_mark", None)
    heard = _number(getattr(player, "_last_position", None)) or 0
    length = _length(current)
    started = getattr(player, "_started_at", None)
    recent = (
        isinstance(started, (int, float)) and not isinstance(started, bool)
        and time.monotonic() - started < _OPENING_S
    )
    same = ended is not None and current is not None and _overlap(ended, current) is not False
    opening = bool(mark) and same and length > _OPENING_MS and (
        heard < _OPENING_MS or recent and heard > length - _OPENING_MS
    )
    if reason == "finished" and opening:
        return True
    if mark and set(mark) & set(_marks(ended)):
        return True
    if mark and not opening:
        player._replaced_mark = None
    return ended is not None and current is not None and _overlap(ended, current) is False

def _restore(player, guild):
    if getattr(player, "current", None) is not None: return
    line = _stored(guild)
    track = None if line is None or line.current is None else line.current.track
    for name in ("_current", "_track"):
        if track is not None and hasattr(player, name):
            setattr(player, name, track)

async def advance_from_end(payload: object) -> None:
    """Play the next song after one finishes. A song this play just replaced does not count."""
    player = getattr(payload, "player", None)
    guild = None if player is None else getattr(player, "guild", None)
    if player is None or guild is None:
        return
    reason = getattr(payload, "reason", None)
    if reason not in ("finished", "loadFailed"):
        if reason in ("stopped", "cleanup", "replaced"):
            async with _hold(guild):
                _restore(player, guild)
        return
    if _ignore_end(player, guild, payload, reason): return _restore(player, guild)
    async with _hold(guild):
        if hasattr(player, "_track"):
            player._track = None
        await _follow_end(player, guild, str(reason))
    paint_soon(guild)

async def _follow_end(player, guild, reason):
    line = _stored(guild)
    song = None if line is None else line.current
    _note_station(song)
    short = (_number(getattr(player, "_last_position", None)) or 0) < _SHORT_DROP_MS
    choice = "advance" if song is None else track_ending(
        reason=reason, repeating=line.repeat, videos=bool(song.videos),
        length=_length(song.track), short=short, waiting=bool(line.later),
    )
    if choice == "repeat" and not await _begin(player, guild, song): return
    if choice == "videos" and await _backup_video(player, guild, song, short): return
    await _advance_line(guild)

async def _backup_video(player, guild, song, short):
    # After that one drop, play a station backup video.
    drops, count = (song.drops + 1 if short else 0), len(song.videos)
    if drops > count:
        log.warning("station ended before it could hold")
        return False
    start = 0 if short and song.drops == 0 else 1
    for index in ((song.video_at + step) % count for step in range(start, count)):
        opened = await _lookup(guild, song.videos[index], "youtube", "music station failed", "")
        if isinstance(opened, str):
            continue
        note = await _begin(player, guild, _Song(
            opened.tracks[0], song.requester_id, "youtube", videos=song.videos,
            video_at=index, drops=drops, station=song.station,
        ))
        if not note:
            log.info("station continued on address %s", index + 1)
            return True
    return False

async def _keep_position(player, line):
    track = None if player is None else getattr(player, "current", None)
    reported = 0 if player is None else (_number(getattr(player, "position", None)) or 0)
    saved = 0 if line is None else line.position
    if track is None or not _movable(track) or saved <= 0 or saved <= reported: return
    if await _attempt(lambda: player.seek(saved), "Could not keep the place in the song"):
        _place(player, saved)
        line.position = saved

def _close_line(guild, line):
    if line is not None:
        if line.current is not None:
            _keep_earlier(guild, line, line.current)
        line.current = None
        line.repeat = line.radio = line.whole = False
        _drop_plan(line)
        _store(guild)
    _retire_fill(guild)
    _remember_id(guild, None)

def _next_up(line):
    if line is not None and not line.later:
        _fill_held(line)
    return None if line is None or not line.later else line.later.pop(0)

def _taken(line):
    _fill_held(line)
    if not line.later:
        line.whole = False

def _advance_saved(guild):
    line = _recalled(guild)
    song = _next_up(line)
    if song is None:
        if not _holding(line): return _QUIET
        _close_line(guild, line)
        return None
    if line.current is not None:
        _keep_earlier(guild, line, line.current)
    line.current, line.resting, line.paused, line.position = song, True, False, 0
    _taken(line)
    _remember_id(guild, song.requester_id)
    return _saved(guild)

def _radio_instead(line):
    return line is not None and line.radio and not line.repeat and line.current is not None and not line.current.videos

from music.playback.start import _begin, _play_song
