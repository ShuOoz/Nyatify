"""The live line and a saved playlist for /music playlist."""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable

import discord

from announce import ACCENT_COLOR, notice_text, respond
from music.lavalink import Opened, open_recordings
from music.library import PAGE, SavedList, SavedSong, page_bounds, save_song
from music.playback import (
    drop_queued,
    load_line,
    member_in_voice,
    play_now,
    playing_note,
    queued,
)
from music.commands.player import PANEL_SECONDS, RADIO_MARK, delete_panel

log = logging.getLogger(__name__)

Started = Callable[[discord.Interaction], Awaitable[None]]


def line_text(
    titles: list[str],
    playing: set[int],
    page: int,
    radios: set[int] | None = None,
) -> str:
    start, stop, current = page_bounds(len(titles), page)
    lines = ["**Line**"]
    if not titles:
        lines.append("Nothing is left in the line.")
        return "\n".join(lines)
    stations = radios or set()
    for index in range(start, stop):
        title = titles[index]
        if index in playing:
            title = f"**{title}**"
        row = f"{index + 1}. {title}"
        if index in stations:
            row = f"{row}\n{RADIO_MARK}"
        lines.append(row)
    pages = max(1, (len(titles) + PAGE - 1) // PAGE)
    if pages > 1:
        lines.append(f"Page {current + 1} of {pages}")
    return "\n".join(lines)


def saved_text(saved: SavedList, page: int) -> str:
    lines = [f"**{saved.name}**"]
    if saved.saver_id is not None:
        lines.append(f"<@{saved.saver_id}>")
    if not saved.songs:
        lines.append(f"{saved.name} has no songs.")
        return "\n".join(lines)
    start, stop, current = page_bounds(len(saved.songs), page)
    for index in range(start, stop):
        lines.append(f"{index + 1}. {saved.songs[index].title}")
    pages = max(1, (len(saved.songs) + PAGE - 1) // PAGE)
    if pages > 1:
        lines.append(f"Page {current + 1} of {pages}")
    return "\n".join(lines)


class _Hold(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=PANEL_SECONDS)
        self.message: discord.Message | None = None
        self._working = False

    async def on_timeout(self) -> None:
        await delete_panel(self.message)

    def _claim(self) -> bool:
        if self._working:
            return False
        self._working = True
        return True

    async def _busy(self, interaction: discord.Interaction) -> bool:
        if self._claim():
            return False
        await _note(interaction, "Already working on that.")
        return True


class _Button(discord.ui.Button):
    def __init__(self, label: str, method: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.method = method

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        handler = getattr(view, self.method, None)
        if handler is not None:
            await handler(interaction)


class _Menu(discord.ui.Select):
    def __init__(self, placeholder: str, method: str, custom_id: str) -> None:
        super().__init__(
            custom_id=custom_id,
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="…", value="0")],
        )
        self.method = method

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        handler = getattr(view, self.method, None)
        if handler is not None:
            await handler(interaction)


class SaveSong(discord.ui.Modal, title="Save song"):
    playlist = discord.ui.Label(
        text="Playlist",
        component=discord.ui.TextInput(placeholder="Evening", max_length=80),
    )

    def __init__(self, store: object, guild_id: int, song: SavedSong, saver_id: int | None) -> None:
        super().__init__()
        self.store = store
        self.guild_id = guild_id
        self.song = song
        self.saver_id = saver_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.playlist.component)
        saved = save_song(self.store, self.guild_id, name, self.song, self.saver_id)
        if isinstance(saved, str):
            await _note(interaction, saved)
            return
        await _note(interaction, f"Saved {self.song.title} to {saved.name}.")


