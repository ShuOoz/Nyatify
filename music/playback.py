"""Start, remember, and stop the songs for one server."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import wavelink

from music import source
from music.lavalink import _join, _play_track, _replaced, find_songs

log = logging.getLogger(__name__)

# Under this, Previous plays the song before this one. Past it, Previous restarts.
NEAR_START_MS = 3_000
JUMP_MS = 15_000
VOLUME_STEP = 10

# Who asked for the current song, so the player card can name them.
_requesters: dict[int, int] = {}


@dataclass
class _Song:
    track: object
    requester_id: int | None
    platform: str
    lives: tuple[str, ...] = ()
    live_at: int = 0
    drops: int = 0
    station: str = ""


@dataclass
class _Line:
    earlier: list[_Song] = field(default_factory=list)
    current: _Song | None = None
    later: list[_Song] = field(default_factory=list)
    volume: int = 100


_lines: dict[int, _Line] = {}
_gates: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def _hold(guild: object | None) -> AsyncIterator[None]:
    """One handoff at a time. A play during a song change waits, then lines up."""
    guild_id = _guild_id(guild)
    if guild_id is None:
        yield
        return
    lock = _gates.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _gates[guild_id] = lock
    async with lock:
        yield


def playing_note(line: str) -> bool:
    return line.startswith(("Playing ", "Added "))


async def load_line(member: object, guild: object | None, tracks: list[tuple[object, str]]) -> str:
    """Replace the line with these songs and start the first."""
    return await _start_songs(member, guild, tracks, replace=True)


async def play_now(member: object, guild: object | None, track: object, platform: str) -> str:
    """Start this song now. Songs already lined up stay after it."""
    return await _start_songs(member, guild, [(track, platform)], replace=False)


async def queue_tracks(
    member: object,
    guild: object | None,
    tracks: list,
    platform: str = "youtube",
    lives: tuple[str, ...] = (),
    live_at: int = 0,
    station: str = "",
) -> str:
    """Join the member's voice channel when this is what starts playback."""
    if guild is None:
        return "Use this command in a server."
    platform = source.normalize_platform(platform)
    label = source.platform_label("", platform)
    if not tracks:
        return f"Nothing on {label} matched that."
    channel = _room(member, guild)
    if isinstance(channel, str):
        return channel
    requester = _whole(getattr(member, "id", None))
    songs = [_Song(track, requester, platform) for track in tracks]
    if lives:
        songs[0].lives = lives
        songs[0].live_at = live_at
        songs[0].station = station
    async with _hold(guild):
        player = _voice_player(guild)
        line = _stored(guild)
        # Lavalink clears the current track before the next song starts. The line
        # still owns that song, so a play in the gap lines up instead of cutting in.
        if _still_on(player, line):
            kept = _line(guild)
            if kept is None:
                return "Use this command in a server."
            kept.later.extend(songs)
            if len(songs) == 1:
                return f"Added {_title(songs[0].track)}."
            return f"Added {len(songs)} songs."
        if player is None:
            release(guild)
        player = await _join(guild, channel)
        if isinstance(player, str):
            return player
        first, rest = songs[0], songs[1:]
        try:
            await _play_track(player, first.track)
        except wavelink.WavelinkException:
            log.exception("Could not play a %s track", label)
            return f"{label} did not start that song."
        line = _line(guild)
        if line is not None:
            line.current = first
            line.later.extend(rest)
        _remember(guild, member)
        return f"Playing {_title(first.track)}."


def _still_on(player: wavelink.Player | None, line: _Line | None) -> bool:
    """True while this player is on a song, including the gap before the next one."""
    if player is None or _replaced(player):
        return False
    if player.current is not None:
        return True
    return line is not None and line.current is not None


def _room(member: object, guild: object | None) -> object | str:
    """The voice channel to use. Someone outside an open room stays outside."""
    if guild is None:
        return "Use this command in a server."
    voice = getattr(member, "voice", None)
    channel = getattr(voice, "channel", None)
    if channel is None:
        return "Join a voice channel first."
    player = _voice_player(guild)
    here = getattr(player, "channel", None) if player is not None else None
    if here is not None and not member_in_voice(member, guild) and here != channel:
        return "Join the voice channel first."
    return channel


