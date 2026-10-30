"""Pause, skip, volume, a site change, and the moment a song ends."""

from __future__ import annotations

import importlib
import logging
import random

import wavelink

from music import source
from music.player.radio import radio_seed, saved_key, similar_songs
from music.lavalink import _join, _play_track
from music.playback.arrange import _claim_current, _keep_earlier
from music.playback.remember import (
    NEAR_START_MS,
    QUEUE_SONGS,
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
    _order_drop,
    _promote_waiting,
    _guild_id,
    _recall,
    _seen_channel,
    _store,
    _stored,
    _unshuffle,
    _text,
    _voice_player,
    _whole,
    paint_soon,
)

log = logging.getLogger(__name__)

# A livestream that ends this fast was dropped by the YouTube loader, not by the station.
_SHORT_DROP_MS = 20_000
# One similar name is usually enough. Two more cover a search that only finds this song.
_SIMILAR_TRIES = 3
_CHANGE = "Something went wrong while changing the song."
_QUIET = "Nothing is playing."
_KEY = "You need a Last.fm key to do this."
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


async def toggle_pause(guild: object | None, member: object | None = None) -> str | None:
    if guild is None:
        return "Use this command in a server."
    async with _hold(guild):
        player = _voice_player(guild)
        if player is None or player.current is None:
            # A failed resume can leave the bot in the channel with no track.
            if member is not None and _paused_song(guild) is not None:
                return await _resume_out(guild, member)
            return _QUIET
        was_paused = bool(player.paused)
        # Read this before the flag. Once paused, the player reports its last
        # update and drops the time since then, which can be the start.
        spot = _whole(getattr(player, "position", None)) or 0
        if was_paused:
            await _keep_spot(player, _stored(guild))
            held = _stored(guild)
            if held is not None and held.spot > spot:
                spot = held.spot
        _place(player, spot)
        paused = not was_paused
        previous = was_paused
        # The card reads this before Lavalink answers. A failed request puts it back.
        if hasattr(player, "_paused"):
            player._paused = paused
        try:
            changed = await _attempt(lambda: player.pause(paused), "Could not pause the player")
        except Exception:
            if hasattr(player, "_paused"):
                player._paused = previous
            raise
        if not changed:
            if hasattr(player, "_paused"):
                player._paused = previous
            return "I could not pause that."
        line = _stored(guild)
        if line is not None:
            line.paused = paused
            line.spot = 0 if was_paused else spot
            line.resting = False
            _store(guild)
        _pause_wait(guild, paused)
        return None


async def _resume_out(guild: object, member: object) -> str | None:
    """The bot left after a pause. Join this person and continue the song."""
    channel = _seen_channel(member, guild)
    if channel is None:
        return "Join a voice channel first."
    line = _paused_song(guild)
    if line is None or line.current is None:
        return _QUIET
    start = importlib.import_module("music.playback.start")
    return await start._play_song(guild, channel, line.current, at=line.spot)


def _paused_song(guild: object) -> _Line | None:
    """The saved line when a song is still waiting and nothing is on the player."""
    guild_id = _guild_id(guild)
    if guild_id is not None:
        _recall(guild_id)
    line = _stored(guild)
    if line is None or line.current is None:
        return None
    return line


def _pause_wait(guild: object, paused: bool) -> None:
    """Pausing starts the quiet leave. Playing again drops that wait."""
    start = importlib.import_module("music.playback.start")
    if paused:
        start._arm_pause_leave(guild)
    else:
        start._cancel_pause_leave(guild)


async def previous_track(guild: object | None, member: object | None = None) -> str | None:
    async with _hold(guild):
        player = _voice_player(guild)
        if player is None:
            opened = await _open_for(guild, member)
            if isinstance(opened, str):
                return opened
            if opened is None:
                return _QUIET
            player = opened
        line = _attach(guild, player)
        if line is not None and line.paused:
            await _keep_spot(player, line)
        position = _whole(getattr(player, "position", None)) or 0
        if line is not None and line.paused and line.spot > position:
            position = line.spot
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
                if line.shuffle:
                    line.order.insert(0, playing)
                # A normal queue stays at its cap. A playlist keeps every song.
                if not line.whole and len(line.later) > QUEUE_SONGS:
                    dropped = line.later[QUEUE_SONGS:]
                    del line.later[QUEUE_SONGS:]
                    for item in dropped:
                        _order_drop(line, item)
            _store(guild)
            paint_soon(guild)
            return None
        if player.current is None:
            if line is not None and line.current is not None:
                note = await _begin(player, guild, line.current, at=line.spot if line.paused else 0)
                if note is None:
                    paint_soon(guild)
                return note
            return _QUIET
        if not _movable(player.current):
            return "Nothing is before this."
        return await _restart(player)


