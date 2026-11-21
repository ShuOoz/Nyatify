"""Queue, history, and the saved queue for one server."""

from __future__ import annotations

import importlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from config import write_text_atomic
from music import archive, source

log = logging.getLogger(__name__)

def load_saved_lines() -> None:
    # A line already in memory stays. History alone does not: leaving voice drops the line.
    for guild_id, raw in read_all().items():
        if guild_id not in _lines or guild_id not in _heard:
            _install(guild_id, raw)
            _missing.discard(guild_id)

def save_open_lines() -> None:
    for guild_id in set(_lines) | set(_heard):
        record = _record(guild_id)
        if guild_id not in _lines:
            # Leaving voice stores the queue, then drops it. History alone must not replace that queue.
            saved = read_guild(guild_id)
            if isinstance(saved, dict):
                record = saved if record is None else {**saved, "history": record.get("history", [])}
        write_guild(guild_id, record)

def _store(guild: object | None) -> None:
    if (guild_id := _guild_id(guild)) is not None:
        _missing.discard(guild_id)
        write_guild(guild_id, _record(guild_id))

def _rows(songs) -> list[dict[str, object]]:
    return [_dump_song(song) for song in songs]

def _omit(values: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}

def _record(guild_id: int) -> dict | None:
    line, heard = _lines.get(guild_id), _heard.get(guild_id, [])
    current, later, held = (None, [], []) if line is None else (line.current, line.later, line.held)
    if current is None and not later and not held and not heard:
        return None
    # Saved files still call this spot, so a restart finds the same place.
    payload: dict[str, object] = {
        "current": None if current is None else _dump_song(current),
        "later": _rows(later), "history": _rows(heard),
    }
    if line is None:
        return payload
    radio = line.radio_next
    payload.update(_omit({
        "held": _rows(held) or None, "volume": None if line.volume == 100 else line.volume,
        "spot": line.position if line.paused and line.position > 0 else None,
        "order": _rows(line.order) if line.shuffle and line.order else None,
        "radio_next": None if radio is None else {"artist": radio[0], "title": radio[1]},
        "radio_from": None if line.radio_from is None else _dump_song(line.radio_from),
        **{name: True for name in _MODES if getattr(line, name)},
    }))
    return payload

def _dump_song(song: _Song) -> dict[str, object]:
    track = song.track
    payload = {
        "title": _title(track), "author": _text(getattr(track, "author", None)),
        "url": source.remembered_share(track) or _http(getattr(track, "uri", None)) or "",
        "platform": song.platform, "length": _number(getattr(track, "length", None)) or 0,
        "stream": _stream(track) or bool(song.videos),
        "artwork": _http(getattr(track, "artwork", None)), "requester_id": song.requester_id,
    }
    if song.videos:
        payload.update(videos=list(song.videos), video_at=song.video_at, station=song.station)
    return _omit(payload)

def _recall(guild_id: int) -> None:
    if guild_id in _missing or (guild_id in _lines and guild_id in _heard):
        return
    raw = read_guild(guild_id)
    if raw is None:
        _missing.add(guild_id)
        return
    _install(guild_id, raw)

def _install(guild_id: int, raw: dict) -> None:
    if guild_id not in _heard and (history := _load_rows(raw.get("history"))):
        _heard[guild_id] = history[-HISTORY_SONGS:]
    if guild_id in _lines:
        return
    later, parked = _load_rows(raw.get("later")), _load_rows(raw.get("held"))
    current = _load_song(raw.get("current"))
    # The song that was on can be missing. The next queued one takes that place.
    took_next = current is None
    pool = later or parked
    if took_next and not pool:
        return
    if took_next:
        current = pool.pop(0)
    on = {name: raw.get(name) is True for name in _MODES}
    paused, spot = not took_next and on["paused"], _number(raw.get("spot")) or 0
    radio = raw.get("radio_next") if isinstance(raw.get("radio_next"), dict) else {}
    artist, title = _text(radio.get("artist")), _text(radio.get("title"))
    volume = _number(raw.get("volume"))
    restored = _Line(
        current=current, later=later, held=parked, volume=100 if volume is None else volume,
        repeat=on["repeat"], whole=on["whole"], shuffle=on["shuffle"], radio=on["radio"],
        radio_next=(artist, title) if artist and title else None,
        radio_from=_load_song(raw.get("radio_from")),
        order=_bind_order(_load_rows(raw.get("order")), later, playing=current) if on["shuffle"] else [],
        paused=paused, position=spot if paused and spot > 0 else 0, resting=True,
    )
    _fill_held(restored)
    if restored.held:
        restored.whole = False
    _lines[guild_id] = restored

