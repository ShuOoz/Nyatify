"""The Playlist tab: the shelf of saved lists, and the songs in the open one."""

from __future__ import annotations

import asyncio
import logging
import unicodedata

import discord

from music.archive import (
    NAME_LIMIT, PLAYLIST_LIMIT, SONG_LIMIT, SavedList, SavedSong,
    create_playlist, delete_playlist, find_playlist, list_playlists,
    playlist_name, replace_songs, save_song,
)
from music.panel import MARK_ADD, SearchSong, _note, _row, _selected
from music.playback import leave_if_idle, played, playing_note, warm_voice
from music.playlist.popups import FindInList, NamePlaylist, RemovePlaylist, SaveSong
from music.playlist.card import (
    HISTORY, MARK_MINUS, PICK, PLAYLISTS, _READ_AT_ONCE,
    _BUSY, _as_saved, _defer, _guild_num, _heard, _list_noun, _menu_gap, _mixed_platforms,
    _person, _saved_span, _song_noun, _song_span, _track_author, _track_length,
    empty_text, pick_text, saved_row,
)
from music.source import (
    canonical_url, import_link, is_link, needs_redirect, platform_line, track_query,
)

log = logging.getLogger(__name__)

# Longer than any saved name, so Pick a playlist never selects a list called "pick".
_BACK = "pick" + ("\u200b" * (NAME_LIMIT + 1 - len("pick")))
_FAILED = "Something went wrong while starting the song."


def _pkg():
    import sys
    return sys.modules["music.playlist"]


async def _can_click(view, interaction: discord.Interaction, *, closed: bool = True) -> bool:
    if closed and view.is_finished():
        await _defer(interaction)
        return False
    if view._working:
        await _note(interaction, _BUSY)
        return False
    return True


# An apostrophe sits inside a word. A hyphen still splits, so "slow-down" is two words.
_APOSTROPHE = frozenset("'’ʼ‘")


def song_matches(query: str, song: SavedSong) -> bool:
    """Words may be out of order. A short word must be typed. A longer word may be one or two edits off."""
    needle = _plain(query)
    if not needle:
        return False
    title, author = _plain(song.title), _plain(song.author)
    pieces = f"{title} {author}".split()
    if pieces and all(_word_hits(word, pieces) for word in needle.split()):
        return True
    compact = needle.replace(" ", "")
    # "REM" is "R.E.M." joined. Under four letters, only that exact join counts.
    if _joined(compact, title.split()) or _joined(compact, author.split()):
        return True
    return len(compact) >= 4 and (_fuzzy(compact, title) or _fuzzy(compact, author))


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).casefold()
    chars = ["" if ch in _APOSTROPHE else ch if ch.isalnum() else " " for ch in folded]
    return " ".join("".join(chars).split())

def _word_hits(word: str, pieces: list[str]) -> bool:
    # The query may start a longer song word. "beatles" must not match "beat".
    limit = 0 if len(word) <= 3 else 1 if len(word) <= 6 else 2
    for piece in pieces:
        if word == piece or (len(word) >= 3 and piece.startswith(word)):
            return True
        if limit == 0 or abs(len(word) - len(piece)) > limit:
            continue
        dist = _distance(word, piece)
        if dist <= limit and (word[0] == piece[0] or (len(word) >= 5 and dist <= 1)):
            return True
    return False

def _joined(query: str, words: list[str]) -> bool:
    for start in range(len(words)):
        joined = ""
        for word in words[start:]:
            joined += word
            if joined == query:
                return True
            if len(joined) > len(query):
                break
    return False

def _fuzzy(query: str, field: str) -> bool:
    compact = field.replace(" ", "")
    limit = 1 if len(query) <= 6 else 2
    if not query or not compact or query[0] != compact[0] or abs(len(query) - len(compact)) > limit:
        return False
    return _distance(query, compact) <= limit

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


def _spare_name(store: object, guild_id: int) -> str:
    taken = {saved.name.casefold() for saved in list_playlists(store, guild_id)}
    number = 1
    while True:
        name = "Playlist" if number == 1 else f"Playlist {number}"
        if name.casefold() not in taken:
            return name
        number += 1