class LiveLine(_Hold):
    """The songs lined up now. Each one can be removed or saved."""

    def __init__(self, guild: object, store: object) -> None:
        super().__init__()
        self.guild = guild
        self.store = store
        self.page = 0
        self._box: discord.ui.Container | None = None
        self.body = discord.ui.TextDisplay("…")
        self.back = _Button("Back", "on_back")
        self.forward = _Button("Next", "on_forward")
        self.remove = _Menu("Remove", "on_remove", "music-line-remove")
        self.save = _Menu("Save", "on_save", "music-line-save")
        self.paint()

    def paint(self) -> None:
        rows = queued(self.guild) or []
        titles = [row.title for row in rows]
        playing = {index for index, row in enumerate(rows) if row.playing}
        radios = {index for index, row in enumerate(rows) if row.stream}
        self.page = page_bounds(len(titles), self.page)[2]
        self.body.content = line_text(titles, playing, self.page, radios)
        start, stop, _page = page_bounds(len(titles), self.page)
        options = _options(titles[start:stop], start)
        if options:
            self.remove.options = options
            self.save.options = options
        self._mount(len(titles), bool(options))

    async def on_back(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, -1)

    async def on_forward(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, 1)

    async def on_remove(self, interaction: discord.Interaction) -> None:
        if await self._busy(interaction):
            return
        try:
            if not member_in_voice(interaction.user, self.guild):
                await _note(interaction, "Join the voice channel first.")
                return
            index = _chosen(self.remove, len(queued(self.guild) or []))
            if index is None:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                return
            await interaction.response.defer()
            note = await drop_queued(self.guild, index)
            if note:
                await _note(interaction, note)
                return
            self.paint()
            await self._refresh(interaction)
        finally:
            self._working = False

    async def on_save(self, interaction: discord.Interaction) -> None:
        if await self._busy(interaction):
            return
        try:
            rows = queued(self.guild) or []
            index = _chosen(self.save, len(rows))
            if index is None:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                return
            row = rows[index]
            if not row.url:
                await _note(interaction, "That song has no link to save.")
                return
            song = SavedSong(row.title, row.platform, row.url)
            guild_id = getattr(self.guild, "id", None)
            if isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0:
                await _note(interaction, "Use this command in a server.")
                return
            modal = SaveSong(self.store, guild_id, song, _user_id(interaction.user))
            await interaction.response.send_modal(modal)
        finally:
            self._working = False

    async def _turn(self, interaction: discord.Interaction, step: int) -> None:
        self.page += step
        self.paint()
        try:
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning("music line could not turn the page", exc_info=True)

    def _mount(self, count: int, menus: bool) -> None:
        box = self._box
        if box is not None:
            self.remove_item(box)
        box = discord.ui.Container(accent_colour=ACCENT_COLOR)
        box.add_item(self.body)
        if count > PAGE:
            box.add_item(_row(self.back, self.forward))
        if menus:
            box.add_item(_row(self.remove))
            box.add_item(_row(self.save))
        self._box = box
        self.add_item(box)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.edit_original_response(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning("music line could not refresh", exc_info=True)


class SavedPlaylist(_Hold):
    """Play a saved list in order, shuffled, or one song now."""

    def __init__(self, saved: SavedList, on_started: Started) -> None:
        super().__init__()
        self.saved = saved
        self.on_started = on_started
        self.page = 0
        self._box: discord.ui.Container | None = None
        self.body = discord.ui.TextDisplay("…")
        self.back = _Button("Back", "on_back")
        self.forward = _Button("Next", "on_forward")
        self.play = _Button("Play", "on_play")
        self.shuffle = _Button("Shuffle", "on_shuffle")
        self.one = _Menu("Play one", "on_one", "music-saved-play")
        self.paint()

    def paint(self) -> None:
        titles = [song.title for song in self.saved.songs]
        self.page = page_bounds(len(titles), self.page)[2]
        self.body.content = saved_text(self.saved, self.page)
        start, stop, _page = page_bounds(len(titles), self.page)
        options = _options(titles[start:stop], start)
        if options:
            self.one.options = options
        self._mount(len(titles), bool(options))

    async def on_back(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, -1)

    async def on_forward(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, 1)

    async def on_play(self, interaction: discord.Interaction) -> None:
        await self._load(interaction, shuffle=False)

    async def on_shuffle(self, interaction: discord.Interaction) -> None:
        await self._load(interaction, shuffle=True)

    async def on_one(self, interaction: discord.Interaction) -> None:
        if await self._busy(interaction):
            return
        try:
            index = _chosen(self.one, len(self.saved.songs))
            if index is None:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                return
            await interaction.response.defer()
            opened = await open_recordings(interaction.guild, [self.saved.songs[index]])
            if isinstance(opened, str):
                await _note(interaction, opened)
                return
            track, platform = opened.tracks[0]
            line = await play_now(interaction.user, interaction.guild, track, platform)
            if not playing_note(line):
                await _note(interaction, line)
                return
            self.stop()
            await self.on_started(interaction)
        finally:
            self._working = False

    async def _load(self, interaction: discord.Interaction, *, shuffle: bool) -> None:
        if await self._busy(interaction):
            return
        try:
            if not self.saved.songs:
                await _note(interaction, f"{self.saved.name} has no songs.")
                return
            await interaction.response.defer()
            songs = list(self.saved.songs)
            if shuffle:
                random.shuffle(songs)
            opened = await open_recordings(interaction.guild, songs)
            if isinstance(opened, str):
                await _note(interaction, opened)
                return
            line = await load_line(interaction.user, interaction.guild, opened.tracks)
            if not playing_note(line):
                await _note(interaction, line)
                return
            if opened.skipped:
                await _note(interaction, _skipped(line, opened))
            self.stop()
            await self.on_started(interaction)
        finally:
            self._working = False

    async def _turn(self, interaction: discord.Interaction, step: int) -> None:
        self.page += step
        self.paint()
        try:
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning("music playlist could not turn the page", exc_info=True)

    def _mount(self, count: int, menus: bool) -> None:
        box = self._box
        if box is not None:
            self.remove_item(box)
        box = discord.ui.Container(accent_colour=ACCENT_COLOR)
        box.add_item(self.body)
        if count > PAGE:
            box.add_item(_row(self.back, self.forward))
        if menus:
            box.add_item(_row(self.play, self.shuffle))
            box.add_item(_row(self.one))
        self._box = box
        self.add_item(box)


def _options(titles: list[str], start: int) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label=_clip(f"{start + offset + 1}. {title}"), value=str(start + offset))
        for offset, title in enumerate(titles)
    ]


def _row(*items: discord.ui.Item) -> discord.ui.ActionRow:
    row = discord.ui.ActionRow()
    for item in items:
        row.add_item(item)
    return row


def _chosen(menu: discord.ui.Select, count: int) -> int | None:
    values = menu.values
    if not values:
        return None
    raw = values[0]
    if not isinstance(raw, str) or not raw.isdigit():
        return None
    index = int(raw)
    if index >= count:
        return None
    return index


def _user_id(user: object) -> int | None:
    value = getattr(user, "id", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _clip(text: str, limit: int = 100) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _skipped(line: str, opened: Opened) -> str:
    names = ", ".join(opened.skipped)
    return f"{line} Skipped {names}."


async def _note(interaction: discord.Interaction, line: str) -> None:
    await respond(interaction, notice_text(line), ephemeral=True)