def _load_rows(raw: object) -> list[_Song]:
    rows = raw if isinstance(raw, list) else []
    return [song for item in rows if (song := _load_song(item)) is not None]

def _load_song(raw: object) -> _Song | None:
    if not isinstance(raw, dict):
        return None
    title = _text(raw.get("title"), "Unknown")
    url = raw.get("url") if isinstance(raw.get("url"), str) else ""
    if title == "Unknown" and not _text(url):
        return None
    listed = raw.get("videos", raw.get("lives"))
    platform = raw.get("platform") if isinstance(raw.get("platform"), str) else "youtube"
    song = _Song(
        _Kept(title, _text(raw.get("author")), url or None, _http(raw.get("artwork")),
              _number(raw.get("length")) or 0, raw.get("stream") is True),
        _number(raw.get("requester_id")), source.normalize_platform(platform),
        videos=tuple(item for item in listed if isinstance(item, str)) if isinstance(listed, list) else (),
        video_at=_number(raw.get("video_at", raw.get("live_at"))) or 0, station=_text(raw.get("station")),
    )
    _note_station(song)
    return song

def _adopt_previous(path: Path, previous_name: str) -> None:
    """Move an older session file onto `path` once. An existing `path` stays."""
    previous = path.with_name(previous_name)
    if not path.exists() and previous.is_file():
        previous.replace(path)

def read_guild(guild_id: int) -> dict | None:
    book = _opened()
    if book is None:
        return None
    return archive._record(book.get(str(guild_id)))

def read_all() -> dict[int, dict]:
    book = _opened()
    if book is None:
        return {}
    found: dict[int, dict] = {}
    for key, raw in book.items():
        guild_id = _session_key(key)
        row = archive._record(raw)
        if guild_id is not None and row is not None:
            found[guild_id] = row
    return found

def write_guild(guild_id: int, record: dict | None) -> None:
    book = _opened()
    if book is None:
        return
    key = str(guild_id)
    if record is None:
        if key not in book:
            return
        del book[key]
    else:
        book[key] = record
    _write_line(book)

class _UnreadableSession(Exception):
    """The session file is there, but this process could not trust it."""

def _opened() -> dict[str, dict] | None:
    try:
        return _open()
    except _UnreadableSession:
        return None

def _open() -> dict[str, dict]:
    # These four are reassigned on remember. A copied name here would keep the old file.
    if remember._cache_path != remember.PATH:
        remember._failed_path = None
    if remember._failed_path == remember.PATH:
        raise _UnreadableSession
    if remember._book is not None and remember._cache_path == remember.PATH:
        return remember._book
    try:
        loaded = _read(remember.PATH)
    except _UnreadableSession:
        remember._failed_path = remember.PATH
        remember._cache_path = remember.PATH
        remember._book = None
        raise
    remember._failed_path = None
    remember._cache_path = remember.PATH
    remember._book = loaded
    return remember._book

