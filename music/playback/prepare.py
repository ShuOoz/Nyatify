"""Songs prepared while one is already playing: the next radio tracks, and a playlist opened in order."""

from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass
from types import SimpleNamespace

import discord

from announce import notice_text, quote_text
from music import source
from music.panel import playing_note
from music.playback.arrange import _after_drop, _keep_earlier, _revise
from music.playback.remember import (
    _CHANGED, _Kept, _Song, _end_saved, _guild_id, _heard, _hold, _http, _number, _pull,
    _radio_epoch, _radio_fills, _saved_generation, _saved_lock_for, _saved_task, _stored, _text,
)
from music.player.radio import (
    _search_named, another_track, choose_next, found_platform, same_title, station_heard,
)

log = logging.getLogger(__name__)

# Songs kept ready so a skip can start. The rest are prepared while one is already playing.
_AHEAD = 3

@dataclass
class _Ahead:
    """One station song. The name is known before the track is searched."""
    artist: str
    title: str
    song: _Song

def _epoch(guild) -> int:
    gid = _guild_id(guild)
    return 0 if gid is None else _radio_epoch.get(gid, 0)

def _plan(line) -> list:
    found = getattr(line, "radio_plan", None)
    if not isinstance(found, list):
        found = []
        line.radio_plan = found
    return found

def _radio_list(line) -> list:
    found = getattr(line, "radio_ready", None)
    if not isinstance(found, list):
        found = []
        line.radio_ready = found
    return found

def _drop_plan(line) -> None:
    line.radio_plan = []
    line.radio_ready = []
    line.radio_next = None

def _station(line):
    """The song this station is about. A suggestion does not take its place."""
    root = getattr(line, "radio_from", None)
    return root if root is not None else line.current

def _keep_station(line) -> None:
    if line.current is not None:
        line.radio_from = line.current

def _clear_station(line) -> None:
    line.radio_from = None

def _sync_radio_next(line) -> None:
    ready = _radio_list(line)
    if ready:
        line.radio_next = (ready[0].artist, ready[0].title)
        return
    plan = _plan(line)
    line.radio_next = plan[0] if plan else None

def _retire_fill(guild) -> int:
    """Stop the prepare that is running. The next one uses the new token."""
    gid = _guild_id(guild)
    if gid is None: return 0
    token = _radio_epoch.get(gid, 0) + 1
    _radio_epoch[gid] = token
    running = _radio_fills.pop(gid, None)
    if running is not None and not running[1].done(): running[1].cancel()
    return token

def _point_radio(guild, line, seed) -> None:
    """The next station song is this one. Anything prepared for the old song is dropped."""
    _retire_fill(guild)
    _drop_plan(line)
    if seed is None: return
    line.radio_next = seed
    _plan(line).append(seed)
    _schedule_fill(guild, line)

def _quiet_task(task: asyncio.Task) -> None:
    if task.cancelled(): return
    task.exception()

def _resolver_ready() -> bool:
    from music.lavalink import JAVA_NODE, _named_node
    return _named_node(JAVA_NODE) is not None

def _schedule_fill(guild, line) -> None:
    if line is None or not line.radio or line.current is None or not _resolver_ready(): return
    gid = _guild_id(guild)
    if gid is None: return
    token = _epoch(guild)
    running = _radio_fills.get(gid)
    if running is not None and running[0] == token and not running[1].done(): return
    if running is not None and not running[1].done(): running[1].cancel()
    task = asyncio.create_task(_fill_radio(guild, token), name="music-radio")
    task.add_done_callback(_quiet_task)
    _radio_fills[gid] = (token, task)

def _open_radio(guild, token):
    if _epoch(guild) != token: return None
    line = _stored(guild)
    if line is None or not line.radio or line.current is None: return None
    return line

def _played(guild, line):
    # The queue is still waiting. Only songs that already played are out of the station.
    extra = () if (guild_id := _guild_id(guild)) is None else _heard.get(guild_id, ())
    return [*line.earlier, *([line.current] if line.current is not None else []), *extra]