async def _start_songs(
    member: object,
    guild: object | None,
    tracks: list[tuple[object, str]],
    *,
    replace: bool,
) -> str:
    if not tracks:
        return "Nothing on YouTube matched that."
    channel = _room(member, guild)
    if isinstance(channel, str):
        return channel
    player = await _join(guild, channel)
    if isinstance(player, str):
        return player
    requester = _whole(getattr(member, "id", None))
    songs = [
        _Song(track, requester, source.normalize_platform(platform))
        for track, platform in tracks
    ]
    old = None if _stored(guild) is None else _stored(guild).current
    note = await _begin(player, guild, songs[0])
    if note:
        return note
    line = _stored(guild)
    if line is not None and old is not None and old is not line.current:
        _keep_earlier(line, old)
    if line is not None and replace:
        line.later = songs[1:]
    return f"Playing {_title(songs[0].track)}."


async def stop_track(guild: object | None) -> str:
    if guild is None:
        return "Use this command in a server."
    _drop(guild)
    _forget(guild)
    player = getattr(guild, "voice_client", None)
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


def member_in_voice(member: object, guild: object | None) -> bool:
    """True when this member is in the voice channel the bot is using."""
    player = getattr(guild, "voice_client", None)
    channel = getattr(player, "channel", None)
    channel_id = _whole(getattr(channel, "id", None))
    voice = getattr(member, "voice", None)
    here = getattr(voice, "channel", None)
    return channel_id is not None and channel_id == _whole(getattr(here, "id", None))


@dataclass(frozen=True)
class PlayState:
    title: str
    author: str
    uri: str | None
    artwork: str | None
    position: int
    length: int
    paused: bool
    requester_id: int | None
    channel_id: int | None
    volume: int
    upcoming: str | None
    has_track: bool
    stream: bool


def now_playing(guild: object | None) -> PlayState | None:
    if guild is None or getattr(guild, "voice_client", None) is None:
        return None
    player = _voice_player(guild)
    line = _stored(guild)
    volume = 100 if line is None else line.volume
    upcoming = None if line is None or not line.later else _song_name(line.later[0])
    channel = None if player is None else getattr(player, "channel", None)
    channel_id = _whole(getattr(channel, "id", None))
    guild_id = _guild_id(guild)
    requester = None if guild_id is None else _requesters.get(guild_id)
    track = None if player is None else player.current
    if track is None:
        return PlayState(
            title="",
            author="",
            uri=None,
            artwork=None,
            position=0,
            length=0,
            paused=False,
            requester_id=None,
            channel_id=channel_id,
            volume=volume,
            upcoming=upcoming,
            has_track=False,
            stream=False,
        )
    length = _whole(getattr(track, "length", None)) or 0
    position = _whole(getattr(player, "position", None)) or 0
    if length > 0:
        position = min(max(0, position), length)
    return PlayState(
        title=_text(getattr(track, "title", None), "Unknown"),
        author=_text(getattr(track, "author", None)),
        uri=_http(getattr(track, "uri", None)),
        artwork=_http(getattr(track, "artwork", None)),
        position=position,
        length=length,
        paused=player.paused is True,
        requester_id=requester,
        channel_id=channel_id,
        volume=volume,
        upcoming=upcoming,
        has_track=True,
        stream=_on_air(line, track),
    )


@dataclass(frozen=True)
class Queued:
    title: str
    platform: str
    url: str | None
    playing: bool
    stream: bool


def queued(guild: object | None) -> list[Queued] | None:
    """The current song, then the rest. None when the bot is not in voice."""
    state = now_playing(guild)
    if state is None:
        return None
    line = _stored(guild)
    rows: list[Queued] = []
    if line is not None and line.current is not None:
        rows.append(_queued_row(line.current, True))
    elif state.has_track:
        rows.append(Queued(_label(state.title, state.author), "youtube", state.uri, True, state.stream))
    if line is not None:
        rows.extend(_queued_row(song, False) for song in line.later)
    return rows


