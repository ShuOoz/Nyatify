"""Pause, skip, volume, a site change, and the moment a song ends."""

from __future__ import annotations

import importlib
import logging
import random

import wavelink

from music import source
from music.lavalink import _join, _play_track
from music.playback.arrange import _claim_current, _keep_earlier
from music.playback.remember import (
    QUEUE_SONGS, _Line, _Song, _clear_track, _drop, _fill_held, _guild_id, _hold, _line,
    _movable, _on_air, _order_drop, _place, _promote_waiting, _recall, _remember_id,
    _seen_channel, _store, _stored, _text, _unshuffle, _voice_player, _number,
    keep_track, paint_soon, ready_track,
)
from music.player.rule import (
    line_modes, pause_position, previous_step, repeat_after_replacement, track_ending, waiting_kept,
)
from music.player.radio import _heard_query, _words_in, another_track, saved_key

log = logging.getLogger(__name__)

# A livestream that ends this fast was dropped by the loader, not by the station.
_SHORT_DROP_MS = 20_000
_CHANGE = "Something went wrong while changing the song."
_QUIET = "Nothing is playing."
_KEY = "You need a Last.fm key to do this."
_STUCK = "That song cannot move."
_JOIN = "Join a voice channel first."
_VOLUME = "Something went wrong while changing the volume."
# play() can replace the line object. These fields are copied onto the new one.
_CARRIED = ("later", "order", "earlier", "held", "repeat", "whole", "shuffle", "radio", "volume")
_LISTS = {"later", "order", "earlier", "held"}

def _start_module():
    # Imported late so this module can load before music.playback.start.
    return importlib.import_module("music.playback.start")
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
    # The card reads this before Lavalink answers.
    if hasattr(player, "_paused"): player._paused = paused
def _room(member, guild):
    channel = _seen_channel(member, guild)
    return channel if channel is not None else _JOIN
async def _play_at(guild, member, song, at):
    channel = _room(member, guild)
    if isinstance(channel, str): return channel
    return await _start_module()._play_song(guild, channel, song, at=at)
async def _attempt(call, problem):
    try:
        await call()
    except wavelink.WavelinkException:
        log.exception(problem)
        return False
    return True
def _pause_wait(guild, paused):
    start = _start_module()
    (start._arm_pause_leave if paused else start._cancel_pause_leave)(guild)
async def _try_song(player, guild, line, song, undo):
    playing, note = line.current, await _begin(player, guild, song)
    if note:
        undo()
        return note, None
    return None, playing
def _snapshot(line):
    if line is None: return None
    return {name: list(getattr(line, name)) if name in _LISTS else getattr(line, name) for name in _CARRIED}
def _counted(value, *, high=None):
    # True and False are ints, and they are not a volume or a time.
    number = _number(value)
    if number is None or number < 0 or (high is not None and number > high):
        return None
    return number
async def toggle_pause(guild: object | None, member: object | None = None) -> str | None:
    if guild is None: return "Use this command in a server."
    async with _hold(guild):
        player = _voice_player(guild)
        if player is None or player.current is None:
            # A failed resume can leave the bot in the channel with no track.
            return _QUIET if member is None else await _resume_away(guild, member)
        # Read this before the flag. Once paused, the player can report the start.
        line = _stored(guild)
        was_paused, position = bool(player.paused), _number(getattr(player, "position", None)) or 0
        if was_paused:
            await _keep_position(player, line)
            position = pause_position(position, 0 if line is None else line.position)
        _place(player, position)
        paused = not was_paused
        changed = False
        _show_paused(player, paused)
        try:
            changed = await _attempt(lambda: player.pause(paused), "Could not pause the player")
        finally:
            if not changed:
                _show_paused(player, was_paused)
        if not changed:
            return "I could not pause that."
        if line is not None:
            line.paused, line.position, line.resting = paused, 0 if was_paused else position, False
            _store(guild)
        _pause_wait(guild, paused)
        return None
async def _resume_away(guild, member):
    # The bot left after a pause. Continue at the saved place.
    line = _recalled(guild)
    if line is None or line.current is None: return _QUIET
    return await _play_at(guild, member, line.current, line.position)
async def previous_track(guild: object | None, member: object | None = None) -> str | None:
    async with _hold(guild):
        player = await _connect(guild, member, advance=False)
        if isinstance(player, str): return player
        line = _attach(guild, player)
        # An earlier song plays when there is one. The first song starts again.
        playing = player.current is not None
        step = previous_step(
            movable=playing and _movable(player.current),
            earlier=line is not None and bool(line.earlier), playing=playing,
        )
        if step == "restart": return await _seek_at(player, 0, "Could not restart the song")
        if step == "back": return await _step_back(player, guild, line)
        if step == "resume": return await _resume_current(player, guild, line)
        return "Nothing is before this."
