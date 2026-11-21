"""Named playlists for one server.

A playlist is a named list in the server config. A song keeps its title,
artist, length, platform, and link.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from guild import GuildConfig
from music.source import canonical_url, kept_url, normalize_platform

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
    playable: bool = True

@dataclass(frozen=True)
class SavedList:
    name: str
    saver_id: int | None
    songs: tuple[SavedSong, ...]

def _visible_name(text: str) -> str:
    """One spelling for one name. Hidden marks and split accents do not make a second list."""
    folded = unicodedata.normalize("NFC", text)
    visible = "".join(char for char in folded if unicodedata.category(char) != "Cf")
    return " ".join(visible.split())

def playlist_name(text: str) -> str | None:
    name = _visible_name(text)
    return name[:NAME_LIMIT] if name else None

def name_too_long(text: str) -> bool:
    """True when the typed name would be cut. Cutting it would hide a collision."""
    return len(_visible_name(text)) > NAME_LIMIT

def list_playlists(store: GuildConfig, guild_id: int) -> list[SavedList]:
    found: list[SavedList] = []
    index: dict[str, int] = {}
    for raw in store.guild_list(guild_id, PLAYLISTS):
        saved = _saved_list(raw)
        if saved is None:
            continue
        key = saved.name.casefold()
        place = index.get(key)
        if place is None:
            index[key] = len(found)
            found.append(saved)
            continue
        current = found[place]
        found[place] = SavedList(
            current.name,
            current.saver_id,
            _unique(current.songs + saved.songs),
        )
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
    if name_too_long(name):
        return f"A playlist name can be {NAME_LIMIT} characters."
    shelf = _Shelf(store, guild_id)
    if shelf.place(cleaned) is not None:
        return "That name is already a playlist."
    if len(shelf.lists) >= PLAYLIST_LIMIT:
        return f"A server keeps {PLAYLIST_LIMIT} playlists."
    created = SavedList(cleaned, _positive(saver_id), _unique(songs))
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
    updated = SavedList(saved.name, saved.saver_id, _unique(songs))
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
    if _http(song.url) is None:
        return "That song has no link to save."
    stored = _keep(song)
    shelf = _Shelf(store, guild_id)
    place = shelf.place(cleaned)
    if place is None:
        if len(shelf.lists) >= PLAYLIST_LIMIT:
            return f"A server keeps {PLAYLIST_LIMIT} playlists."
        created = SavedList(cleaned, _positive(saver_id), (stored,))
        shelf.lists.append(created)
        shelf.write()
        return created
    saved = shelf.lists[place]
    if any(canonical_url(item.url) == canonical_url(stored.url) for item in saved.songs):
        return f"{stored.title} is already in the playlist."
    if len(saved.songs) >= SONG_LIMIT:
        return f"A playlist keeps {SONG_LIMIT} songs."
    # The newest song stays at the top. The rest keep their order.
    updated = SavedList(saved.name, saved.saver_id, (stored,) + saved.songs)
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

# Read a field and reject a bad value. Playlist rows and the session file share these.

def _text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""

def _http(value: object) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return kept_url(value)
    return None

def _positive(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value

def _record(value: object) -> dict | None:
    return value if isinstance(value, dict) else None

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
    if not song.playable:
        payload["playable"] = False
    return payload

def _saved_list(raw: object) -> SavedList | None:
    row = _record(raw)
    if row is None:
        return None
    given = row.get("name")
    name = playlist_name(given if isinstance(given, str) else "")
    if name is None:
        return None
    return SavedList(name, _positive(row.get("saver_id")), _saved_songs(row.get("songs")))

def _saved_songs(raw: object) -> tuple[SavedSong, ...]:
    if not isinstance(raw, list):
        return ()
    return _unique(tuple(song for item in raw if (song := _saved_song(item)) is not None))

def _saved_song(raw: object) -> SavedSong | None:
    row = _record(raw)
    if row is None:
        return None
    url = _http(row.get("url"))
    if url is None:
        return None
    platform = row.get("platform")
    return SavedSong(
        _text(row.get("title")) or "Unknown",
        normalize_platform(platform if isinstance(platform, str) else None),
        url,
        _text(row.get("author")),
        _positive(row.get("length")) or 0,
        row.get("playable") is not False,
    )

def _unique(songs: tuple[SavedSong, ...]) -> tuple[SavedSong, ...]:
    """The first copy of a link, up to the song cap."""
    kept: list[SavedSong] = []
    seen: set[str] = set()
    for song in songs:
        if _http(song.url) is None:
            continue
        stored = _keep(song)
        ident = canonical_url(stored.url)
        if ident in seen:
            continue
        seen.add(ident)
        kept.append(stored)
        if len(kept) == SONG_LIMIT:
            break
    return tuple(kept)

def _keep(song: SavedSong) -> SavedSong:
    return SavedSong(
        song.title or "Unknown",
        normalize_platform(song.platform),
        kept_url(song.url),
        _text(song.author),
        _positive(song.length) or 0,
        song.playable,
    )

# An apostrophe sits inside a word. A hyphen still splits, so "slow-down" is two words.
_APOSTROPHE = frozenset("'’ʼ‘")

def song_matches(query: str, song: SavedSong) -> bool:
    """Words may be out of order. A short word must be typed. A longer word may be one or two edits off."""
    needle = _plain(query)
    if not needle:
        return False
    return _close(needle, _plain(song.title), _plain(song.author))

def _unspaced(text: str) -> bool:
    """Chinese, Japanese, and Korean text has no spaces between words."""
    return any(
        "\u3040" <= char <= "\u30ff"
        or "\u3400" <= char <= "\u9fff"
        or "\uac00" <= char <= "\ud7af"
        for char in text
    )

def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).casefold()
    chars = ["" if ch in _APOSTROPHE else ch if ch.isalnum() else " " for ch in folded]
    return " ".join("".join(chars).split())

def _close(needle: str, title: str, author: str) -> bool:
    """One walk over the title and the artist. The same titles still match."""
    words = needle.split()
    compact = needle.replace(" ", "")
    pending = set(range(len(words)))
    saw = False
    inside = len(compact) >= 2 and _unspaced(compact)
    for field in (title, author):
        parts = field.split()
        # "REM" is "R.E.M." joined. Under four letters, only that exact join counts.
        spans = [""]
        for part in parts:
            saw = True
            matched: set[int] = set()
            for index in pending:
                word = words[index]
                if word == part or (len(word) >= 3 and part.startswith(word)):
                    matched.add(index)
                    continue
                limit = 0 if len(word) <= 3 else 1 if len(word) <= 6 else 2
                if limit == 0 or abs(len(word) - len(part)) > limit:
                    continue
                dist = _distance(word, part)
                if dist <= limit and (word[0] == part[0] or (len(word) >= 5 and dist <= 1)):
                    matched.add(index)
            pending -= matched
            if not pending:
                return True
            grown: list[str] = []
            for span in spans:
                built = span + part
                if built == compact:
                    return True
                if len(built) < len(compact):
                    grown.append(built)
            spans = grown + [""]
        squashed = "".join(parts)
        if len(compact) >= 4 and squashed and compact[0] == squashed[0]:
            limit = 1 if len(compact) <= 6 else 2
            if abs(len(compact) - len(squashed)) <= limit and _distance(compact, squashed) <= limit:
                return True
        # A title with no spaces is one word, so "里香" would miss "七里香".
        if inside and compact in squashed:
            return True
    return saw and not pending

def _distance(left: str, right: str) -> int:
    """Swapping two neighbouring letters counts as one edit."""
    if left == right:
        return 0
    previous, current = [0] * (len(right) + 1), list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        row = [i]
        for j, b in enumerate(right, start=1):
            best = min(current[j] + 1, row[-1] + 1, current[j - 1] + (a != b))
            if i > 1 and j > 1 and a == right[j - 2] and left[i - 2] == b:
                best = min(best, previous[j - 2] + 1)
            row.append(best)
        previous, current = current, row
    return current[-1]
