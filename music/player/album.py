"""The album behind the song that is playing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord

from announce import ACCENT_COLOR, notice_text, respond
from music import source
from music.archive import SavedSong, find_playlist, list_playlists, save_song
from music.source import _album, _spotify_track
from music.panel import (
    LABEL_ADD_ALL,
    LABEL_LIKED,
    MARK_ADD,
    MARK_BACK,
    MARK_NEXT,
    PAGE,
    _Add,
    _Close,
    _Move,
    _COVER,
    _clip,
    _hold_card,
    _head_link,
    _row,
    delete_panel,
    finish_note,
    page_line,
    show_note,
    song_label,
    song_row,
    click_answer,
    visible_note,
)
from music.playback.remember import cover_url
from music.playback import (
    _stored, find_songs, leave_if_idle, now_playing, paused_away, queue_tracks, warm_voice,
)
from music.playlist.popups import SaveSong, means_yes

log = logging.getLogger(__name__)

# No press for this long removes the album. A button starts that wait again.
ALBUM_SECONDS = 45.0
ALBUM_TITLE = "## Osaka Albums"
ALL_HINT = 'This adds every song on the album. Type "yes" to confirm'
_FAILED = "Something went wrong while starting the song."

def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value

def _record_id(value: object) -> int | None:
    return _count(getattr(value, "id", None))

def _words(value: object, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    return " ".join(value.split()) or fallback

def _web(value: object) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")) and " " not in value:
        return value
    return None

def _playlists(store: object | None, guild: object) -> list:
    guild_id = _record_id(guild)
    if store is None or guild_id is None:
        return []
    return list_playlists(store, guild_id)

def _same_recording(left: object, right: object) -> bool:
    """Same link, or the same name on another site. A missing artist still matches."""
    urls = [_web(getattr(item, "uri", None)) for item in (left, right)]
    if all(urls):
        if source.same_url(urls[0], urls[1]):
            return True
        if source.different_track(urls[0], urls[1]):
            return False
    titles = [_words(getattr(item, "title", None)).casefold() for item in (left, right)]
    if not titles[0] or titles[0] != titles[1]:
        return False
    authors = [_words(getattr(item, "author", None)).casefold() for item in (left, right)]
    return not all(authors) or authors[0] == authors[1]

def _current_track(guild: object) -> object | None:
    """The playing track. A mirrored one can drop the album, so the saved song counts too."""
    current = getattr(getattr(guild, "voice_client", None), "current", None)
    line = _stored(guild)
    saved = None if line is None or line.current is None else line.current.track
    if current is not None and _album(current):
        return current
    # The saved copy is the previous song once the live one has moved on.
    if saved is not None and _album(saved) and (current is None or _same_recording(saved, current)):
        return saved
    return current if current is not None else saved

def _first(values, read):
    for value in values:
        got = read(value)
        if got:
            return got
    return None

def _on_album(tracks: list, current: object) -> int | None:
    """The playing row. The link wins over the title, which can repeat on the album."""
    uri = _web(getattr(current, "uri", None))
    title = _words(getattr(current, "title", None)).casefold()
    named = None
    for index, track in enumerate(tracks):
        if uri and _web(getattr(track, "uri", None)) == uri:
            return index
        if named is None and title and _words(getattr(track, "title", None)).casefold() == title:
            named = index
    return named

def _album_line(name: str, artist: str, url: str | None) -> str:
    """Album name, then who made it. Wide characters take more of the line than narrow ones."""
    head = f"### {_head_link(_clip(_words(name, 'Album')), url)}"
    who = _words(artist)
    return head if not who else f"{head}\n{_clip(f'By {who}', _COVER)}"

def _cover(track: object, tracks: list) -> str | None:
    """The playing song's art, or the first picture on the album."""
    return _first((track, *tracks), lambda item: cover_url(getattr(item, "artwork", None)))

def _song(track: object) -> tuple[str, str, int]:
    return (
        _words(getattr(track, "title", None), "Unknown"),
        _words(getattr(track, "author", None)),
        _count(getattr(track, "length", None)) or 0,
    )