async def _restart(player: wavelink.Player) -> str | None:
    if player.current is None or not _movable(player.current):
        return _STUCK
    if not await _attempt(lambda: player.seek(0), "Could not restart the song"):
        return _STUCK
    _place(player, 0)
    _mark_paused(player, 0)
    return None


def _mode_line(guild: object | None, player: object | None) -> tuple[_Line | None, object | None]:
    """The line, and the song a mode can see.

    The player can be between songs. The one saved on the line is still that song.
    """
    if player is not None and getattr(player, "current", None) is not None:
        return _attach(guild, player), player.current
    line = _stored(guild)
    track = None if line is None or line.current is None else line.current.track
    return line, track


async def repeat_track(guild: object | None) -> str | None:
    """Play this song again when it ends. The waiting queue is left alone.

    Nothing has to be on the player. The mode still changes.
    """
    async with _hold(guild):
        player = _voice_player(guild)
        line, track = _mode_line(guild, player)
        if line is None:
            return _QUIET
        # A stream cannot start repeating. One that is already on can still be turned off.
        if (
            not line.repeat
            and track is not None
            and (_on_air(line, track) or not _movable(track))
        ):
            return "That song cannot repeat."
        line.repeat = not line.repeat
        if line.repeat and line.shuffle:
            _unshuffle(line)
            paint_soon(guild)
        _store(guild)
        await _keep_spot(player, line)
        return None


async def shuffle_queue(guild: object | None) -> str | None:
    """Mix the waiting songs. The one on now stays, and repeat turns off.

    Nothing has to be on the player. The mode still changes.
    """
    async with _hold(guild):
        player = _voice_player(guild)
        line, _track = _mode_line(guild, player)
        if line is None:
            return _QUIET
        if line.radio:
            return None
        if line.shuffle:
            _unshuffle(line)
        else:
            line.repeat = False
            line.order = list(line.later)
            random.shuffle(line.later)
            line.shuffle = True
        _store(guild)
        await _keep_spot(player, line)
        paint_soon(guild)
        return None


async def next_track(guild: object | None, member: object | None = None) -> str | None:
    async with _hold(guild):
        note = await _advance_line(guild, member)
    paint_soon(guild)
    return note


async def _open_for(guild: object, member: object | None) -> object | str | None:
    """Join this person's channel when a song is saved and the bot is out.

    None when there is no one to join or no song. A string is why the join failed.
    """
    if member is None:
        return None
    guild_id = _guild_id(guild)
    if guild_id is not None:
        _recall(guild_id)
    line = _stored(guild)
    if line is None or (line.current is None and not line.later):
        return None
    channel = _seen_channel(member, guild)
    if channel is None:
        return "Join a voice channel first."
    return await _join(guild, channel)


def _advance_saved(guild: object) -> str | None:
    """Move to the next saved song when nothing is connected."""
    guild_id = _guild_id(guild)
    if guild_id is not None:
        _recall(guild_id)
    line = _stored(guild)
    if line is None or (line.current is None and not line.later):
        return _QUIET
    if line.later:
        song = line.later.pop(0)
        if line.current is not None:
            _keep_earlier(guild, line, line.current)
        _order_drop(line, song)
        line.current = song
        line.resting = True
        line.paused = False
        line.spot = 0
        if not line.later:
            line.whole = False
        _remember_id(guild, song.requester_id)
        _store(guild)
        return None
    if line.current is not None:
        _keep_earlier(guild, line, line.current)
        line.current = None
    line.repeat = False
    line.radio = False
    line.whole = False
    _store(guild)
    _remember_id(guild, None)
    return None


