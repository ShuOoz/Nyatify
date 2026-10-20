"""Saved playlists for one server. A song keeps its title, platform, and link."""

from __future__ import annotations

from dataclasses import dataclass

from guild import GuildConfig
from music.source import normalize_platform

# About ten songs on the live line and on a saved list.
PAGE = 10
PLAYLISTS = "music_playlists"
_NAME_LIMIT = 80


@dataclass(frozen=True)
class SavedSong:
    title: str
    platform: str
    url: str


@dataclass(frozen=True)
class SavedList:
    name: str
    saver_id: int | None
    songs: tuple[SavedSong, ...]


def playlist_name(text: str) -> str | None:
    name = " ".join(text.split())
    if not name:
        return None
    return name[:_NAME_LIMIT]


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
    key = cleaned.casefold()
    for saved in list_playlists(store, guild_id):
        if saved.name.casefold() == key:
            return saved
    return None


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
    if not song.url.startswith(("http://", "https://")):
        return "That song has no link to save."
    stored = SavedSong(song.title or "Unknown", normalize_platform(song.platform), song.url)
    lists = list_playlists(store, guild_id)
    for index, saved in enumerate(lists):
        if saved.name.casefold() != cleaned.casefold():
            continue
        lists[index] = SavedList(saved.name, saved.saver_id, saved.songs + (stored,))
        _write(store, guild_id, lists)
        return lists[index]
    created = SavedList(cleaned, _saver(saver_id), (stored,))
    lists.append(created)
    _write(store, guild_id, lists)
    return created


def page_bounds(count: int, page: int) -> tuple[int, int, int]:
    """Start, stop, and the page clamped into range. Pages are 0-based."""
    pages = max(1, (count + PAGE - 1) // PAGE) if count else 1
    current = min(max(0, page), pages - 1)
    start = current * PAGE
    return start, min(count, start + PAGE), current


def _write(store: GuildConfig, guild_id: int, lists: list[SavedList]) -> None:
    store.set_guild_list(guild_id, PLAYLISTS, [_dump(saved) for saved in lists])


def _dump(saved: SavedList) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": saved.name,
        "songs": [
            {"title": song.title, "platform": song.platform, "url": song.url}
            for song in saved.songs
        ],
    }
    if saved.saver_id is not None:
        payload["saver_id"] = saved.saver_id
    return payload


def _saved_list(raw: object) -> SavedList | None:
    if not isinstance(raw, dict):
        return None
    name = playlist_name(raw.get("name") if isinstance(raw.get("name"), str) else "")
    if name is None:
        return None
    songs = raw.get("songs")
    kept: list[SavedSong] = []
    if isinstance(songs, list):
        for item in songs:
            song = _saved_song(item)
            if song is not None:
                kept.append(song)
    return SavedList(name, _saver(raw.get("saver_id")), tuple(kept))


def _saved_song(raw: object) -> SavedSong | None:
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        title = "Unknown"
    else:
        title = " ".join(title.split())
    platform = raw.get("platform")
    return SavedSong(title, normalize_platform(platform if isinstance(platform, str) else None), url)


def _saver(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
