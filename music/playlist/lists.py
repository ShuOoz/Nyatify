"""The Playlist tab: the shelf of saved lists, and the songs in the open one."""

from __future__ import annotations

import asyncio
import logging

import discord

from music.archive import (
    NAME_LIMIT, PLAYLIST_LIMIT, SONG_LIMIT, SavedList, SavedSong,
    create_playlist, delete_playlist, find_playlist, list_playlists,
    playlist_name, replace_songs, save_song, song_matches,
)
from music.lavalink import find_songs, open_recordings
from music.panel import MARK_ADD, SearchSong, _note, _row, _selected
from music.playback import leave_if_idle, paused_away, played, playing_note, queue_tracks, warm_voice
from music.playback.remember import warm_saved
from music.playlist.popups import FindInList, NamePlaylist, RemovePlaylist, SaveSong
from music.playlist.card import (
    HISTORY, MARK_MINUS, PICK, PLAYLISTS, _READ_AT_ONCE,
    _as_saved, _defer, _guild_num, _heard, _list_noun, _menu_gap, _mixed_platforms,
    _person, _saved_span, _song_noun, _song_span, _track_author, _track_length,
    empty_text, pick_text, saved_row,
)
from music.source import (
    canonical_url, import_link, is_link, kept_url, needs_redirect, platform_line,
    remembered_share, track_query, follow_redirect,
)

log = logging.getLogger(__name__)

# Longer than any saved name, so Pick a playlist never selects a list called "pick".
_BACK = "pick" + ("\u200b" * (NAME_LIMIT + 1 - len("pick")))
_FAILED = "Something went wrong while starting the song."


async def _can_click(view, interaction: discord.Interaction, *, closed: bool = True) -> bool:
    if closed and view.is_finished():
        await _defer(interaction)
        return False
    return True


def _free_name(store: object, guild_id: int, wanted: str) -> str:
    """`wanted`, or `wanted 2` when that name is already a playlist."""
    taken = {saved.name.casefold() for saved in list_playlists(store, guild_id)}
    name, number = wanted, 2
    while name.casefold() in taken:
        suffix = f" {number}"
        name = f"{wanted[: NAME_LIMIT - len(suffix)].rstrip()}{suffix}"
        number += 1
    return name


def _imported(found: object) -> tuple[list[SavedSong], int]:
    """Unique songs with a link, then how many unique songs did not fit."""
    tracks = getattr(found, "tracks", None)
    if not isinstance(tracks, list):
        return [], 0
    platform = getattr(found, "platform", None)
    site = platform if isinstance(platform, str) else "youtube"
    songs, seen = [], set()
    for track in tracks:
        raw = remembered_share(track) or getattr(track, "uri", None)
        url = kept_url(raw) if isinstance(raw, str) and raw.startswith(("http://", "https://")) else ""
        ident = canonical_url(url) if url else ""
        if not ident or ident in seen:
            continue
        seen.add(ident)
        if len(songs) >= SONG_LIMIT:
            continue
        title = getattr(track, "title", None)
        title = " ".join(title.split()) if isinstance(title, str) and title.strip() else "Unknown"
        songs.append(SavedSong(title, site, url, _track_author(track), _track_length(track)))
    return songs, len(seen) - len(songs)


def _bare(song: SavedSong) -> bool:
    return not song.author or song.length <= 0