async def _advance_line(guild: object | None, member: object | None = None) -> str | None:
    player = _voice_player(guild)
    if player is None and guild is not None:
        opened = await _open_for(guild, member)
        if isinstance(opened, str) and opened != "Join a voice channel first.":
            return opened
        if not isinstance(opened, str) and opened is not None:
            player = opened
        else:
            return _advance_saved(guild)
    if player is None:
        return _QUIET
    line = _attach(guild, player)
    if line is not None and line.radio and line.current is not None and not line.current.videos:
        # The waiting queue stays put. A miss replays this song instead of taking the next one.
        if not await _play_radio(player, guild, line):
            note = await _begin(player, guild, line.current)
            if note:
                return note
        _store(guild)
        return None
    if line is not None and line.later:
        song = line.later.pop(0)
        note, playing = await _try_song(
            player, guild, line, song, lambda: line.later.insert(0, song),
        )
        if note:
            return note
        _order_drop(line, song)
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
        line.radio = False
        line.whole = False
        _store(guild)
    _remember_id(guild, None)
    _pause_wait(guild, False)
    return None


async def change_volume(guild: object | None, delta: int) -> str | None:
    async with _hold(guild):
        ready = _live_line(guild)
        if isinstance(ready, str):
            return ready
        player, line = ready
        return await _write_volume(guild, player, line, min(100, max(0, line.volume + delta)))


async def set_volume(guild: object | None, level: int) -> str | None:
    """Set the loudness to a number from 0 to 100."""
    if isinstance(level, bool) or not isinstance(level, int) or level < 0 or level > 100:
        return "Type a volume from 0 to 100."
    async with _hold(guild):
        ready = _live_line(guild)
        if isinstance(ready, str):
            return ready
        player, line = ready
        return await _write_volume(guild, player, line, level)


async def tune_track(
    guild: object | None,
    level: int,
    platform: str | None = None,
    position: int | None = None,
    member: object | None = None,
) -> str | None:
    """Set the volume. Another site replaces this song. A time moves through it."""
    if isinstance(level, bool) or not isinstance(level, int) or level < 0 or level > 100:
        return "Type a volume from 0 to 100."
    if position is not None and (isinstance(position, bool) or not isinstance(position, int) or position < 0):
        return "Type a time like 1:30."
    async with _hold(guild):
        note = await _bring_up(guild, member)
        if note:
            return note
        ready = _live_line(guild)
        if isinstance(ready, str):
            return ready
        player, line = ready
        if line.volume != level:
            note = await _write_volume(guild, player, line, level)
            if note:
                return note
        chosen = None if platform is None else source.normalize_platform(platform)
        if chosen is not None and (line.current is None or chosen != line.current.platform):
            note = await _switch_platform(guild, player, line, chosen)
            if note:
                return note
            player = _voice_player(guild) or player
            paint_soon(guild)
        if position is None:
            return None
        note = await _move_to(player, position)
        if note is None:
            paint_soon(guild)
        return note


async def _switch_platform(guild: object, player: object, line: _Line, platform: str) -> str | None:
    """Play this title from another site. The waiting songs stay."""
    song = line.current
    if song is None or getattr(player, "current", None) is None:
        return _QUIET
    title = _text(getattr(song.track, "title", None), "Unknown")
    author = _text(getattr(song.track, "author", None))
    try:
        found = await _find(guild, _heard_query(title, author), platform)
    except Exception:
        log.exception("music could not switch platform")
        return _CHANGE
    if isinstance(found, str):
        return found
    if not found.tracks:
        return f"Nothing on {source.platform_label('', platform)} matched that."
    replacement = _Song(found.tracks[0], song.requester_id, found.platform)
    return await _begin(player, guild, replacement)


async def _move_to(player: object, position: int) -> str | None:
    track = getattr(player, "current", None)
    if track is None:
        return _QUIET
    if not _movable(track):
        return _STUCK
    length = _whole(getattr(track, "length", None)) or 0
    if position > length:
        return "That is past the end of the song."
    if not await _attempt(lambda: player.seek(position), "Could not move through the song"):
        return _STUCK
    _place(player, position)
    _mark_paused(player, position)
    return None


def _live_line(guild: object | None) -> str | tuple[object, _Line]:
    player = _voice_player(guild)
    line = None if player is None else _stored(guild)
    if player is None or line is None:
        return _QUIET
    return player, line


async def _write_volume(guild: object | None, player: object, line: _Line, level: int) -> str | None:
    previous = line.volume
    line.volume = level
    if await _attempt(lambda: player.set_volume(level), "Could not change the volume"):
        _store(guild)
        return None
    # The card already showed this level. Put the old one back.
    line.volume = previous
    return "Something went wrong while changing the volume."