def _ahead_heard(guild, line) -> set:
    heard = set(station_heard(_played(guild, line)))
    for item in _radio_list(line):
        heard.add((item.artist.casefold(), item.title.casefold()))
    for artist, title in _plan(line):
        heard.add((artist.casefold(), title.casefold()))
    for artist, title in _miss_list(line):
        heard.add((artist.casefold(), title.casefold()))
    return heard

def _miss_list(line) -> list:
    found = getattr(line, "radio_miss", None)
    if not isinstance(found, list):
        found = []
        line.radio_miss = found
    return found

def _remember_miss(line, item) -> None:
    """This prepared song did not start. The station does not pick it again."""
    found = _miss_list(line)
    key = (item.artist, item.title)
    if key in found: return
    found.append(key)
    del found[:-50]

def _missed(line, name) -> bool:
    artist, title = name
    key = (artist.casefold(), title.casefold())
    return any((one.casefold(), two.casefold()) == key for one, two in _miss_list(line))

def _take_ready(line) -> _Ahead | None:
    ready = _radio_list(line)
    if not ready: return None
    item = ready.pop(0)
    _sync_radio_next(line)
    return item

def _take_planned(line):
    plan = _plan(line)
    if plan:
        name = plan.pop(0)
        _sync_radio_next(line)
        return name
    name = line.radio_next
    line.radio_next = None
    return name

def _is_preferred(track, preferred) -> bool:
    """This recording is the name that was about to play. The site ranked the artist."""
    _artist, title = preferred
    return same_title(title, _text(getattr(track, "title", None)))

async def _radio_step(guild, token):
    async with _hold(guild):
        line = _open_radio(guild, token)
        if line is None: return None
        ready, plan = _radio_list(line), _plan(line)
        if len(ready) >= _AHEAD: return None
        base = _station(line)
        if base is None: return None
        if plan:
            artist, title = plan[0]
            return ("resolve", artist, title, base.platform, base.requester_id, base.track)
        return ("choose", base, _ahead_heard(guild, line))

async def _keep_name(guild, token, name) -> bool:
    async with _hold(guild):
        line = _open_radio(guild, token)
        if line is None or name is None: return False
        plan, ready = _plan(line), _radio_list(line)
        if len(ready) + len(plan) >= _AHEAD: return bool(plan) and len(ready) < _AHEAD
        key = (name[0].casefold(), name[1].casefold())
        if any((artist.casefold(), title.casefold()) == key for artist, title in plan): return False
        if any((item.artist.casefold(), item.title.casefold()) == key for item in ready): return False
        plan.append(name)
        if not ready and line.radio_next is None: line.radio_next = name
        return True

async def _keep_track(guild, token, step, track) -> bool:
    _kind, artist, title, platform, requester, _current = step
    async with _hold(guild):
        line = _open_radio(guild, token)
        if line is None: return False
        plan = _plan(line)
        if not plan or (plan[0][0].casefold(), plan[0][1].casefold()) != (artist.casefold(), title.casefold()):
            return len(_radio_list(line)) < _AHEAD
        plan.pop(0)
        if track is not None and len(_radio_list(line)) < _AHEAD:
            _radio_list(line).append(_Ahead(
                artist, title, _Song(track, requester, found_platform(track, platform)),
            ))
        _sync_radio_next(line)
        return len(_radio_list(line)) < _AHEAD

async def _resolve_name(platform, title, artist, current):
    try:
        return await _search_named(SimpleNamespace(platform=platform, track=current), title, artist)
    except Exception:
        log.exception("radio could not prepare a song")
        return None

async def _fill_radio(guild, token) -> None:
    """Search the next songs while one is already playing. Stop at three."""
    try:
        for _step in range(_AHEAD * 2):
            step = await _radio_step(guild, token)
            if step is None: return
            if step[0] == "choose":
                if not await _keep_name(guild, token, await choose_next(step[1], heard=step[2])): return
                continue
            track = await _resolve_name(step[3], step[2], step[1], step[5])
            if not await _keep_track(guild, token, step, track): return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("radio could not prepare the next songs")