async def _step_back(player, guild, line):
    # The song that was on goes to the front of the queue. The cap still holds.
    song = line.earlier.pop()
    note, playing = await _try_song(player, guild, line, song, lambda: line.earlier.append(song))
    if note: return note
    if playing is not None:
        line.later.insert(0, playing)
        keep = waiting_kept(len(line.later), cap=QUEUE_SONGS)
        bumped = line.later[keep:]
        for item in bumped:
            _order_drop(line, item)
        del line.later[keep:]
        # The cap still holds. The song that fell off plays when a slot opens.
        if bumped:
            line.held = [*bumped, *line.held]
    _store(guild)
    paint_soon(guild)
    return None
async def _resume_current(player, guild, line):
    if line is None or line.current is None: return _QUIET
    note = await _begin(player, guild, line.current, at=line.position if line.paused else 0)
    if note is None: paint_soon(guild)
    return note
async def _commit_mode(guild, player, line, kind, *, place):
    # Radio ignores a shuffle press. Repeat redraws only when that press turns shuffle off.
    updated = line_modes(line.repeat, line.shuffle, line.radio, kind)
    if updated is None: return None
    repeating, shuffling, radio = updated
    unshuffled = line.shuffle and not shuffling
    if unshuffled:
        _unshuffle(line)
    elif kind == "shuffle" and shuffling:
        line.order = list(line.later)
        random.shuffle(line.later)
    line.repeat, line.shuffle, line.radio = repeating, shuffling, radio
    _store(guild)
    if place: await _keep_position(player, line)
    if kind != "repeat" or unshuffled: paint_soon(guild)
    return None
async def _press_mode(guild, kind):
    async with _hold(guild):
        player = _voice_player(guild)
        # Between songs, the one saved on the line is still that song.
        live = player is not None and getattr(player, "current", None) is not None
        line = _attach(guild, player) if live else _stored(guild)
        track = player.current if live else (None if line is None or line.current is None else line.current.track)
        if line is None: return _QUIET
        # A stream cannot start repeating. One that is already on can still be turned off.
        stuck = track is not None and (_on_air(line, track) or not _movable(track))
        if kind == "repeat" and not line.repeat and stuck: return "That song cannot repeat."
        return await _commit_mode(guild, player, line, kind, place=True)
async def repeat_track(guild: object | None) -> str | None:
    """Play this song again when it ends. The waiting queue is left alone."""
    return await _press_mode(guild, "repeat")
async def shuffle_queue(guild: object | None) -> str | None:
    """Mix the waiting songs. The one on now stays, and repeat turns off."""
    return await _press_mode(guild, "shuffle")
async def next_track(guild: object | None, member: object | None = None) -> str | None:
    async with _hold(guild):
        note = await _advance_line(guild, member)
    paint_soon(guild)
    return note
def _saved(guild):
    _store(guild)
    return None
def _close_line(guild, line):
    if line is None: return
    if line.current is not None:
        _keep_earlier(guild, line, line.current)
        line.current = None
    line.repeat = line.radio = line.whole = False
    _store(guild)
async def _connect(guild, member, *, advance):
    player = _voice_player(guild)
    if player is not None:
        return player
    # None when there is no one to join or no song. A string is why the join failed.
    opened = None
    if guild is not None and member is not None and _holding(_recalled(guild)):
        channel = _room(member, guild)
        opened = channel if isinstance(channel, str) else await _join(guild, channel)
    if opened is not None and not isinstance(opened, str):
        return opened
    # Not being in a channel still moves the saved line. A failed join does not.
    if advance and guild is not None and opened in (None, _JOIN):
        return _advance_saved(guild)
    return opened if isinstance(opened, str) else _QUIET
def _next_up(line):
    """The next waiting song, once parked songs have filled an empty queue."""
    if line is not None and not line.later: _fill_held(line)
    return None if line is None or not line.later else line.later.pop(0)
def _taken(line):
    _fill_held(line)
    if not line.later: line.whole = False
def _advance_saved(guild):
    line = _recalled(guild)
    song = _next_up(line)
    if song is None:
        if not _holding(line): return _QUIET
        _close_line(guild, line)
        _remember_id(guild, None)
        return None
    if line.current is not None:
        _keep_earlier(guild, line, line.current)
    line.current, line.resting, line.paused, line.position = song, True, False, 0
    _taken(line)
    _remember_id(guild, song.requester_id)
    return _saved(guild)
