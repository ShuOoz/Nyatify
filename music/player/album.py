"""The album behind the song that is playing."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable

import discord

from announce import ACCENT_COLOR, notice_text, respond
from music import source
from music.panel import (
    LABEL_ADD_ALL,
    LABEL_LIKED,
    MARK_ADD,
    MARK_BACK,
    MARK_CLOSE,
    MARK_NEXT,
    PAGE,
    _Add,
    _Close,
    _Move,
    _clip,
    _hold_card,
    _link,
    _row,
    delete_panel,
    finish_note,
    page_line,
    show_note,
    visible_note,
    song_label,
    song_row,
)
from music.playback import (
    find_songs,
    leave_if_idle,
    now_playing,
    queue_tracks,
    warm_voice,
)

log = logging.getLogger(__name__)

# No press for this long removes the album. A button starts that wait again.
ALBUM_SECONDS = 45.0
ALBUM_TITLE = "## Osaka Albums"
ALL_HINT = 'This adds every song on the album. Type "yes" to confirm'


def _record_id(value: object) -> int | None:
    got = getattr(value, "id", None)
    if isinstance(got, bool) or not isinstance(got, int) or got <= 0:
        return None
    return got


def _playlists(store: object | None, guild: object) -> list:
    from music.archive import list_playlists

    guild_id = _record_id(guild)
    if store is None or guild_id is None:
        return []
    return list_playlists(store, guild_id)


def _words(value: object, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    return " ".join(value.split()) or fallback


def _span(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return 0
    return value


def _web(value: object) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")) and " " not in value:
        return value
    return None


def _saved_track(guild: object) -> object | None:
    from music.playback.remember import _stored

    line = _stored(guild)
    if line is None or line.current is None:
        return None
    return line.current.track


def _same_recording(left: object, right: object) -> bool:
    """True when these are the same song. A mirror can drop the artist."""
    left_title = _words(getattr(left, "title", None)).casefold()
    right_title = _words(getattr(right, "title", None)).casefold()
    if not left_title or left_title != right_title:
        return False
    left_author = _words(getattr(left, "author", None)).casefold()
    right_author = _words(getattr(right, "author", None)).casefold()
    return not left_author or not right_author or left_author == right_author


def _current_track(guild: object) -> object | None:
    """The playing track. A mirrored one can drop the album, so the saved song counts too."""
    player = getattr(guild, "voice_client", None)
    current = getattr(player, "current", None)
    saved = _saved_track(guild)
    if current is not None and _album(current):
        return current
    # The saved copy is the previous song once the live one has moved on.
    if (
        saved is not None
        and _album(saved)
        and (current is None or _same_recording(saved, current))
    ):
        return saved
    return current if current is not None else saved


# Spotify puts this address in the track bytes. The JSON field next to it is often empty.
# Spotify album ids are 22 characters. A longer run would swallow the bytes that follow.
_ALBUM_IN_TRACK = re.compile(rb"https://open\.spotify\.com/album/([A-Za-z0-9]{22})")


def _album_page(value: object) -> str | None:
    """An album address we can open. A picture or a song link is not one."""
    if not isinstance(value, str):
        return None
    page = source.spotify_album(value)
    if page:
        return page
    web = _web(value)
    if web is None:
        return None
    query = source.track_query(web)
    if not query or source.request_mode(query) != "playlist":
        return None
    return query


def _plugin(track: object) -> dict:
    raw = getattr(track, "raw_data", None)
    plugin = raw.get("pluginInfo") if isinstance(raw, dict) else None
    return plugin if isinstance(plugin, dict) else {}


def _plugin_album(track: object) -> tuple[str, str | None]:
    plugin = _plugin(track)
    nested = plugin.get("album")
    name = _words(plugin.get("albumName"))
    if not name and isinstance(nested, str):
        name = _words(nested)
    url = _album_page(plugin.get("albumUrl")) or _album_page(plugin.get("albumURI"))
    if isinstance(nested, dict):
        name = name or _words(nested.get("name"))
        url = url or _album_page(nested.get("url"))
        links = nested.get("external_urls")
        if isinstance(links, dict):
            url = url or _album_page(links.get("spotify"))
    return name, url


def _playlist_album(track: object) -> tuple[str, str | None]:
    playlist = getattr(track, "playlist", None)
    if playlist is None:
        return "", None
    if str(getattr(playlist, "type", "") or "").casefold() != "album":
        return "", None
    return _words(getattr(playlist, "name", None)), _album_page(getattr(playlist, "url", None))


def _encoded_album(track: object) -> str | None:
    """The album address stored in the track bytes."""
    encoded = getattr(track, "encoded", None)
    if not isinstance(encoded, str) or len(encoded) < 16:
        return None
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(padded, validate=False)
    except Exception:
        return None
    found = _ALBUM_IN_TRACK.search(raw)
    if found is None:
        return None
    return f"https://open.spotify.com/album/{found.group(1).decode()}"


def _album(track: object) -> tuple[str, str | None] | None:
    """Album name and address.

    Lavalink often leaves its album fields empty. An Apple song address still
    carries the album, as /album/{name}/{id}?i={song}. A Spotify song keeps the
    album address in the track bytes when the JSON field is only a picture.
    """
    album = getattr(track, "album", None)
    name = _words(getattr(album, "name", None))
    url = _album_page(getattr(album, "url", None))
    plugin_name, plugin_url = _plugin_album(track)
    list_name, list_url = _playlist_album(track)
    name = name or plugin_name or list_name
    url = url or plugin_url or list_url
    uri = getattr(track, "uri", None)
    linked = source.apple_album(uri) if isinstance(uri, str) else None
    if linked:
        slug, address = linked
        name = name or slug
        if url is None or "?i=" in url:
            url = address
    if url is None:
        url = _encoded_album(track)
    if not name and url is None:
        return None
    return name or "Album", url


def _spotify_track(track: object) -> str | None:
    """The song's Spotify address, when this recording is one."""
    for value in (getattr(track, "uri", None), _plugin(track).get("save_uri")):
        if not isinstance(value, str):
            continue
        query = source.track_query(value)
        if query and source.platform_key(query) == "spotify" and "/track/" in query:
            return query
    if getattr(track, "source", None) != "spotify":
        return None
    ident = getattr(track, "identifier", None)
    if isinstance(ident, str) and ident and ident != "local":
        return f"https://open.spotify.com/track/{ident}"
    return None