async def _play_radio(player, guild, line):
    song = line.current
    if song is None: return False
    # One searched song starts now. Preparing the other two does not hold this skip.
    # A song that will not start is left behind. The next prepared one still plays.
    for _ready in range(len(_radio_list(line))):
        item = _take_ready(line)
        if item is None: break
        if await _begin(player, guild, item.song):
            _remember_miss(line, item)
            continue
        line = _stored(guild) or line
        _keep_earlier(guild, line, song)
        _schedule_fill(guild, line)
        return True
    while (preferred := _take_planned(line)) is not None and _missed(line, preferred):
        pass
    heard = station_heard(_played(guild, line))
    heard.update((artist.casefold(), title.casefold()) for artist, title in _miss_list(line))
    # The song that started the station is still the one Last.fm is asked about.
    root = _station(line) or song
    match = await another_track(root, heard=heard, preferred=preferred)
    if match is None or await _begin(player, guild, _Song(match, song.requester_id, found_platform(match, root.platform))):
        return False
    _keep_earlier(guild, line, song)
    if preferred is not None and not _is_preferred(match, preferred):
        _retire_fill(guild)
        _drop_plan(line)
    _schedule_fill(guild, line)
    return True

async def _arm_next(guild, line):
    # One name now, so a skip still has a song if none is searched yet.
    # The other two are searched after this returns.
    token = _retire_fill(guild)
    _drop_plan(line)
    if line.current is None or not line.radio: return
    # Radio on, or a song the listener chose, starts the station over from that song.
    _keep_station(line)
    name = await choose_next(_station(line), heard=station_heard(_played(guild, line)))
    if _epoch(guild) != token or not line.radio or line.current is None or name is None: return
    line.radio_next = name
    _plan(line).append(name)
    _schedule_fill(guild, line)

_NO_SERVER = "Use this command in a server."
_OPEN_FAILED = "Nothing on YouTube matched that."
_START_FAILED = "Something went wrong while starting the song."

def _module(name: str):
    return importlib.import_module(name)

def _token_holds(guild_id: int | None, token: int) -> bool:
    return guild_id is not None and _saved_generation.get(guild_id, 0) == token

def _queued_song(member: object, song: object) -> _Song:
    """The queue row for a saved song. The link opens later, one song at a time."""
    raw_title = getattr(song, "title", None)
    title = raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else "Unknown"
    author = getattr(song, "author", None)
    author = author.strip() if isinstance(author, str) else ""
    length = getattr(song, "length", None)
    length = length if isinstance(length, int) and not isinstance(length, bool) and length > 0 else 0
    platform = source.normalize_platform(getattr(song, "platform", None) or "youtube")
    kept = _Kept(title, author, _http(getattr(song, "url", None)), None, length, False)
    return _Song(kept, _number(getattr(member, "id", None)), platform)

def _still_waiting(line, song: _Song) -> bool:
    if line is None:
        return False
    return any(item is song for item in line.later) or any(item is song for item in line.held)

async def _show_rest(
    member: object, guild: object, songs: list, token: int,
) -> list[tuple[object, _Song]] | None:
    """Put the songs still to open on the queue. None when this playlist already lost."""
    pending = [_queued_song(member, song) for song in songs]
    async with _revise(guild, settle=False) as (_missing, _rows, line, commit):
        if not _token_holds(_guild_id(guild), token) or line is None:
            return None
        _module("music.playback.start")._take(line, pending, True)
        commit()
        return list(zip(songs, pending))