def _radio_instead(line):
    # Radio alone keeps the queue. Repeat reaches the queue.
    return line is not None and line.radio and not line.repeat and line.current is not None and not line.current.videos
async def _advance_line(guild: object | None, member: object | None = None) -> str | None:
    player = await _connect(guild, member, advance=True)
    if isinstance(player, str) or player is None: return player
    line = _attach(guild, player)
    # Repeat wins, the same as when the song ends, so skip can reach the queue.
    # Radio leaves the waiting queue. A miss replays this song instead of taking the next one.
    if _radio_instead(line):
        if not await _play_radio(player, guild, line) and (note := await _begin(player, guild, line.current)):
            return note
        return _saved(guild)
    song = _next_up(line)
    if song is not None:
        note, playing = await _try_song(player, guild, line, song, lambda: line.later.insert(0, song))
        if note: return note
        if playing is not None: _keep_earlier(guild, line, playing)
        _taken(line)
        return _saved(guild)
    if player.current is None and (line is None or line.current is None): return _QUIET
    if not await _attempt(player.stop, "Could not end the song"): return _CHANGE
    _clear_track(player)
    _close_line(guild, line)
    # The end event can put the track back. Nothing is queued, so it is not playing.
    player._idle = True
    _remember_id(guild, None)
    _pause_wait(guild, False)
    return None
async def toggle_radio(guild: object | None, member: object | None = None) -> str | None:
    """After this song, a similar one plays. This song and the waiting queue stay."""
    async with _hold(guild):
        note = await _bring_up(guild, member)
        if note: return note
        player = _voice_player(guild)
        line = None if player is None or player.current is None else _attach(guild, player)
        if line is None or line.current is None: return _QUIET
        if not saved_key(): return _KEY
        return await _commit_mode(guild, player, line, "radio", place=False)
async def _play_radio(player, guild, line):
    song = line.current
    if song is None: return False
    match = await another_track(song)
    if match is None or await _begin(player, guild, _Song(match, song.requester_id, song.platform)):
        return False
    _keep_earlier(guild, line, song)
    return True
def _live(guild):
    player = _voice_player(guild)
    line = None if player is None else _stored(guild)
    return _QUIET if player is None or line is None else (player, line)
async def _write_volume(guild, player, line, level):
    previous, line.volume = line.volume, level
    if await _attempt(lambda: player.set_volume(level), "Could not change the volume"):
        return _saved(guild)
    line.volume = previous
    return _VOLUME
async def _volume(guild, level, *, bump):
    async with _hold(guild):
        live = _live(guild)
        if isinstance(live, str): return live
        player, line = live
        if bump:
            level = min(100, max(0, line.volume + level))
        return await _write_volume(guild, player, line, level)
async def change_volume(guild: object | None, delta: int) -> str | None:
    return await _volume(guild, delta, bump=True)
def _volume_text(level):
    return None if _counted(level, high=100) is not None else "Type a volume from 0 to 100."
def _time_text(position):
    return None if position is None or _counted(position) is not None else "Type a time like 1:30."
async def set_volume(guild: object | None, level: int) -> str | None:
    return _volume_text(level) or await _volume(guild, level, bump=False)
async def apply_playback(
    guild: object | None, level: int, platform: str | None = None,
    position: int | None = None, member: object | None = None,
) -> str | None:
    """Set the volume. Another site replaces this song. A time moves through it."""
    if (note := _volume_text(level) or _time_text(position)):
        return note
    async with _hold(guild):
        if (note := await _bring_up(guild, member)):
            return note
        live = _live(guild)
        if isinstance(live, str): return live
        player, line = live
        if line.volume != level and (note := await _write_volume(guild, player, line, level)):
            return note
        chosen = None if platform is None else source.normalize_platform(platform)
        if chosen is not None and (line.current is None or chosen != line.current.platform):
            # A time the copy cannot take leaves this song on, and repeat stays with it.
            if (note := await _switch_platform(guild, player, line, chosen, position)):
                return note
            player = _voice_player(guild) or player
            paint_soon(guild)
        if position is None:
            return None
        note = await _move_to(player, guild, position)
        if note is None:
            paint_soon(guild)
        return note