async def _fill_spotify_album(
    guild: object,
    track: object,
    album: tuple[str, str | None] | None,
) -> tuple[object, tuple[str, str | None] | None]:
    """Load a Spotify song once when the playing copy has no album address."""
    if album is not None and album[1]:
        return track, album
    link = _spotify_track(track)
    if link is None:
        return track, album
    found = await find_songs(guild, link, "spotify", mode="track")
    if isinstance(found, str) or not found.tracks:
        return track, album
    loaded = _album(found.tracks[0])
    if loaded is None or not loaded[1]:
        return track, album
    name = album[0] if album is not None and album[0] else loaded[0]
    return found.tracks[0], (name, loaded[1])


def _on_album(tracks: list, current: object) -> int | None:
    uri = _web(getattr(current, "uri", None))
    title = _words(getattr(current, "title", None)).casefold()
    if uri:
        for index, track in enumerate(tracks):
            if _web(getattr(track, "uri", None)) == uri:
                return index
    if not title:
        return None
    for index, track in enumerate(tracks):
        if _words(getattr(track, "title", None)).casefold() == title:
            return index
    return None


def _album_line(name: str, artist: str, url: str | None) -> str:
    """Album name, then who made it. Wide characters take more of the line than narrow ones."""
    head = f"### {_link(_clip(' '.join(name.split()) or 'Album'), url)}"
    who = " ".join(artist.split())
    if not who:
        return head
    return f"{head}\nBy {_clip(who)}"