async def _bring_up(guild: object | None, member: object | None) -> str | None:
    """Start the saved song when this person is in a channel and nothing is on.

    None when the caller can continue. A string is the reply.
    """
    if guild is None:
        return None
    player = _voice_player(guild)
    if player is not None and getattr(player, "current", None) is not None:
        return None
    guild_id = _guild_id(guild)
    if guild_id is not None:
        _recall(guild_id)
    line = _stored(guild)
    live = None if player is None else getattr(player, "current", None)
    if line is not None and line.current is None and live is not None:
        _claim_current(line, live, guild)
    if live is None and _promote_waiting(line):
        _store(guild)
    player = _voice_player(guild)
    if player is not None and getattr(player, "current", None) is not None:
        return None
    line = _stored(guild)
    if line is None or line.current is None:
        return None if member is None and player is not None else _QUIET
    if member is None:
        return None
    channel = _seen_channel(member, guild)
    if channel is None:
        return "Join a voice channel first."
    start = importlib.import_module("music.playback.start")
    return await start._play_song(guild, channel, line.current, at=line.spot if line.paused else 0)


async def toggle_radio(guild: object | None, member: object | None = None) -> str | None:
    """After this song, a similar one plays. This song and the waiting queue stay."""
    async with _hold(guild):
        note = await _bring_up(guild, member)
        if note:
            return note
        player = _voice_player(guild)
        if player is None or player.current is None:
            return _QUIET
        line = _attach(guild, player)
        if line is None or line.current is None:
            return _QUIET
        if not saved_key():
            return _KEY
        line.radio = not line.radio
        if line.radio and line.shuffle:
            _unshuffle(line)
        _store(guild)
        paint_soon(guild)
        return None


async def _play_radio(player, guild: object, line: _Line) -> bool:
    """Play a similar song on now. It is not added to the waiting queue."""
    song = line.current
    if song is None:
        return False
    match = await _other_track(song)
    if match is None:
        return False
    note = await _begin(player, guild, _Song(match, song.requester_id, song.platform))
    if note:
        return False
    _keep_earlier(guild, line, song)
    return True


def _heard_query(title: str, author: str) -> str:
    if not author or author.casefold() in title.casefold():
        return title
    return f"{title} {author}"


async def _search_other(song: _Song, query: str):
    try:
        found = await source.search_track(query, song.platform)
    except wavelink.WavelinkException:
        log.exception("Similar song search failed")
        return None
    tracks = _each(found)
    if song.platform == "soundcloud":
        tracks = source.drop_previews(tracks)
    return next((track for track in tracks if not _same_track(track, song.track)), None)


async def _from_lastfm(song: _Song):
    title = _text(getattr(song.track, "title", None), "Unknown")
    author = _text(getattr(song.track, "author", None))
    pair = radio_seed(title, author)
    if pair is None:
        return None
    artist, name = pair
    found = await similar_songs(artist, name)
    choices = [
        (other_artist, other_name)
        for other_artist, other_name in found
        if other_artist.casefold() != artist.casefold() or other_name.casefold() != name.casefold()
    ]
    if not choices:
        return None
    random.shuffle(choices)
    for other_artist, other_name in choices[:_SIMILAR_TRIES]:
        track = await _search_other(song, f"{other_name} {other_artist}")
        if track is not None:
            return track
    return None


async def _other_track(song: _Song):
    title = _text(getattr(song.track, "title", None), "Unknown")
    author = _text(getattr(song.track, "author", None))
    track = await _from_lastfm(song)
    if track is not None:
        return track
    return await _search_other(song, _heard_query(title, author))


async def advance_from_end(payload: object) -> None:
    """Play the next song after one finishes. A song this play just replaced does not count."""
    reason = getattr(payload, "reason", None)
    if reason not in ("finished", "loadFailed"):
        return
    player = getattr(payload, "player", None)
    guild = getattr(player, "guild", None)
    if player is None or guild is None:
        return
    if _replaced_just_now(player, payload):
        _keep_playing(player, guild)
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


def _track_mark(track: object) -> tuple[str, ...]:
    """Identity of a track an end event can carry. A new object still matches."""
    if track is None:
        return ()
    marks: list[str] = []
    ident = getattr(track, "identifier", None)
    if isinstance(ident, str) and ident:
        marks.append(f"id:{ident}")
    uri = getattr(track, "uri", None)
    if isinstance(uri, str) and uri:
        marks.append(f"uri:{uri}")
    return tuple(marks)