async def play_saved(
    member: object, guild: object | None, songs: list, *, replace: bool, channel: object | None = None,
) -> str:
    """The first open song starts. The rest show in the queue, then each link opens in order.

    Play replaces; queue appends. A song that does not open is named in `channel`.
    """
    if guild is None:
        return _NO_SERVER
    if not songs:
        return "That playlist has no songs."
    start = _module("music.playback.start")
    room = start._room(member, guild)
    if isinstance(room, str):
        return room
    guild_id = _guild_id(guild)
    if guild_id is None:
        return _NO_SERVER
    async with _hold(guild):
        token = _end_saved(guild_id) if replace else _saved_generation.get(guild_id, 0)
    lock = _saved_lock_for(guild_id)
    await lock.acquire()
    missed: list[str] = []
    handed, error = False, _OPEN_FAILED
    try:
        for index, song in enumerate(songs):
            if not _token_holds(guild_id, token):
                return _CHANGED
            opened, title, reason = await _open_saved(guild, song)
            if not _token_holds(guild_id, token):
                return _CHANGED
            if opened is None:
                missed.append(title)
                error = reason or error
                continue
            if replace:
                note = await start.load_line(member, guild, [opened])
            else:
                note = await start.queue_opened(member, guild, [opened], whole=True, token=token)
            if not playing_note(note):
                return note
            if missed:
                note = f"{note} Skipped {', '.join(missed)}."
            rest = songs[index + 1:]
            if not rest:
                await _name_skips(guild_id, token, missed, channel)
                return note
            pending = await _show_rest(member, guild, rest, token)
            if pending:
                _saved_task[guild_id] = asyncio.create_task(
                    _resolve_shown(guild, pending, token, lock, missed, channel),
                    name="music-playlist",
                )
                handed = True
                # The first song is named on its own. The rest are already queued with it.
                if not replace:
                    note = _added_together(note, 1 + len(pending))
            return note
        return error
    finally:
        if not handed:
            lock.release()

def _added_together(note: str, count: int) -> str:
    """Name every song that joined. A skip sentence after the first one stays."""
    if count <= 1 or not note.startswith("Added "):
        return note
    mark = ". Skipped "
    extra = note[note.index(mark):] if mark in note else ""
    return f"Added {count} songs{extra}"

async def finish_saved(guild: object | None) -> None:
    guild_id = _guild_id(guild)
    if guild_id is not None and (task := _saved_task.get(guild_id)) is not None:
        await task

async def _open_saved(guild: object, song: object):
    try:
        return await _module("music.playback").open_one(guild, song)
    except Exception:
        log.exception("music playlist could not open a song")
        title = getattr(song, "title", None)
        name = title.strip() if isinstance(title, str) else ""
        return None, name or "Unknown", _START_FAILED

async def _resolve_shown(
    guild, pending: list[tuple[object, _Song]], token: int, lock: asyncio.Lock, missed: list[str],
    channel: object | None,
) -> None:
    """Open each queued song in order. One link is looked up at a time."""
    guild_id = _guild_id(guild)
    try:
        for saved, item in pending:
            if not _token_holds(guild_id, token):
                return
            opened, title, _reason = await _open_saved(guild, saved)
            if not _token_holds(guild_id, token):
                return
            async with _revise(guild, settle=False) as (_missing, _rows, line, commit):
                if not _token_holds(guild_id, token):
                    return
                # The song may already be playing, or the listener removed it.
                if not _still_waiting(line, item):
                    continue
                if opened is None:
                    _pull(line.later, item)
                    _pull(line.held, item)
                    _after_drop(line, item)
                    missed.append(title)
                else:
                    track, platform = opened
                    item.track = track
                    item.platform = str(platform)
                commit()
    except Exception:
        log.exception("music playlist could not be queued")
    finally:
        try:
            await _name_skips(guild_id, token, missed, channel)
        finally:
            lock.release()

async def _name_skips(
    guild_id: int | None, token: int, missed: list[str], channel: object | None,
) -> None:
    send = None if channel is None else getattr(channel, "send", None)
    if not missed or not _token_holds(guild_id, token) or not callable(send):
        return
    text = quote_text(notice_text(f"Skipped {', '.join(missed)}."))
    try:
        await send(content=text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.warning("music playlist could not name the songs it skipped", exc_info=True)

from music.playback.start import _begin