def _queued_row(song: _Song, playing: bool) -> Queued:
    return Queued(
        _song_name(song),
        song.platform,
        _http(getattr(song.track, "uri", None)),
        playing,
        _stream(song.track) or bool(song.lives),
    )


async def drop_queued(guild: object | None, index: int) -> str | None:
    rows = queued(guild)
    if rows is None:
        return "Nothing is playing."
    if index < 0 or index >= len(rows):
        return "That song is gone."
    if rows[index].playing:
        return await next_track(guild)
    line = _stored(guild)
    if line is None:
        return "That song is gone."
    later_index = index - (1 if rows[0].playing else 0)
    if later_index < 0 or later_index >= len(line.later):
        return "That song is gone."
    line.later.pop(later_index)
    return None


def lineup(guild: object | None) -> str:
    """The whole line, for /music playlist."""
    state = now_playing(guild)
    if state is None:
        return "Nothing is playing."
    lines: list[str] = []
    if state.has_track:
        lines.append(f"Now playing: {_label(state.title, state.author)}")
    line = _stored(guild)
    if line is None or not line.later:
        lines.append("Nothing is after this song.")
    else:
        lines.extend(f"{index}. {_song_name(song)}" for index, song in enumerate(line.later, start=1))
    return "\n".join(lines)


def next_line(name: str | None) -> str:
    if name:
        return f"Up next: {name}"
    return "Nothing is after this song."


async def toggle_pause(guild: object | None) -> str | None:
    if guild is None:
        return "Use this command in a server."
    player = _voice_player(guild)
    if player is None or player.current is None:
        return "Nothing is playing."
    try:
        await player.pause(not player.paused)
    except wavelink.WavelinkException:
        log.exception("Could not pause the player")
        return "I could not pause that."
    return None


async def previous_track(guild: object | None) -> str | None:
    player = _voice_player(guild)
    if player is None:
        return "Nothing is playing."
    line = _line(guild)
    position = _whole(getattr(player, "position", None)) or 0
    if player.current is not None and _movable(player.current) and position > NEAR_START_MS:
        return await _restart(player)
    if line is not None and line.earlier:
        song = line.earlier.pop()
        playing = line.current
        note = await _begin(player, guild, song)
        if note:
            line.earlier.append(song)
            return note
        if playing is not None:
            line.later.insert(0, playing)
        return None
    if player.current is None:
        return "Nothing is playing."
    if not _movable(player.current):
        return "Nothing is before this."
    return await _restart(player)


async def nudge(guild: object | None, delta: int) -> str | None:
    player = _voice_player(guild)
    if player is None or player.current is None:
        return "Nothing is playing."
    if not _movable(player.current):
        return "That song cannot move."
    length = _whole(getattr(player.current, "length", None)) or 0
    position = _whole(getattr(player, "position", None)) or 0
    target = min(max(0, position + delta), length)
    try:
        await player.seek(target)
    except wavelink.WavelinkException:
        log.exception("Could not move through the song")
        return "That song cannot move."
    _place(player, target)
    return None


async def next_track(guild: object | None) -> str | None:
    async with _hold(guild):
        return await _advance_line(guild)


async def _advance_line(guild: object | None) -> str | None:
    player = _voice_player(guild)
    if player is None:
        return "Nothing is playing."
    line = _line(guild)
    if line is not None and line.later:
        song = line.later.pop(0)
        playing = line.current
        note = await _begin(player, guild, song)
        if note:
            line.later.insert(0, song)
            return note
        if playing is not None:
            _keep_earlier(line, playing)
        return None
    if player.current is None and (line is None or line.current is None):
        return "Nothing is playing."
    if line is not None and line.current is not None:
        _keep_earlier(line, line.current)
        line.current = None
    _remember_id(guild, None)
    try:
        await player.stop()
    except wavelink.WavelinkException:
        log.exception("Could not end the song")
        return "Something went wrong while changing the song."
    _clear_track(player)
    return None