class _All(discord.ui.Modal):
    """yes queues every song on the album. Any other word leaves them."""

    query = discord.ui.Label(
        text="This adds every song on the album.",
        description=ALL_HINT,
        component=discord.ui.TextInput(placeholder="yes", max_length=16),
    )

    def __init__(self, apply: Callable[[discord.Interaction], Awaitable[None]]) -> None:
        super().__init__(title="Add All")
        self._apply = apply

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if means_yes(str(self.query.component)):
            await self._apply(interaction)
            return
        await _answer(interaction, ephemeral=True)

async def _answer(
    interaction: discord.Interaction, line: str | None = None, *,
    ephemeral: bool = False, view: discord.ui.LayoutView | None = None,
) -> None:
    """Defer, tell the user, or redraw. A line is a private note. A view replaces the card."""
    if line is not None:
        await respond(interaction, notice_text(line), ephemeral=True)
        return
    if view is not None:
        done = interaction.response.is_done()
        edit = interaction.edit_original_response if done else interaction.response.edit_message
        try:
            await edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("music album could not refresh", exc_info=True)
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=ephemeral)

async def _caught(call: Awaitable, note: str):
    try:
        return await call
    except Exception:
        log.exception(note)
        return None

class _AlbumCard(discord.ui.LayoutView):
    """One album: a cover, five songs a page, and a confirm before every song is queued."""

    def __init__(
        self, name: str, url: str | None, artist: str, artwork: str | None,
        tracks: list, playing: int | None, platform: str,
        guild: object | None = None, store: object | None = None,
    ) -> None:
        super().__init__(timeout=ALBUM_SECONDS)
        self.message: discord.Message | None = None
        self.name, self.url, self.artist, self.artwork = name, url, artist, artwork
        self.tracks = list(tracks)
        self.playing, self.platform = playing, platform
        self.guild, self._lists = guild, store
        self._release: Callable[[_AlbumCard], None] | None = None
        self.page = 0 if playing is None else playing // PAGE
        self._closed = False
        # Liked switches + over to save. The word stays, so the width stays.
        self._saving = False
        self._box: discord.ui.Container | None = None
        self._back = _Move("on_previous", label=MARK_BACK)
        self._next = _Move("on_next", label=MARK_NEXT)
        self._all = _Move("on_all", label=LABEL_ADD_ALL)
        self._queue = _Move("on_queue", label=LABEL_LIKED)
        self._close = _Close()
        self._adds = [_Add(slot) for slot in range(PAGE)]
        for slot, button in enumerate(self._adds):
            button.id = 50 + slot
        self.paint()

    def paint(self) -> None:
        count = len(self.tracks)
        pages = max(1, (count + PAGE - 1) // PAGE)
        self.page = min(max(0, self.page), pages - 1)
        start = self.page * PAGE
        box = discord.ui.Container(accent_colour=ACCENT_COLOR)
        title, album = ALBUM_TITLE, _album_line(self.name, self.artist, self.url)
        if self.artwork:
            box.add_item(discord.ui.Section(
                title, album,
                accessory=discord.ui.Thumbnail(
                    self.artwork, description=song_label(self.name, self.artist)[:256],
                ),
            ))
        else:
            box.add_item(discord.ui.TextDisplay(title))
            box.add_item(discord.ui.TextDisplay(album))
        box.add_item(discord.ui.Separator())
        shown = self.tracks[start:start + PAGE]
        if not shown:
            box.add_item(discord.ui.TextDisplay("This album has no songs."))
        method = "on_keep" if self._saving else "on_add"
        for offset, track in enumerate(shown):
            index = start + offset
            button = self._adds[offset]
            blocked = source.unplayable(track)
            button.index, button.label, button.method = index, MARK_ADD, method
            button.disabled = blocked
            title, author, length = _song(track)
            playing = "Playing" if self.playing == index and not blocked else ""
            box.add_item(discord.ui.Section(
                song_row(index + 1, title, author, length, playing, fit=True), accessory=button,
            ))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(_hold_card(page_line(self.page + 1, pages, count, "Track"))))
        self._back.disabled = self.page == 0
        self._next.disabled = self.page >= pages - 1
        self._all.disabled = not any(not source.unplayable(track) for track in self.tracks)
        # Blurple means + saves. Grey means + adds. The Liked label stays.
        self._queue.style = discord.ButtonStyle.primary if self._saving else discord.ButtonStyle.secondary
        box.add_item(_row(self._back, self._next, self._all, self._queue, self._close))
        if self._box is not None:
            self.remove_item(self._box)
        self._box = box
        self.add_item(box)

    async def _press(
        self, interaction: discord.Interaction, *,
        page: int | None = None, saving: bool | None = None, skip: bool = False,
    ) -> bool:
        """False when this press only gets an ack. A page or save change redraws the list."""
        if self._closed or skip:
            await _answer(interaction, ephemeral=True)
            return False
        if page is None and saving is None:
            return True
        if page is not None:
            self.page = page
        if saving is not None:
            self._saving = saving
        self.paint()
        await _answer(interaction, view=self)
        return True

    async def on_previous(self, interaction: discord.Interaction) -> None:
        await self._press(interaction, page=self.page - 1)

    async def on_next(self, interaction: discord.Interaction) -> None:
        await self._press(interaction, page=self.page + 1)

    async def on_add(self, interaction: discord.Interaction, index: int) -> None:
        blocked = 0 <= index < len(self.tracks) and source.unplayable(self.tracks[index])
        if not await self._press(interaction, skip=blocked or index < 0 or index >= len(self.tracks)):
            return
        title = _song(self.tracks[index])[0]
        # Already on a song, so this one only waits. "Starting" is for the song that begins now.
        state = now_playing(self.guild)
        starting = None if state is not None and state.has_track else f"Starting {title}"
        await self._play(interaction, [self.tracks[index]], starting)

    async def on_all(self, interaction: discord.Interaction) -> None:
        """Ask before every song on the album joins the queue."""
        playable = any(not source.unplayable(track) for track in self.tracks)
        if await self._press(interaction, skip=not playable):
            await interaction.response.send_modal(_All(self._queue_all))

    async def _queue_all(self, interaction: discord.Interaction) -> None:
        tracks = [track for track in self.tracks if not source.unplayable(track)]
        if not await self._press(interaction, skip=not tracks):
            return
        await self._play(interaction, tracks, f"Queuing {self.name or 'this album'}")

    async def _play(self, interaction: discord.Interaction, tracks: list, starting: str | None) -> None:
        line = _FAILED
        # A paused song is still current. Joining would sit there in silence.
        cold = (
            getattr(interaction.guild, "voice_client", None) is None
            and not paused_away(interaction.guild)
        )
        if cold:
            warm_voice(interaction.user, interaction.guild)
        # Queue before the note so the song does not wait on Discord.
        # finish_note would edit the album, so a song already on uses a separate note.
        queued = asyncio.create_task(
            queue_tracks(interaction.user, interaction.guild, tracks, self.platform),
        )
        answer = click_answer("album", playing=starting is None)
        if answer == "note" and starting:
            await show_note(interaction, starting)
        elif answer == "aside":
            await _answer(interaction)
        got = await _caught(queued, "music album could not queue the song")
        if got is not None:
            line = got
        # A plain add stays quiet. A full queue or a failure is its own note, beside the album.
        if starting:
            await finish_note(interaction, line)
        else:
            await _answer(interaction, visible_note(line))
        if cold and not line.startswith(("Playing ", "Added ")):
            asyncio.create_task(leave_if_idle(interaction.guild), name="music-idle")

    async def on_queue(self, interaction: discord.Interaction) -> None:
        """+ saves instead of adding. The plus mark and the Liked label both stay."""
        await self._press(interaction, saving=not self._saving)

    async def on_keep(self, interaction: discord.Interaction, index: int) -> None:
        """Save this album song into a playlist."""
        blocked = 0 <= index < len(self.tracks) and source.unplayable(self.tracks[index])
        if not await self._press(
            interaction,
            skip=blocked or not self._saving or index < 0 or index >= len(self.tracks),
        ):
            return
        track = self.tracks[index]
        url = _web(getattr(track, "uri", None))
        lists = _playlists(self._lists, self.guild) if url else []
        if not url or not lists:
            missing = "That song has no link to save." if not url else "There is no playlist yet."
            await _answer(interaction, missing)
            return
        title, author, length = _song(track)
        song = SavedSong(title, self.platform, url, author, length, not source.unplayable(track))
        await interaction.response.send_modal(SaveSong(lists, lambda press, name: self._kept(press, name, song)))

    async def _kept(self, interaction: discord.Interaction, name: str, song: object) -> None:
        guild_id = _record_id(self.guild)
        if self._lists is None or guild_id is None or find_playlist(self._lists, guild_id, name) is None:
            await _answer(interaction, "That playlist is gone.")
            return
        updated = save_song(self._lists, guild_id, name, song, _record_id(interaction.user))
        await _answer(interaction, updated if isinstance(updated, str) else f"Saved to {updated.name}.")

    async def on_close(self, interaction: discord.Interaction) -> None:
        if not await self._press(interaction):
            return
        self._drop(stop=True)
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except discord.HTTPException:
            log.warning("music album could not close", exc_info=True)

    async def on_timeout(self) -> None:
        """No button in the wait. The album message goes away."""
        if self._drop(stop=False):
            await delete_panel(self.message)

    def _drop(self, *, stop: bool) -> bool:
        if self._closed:
            return False
        self._closed = True
        if stop:
            self.stop()
        release, self._release = self._release, None
        if release is not None:
            release(self)
        return True