def _read(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("music session file could not be read and will not be replaced", exc_info=True)
        raise _UnreadableSession
    book = archive._record(raw)
    if book is None:
        log.warning("music session file could not be read and will not be replaced")
        raise _UnreadableSession
    return {str(key): value for key, value in book.items() if archive._record(value) is not None}

def _write_line(book: dict[str, dict]) -> None:
    remember._book = book
    remember._cache_path = remember.PATH
    write_text_atomic(remember.PATH, json.dumps(book, indent=2) + "\n")

def _session_key(key: object) -> int | None:
    try:
        number = int(key)
    except (TypeError, ValueError):
        return None
    return archive._positive(number)

def _station_of(url: str) -> tuple[str, int, tuple[str, ...]] | None:
    """The menu station this video belongs to: key, place in the list, and the list.

    YouTube can spell one video as more than one address. The id is what matches.
    """
    video, _list_id = source._youtube(url)
    if not video:
        return None
    for _label, key, ids in source._STATIONS:
        if video not in ids:
            continue
        return key, ids.index(video), source.station_videos(key)
    return None

def _note_station(song: object | None) -> None:
    """A menu-radio video keeps its station list when the save forgot it."""
    if song is None or song.videos:
        return
    found = _station_of(_http(getattr(song.track, "uri", None)) or "")
    if found is None:
        return
    song.station, song.video_at, song.videos = found

# remember imports the readers above before now_playing exists.

import music.playback.remember as remember

from music.playback.remember import (
    HISTORY_SONGS, Queued, _Kept, _Line, _MODES, _Song,
    _bind_order, _end_saved, _fill_held, _guild_id, _heard, _hold, _http,
    _lines, _missing, _number, _order_drop, _queued_index,
    _requesters, _stored, _stream, _text, _title, paint_soon,
)

_GONE = "That song is gone."
_SILENT = "Nothing is playing."

def queued(guild: object | None) -> list[Queued] | None:
    """The current song, then the rest. A saved line still reads when voice is gone."""
    from music.playback.remember import now_playing

    state = now_playing(guild)
    line = _stored(guild)
    if state is None:
        return [_queued_row(song, False) for song in _saved_songs(line)] or None
    rows: list[Queued] = []
    if line is not None and line.current is not None:
        rows.append(_queued_row(line.current, True))
    elif state.has_track:
        rows.append(Queued(
            state.title or "Unknown",
            source.platform_key(state.uri or "", "youtube"),
            state.uri,
            True,
            state.stream,
            state.author,
            state.length,
            state.requester_id,
        ))
    if line is not None:
        rows.extend(_queued_row(song, False) for song in line.later)
    return rows

def _saved_songs(line: _Line | None) -> list[_Song]:
    if line is None:
        return []
    return ([line.current] if line.current is not None else []) + list(line.later)

def _claim_current(line: _Line, track: object, guild: object | None = None) -> None:
    """A reload parks the playing song on the queue. Put that song back."""
    # A start has set the current song aside. Leave that queue alone.
    if line.handoff or line.current is not None or not line.later:
        return
    index = _queued_index(line.later, track)
    if index is None:
        return
    line.current = line.later.pop(index)
    requester = line.current.requester_id
    guild_id = _guild_id(guild)
    if requester is not None and guild_id is not None and guild_id not in _requesters:
        _requesters[guild_id] = requester

def _queued_row(song: _Song, playing: bool) -> Queued:
    track = song.track
    return Queued(
        _text(getattr(track, "title", None), "Unknown"),
        song.platform,
        source.remembered_share(track) or _http(getattr(track, "uri", None)),
        playing,
        _stream(track) or bool(song.videos),
        _text(getattr(track, "author", None)),
        _number(getattr(track, "length", None)) or 0,
        song.requester_id,
    )

def played(guild: object | None) -> list[Queued] | None:
    """Songs already heard, newest first. One on now or waiting stays hidden."""
    songs = _past(guild)
    return None if songs is None else [_queued_row(song, False) for song in reversed(songs)]

def earlier_at(guild: object | None, index: int) -> tuple[object, str] | None:
    songs = _past(guild)
    if songs is None or not 0 <= index < len(songs):
        return None
    song = songs[-1 - index]
    return song.track, song.platform

@asynccontextmanager
async def _revise(guild: object | None, *indexes: int, settle: bool = True):
    """Read the line, check the index, update the order, and refresh.

    commit(save=False) only refreshes a change the player already stored.
    """
    changed = False
    keep = False

    def commit(*, save: bool = True) -> None:
        nonlocal changed, keep
        changed = True
        keep = save

    async with _hold(guild):
        rows = queued(guild) if settle else None
        missing = _listed(rows, *indexes) if indexes else None
        yield missing, rows, _stored(guild), commit
        if missing is None and keep:
            _store(guild)
    if changed:
        paint_soon(guild)

def _listed(rows: list[Queued] | None, *indexes: int) -> str | None:
    if rows is None:
        return _SILENT
    if any(not 0 <= index < len(rows) for index in indexes):
        return _GONE
    return None

def _slot(rows: list[Queued], index: int) -> int:
    return index - bool(rows[0].playing)

def _after_drop(line: _Line, song: _Song) -> None:
    """Order, parked songs, and the playlist flag follow a song that already left."""
    _order_drop(line, song)
    _fill_held(line)
    if not line.later:
        line.whole = False

async def drop_queued(guild: object | None, index: int, member: object | None = None) -> str | None:
    async with _revise(guild, index) as (missing, rows, line, commit):
        if missing is not None or rows is None:
            return missing
        if rows[index].playing:
            note = await _module("music.playback.control")._advance_line(guild, member)
            commit(save=False)
            return note
        slot = _slot(rows, index)
        if line is None or not 0 <= slot < len(line.later):
            return _GONE
        _after_drop(line, line.later.pop(slot))
        commit()
        return None

async def move_queued(guild: object | None, source: int, dest: int) -> str | None:
    """Exchange two waiting songs. The one on now stays where it is."""
    async with _revise(guild, source, dest) as (missing, rows, line, commit):
        if missing is not None or rows is None:
            return missing
        if rows[source].playing or rows[dest].playing or source == dest:
            return None
        src, dst = _slot(rows, source), _slot(rows, dest)
        if line is None or not 0 <= src < len(line.later) or not 0 <= dst < len(line.later):
            return _GONE
        line.later[src], line.later[dst] = line.later[dst], line.later[src]
        # Shuffle off rebuilds the queue from order. A move the user just made is that order.
        if line.shuffle:
            line.order = list(line.later)
        commit()
        return None

async def clear_queued(guild: object | None) -> None:
    """Drop every waiting song. The one on now keeps playing, and an opening playlist stops."""
    async with _revise(guild) as (_missing, _rows, line, commit):
        guild_id = _guild_id(guild)
        if guild_id is not None:
            _end_saved(guild_id)
        if line is None or not (line.later or line.held or line.whole):
            return
        line.later.clear()
        line.held.clear()
        line.order.clear()
        line.whole = False
        commit()

async def clear_played(guild: object | None, requester_id: int | None = None) -> None:
    """Drop songs already played. One person drops only the songs they asked for."""
    async with _revise(guild, settle=False) as (_missing, _rows, _line, commit):
        guild_id = _guild_id(guild)
        if guild_id is None:
            return
        _recall(guild_id)
        heard = _heard.get(guild_id)
        if not heard:
            return
        if requester_id is None:
            heard.clear()
        else:
            kept = [song for song in heard if song.requester_id != requester_id]
            if len(kept) == len(heard):
                return
            heard[:] = kept
        commit()

def _past(guild: object | None) -> list[_Song] | None:
    """History, minus a song on now or waiting. Removing it before it plays shows it again."""
    guild_id = _guild_id(guild)
    if guild_id is None:
        return None
    _recall(guild_id)
    songs = _heard.get(guild_id, [])
    line = _stored(guild)
    if line is None:
        return songs
    lined = _saved_songs(line)
    return [song for song in songs if not any(_same_song(song, item) for item in lined)]

def _keep_current(guild: object) -> None:
    """The song on now counts as played, even when playback stops before it ends."""
    line = _stored(guild)
    if line is None or line.current is None:
        return
    song = line.current
    # Stop drops the queue, so another copy waiting there does not hide this play.
    line.later = [item for item in line.later if not _same_song(item, song)]
    line.order = [item for item in line.order if not _same_song(item, song)]
    _keep_earlier(guild, line, song)

def _keep_earlier(guild: object | None, line: _Line, song: _Song) -> None:
    _push(line.earlier, song, newest=False)
    if any(_same_song(song, item) for item in _saved_songs(line) if item is not song):
        return
    guild_id = _guild_id(guild)
    if guild_id is not None:
        _push(_heard.setdefault(guild_id, []), song, newest=True)

def _push(songs: list[_Song], song: _Song, *, newest: bool) -> None:
    if newest:
        songs[:] = [item for item in songs if not _same_song(item, song)]
    songs.append(song)
    del songs[:-HISTORY_SONGS]

def _same_song(song: _Song, other: _Song) -> bool:
    """The same recording. A link matches even when the saved title differs."""
    left, right = song.track, other.track
    one, two = _http(getattr(left, "uri", None)), _http(getattr(right, "uri", None))
    if one and two:
        return source.same_url(one, two)
    ident, other_id = getattr(left, "identifier", None), getattr(right, "identifier", None)
    if isinstance(ident, str) and isinstance(other_id, str) and ident and other_id:
        return ident == other_id
    title, other_title = (_text(getattr(item, "title", None)).casefold() for item in (left, right))
    if not title or title != other_title:
        return False
    author, other_author = (_text(getattr(item, "author", None)).casefold() for item in (left, right))
    return not author or not other_author or author == other_author

def _module(name: str):
    return importlib.import_module(name)