async def change_volume(guild: object | None, delta: int) -> str | None:
    player = _voice_player(guild)
    if player is None:
        return "Nothing is playing."
    line = _line(guild)
    if line is None:
        return "Nothing is playing."
    line.volume = min(100, max(0, line.volume + delta))
    try:
        await player.set_volume(line.volume)
    except wavelink.WavelinkException:
        log.exception("Could not change the volume")
        return "Something went wrong while changing the volume."
    return None


async def suggest_next(guild: object | None, member: object) -> str | None:
    """Search this song's platform and put a different match on next."""
    player = _voice_player(guild)
    line = None if guild is None else _stored(guild)
    if player is None or player.current is None or line is None or line.current is None:
        return "Nothing is playing."
    song = line.current
    title = _text(getattr(song.track, "title", None), "Unknown")
    author = _text(getattr(song.track, "author", None))
    query = title if not author or author.casefold() in title.casefold() else f"{title} {author}"
    try:
        found = await source.search_track(query, song.platform)
    except wavelink.WavelinkException:
        log.exception("Similar song search failed")
        return "Nothing similar came up."
    tracks = _each(found)
    if song.platform == "soundcloud":
        tracks = source.drop_previews(tracks)
    match = next((track for track in tracks if not _same_track(track, song.track)), None)
    if match is None:
        return "Nothing similar came up."
    line.later.append(_Song(match, _whole(getattr(member, "id", None)), song.platform))
    return None


async def advance_from_end(payload: object) -> None:
    """Play the next song after one finishes. Replaced and stopped are already handled."""
    if getattr(payload, "reason", None) not in ("finished", "loadFailed"):
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
        played = _whole(getattr(player, "_last_position", None)) or 0
        short = played < _SHORT_STATION_MS
        waiting = line is not None and bool(line.later)
        if song is not None and song.lives and (short or not waiting):
            if await _continue_station(player, guild, song):
                return
        await _advance_line(guild)


# A livestream that ends this fast was dropped by the YouTube loader, not by the station.
_SHORT_STATION_MS = 20_000


async def _continue_station(player: wavelink.Player, guild: object, song: _Song) -> bool:
    """Keep a station on the air. The first short drop reloads it, then the other addresses play."""
    played = _whole(getattr(player, "_last_position", None)) or 0
    short = played < _SHORT_STATION_MS
    drops = song.drops + 1 if short else 0
    if drops > len(song.lives):
        log.warning("radio station ended before it could hold")
        return False
    # One reload of the address that just died. After that, only the backups are tried.
    indexes = [song.live_at] if short and song.drops == 0 else []
    count = len(song.lives)
    indexes.extend((song.live_at + step) % count for step in range(1, count))
    for index in indexes:
        link = song.lives[index]
        try:
            found = await find_songs(guild, link, "youtube")
        except Exception:
            log.exception("music radio failed")
            continue
        if isinstance(found, str) or not found.tracks:
            continue
        station = found.tracks[0]
        note = await _begin(
            player,
            guild,
            _Song(
                station,
                song.requester_id,
                "youtube",
                lives=song.lives,
                live_at=index,
                drops=drops,
                station=song.station,
            ),
        )
        if note:
            continue
        log.info("radio continued on address %s", index + 1)
        return True
    return False


async def _restart(player: wavelink.Player) -> str | None:
    if player.current is None or not _movable(player.current):
        return "That song cannot move."
    try:
        await player.seek(0)
    except wavelink.WavelinkException:
        log.exception("Could not restart the song")
        return "That song cannot move."
    _place(player, 0)
    return None


async def _begin(player: wavelink.Player, guild: object, song: _Song) -> str | None:
    line = _line(guild)
    try:
        await _play_track(player, song.track)
    except wavelink.WavelinkException:
        log.exception("Could not play the next song")
        return "Something went wrong while changing the song."
    if getattr(player, "current", None) is not song.track and hasattr(player, "_track"):
        player._track = song.track
    if player.paused is True:
        try:
            await player.pause(False)
        except wavelink.WavelinkException:
            log.exception("Could not resume the next song")
        if hasattr(player, "_paused"):
            player._paused = False
    if line is not None:
        line.current = song
    if line is not None and line.volume != 100:
        try:
            await player.set_volume(line.volume)
        except wavelink.WavelinkException:
            log.exception("Could not keep the volume")
    _place(player, 0)
    _remember_id(guild, song.requester_id)
    return None