def _imported(found: object) -> tuple[list[SavedSong], int]:
    """Unique songs with a link, then how many unique songs did not fit."""
    tracks = getattr(found, "tracks", None)
    if not isinstance(tracks, list):
        return [], 0
    platform = getattr(found, "platform", None)
    site = platform if isinstance(platform, str) else "youtube"
    songs, seen = [], set()
    for track in tracks:
        raw = getattr(track, "uri", None)
        url = canonical_url(raw) if isinstance(raw, str) and raw.startswith(("http://", "https://")) else ""
        if not url or url in seen:
            continue
        seen.add(url)
        if len(songs) >= SONG_LIMIT:
            continue
        title = getattr(track, "title", None)
        title = " ".join(title.split()) if isinstance(title, str) and title.strip() else "Unknown"
        songs.append(SavedSong(title, site, url, _track_author(track), _track_length(track)))
    return songs, len(seen) - len(songs)


def _bare(song: SavedSong) -> bool:
    return not song.author or song.length <= 0


def _with_meta(song: SavedSong, author: str, length: int) -> SavedSong:
    if (author, length) == (song.author, song.length):
        return song
    return SavedSong(song.title, song.platform, song.url, author, length)


async def _read_list(guild: object, saved: SavedList, seen: set[str]) -> SavedList | None:
    """A miss is not read again. A hit can fill the same link on the next list."""
    bare = [i for i, song in enumerate(saved.songs) if _bare(song) and song.url not in seen]
    if not bare:
        return None
    gate = asyncio.Semaphore(_READ_AT_ONCE)

    async def one(song: SavedSong) -> SavedSong:
        async with gate:
            try:
                found = await _pkg().find_songs(guild, song.url, song.platform, mode="track")
            except Exception:
                log.warning("music playlist could not read a song", exc_info=True)
                found = None
        if not found or isinstance(found, str) or not found.tracks:
            return song
        track = found.tracks[0]
        return _with_meta(song, _track_author(track) or song.author, _track_length(track) or song.length)

    fresh = await asyncio.gather(*(one(saved.songs[i]) for i in bare))
    songs = list(saved.songs)
    for index, song in zip(bare, fresh):
        if _bare(song):
            seen.add(songs[index].url)
        songs[index] = song
    if tuple(songs) == saved.songs:
        return None
    return SavedList(saved.name, saved.saver_id, tuple(songs))


def _carry(current: SavedList, filled: SavedList) -> SavedList:
    """Artist and length, copied onto the list as it stands now."""
    known = {song.url: song for song in filled.songs}
    songs = tuple(
        song if (found := known.get(song.url)) is None else _with_meta(
            song, found.author or song.author, found.length or song.length,
        )
        for song in current.songs
    )
    return current if songs == current.songs else SavedList(current.name, current.saver_id, songs)