def _with_meta(song: SavedSong, author: str, length: int) -> SavedSong:
    # A blank artist or length does not wipe what is already stored.
    author, length = author or song.author, length or song.length
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
                found = await find_songs(guild, song.url, song.platform, mode="track")
            except Exception:
                log.warning("music playlist could not read a song", exc_info=True)
                found = None
        if not found or isinstance(found, str) or not found.tracks:
            return song
        track = found.tracks[0]
        return _with_meta(song, _track_author(track), _track_length(track))

    fresh = await asyncio.gather(*(one(saved.songs[i]) for i in bare))
    songs = list(saved.songs)
    for index, song in zip(bare, fresh):
        if _bare(song):
            seen.add(songs[index].url)
        songs[index] = song
    if tuple(songs) == saved.songs:
        return None
    return SavedList(saved.name, saved.saver_id, tuple(songs))


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
            await self._release()

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
            followed = await follow_redirect(query)
            if not followed:
                return "That link did not open."
            query = followed
        query = import_link(query)
        if query is None:
            return "Paste a playlist link."
        await _defer(interaction)
        found = await find_songs(interaction.guild, query)
        if isinstance(found, str):
            return found
        songs, left = _imported(found)
        if not songs:
            return "That playlist has no songs."
        # A taken name becomes "Name 2". The songs still land on a new list.
        name = _free_name(self.store, guild_id, playlist_name(found.name or "") or "Playlist")
        created = create_playlist(self.store, guild_id, name, _person(interaction.user), tuple(songs))
        return created if isinstance(created, str) else (created, left)

    async def on_remove_ask(self, interaction: discord.Interaction) -> None:
        if not await _can_click(self, interaction):
            return
        if not await self._in_room(interaction):
            return
        saved = self.opened
        if self.panel != PLAYLISTS or saved is None:
            await _defer(interaction)
            return
        # The box keeps this name. Another list can open before it is sent.
        name = saved.name
        await interaction.response.send_modal(
            RemovePlaylist(lambda press: self.on_remove_playlist(press, name)),
        )

    async def on_remove_playlist(self, interaction: discord.Interaction, name: str) -> None:
        if await self._busy(interaction):
            return
        try:
            if not await self._in_room(interaction):
                return
            guild_id = _guild_num(self.guild)
            if guild_id is None:
                await _defer(interaction)
                return
            await interaction.response.defer()
            removed = delete_playlist(self.store, guild_id, name)
            if isinstance(removed, str):
                await _note(interaction, removed)
                return
            opened = self.opened
            if self.panel == PLAYLISTS and opened is not None and opened.name.casefold() == removed.name.casefold():
                self._choose(None)
            self.paint()
            await self._push()
        finally:
            await self._release()

    async def on_pick(self, interaction: discord.Interaction) -> None:
        # Read before the first wait. The next click replaces this menu's values.
        chosen = _selected(self.menu)
        if not await _can_click(self, interaction, closed=False):
            return
        if chosen == _BACK:
            found = None
        else:
            found = self._named_list(chosen or "")
            if isinstance(found, str):
                await _note(interaction, found)
                return
        self._choose(found)
        await self._show_songs(interaction)

    def _lists(self, box: discord.ui.Container) -> tuple[int, int, int | None, str]:
        lists = self._saved_lists()
        self._fill_menu(box)
        box.add_item(discord.ui.TextDisplay(pick_text(), id=17))
        return 1, len(lists), _saved_span(lists), _list_noun(len(lists))

    def _fill_menu(self, box: discord.ui.Container) -> None:
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
        box.add_item(_row(self.menu, id=18))

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
        self._begin_press()
        try:
            await opener(interaction, query, None, new_message=True)
        except Exception:
            log.exception("music search failed")
            await _note(interaction, _FAILED)
        finally:
            await self._release()

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
            # A paused song is still current. Joining would sit there in silence.
            cold = (
                getattr(interaction.guild, "voice_client", None) is None
                and not paused_away(interaction.guild)
            )
            if cold:
                warm_voice(interaction.user, interaction.guild)
            await interaction.response.defer()
            opened = await open_recordings(interaction.guild, [saved.songs[index]])
            if isinstance(opened, str):
                await _note(interaction, opened)
                return
            track, platform = opened.tracks[0]
            try:
                # Plus lines this song up. It does not cut off the one on now.
                line = await queue_tracks(
                    interaction.user, interaction.guild, [track], platform,
                )
            except Exception:
                log.exception("music playlist failed")
                line = _FAILED
            await self._after_play(interaction, line)
        finally:
            await self._release()
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
        warm_saved(self.guild, song)
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
        self._fill_menu(box)
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
        merged = current
        if filled is not None:
            # Artist and length, copied onto the list as it stands now.
            known = {song.url: song for song in filled.songs}
            songs = tuple(
                song if (found := known.get(song.url)) is None else _with_meta(song, found.author, found.length)
                for song in current.songs
            )
            if songs != current.songs:
                merged = SavedList(current.name, current.saver_id, songs)
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
            if not held:
                await self._catch_line()