def _keep_earlier(line: _Line, song: _Song) -> None:
    line.earlier.append(song)
    if len(line.earlier) > 50:
        del line.earlier[:-50]


def _clear_track(player: object) -> None:
    if hasattr(player, "_track"):
        player._track = None
    player._current = None


def _place(player: object, position: int) -> None:
    player._last_position = position
    player._last_update = time.monotonic_ns()
    if hasattr(player, "_position"):
        player._position = position


def _stream(track: object) -> bool:
    return getattr(track, "is_stream", None) is True


def _on_air(line: _Line | None, track: object) -> bool:
    """A livestream, or a long station, has no time bar."""
    if _stream(track):
        return True
    song = None if line is None else line.current
    return song is not None and bool(song.lives) and song.track is track


def _movable(track: object) -> bool:
    if getattr(track, "is_seekable", None) is False:
        return False
    return (_whole(getattr(track, "length", None)) or 0) > 0


def _same_track(left: object, right: object) -> bool:
    one = getattr(left, "identifier", None)
    other = getattr(right, "identifier", None)
    if isinstance(one, str) and isinstance(other, str) and one and other:
        return one == other
    return _text(getattr(left, "title", None)).casefold() == _text(getattr(right, "title", None)).casefold()


def _each(results: object) -> list:
    tracks = getattr(results, "tracks", None)
    if isinstance(tracks, list):
        return tracks
    if isinstance(results, list):
        return results
    return []


def _song_name(song: _Song) -> str:
    return _label(
        _text(getattr(song.track, "title", None), "Unknown"),
        _text(getattr(song.track, "author", None)),
    )


def _label(title: str, author: str) -> str:
    if not author or author.casefold() in title.casefold():
        return title or "Unknown"
    return f"{title} - {author}"


def _voice_player(guild: object | None) -> wavelink.Player | None:
    player = getattr(guild, "voice_client", None)
    if isinstance(player, wavelink.Player):
        return player
    return None


def _line(guild: object) -> _Line | None:
    guild_id = _guild_id(guild)
    if guild_id is None:
        return None
    line = _lines.get(guild_id)
    if line is None:
        line = _Line()
        _lines[guild_id] = line
    return line


def _stored(guild: object | None) -> _Line | None:
    guild_id = _guild_id(guild)
    if guild_id is None:
        return None
    return _lines.get(guild_id)


def release(guild: object | None) -> None:
    """Forget the line after the bot has left the voice channel."""
    if guild is None:
        return
    _drop(guild)
    _forget(guild)


def _drop(guild: object) -> None:
    guild_id = _guild_id(guild)
    if guild_id is not None:
        _lines.pop(guild_id, None)


def _guild_id(guild: object | None) -> int | None:
    return _whole(getattr(guild, "id", None))


def _remember(guild: object, member: object) -> None:
    _remember_id(guild, _whole(getattr(member, "id", None)))


def _remember_id(guild: object, user_id: int | None) -> None:
    guild_id = _guild_id(guild)
    if guild_id is None:
        return
    if user_id is None:
        _requesters.pop(guild_id, None)
        return
    _requesters[guild_id] = user_id


def _forget(guild: object) -> None:
    _remember_id(guild, None)


def _title(track: object) -> str:
    title = getattr(track, "title", None)
    if isinstance(title, str):
        cleaned = " ".join(title.split())
        if cleaned:
            return cleaned
    return "that song"


def _text(value: object, fallback: str = "") -> str:
    if isinstance(value, str):
        cleaned = " ".join(value.split())
        if cleaned:
            return cleaned
    return fallback


def _http(value: object) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def _whole(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