class _Lists:
    def _choose(self, saved: SavedList | None) -> None:
        self.opened, self.panel, self.page = saved, PLAYLISTS, 0
        self._query, self._removing = None, False

    def _close_list(self, songs: tuple[SavedSong, ...] = ()) -> None:
        for song in songs:
            self._filled.discard(song.url)
        self._choose(None)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self.paint()
        await self._edit(interaction)

    async def on_new(self, interaction: discord.Interaction) -> None:
        if not await _can_click(self, interaction):
            return
        if len(self._saved_lists()) >= PLAYLIST_LIMIT:
            await _note(interaction, f"A server keeps {PLAYLIST_LIMIT} playlists.")
            return
        await interaction.response.send_modal(NamePlaylist(self._created))

    async def _created(self, interaction: discord.Interaction, name: str) -> None:
        if not await _can_click(self, interaction):
            return
        guild_id = _guild_num(self.guild)
        if guild_id is None:
            await _defer(interaction)
            return
        self._working = True
        try:
            try:
                made = await self._make(interaction, guild_id, name)
            except Exception:
                log.exception("music playlist import failed")
                made = "Something went wrong while creating the playlist."
            made, left = made if isinstance(made, tuple) else (made, 0)
            if isinstance(made, str):
                await _note(interaction, made)
                return
            # A link opens the songs it brought. A name stays on the shelf.
            self._choose(made if made.songs else None)
            await self._show_songs(interaction)
            if left:
                await _note(interaction, f"Saved {len(made.songs)} songs. {left} did not fit.")
        finally:
            self._working = False

    async def _make(self, interaction: discord.Interaction, guild_id: int, text: str):
        query = track_query(text)
        if query is None:
            return f"This plays {platform_line()}."
        if not query:
            return "Name the playlist."
        if not is_link(query):
            return create_playlist(self.store, guild_id, query, _person(interaction.user))
        if needs_redirect(query):
            await _defer(interaction)
            followed = await _pkg().follow_redirect(query)
            if not followed:
                return "That link did not open."
            query = followed
        query = import_link(query)
        if query is None:
            return "Paste a playlist link."
        await _defer(interaction)
        found = await _pkg().find_songs(interaction.guild, query)
        if isinstance(found, str):
            return found
        name = playlist_name(found.name or "") or _spare_name(self.store, guild_id)
        songs, left = _imported(found)
        if not songs:
            return "That playlist has no songs."
        created = create_playlist(self.store, guild_id, name, _person(interaction.user), tuple(songs))
        return created if isinstance(created, str) else (created, left)

    async def on_remove_ask(self, interaction: discord.Interaction) -> None:
        if not await _can_click(self, interaction):
            return
        if self.panel != PLAYLISTS or self.opened is None:
            await _defer(interaction)
            return
        await interaction.response.send_modal(RemovePlaylist(self.on_remove_playlist))

    async def on_remove_playlist(self, interaction: discord.Interaction) -> None:
        if await self._busy(interaction):
            return
        try:
            saved, guild_id = self.opened, _guild_num(self.guild)
            if saved is None or self.panel != PLAYLISTS or guild_id is None:
                await _defer(interaction)
                return
            await interaction.response.defer()
            removed = delete_playlist(self.store, guild_id, saved.name)
            if isinstance(removed, str):
                await _note(interaction, removed)
                return
            self._choose(None)
            self.paint()
            await self._push()
        finally:
            self._working = False

    async def on_pick(self, interaction: discord.Interaction) -> None:
        # Read before the first wait. The next click replaces this menu's values.
        chosen = _selected(self.menu)
        if not await _can_click(self, interaction, closed=False):
            return
        if chosen == _BACK:
            self._choose(None)
            await self._redraw(interaction)
            return
        found = self._named_list(chosen or "")
        if isinstance(found, str):
            await _note(interaction, found)
            return
        self._choose(found)
        await self._show_songs(interaction)

    def _lists(self, box: discord.ui.Container) -> tuple[int, int, int | None, str]:
        lists = self._saved_lists()
        self._fill_menu()
        box.add_item(_row(self.menu, id=18))
        box.add_item(discord.ui.TextDisplay(pick_text(), id=17))
        return 1, len(lists), _saved_span(lists), _list_noun(len(lists))

    def _fill_menu(self) -> None:
        """The shelf leaves every name unselected. An open list shows that name."""
        lists = self._saved_lists()
        opened = self.opened.name.casefold() if self.panel == PLAYLISTS and self.opened is not None else ""
        options = [discord.SelectOption(label=PICK, value=_BACK)] if opened else []
        for saved in lists[:24 if opened else 25]:
            count = len(saved.songs)
            options.append(discord.SelectOption(
                label=saved.name, value=saved.name,
                description=f"{count} {_song_noun(count)}",
                default=saved.name.casefold() == opened,
            ))
        self.menu.placeholder, self.menu.disabled = PICK, not options
        # An empty shelf still needs one option. It stays unselected.
        self.menu.options = options or [discord.SelectOption(label="\u200b", value="none")]

    def _saved_lists(self) -> list[SavedList]:
        guild_id = _guild_num(self.guild)
        return list_playlists(self.store, guild_id) if guild_id is not None else []

    async def on_add(self, interaction: discord.Interaction) -> None:
        if not await _can_click(self, interaction):
            return
        if self.panel == PLAYLISTS and self.opened is not None:
            if self._query:
                self._query = None
                self.page = 0
                await self._redraw(interaction)
                return
            await interaction.response.send_modal(FindInList(self._narrowed))
            return
        if self.open_search is None:
            await _defer(interaction)
            return
        await interaction.response.send_modal(SearchSong(self._hand_off, title="Search"))

    async def _narrowed(self, interaction: discord.Interaction, query: str) -> None:
        if self._working:
            await _note(interaction, _BUSY)
            return
        saved = self.opened
        if saved is None or self.panel != PLAYLISTS or self.is_finished():
            await _defer(interaction)
            return
        if not self._matched(saved, query):
            await _note(interaction, f"No song in {saved.name} matches that.")
            return
        self._query = query
        self.page = 0
        await self._redraw(interaction)

    async def _hand_off(self, interaction: discord.Interaction, query: str) -> None:
        """Ask the play card to take this search. This card stays where it is."""
        opener = self.open_search
        if opener is None or self.is_finished():
            await _defer(interaction)
            return
        if self._working:
            await _note(interaction, _BUSY)
            return
        self._working = True
        try:
            await opener(interaction, query, None, fresh=True)
        except Exception:
            log.exception("music search failed")
            await _note(interaction, _FAILED)
        finally:
            self._working = False

    async def on_remove(self, interaction: discord.Interaction) -> None:
        if not await _can_click(self, interaction, closed=False):
            return
        saved = self.opened
        if self.panel != PLAYLISTS or saved is None or not saved.songs:
            await _defer(interaction)
            return
        self._removing = not self._removing
        await self._redraw(interaction)

    async def on_song(self, interaction: discord.Interaction, index: int) -> None:
        if await self._busy(interaction):
            return
        cold = False
        try:
            saved = self.opened
            if saved is None or index < 0 or index >= len(saved.songs):
                await _defer(interaction)
                return
            cold = getattr(interaction.guild, "voice_client", None) is None
            if cold:
                warm_voice(interaction.user, interaction.guild)
            await interaction.response.defer()
            opened = await _pkg().open_recordings(interaction.guild, [saved.songs[index]])
            if isinstance(opened, str):
                await _note(interaction, opened)
                return
            track, platform = opened.tracks[0]
            try:
                line = await _pkg().play_now(interaction.user, interaction.guild, track, platform)
            except Exception:
                log.exception("music playlist failed")
                line = _FAILED
            await self._after_play(interaction, line)
        finally:
            self._working = False
            if cold:
                asyncio.create_task(leave_if_idle(interaction.guild), name="music-idle")

    async def _after_play(self, interaction: discord.Interaction, line: str) -> None:
        # This button is still working, so the line watcher will not paint the card.
        if playing_note(line):
            await self._redraw(interaction)
        # The playlist stays. A song that starts playback still gets a player.
        if line.startswith("Playing "):
            opener = self.on_playing if self.on_playing is not None else self.on_started
            if opener is not None:
                await opener(interaction)
        if not _heard(line):
            await _note(interaction, line)

    async def on_save(self, interaction: discord.Interaction) -> None:
        """The star arms every + on History. Another press puts + back to play again."""
        if not await _can_click(self, interaction):
            return
        if self.panel != HISTORY or not played(self.guild):
            await _defer(interaction)
            return
        self._saving = not self._saving
        await self._redraw(interaction)

    async def on_keep(self, interaction: discord.Interaction, index: int) -> None:
        """+ asks which playlist gets this song. The menu is the server's lists."""
        if not await _can_click(self, interaction):
            return
        if not self._saving or self.panel != HISTORY:
            await _defer(interaction)
            return
        place = self._played_index(index)
        rows = played(self.guild) or []
        if place is None or place >= len(rows):
            await _note(interaction, "That song is gone.")
            self.paint()
            await self._edit(interaction)
            return
        song = _as_saved(rows[place])
        if song is None:
            await _note(interaction, "That song has no link to save.")
            return
        lists = self._saved_lists()
        if not lists:
            await _note(interaction, "There is no playlist yet.")
            return

        await interaction.response.send_modal(SaveSong(lists, lambda press, name: self._kept(press, name, song)))

    async def _kept(self, interaction: discord.Interaction, name: str, song: SavedSong) -> None:
        if self._working:
            await _note(interaction, _BUSY)
            return
        guild_id = _guild_num(self.guild)
        if guild_id is None:
            await _defer(interaction)
            return
        found = find_playlist(self.store, guild_id, name)
        if found is None:
            await _note(interaction, "That playlist is gone.")
            return
        updated = save_song(self.store, guild_id, found.name, song, _person(interaction.user))
        if isinstance(updated, str):
            await _note(interaction, updated)
            return
        if self._show_saved(updated):
            await self._push()
        await _note(interaction, f"Saved to {updated.name}.")

    def _show_saved(self, updated: SavedList) -> bool:
        """An open playlist, or the shelf, shows the song that was just saved."""
        if self.is_finished() or self.panel != PLAYLISTS:
            return False
        opened = self.opened
        if opened is not None and opened.name.casefold() != updated.name.casefold():
            return False
        if opened is not None:
            self.opened = updated
        self.paint()
        return True

    def _settle_query(self) -> None:
        """A filter that no longer matches anything returns to the whole list."""
        saved = self.opened
        if self.panel == PLAYLISTS and saved is not None and self._query and not self._matched(saved):
            self._query = None

    def _matched(self, saved: SavedList, query: str | None = None) -> list[tuple[int, SavedSong]]:
        query = self._query if query is None else query
        rows = list(enumerate(saved.songs))
        return rows if not query else [(index, song) for index, song in rows if song_matches(query, song)]

    def _open_list(self, box: discord.ui.Container) -> tuple[int, int, int | None, str]:
        saved = self.opened
        self._fill_menu()
        box.add_item(_row(self.menu, id=18))
        box.add_item(_menu_gap(20))
        visible = self._matched(saved)
        start, stop, pages = self._slice(len(visible))
        mark, method = (MARK_MINUS, "on_drop") if self._removing else (MARK_ADD, "on_song")
        site = _mixed_platforms(saved.songs)
        for offset, (index, song) in enumerate(visible[start:stop]):
            self._show_slot(box, offset, saved_row(index + 1, song, site=site), mark, method, index)
        if not visible:
            box.add_item(discord.ui.TextDisplay(empty_text(saved.name), id=16))
        songs = tuple(song for _index, song in visible)
        return pages, len(visible), _song_span(songs), _song_noun(len(visible))

    def _take_fill(self, saved: SavedList, filled: SavedList | None) -> None:
        """Write the read onto the list as it is now. A removed list closes."""
        guild_id = _guild_num(self.guild)
        if guild_id is None:
            if filled is not None:
                self.opened = filled
            return
        current = find_playlist(self.store, guild_id, saved.name)
        if current is None:
            self._close_list(saved.songs)
            return
        merged = current if filled is None else _carry(current, filled)
        if merged is not current:
            merged = replace_songs(self.store, guild_id, merged.name, merged.songs)
        if isinstance(merged, SavedList):
            self.opened = merged
        else:
            self._close_list(saved.songs)

    async def _show_songs(self, interaction: discord.Interaction) -> None:
        saved = self.opened
        listing = self.panel == PLAYLISTS and saved is not None
        if listing:
            self._slice(len(self._matched(saved)))
        missing = listing and not self._query and any(
            _bare(s) and s.url not in self._filled for s in saved.songs)
        if not missing:
            await self._redraw(interaction)
            return
        held = self._working
        self._working = True
        try:
            await _defer(interaction)
            try:
                filled = await _read_list(interaction.guild, saved, self._filled)
            except Exception:
                log.warning("music playlist could not read songs", exc_info=True)
                filled = None
            self._take_fill(saved, filled)
            await self._redraw(interaction)
        finally:
            self._working = held