async def _switch_platform(guild, player, line, platform, position=None):
    song = line.current
    if song is None or getattr(player, "current", None) is None:
        return _QUIET
    title, author = _text(getattr(song.track, "title", None), "Unknown"), _text(getattr(song.track, "author", None))
    opened = await _lookup(guild, _heard_query(title, author), platform, "music could not switch platform")
    if isinstance(opened, str):
        return opened
    copy = _copy_of(opened.tracks, title, author)
    if copy is None:
        return f"Nothing on {source.platform_label('', platform)} matched that."
    # A time this copy cannot take, including its last moment, leaves this song on.
    if position is not None and (note := _spot_note(copy, position, exact=True)):
        return note
    return await _begin(player, guild, _Song(copy, song.requester_id, opened.platform), replaced=True)
def _length(track):
    return _number(getattr(track, "length", None)) or 0
def _spot_note(track, position, *, exact=False):
    if not _movable(track):
        return _STUCK
    # Past the end stays put. The length itself counts as past only when exact is set.
    length = _length(track)
    if position > length or (exact and position == length):
        return "That is past the end of the song."
    return None
async def _move_to(player, guild, position):
    track = getattr(player, "current", None)
    if track is None:
        return _QUIET
    if (note := _spot_note(track, position)):
        return note
    if position == _length(track):
        await _finish_here(player, guild)
        return None
    return await _seek_at(player, position, "Could not move through the song")
async def _finish_here(player, guild):
    """The typed time is the end, so the next song starts the same way a finish does."""
    track = getattr(player, "current", None)
    player._last_position = _length(track)
    if hasattr(player, "_track"):
        player._track = None
    await _follow_end(player, guild, "finished")
async def _seek_at(player, position, problem):
    if not await _attempt(lambda: player.seek(position), problem):
        return _STUCK
    _place(player, position)
    player._jumped = True
    if getattr(player, "paused", None) is True:
        line = _stored(getattr(player, "guild", None))
        if line is not None:
            line.position = position
    return None
async def _bring_up(guild, member):
    # None when the caller can continue. A string is the reply.
    if guild is None:
        return None
    player = _voice_player(guild)
    if player is not None and getattr(player, "current", None) is not None:
        return None
    line = _recalled(guild)
    # The player is quiet, so a parked queue song can take the empty spot.
    if _promote_waiting(line):
        _store(guild)
    if line is None or line.current is None:
        return None if member is None and player is not None else _QUIET
    if member is None:
        return None
    return await _play_at(guild, member, line.current, line.position if line.paused else 0)
def _copy_of(tracks, title, author):
    # The first hit can be a different edit. Prefer this title and artist.
    folded, artist = title.casefold(), author.casefold()
    def same(track):
        name, who = _text(getattr(track, "title", None)), _text(getattr(track, "author", None))
        title_ok = folded == name.casefold() or _words_in(title, name)
        artist_ok = not author or who.casefold() == artist or _words_in(author, who) or _words_in(author, name)
        return title_ok and artist_ok
    return next((track for track in tracks if same(track)), None)
async def _lookup(guild, query, platform, problem, empty=None, failed=_CHANGE):
    empty = f"Nothing on {source.platform_label('', platform)} matched that." if empty is None else empty
    try:
        found = await importlib.import_module("music.playback").find_songs(guild, query, platform)
    except Exception:
        log.exception(problem)
        found = None
    if found is None or isinstance(found, str):
        return failed if found is None else found
    return found if found.tracks else empty
async def advance_from_end(payload: object) -> None:
    """Play the next song after one finishes. A song this play just replaced does not count."""
    reason, player = getattr(payload, "reason", None), getattr(payload, "player", None)
    guild = None if player is None else getattr(player, "guild", None)
    if reason not in ("finished", "loadFailed") or player is None or guild is None:
        return
    if _replaced_just_now(player, payload):
        _keep_playing(player, guild)
        return
    async with _hold(guild):
        # Lavalink has already dropped the current track. Keep the line in step.
        if hasattr(player, "_track"):
            player._track = None
        await _follow_end(player, guild, str(reason))
    paint_soon(guild)
async def _follow_end(player, guild, reason):
    line = _stored(guild)
    song = None if line is None else line.current
    short = (_number(getattr(player, "_last_position", None)) or 0) < _SHORT_DROP_MS
    # Repeat of this song wins, so radio and the queue wait.
    choice = "advance" if song is None else track_ending(
        reason=reason, repeating=line.repeat, station=bool(song.videos),
        length=_length(song.track), short=short, waiting=bool(line.later),
    )
    if choice == "repeat" and not await _begin(player, guild, song):
        return
    if choice == "station" and await _continue_station(player, guild, song, short):
        return
    await _advance_line(guild)
