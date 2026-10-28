"""Two different kinds of record for one server.

A playlist record is a named list in the server config. A song keeps its
title, artist, length, platform, and link.

A line record is the queue and the history. It lives in data/music_session.json
and is read back after a restart.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from config import BASE_DIR, write_text_atomic
from guild import GuildConfig
from music.source import canonical_url, normalize_platform

log = logging.getLogger(__name__)


# Playlist record. Named lists stored in the server config.

PLAYLISTS = "music_playlists"
PLAYLIST_LIMIT = 5
SONG_LIMIT = 100
NAME_LIMIT = 80


@dataclass(frozen=True)
class SavedSong:
    title: str
    platform: str
    url: str
    author: str = ""
    length: int = 0


@dataclass(frozen=True)
class SavedList:
    name: str
    saver_id: int | None
    songs: tuple[SavedSong, ...]


def playlist_name(text: str) -> str | None:
    name = " ".join(text.split())
    return name[:NAME_LIMIT] if name else None


def list_playlists(store: GuildConfig, guild_id: int) -> list[SavedList]:
    found: list[SavedList] = []
    for raw in store.guild_list(guild_id, PLAYLISTS):
        saved = _saved_list(raw)
        if saved is not None:
            found.append(saved)
    return found


def find_playlist(store: GuildConfig, guild_id: int, name: str) -> SavedList | None:
    cleaned = playlist_name(name)
    if cleaned is None:
        return None
    shelf = _Shelf(store, guild_id)
    place = shelf.place(cleaned)
    return None if place is None else shelf.lists[place]


def create_playlist(
    store: GuildConfig,
    guild_id: int,
    name: str,
    saver_id: int | None,
    songs: tuple[SavedSong, ...] = (),
) -> SavedList | str:
    """A new list. Songs past the cap are left off. A name is kept once."""
    cleaned = playlist_name(name)
    if cleaned is None:
        return "Name the playlist."
    shelf = _Shelf(store, guild_id)
    if shelf.place(cleaned) is not None:
        return "That name is already a playlist."
    if len(shelf.lists) >= PLAYLIST_LIMIT:
        return f"A server keeps {PLAYLIST_LIMIT} playlists."
    created = SavedList(cleaned, _saver(saver_id), _unique(songs))
    shelf.lists.append(created)
    shelf.write()
    return created


def delete_playlist(store: GuildConfig, guild_id: int, name: str) -> SavedList | str:
    held = _held(store, guild_id, name)
    if isinstance(held, str):
        return held
    shelf, place = held
    removed = shelf.lists.pop(place)
    shelf.write()
    return removed


def drop_song(store: GuildConfig, guild_id: int, name: str, index: int) -> SavedList | str:
    """Take one song off a list. The rest stay in order."""
    held = _held(store, guild_id, name)
    if isinstance(held, str):
        return held
    shelf, place = held
    saved = shelf.lists[place]
    if index < 0 or index >= len(saved.songs):
        return "That song is gone."
    updated = SavedList(saved.name, saved.saver_id, saved.songs[:index] + saved.songs[index + 1:])
    shelf.lists[place] = updated
    shelf.write()
    return updated


def replace_songs(
    store: GuildConfig,
    guild_id: int,
    name: str,
    songs: tuple[SavedSong, ...],
) -> SavedList | str:
    """Replace the songs on one list. The name and who saved it stay."""
    held = _held(store, guild_id, name)
    if isinstance(held, str):
        return held
    shelf, place = held
    saved = shelf.lists[place]
    kept = tuple(_keep(song) for song in songs if _link(song.url))[:SONG_LIMIT]
    updated = SavedList(saved.name, saved.saver_id, kept)
    shelf.lists[place] = updated
    shelf.write()
    return updated


def save_song(
    store: GuildConfig,
    guild_id: int,
    name: str,
    song: SavedSong,
    saver_id: int | None,
) -> SavedList | str:
    cleaned = playlist_name(name)
    if cleaned is None:
        return "Name the playlist."
    if _link(song.url) is None:
        return "That song has no link to save."
    stored = _keep(song)
    shelf = _Shelf(store, guild_id)
    place = shelf.place(cleaned)
    if place is None:
        if len(shelf.lists) >= PLAYLIST_LIMIT:
            return f"A server keeps {PLAYLIST_LIMIT} playlists."
        created = SavedList(cleaned, _saver(saver_id), (stored,))
        shelf.lists.append(created)
        shelf.write()
        return created
    saved = shelf.lists[place]
    if any(item.url == stored.url for item in saved.songs):
        return f"{stored.title} is already in the playlist."
    if len(saved.songs) >= SONG_LIMIT:
        return f"A playlist keeps {SONG_LIMIT} songs."
    updated = SavedList(saved.name, saved.saver_id, saved.songs + (stored,))
    shelf.lists[place] = updated
    shelf.write()
    return updated


class _Shelf:
    def __init__(self, store: GuildConfig, guild_id: int):
        self.store = store
        self.guild_id = guild_id
        self.lists = list_playlists(store, guild_id)

    def place(self, name: str) -> int | None:
        key = name.casefold()
        return next((i for i, saved in enumerate(self.lists) if saved.name.casefold() == key), None)

    def write(self) -> None:
        self.store.set_guild_list(self.guild_id, PLAYLISTS, [_dump(saved) for saved in self.lists])


def _held(store: GuildConfig, guild_id: int, name: str) -> tuple[_Shelf, int] | str:
    cleaned = playlist_name(name)
    if cleaned is None:
        return "Name the playlist."
    shelf = _Shelf(store, guild_id)
    place = shelf.place(cleaned)
    if place is None:
        return "That playlist is gone."
    return shelf, place


def _dump(saved: SavedList) -> dict[str, object]:
    payload: dict[str, object] = {"name": saved.name, "songs": [_song(song) for song in saved.songs]}
    if saved.saver_id is not None:
        payload["saver_id"] = saved.saver_id
    return payload


def _song(song: SavedSong) -> dict[str, object]:
    payload: dict[str, object] = {"title": song.title, "platform": song.platform, "url": song.url}
    if song.author:
        payload["author"] = song.author
    if song.length:
        payload["length"] = song.length
    return payload


def _saved_list(raw: object) -> SavedList | None:
    if not isinstance(raw, dict):
        return None
    given = raw.get("name")
    name = playlist_name(given if isinstance(given, str) else "")
    if name is None:
        return None
    return SavedList(name, _saver(raw.get("saver_id")), _saved_songs(raw.get("songs")))


def _saved_songs(raw: object) -> tuple[SavedSong, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(song for item in raw if (song := _saved_song(item)) is not None)


def _saved_song(raw: object) -> SavedSong | None:
    if not isinstance(raw, dict):
        return None
    url = _link(raw.get("url"))
    if url is None:
        return None
    platform = raw.get("platform")
    return SavedSong(
        _words(raw.get("title")) or "Unknown",
        normalize_platform(platform if isinstance(platform, str) else None),
        url,
        _words(raw.get("author")),
        _count(raw.get("length")),
    )


def _unique(songs: tuple[SavedSong, ...]) -> tuple[SavedSong, ...]:
    """The first copy of a link, up to the song cap."""
    kept: list[SavedSong] = []
    seen: set[str] = set()
    for song in songs:
        if _link(song.url) is None:
            continue
        stored = _keep(song)
        if stored.url in seen:
            continue
        seen.add(stored.url)
        kept.append(stored)
        if len(kept) == SONG_LIMIT:
            break
    return tuple(kept)


def _keep(song: SavedSong) -> SavedSong:
    return SavedSong(
        song.title or "Unknown",
        normalize_platform(song.platform),
        canonical_url(song.url),
        _words(song.author),
        _count(song.length),
    )


def _link(value: object) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return canonical_url(value)
    return None


def _words(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return 0
    return value


def _saver(value: object) -> int | None:
    return _count(value) or None


# Line record. The queue and the history, stored in data/music_session.json.

PATH = BASE_DIR / "data" / "music_session.json"
_PREVIOUS_NAME = "music_line.json"

_cache_path: Path | None = None
_book: dict[str, dict] | None = None


def _adopt_previous(path: Path, previous_name: str) -> None:
    """Move an older session file onto `path` once. An existing `path` stays."""
    previous = path.with_name(previous_name)
    if not path.exists() and previous.is_file():
        previous.replace(path)


_adopt_previous(PATH, _PREVIOUS_NAME)


def read_guild(guild_id: int) -> dict | None:
    raw = _open().get(str(guild_id))
    return raw if isinstance(raw, dict) else None


def read_all() -> dict[int, dict]:
    found: dict[int, dict] = {}
    for key, raw in _open().items():
        guild_id = _guild_id(key)
        if guild_id is not None and isinstance(raw, dict):
            found[guild_id] = raw
    return found


def write_guild(guild_id: int, record: dict | None) -> None:
    book = _open()
    key = str(guild_id)
    if record is None:
        if key not in book:
            return
        del book[key]
    else:
        book[key] = record
    _write_line(book)


def _open() -> dict[str, dict]:
    global _cache_path, _book
    if _book is None or _cache_path != PATH:
        _cache_path = PATH
        _book = _read(PATH)
    return _book


def _read(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("music session file could not be read", exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _write_line(book: dict[str, dict]) -> None:
    global _book, _cache_path
    _book = book
    _cache_path = PATH
    write_text_atomic(PATH, json.dumps(book, indent=2) + "\n")


def _guild_id(key: object) -> int | None:
    try:
        guild_id = int(key)
    except (TypeError, ValueError):
        return None
    return guild_id if guild_id > 0 else None