def _cover(track: object, tracks: list) -> str | None:
    """The playing song's art, or the first picture on the album."""
    art = _web(getattr(track, "artwork", None))
    if art:
        return art
    for item in tracks:
        art = _web(getattr(item, "artwork", None))
        if art:
            return art
    return None


def _album_window(count: int, page: int) -> tuple[int, int, int, int]:
    pages = max(1, (count + PAGE - 1) // PAGE)
    current = min(max(0, page), pages - 1)
    start = current * PAGE
    return start, min(count, start + PAGE), current, pages


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
        from music.playlist.popups import means_yes

        if not means_yes(str(self.query.component)):
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return
        await self._apply(interaction)


async def _beside(interaction: discord.Interaction, line: str) -> None:
    """Tell the person without touching the album message.

    A plain add stays quiet. A full queue or a failure is its own note.
    """
    shown = visible_note(line)
    if shown is None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        return
    await respond(interaction, notice_text(shown), ephemeral=True)


class _AlbumCard(discord.ui.LayoutView):
    """One album: a cover, five songs a page, and a confirm before every song is queued."""

    def __init__(
        self,
        name: str,
        url: str | None,
        artist: str,
        artwork: str | None,
        tracks: list,
        playing: int | None,
        platform: str,
        guild: object | None = None,
        store: object | None = None,
    ) -> None:
        super().__init__(timeout=ALBUM_SECONDS)
        self.message: discord.Message | None = None
        self.name = name
        self.url = url
        self.artist = artist
        self.artwork = artwork
        self.tracks = list(tracks)
        self.playing = playing
        self.platform = platform
        self.guild = guild
        self._lists = store
        self._release: Callable[[_AlbumCard], None] | None = None
        self.page = 0 if playing is None else playing // PAGE
        self._closed = False
        self._working = False
        # Liked switches + over to save. The word stays, so the width stays.
        self._saving = False
        self._box: discord.ui.Container | None = None
        self._back = _Move("on_previous", label=MARK_BACK)
        self._next = _Move("on_next", label=MARK_NEXT)
        self._all = _Move("on_all", label=LABEL_ADD_ALL)
        self._queue = _Move("on_queue", label=LABEL_LIKED)
        self._close = _Close()
        self._adds = []
        for slot in range(PAGE):
            button = _Add(slot)
            button.id = 50 + slot
            self._adds.append(button)
        self.paint()

    def paint(self) -> None:
        start, stop, current, pages = _album_window(len(self.tracks), self.page)
        self.page = current
        shown = self.tracks[start:stop]
        box = discord.ui.Container(accent_colour=ACCENT_COLOR)
        title = discord.ui.TextDisplay(ALBUM_TITLE)
        album = discord.ui.TextDisplay(_album_line(self.name, self.artist, self.url))
        if self.artwork:
            box.add_item(discord.ui.Section(
                title,
                album,
                accessory=discord.ui.Thumbnail(
                    self.artwork,
                    description=song_label(self.name, self.artist)[:256],
                ),
            ))
        else:
            box.add_item(title)
            box.add_item(album)
        box.add_item(discord.ui.Separator())
        if shown:
            for offset, track in enumerate(shown):
                index = start + offset
                button = self._adds[offset]
                button.index = index
                button.label = MARK_ADD
                button.method = "on_keep" if self._saving else "on_add"
                box.add_item(discord.ui.Section(
                    discord.ui.TextDisplay(song_row(
                        index + 1,
                        _words(getattr(track, "title", None), "Unknown"),
                        _words(getattr(track, "author", None)),
                        _span(getattr(track, "length", None)),
                        "Playing" if self.playing == index else "",
                        fit=True,
                    )),
                    accessory=button,
                ))
        else:
            box.add_item(discord.ui.TextDisplay("This album has no songs."))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(
            _hold_card(page_line(current + 1, pages, len(self.tracks), "Track")),
        ))
        self._back.disabled = current == 0
        self._next.disabled = current >= pages - 1
        self._all.disabled = not self.tracks
        # Blurple means + saves. Grey means + adds. The Liked label stays.
        self._queue.style = (
            discord.ButtonStyle.primary if self._saving else discord.ButtonStyle.secondary
        )
        box.add_item(_row(self._back, self._next, self._all, self._queue, self._close))
        if self._box is not None:
            self.remove_item(self._box)
        self._box = box
        self.add_item(box)

    async def on_previous(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, self.page - 1)

    async def on_next(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, self.page + 1)

    async def _turn(self, interaction: discord.Interaction, page: int) -> None:
        if self._closed or self._working:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return
        self.page = page
        self.paint()
        await self._show(interaction)

    async def _show(self, interaction: discord.Interaction) -> None:
        edit = (
            interaction.edit_original_response
            if interaction.response.is_done()
            else interaction.response.edit_message
        )
        try:
            await edit(view=self, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("music album could not refresh", exc_info=True)

    async def on_add(self, interaction: discord.Interaction, index: int) -> None:
        if self._closed or index < 0 or index >= len(self.tracks):
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return
        if self._working:
            await respond(interaction, notice_text("Already working on that."), ephemeral=True)
            return
        title = _words(getattr(self.tracks[index], "title", None), "that song")
        # Already on a song, so this one only waits. "Starting" is for the song that begins now.
        state = now_playing(self.guild)
        starting = None if state is not None and state.has_track else f"Starting {title}"
        await self._play(interaction, [self.tracks[index]], starting)

    async def on_all(self, interaction: discord.Interaction) -> None:
        """Ask before every song on the album joins the queue."""
        if self._closed or self._working or not self.tracks:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return
        await interaction.response.send_modal(_All(self._queue_all))

    async def _queue_all(self, interaction: discord.Interaction) -> None:
        if self._closed or not self.tracks:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return
        if self._working:
            await respond(interaction, notice_text("Already working on that."), ephemeral=True)
            return
        await self._play(interaction, list(self.tracks), f"Queuing {self.name or 'this album'}")

    async def _play(self, interaction: discord.Interaction, tracks: list, starting: str | None) -> None:
        self._working = True
        line = "Something went wrong while starting the song."
        cold = getattr(interaction.guild, "voice_client", None) is None
        if cold:
            warm_voice(interaction.user, interaction.guild)
        # Queueing starts before the "Starting…" line, so the song does not wait on it.
        queued = asyncio.create_task(queue_tracks(
            interaction.user, interaction.guild, tracks, self.platform,
        ))
        try:
            if starting:
                await show_note(interaction, starting)
            elif not interaction.response.is_done():
                # This button sits on the album. A deferred update acknowledges
                # the click and leaves that message where it is.
                await interaction.response.defer()
            try:
                line = await queued
            except Exception:
                log.exception("music album could not queue the song")
            if starting:
                await finish_note(interaction, line)
            else:
                # finish_note edits or deletes the original response. After a
                # button ack, that response is the album, so a plain add would
                # close it and an error would replace it.
                await _beside(interaction, line)
            if cold and not line.startswith(("Playing ", "Added ")):
                asyncio.create_task(leave_if_idle(interaction.guild), name="music-idle")
        finally:
            self._working = False

    async def on_queue(self, interaction: discord.Interaction) -> None:
        """+ saves instead of adding. The plus mark and the Liked label both stay."""
        if self._closed or self._working:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return
        self._saving = not self._saving
        self.paint()
        await self._show(interaction)

    async def on_keep(self, interaction: discord.Interaction, index: int) -> None:
        """Save this album song into a playlist."""
        if self._closed or not self._saving or index < 0 or index >= len(self.tracks):
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return
        from music.archive import SavedSong
        from music.playlist.popups import SaveSong

        track = self.tracks[index]
        url = _web(getattr(track, "uri", None))
        if url is None:
            await respond(interaction, notice_text("That song has no link to save."), ephemeral=True)
            return
        song = SavedSong(
            _words(getattr(track, "title", None), "Unknown"),
            self.platform,
            url,
            _words(getattr(track, "author", None)),
            _span(getattr(track, "length", None)),
        )
        lists = _playlists(self._lists, self.guild)
        if not lists:
            await respond(interaction, notice_text("There is no playlist yet."), ephemeral=True)
            return
        await interaction.response.send_modal(SaveSong(lists, lambda press, name: self._kept(press, name, song)))

    async def _kept(self, interaction: discord.Interaction, name: str, song: object) -> None:
        from music.archive import find_playlist, save_song

        guild_id = _record_id(self.guild)
        if self._lists is None or guild_id is None or find_playlist(self._lists, guild_id, name) is None:
            await respond(interaction, notice_text("That playlist is gone."), ephemeral=True)
            return
        updated = save_song(self._lists, guild_id, name, song, _record_id(interaction.user))
        if isinstance(updated, str):
            await respond(interaction, notice_text(updated), ephemeral=True)
            return
        await respond(interaction, notice_text(f"Saved to {updated.name}."), ephemeral=True)

    async def on_close(self, interaction: discord.Interaction) -> None:
        if self._closed:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return
        self._closed = True
        self.stop()
        self._let_go()
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except discord.HTTPException:
            log.warning("music album could not close", exc_info=True)

    async def on_timeout(self) -> None:
        """No button in the wait. The album message goes away."""
        if self._closed:
            return
        self._closed = True
        self._let_go()
        await delete_panel(self.message)

    def _let_go(self) -> None:
        release = self._release
        self._release = None
        if release is not None:
            release(self)


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
            old_card._closed = True
            old_card.stop()
            try:
                await old_message.delete()
            except discord.HTTPException:
                log.warning("music album could not close the previous one", exc_info=True)

    async def on_info(self, interaction: discord.Interaction) -> None:
        """The album this song is on. Only the person who asked sees it.

        Pressing it again removes the album panel already open for them.
        """
        if not self._inside(interaction):
            await respond(interaction, notice_text("Join the voice channel first."), ephemeral=True)
            return
        track = _current_track(self.guild)
        if track is None:
            await respond(interaction, notice_text("Nothing is playing."), ephemeral=True)
            return
        album = _album(track)
        if (album is None or album[1] is None) and _spotify_track(track):
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            try:
                track, album = await _fill_spotify_album(self.guild, track, album)
            except Exception:
                log.exception("music player could not read the album")
        if album is None:
            await respond(interaction, notice_text("This song has no album."), ephemeral=True)
            return
        name, url = album
        tracks = [track]
        platform = source.platform_key(url or str(getattr(track, "uri", "") or ""))
        if url:
            # The album list loads while Discord accepts the click.
            loading = asyncio.create_task(find_songs(self.guild, url))
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            try:
                found = await loading
            except Exception:
                log.exception("music player could not open the album")
                await respond(
                    interaction,
                    notice_text("Something went wrong while opening the album."),
                    ephemeral=True,
                )
                return
            if isinstance(found, str):
                await respond(interaction, notice_text(found), ephemeral=True)
                return
            platform = found.platform or platform
            if found.tracks:
                tracks = list(found.tracks)
            if found.name:
                name = found.name
        card = _AlbumCard(
            name,
            url,
            _words(getattr(track, "author", None)),
            _cover(track, tracks),
            tracks,
            _on_album(tracks, track),
            platform,
            guild=self.guild,
            store=self._lists,
        )
        mentions = discord.AllowedMentions.none()
        sent = {"view": card, "ephemeral": True, "allowed_mentions": mentions}
        user_id = _record_id(interaction.user)
        if url or interaction.response.is_done():
            message = await interaction.followup.send(**sent)
        else:
            await interaction.response.send_message(**sent)
            if user_id is None:
                return
            message = await interaction.original_response()
        card.message = message
        if user_id is None:
            return
        await self._replace_album(user_id, message, card)
