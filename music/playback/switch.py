"""Set the volume, take another site's copy, or move through the song."""

from __future__ import annotations

from music import source
from music.playback.control import (
    _QUIET, _STUCK, _begin, _counted, _follow_end, _hold, _length, _lookup, _miss,
    _ready, _seek_at, _write_volume, repeat_after_replacement,
)
from music.playback.remember import _Song, _movable, _store, _text, _voice_player, paint_soon
from music.player.radio import _heard_query, same_song

async def _replacement(guild, song, platform, position):
    title = _text(getattr(song.track, "title", None), "Unknown")
    author = _text(getattr(song.track, "author", None))
    opened = await _lookup(guild, _heard_query(title, author), platform, "music could not switch platform")
    if isinstance(opened, str): return opened
    # The first hit can be a different edit. Prefer this title and artist.
    copy = next((track for track in opened.tracks if same_song(
        title, author, _text(getattr(track, "title", None)), _text(getattr(track, "author", None)),
    )), None)
    if copy is None: return _miss(platform)
    # A time this copy cannot take, including its last moment, leaves this song on.
    if position is not None and (note := _spot_note(copy, position, exact=True)): return note
    return _Song(copy, song.requester_id, opened.platform)

async def _retarget(guild, line, platform, position):
    """Another site's copy replaces the saved song. Nothing starts playing."""
    found = await _replacement(guild, line.current, platform, position)
    if isinstance(found, str): return found
    line.current = found
    line.repeat = repeat_after_replacement(line.repeat, replaced=True)
    line.position = 0 if position is None else position
    _store(guild)
    return None

async def _switch_platform(guild, player, line, platform, position=None):
    song = line.current
    if song is None or getattr(player, "current", None) is None: return _QUIET
    found = await _replacement(guild, song, platform, position)
    if isinstance(found, str): return found
    return await _begin(player, guild, found, replaced=True)

async def _tune(guild, player, line, level, platform, position):
    if line.volume != level:
        if player is None:
            line.volume = level
            _store(guild)
        elif (note := await _write_volume(guild, player, line, level)): return note
    chosen = None if platform is None else source.normalize_platform(platform)
    if chosen is not None and (line.current is None or chosen != line.current.platform):
        if player is None: note = await _retarget(guild, line, chosen, position)
        else:
            note = await _switch_platform(guild, player, line, chosen, position)
            player = _voice_player(guild) or player
        if note: return note
        paint_soon(guild)
        # The saved copy already kept this time. The live one still has to seek or finish.
        if player is None: return None
    if position is None: return None
    if player is None:
        if (note := _spot_note(line.current.track, position)): return note
        line.position = position
        _store(guild)
    else:
        note = await _move_to(player, guild, position)
        if note: return note
    paint_soon(guild)
    return None

async def apply_playback(
    guild: object | None, level: int, platform: str | None = None,
    position: int | None = None, member: object | None = None) -> str | None:
    """Set the volume. Another site replaces this song. A time moves through it."""
    if _counted(level, high=100) is None: return "Type a volume from 0 to 100."
    if position is not None and _counted(position) is None: return "Type a time like 1:30."
    async with _hold(guild):
        found = await _ready(guild, member, playing=False)
        if isinstance(found, str): return found
        player, line = found
        return await _tune(guild, player, line, level, platform, position)

def _spot_note(track, position, *, exact=False):
    if not _movable(track): return _STUCK
    # Past the end stays put. The length itself counts as past only when exact is set.
    length = _length(track)
    if position > length or (exact and position == length): return "That is past the end of the song."
    return None

async def _move_to(player, guild, position):
    track = getattr(player, "current", None)
    if track is None: return _QUIET
    if (note := _spot_note(track, position)): return note
    if position == _length(track):
        # The typed time is the end, so the next song starts the same way a finish does.
        player._last_position = position
        if hasattr(player, "_track"): player._track = None
        await _follow_end(player, guild, "finished")
        return None
    return await _seek_at(player, position, "Could not move through the song")