async def _continue_station(player, guild, song, short):
    # The first short drop reloads the address that died. After that, only the backups are tried.
    drops, count = (song.drops + 1 if short else 0), len(song.videos)
    if drops > count:
        log.warning("radio station ended before it could hold")
        return False
    start = 0 if short and song.drops == 0 else 1
    for index in ((song.video_at + step) % count for step in range(start, count)):
        opened = await _lookup(guild, song.videos[index], "youtube", "music radio failed", "")
        if isinstance(opened, str):
            continue
        note = await _begin(player, guild, _Song(
            opened.tracks[0], song.requester_id, "youtube", videos=song.videos,
            video_at=index, drops=drops, station=song.station,
        ))
        if not note:
            log.info("radio continued on address %s", index + 1)
            return True
    return False
def _marks(track):
    # Identity an end event can carry. A new object still matches.
    return tuple(
        f"{kind}:{value}" for kind, attr in (("id", "identifier"), ("uri", "uri"))
        if isinstance(value := getattr(track, attr, None), str) and value
    )
def _fresh_mark(previous, following):
    mark = _marks(previous)
    return None if not mark or set(mark) & set(_marks(following)) else mark
def _replaced_just_now(player, payload):
    # True when this end is the song a new play took off. A later end still moves on.
    mark = getattr(player, "_replaced_mark", None)
    if mark and set(mark) & set(_marks(getattr(payload, "track", None))):
        return True
    if mark:
        player._replaced_mark = None
    return False
def _keep_playing(player, guild):
    # Lavalink clears the player on that end. The song that replaced it is still on.
    if getattr(player, "current", None) is not None:
        return
    line = _stored(guild)
    track = None if line is None or line.current is None else line.current.track
    if track is not None:
        for name in ("_current", "_track"):
            if hasattr(player, name):
                setattr(player, name, track)
async def _keep_position(player, line):
    # A paused player can report the start. The saved place is where it was.
    track = None if player is None else getattr(player, "current", None)
    reported = 0 if player is None else (_number(getattr(player, "position", None)) or 0)
    saved = 0 if line is None else line.position
    if track is None or not _movable(track) or saved <= 0 or saved <= reported:
        return
    if await _attempt(lambda: player.seek(saved), "Could not keep the place in the song"):
        _place(player, saved)
        line.position = saved
async def _begin(player, guild, song, *, failed=None, at=0, replaced=False):
    if missed := await _open_saved(guild, song):
        return missed
    had_line, kept_line = _stored(guild) is not None, _line(guild)
    kept, mark = _snapshot(kept_line), _fresh_mark(getattr(player, "current", None), song.track)
    if mark:
        player._replaced_mark = mark
    try:
        # A dead session returns the new player. Volume and position belong to that one.
        live = await _play_track(player, song.track, start=at)
    except wavelink.WavelinkException:
        log.exception("Could not play the next song")
        if mark:
            player._replaced_mark = None
        if not had_line:
            _drop(guild)
        return failed or _CHANGE
    if mark and live is not player:
        live._replaced_mark = mark
    if getattr(live, "current", None) is not song.track and hasattr(live, "_track"):
        live._track = song.track
    live._idle = False
    # A mid-song return was opened paused so the skip could finish. A refused unpause stays here.
    held = live.paused is True
    paused = held and not await _attempt(lambda: live.pause(False), "Could not resume the next song")
    if held:
        _show_paused(live, paused)
    line = _line(guild)
    if line is not None and kept is not None and line is not kept_line:
        for name, value in kept.items():
            setattr(line, name, value)
    if line is not None:
        line.current, line.resting, line.paused, line.position = song, False, paused, at if paused else 0
        line.repeat = repeat_after_replacement(line.repeat, replaced=replaced)
        if line.volume != 100:
            await _attempt(lambda: live.set_volume(line.volume), "Could not keep the volume")
    _place(live, at)
    _remember_id(guild, song.requester_id)
    _store(guild)
    _pause_wait(guild, paused)
    return None
async def _open_saved(guild, song):
    # A reload builds _Kept again, so the type name is what matches the song just loaded.
    if type(song.track).__name__ != "_Kept":
        return None
    cached = await ready_track(getattr(song.track, "uri", None))
    if cached is not None:
        song.track = cached
        return None
    if not song.track.uri:
        return "That song has no link to play."
    opened = await _lookup(
        guild, song.track.uri, song.platform, "music could not open a saved song",
        failed="Something went wrong while starting the song.",
    )
    if isinstance(opened, str):
        return opened
    song.track = opened.tracks[0]
    keep_track(song.track.uri, song.track)
    return None