def _fresh_mark(previous: object, nxt: object) -> tuple[str, ...] | None:
    """The song play() is about to replace, when the next one is a different track."""
    mark = _track_mark(previous)
    if not mark or set(mark) & set(_track_mark(nxt)):
        return None
    return mark


def _replaced_just_now(player: object, payload: object) -> bool:
    """True when this end is the song a new play took off. A later end still moves on."""
    mark = getattr(player, "_replaced_mark", None)
    if not mark:
        return False
    ended = _track_mark(getattr(payload, "track", None))
    if ended and set(mark) & set(ended):
        return True
    player._replaced_mark = None
    return False


def _keep_playing(player: object, guild: object) -> None:
    """Lavalink clears the player on that end. The song that replaced it is still on."""
    if getattr(player, "current", None) is not None:
        return
    line = _stored(guild)
    song = None if line is None else line.current
    track = None if song is None else song.track
    if track is None:
        return
    if hasattr(player, "_current"):
        player._current = track
    if hasattr(player, "_track"):
        player._track = track


def _aside(line: _Line | None) -> tuple | None:
    """Waiting songs and the flags that belong with them."""
    if line is None:
        return None
    return (
        list(line.later),
        list(line.order),
        list(line.earlier),
        line.repeat,
        line.whole,
        line.shuffle,
        line.radio,
        line.volume,
    )


def _put_aside(line: _Line, aside: tuple) -> None:
    later, order, earlier, repeat, whole, shuffle, radio, volume = aside
    line.later = later
    line.order = order
    line.earlier = earlier
    line.repeat = repeat
    line.whole = whole
    line.shuffle = shuffle
    line.radio = radio
    line.volume = volume


async def _keep_spot(player: object | None, line: _Line | None) -> bool:
    """Seek to the place saved at the pause. The song stays paused.

    The player mark can fall back to the start. The saved place is where it was.
    """
    if player is None or line is None or line.spot <= 0:
        return False
    track = getattr(player, "current", None)
    if track is None or not _movable(track):
        return False
    reported = _whole(getattr(player, "position", None)) or 0
    if line.spot <= reported:
        return False
    saved = line.spot
    if not await _attempt(lambda: player.seek(saved), "Could not keep the place in the song"):
        return False
    _place(player, saved)
    line.spot = saved
    return True


def _mark_paused(player: object, position: int) -> None:
    """A seek while paused is the new place in the song."""
    if getattr(player, "paused", None) is not True:
        return
    line = _stored(getattr(player, "guild", None))
    if line is not None:
        line.spot = position


async def _begin(
    player: wavelink.Player,
    guild: object,
    song: _Song,
    *,
    failed: str | None = None,
    at: int = 0,
) -> str | None:
    missed = await _open_saved(guild, song)
    if missed:
        return missed
    had_line = _stored(guild) is not None
    line = _line(guild)
    # play() can leave voice and drop this line. The copy is what gets written back.
    kept_line = line
    kept = _aside(line)
    mark = _fresh_mark(getattr(player, "current", None), song.track)
    if mark:
        setattr(player, "_replaced_mark", mark)
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
        setattr(live, "_replaced_mark", mark)
    if getattr(live, "current", None) is not song.track and hasattr(live, "_track"):
        live._track = song.track
    if live.paused is True:
        await _attempt(lambda: live.pause(False), "Could not resume the next song")
        if hasattr(live, "_paused"):
            live._paused = False
    # The line in memory may be a new one. Put the waiting songs back before saving.
    line = _line(guild)
    if line is not None and kept is not None and line is not kept_line:
        _put_aside(line, kept)
    if line is not None:
        line.current = song
        line.resting = False
        line.paused = False
        line.spot = 0
        if line.volume != 100:
            await _attempt(lambda: live.set_volume(line.volume), "Could not keep the volume")
    _place(live, at)
    _remember_id(guild, song.requester_id)
    _store(guild)
    _pause_wait(guild, False)
    return None


def _from_disk(track: object) -> bool:
    """A song read back from disk, not a track Lavalink can play.

    Reloading music builds that class again. This module can still hold the
    previous class, so isinstance does not match the song just loaded.
    """
    return type(track).__name__ == "_Kept"


async def _open_saved(guild: object, song: _Song) -> str | None:
    """Turn a saved song back into a track Lavalink can play."""
    if not _from_disk(song.track):
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