class _Album:
    """The album button on the player."""

    def _forget_album(self, user_id: int, card: _AlbumCard) -> None:
        held = self._albums.get(user_id)
        if held is not None and held[1] is card:
            self._albums.pop(user_id, None)

    async def _replace_album(self, user_id: int, message: object, card: _AlbumCard) -> None:
        """Keep this album and remove the one this person already had open."""
        async with self._album_lock:
            prior = self._albums.get(user_id)
            self._albums[user_id] = (message, card)
            card._release = lambda held, owner=user_id: self._forget_album(owner, held)
            if prior is None or prior[1] is card:
                return
            old_message, old_card = prior
            old_card._release = None
            old_card._drop(stop=True)
            try:
                await old_message.delete()
            except discord.HTTPException:
                log.warning("music album could not close the previous one", exc_info=True)

    async def on_info(self, interaction: discord.Interaction) -> None:
        """The album this song is on. A second press replaces the one this person already has open."""
        if not self._can_edit(interaction):
            return await _answer(interaction, "Join the voice channel first.")
        track = _current_track(self.guild)
        if track is None:
            return await _answer(interaction, "Nothing is playing.")
        album = _album(track)
        link = _spotify_track(track) if album is None or album[1] is None else None
        if link:
            await _answer(interaction, ephemeral=True)
            found = await _caught(
                find_songs(self.guild, link, "spotify", mode="track"),
                "music player could not read the album",
            )
            loaded = _album(found.tracks[0]) if found is not None and not isinstance(found, str) and found.tracks else None
            if loaded is not None and loaded[1]:
                picked = album[0] if album is not None and album[0] else loaded[0]
                track, album = found.tracks[0], (picked, loaded[1])
            elif loaded is not None and album is None:
                album = loaded
            # Lavalink can leave the album off the song and off the reload. The page still has it.
            if album is None or album[1] is None:
                released = await _caught(source.spotify_release(link), "music player could not read the album")
                if released:
                    picked = album[0] if album is not None and album[0] and album[0] != "Album" else released[0]
                    album = (picked or "Album", released[1])
        if album is None:
            return await _answer(interaction, "This song has no album.")
        name, url = album
        tracks = [track]
        platform = source.platform_key(url or str(getattr(track, "uri", "") or ""))
        if url:
            # The album list loads while Discord accepts the click.
            loading = asyncio.create_task(find_songs(self.guild, url))
            await _answer(interaction, ephemeral=True)
            found = await _caught(loading, "music player could not open the album")
            if found is None or isinstance(found, str):
                # A collection that did not open is playlist mode, not a broken song.
                line = found if isinstance(found, str) else (
                    f"{source.platform_label(url)} does not support playlist mode."
                )
                return await _answer(interaction, line)
            platform = found.platform or platform
            tracks = list(found.tracks) or tracks
            name = found.name or name
        card = _AlbumCard(
            name, url, _words(getattr(track, "author", None)), _cover(track, tracks),
            tracks, _on_album(tracks, track), platform, guild=self.guild, store=self._lists,
        )
        sent = {"view": card, "ephemeral": True, "allowed_mentions": discord.AllowedMentions.none()}
        user_id = _record_id(interaction.user)
        if url or interaction.response.is_done():
            message = await interaction.followup.send(**sent)
        else:
            await interaction.response.send_message(**sent)
            if user_id is None:
                return
            message = await interaction.original_response()
        card.message = message
        if user_id is not None:
            await self._replace_album(user_id, message, card)
