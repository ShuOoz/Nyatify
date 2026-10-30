"""The search card after a song name or a link."""

from __future__ import annotations

import asyncio
import logging

import discord

from announce import ACCENT_COLOR
from music.panel import (
    LABEL_PLAYER,
    LABEL_QUEUE,
    MARK_BACK,
    MARK_NEXT,
    PAGE,
    Search,
    SearchSong,
    Started,
    _Add,
    _Close,
    _Menu,
    _Move,
    _Search,
    _SearchCard,
    _hold_card,
    _note,
    _row,
    _selected,
    finish_note,
    page_line,
    show_note,
    song_row,
)
from music.playback import leave_if_idle, warm_voice
from music.source import normalize_platform, platform_label, platform_pairs

log = logging.getLogger(__name__)

TITLE = "## Osaka Play"


def _pkg():
    import sys
    return sys.modules["music.play"]


def _window(count: int, page: int) -> tuple[int, int, int, int]:
    pages = max(1, (count + PAGE - 1) // PAGE)
    current = min(max(0, page), pages - 1)
    start = current * PAGE
    return start, min(count, start + PAGE), current, pages


def _options(current: str) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label=label, value=key, default=key == current)
        for key, label in platform_pairs()
    ]


def _plain(value: object, fallback: str = "") -> str:
    if isinstance(value, str):
        cleaned = " ".join(value.split())
        if cleaned:
            return cleaned
    return fallback


def _ms(track: object) -> int:
    value = getattr(track, "length", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _swappable(line: str) -> bool:
    """An empty or silent site can replace the rows. Anything else leaves them."""
    return line.startswith("Nothing on ") or line.endswith("did not answer.")


class MatchList(_SearchCard):
    """One site, five songs a page, and a menu to switch site."""

    def __init__(
        self,
        query: str,
        platform: str,
        tracks: list,
        on_started: Started,
        requester_id: int | None = None,
        on_queue: Started | None = None,
        on_playing: Started | None = None,
        note: str | None = None,
        on_search: Search | None = None,
        on_player: Started | None = None,
    ) -> None:
        super().__init__()
        self.query = query
        self.platform = normalize_platform(platform)
        self.tracks = list(tracks)
        self.on_started = on_started
        self.open_queue = on_queue
        self.on_playing = on_playing
        self.open_search = on_search
        self.open_player = on_player
        self.requester_id = requester_id
        self.page = 0
        self._closed = False
        self._switch = 0
        self._pending = 0
        self._box: discord.ui.Container | None = None
        if tracks:
            self.note = None
        elif note:
            self.note = note
        else:
            self.note = f"Nothing on {platform_label('', self.platform)} matched that."
        self.platforms = _Menu(
            platform_label("", self.platform),
            "on_platform",
            _options(self.platform),
            "music-platform",
        )
        # The same controls stay on the card. Turning the page only changes the songs.
        self.platforms.id = 19
        self._search = _Search()
        self._search.id = 4
        self._back = _Move("on_previous", label=MARK_BACK)
        self._back.id = 63
        self._next = _Move("on_next", label=MARK_NEXT)
        self._next.id = 64
        self._player = _Move("on_player", label=LABEL_PLAYER)
        self._player.id = 65
        self._queue_button = _Move("on_queue", label=LABEL_QUEUE)
        self._queue_button.id = 66
        self._close = _Close()
        self._close.id = 67
        self._adds = []
        for slot in range(PAGE):
            button = _Add(slot)
            button.id = 50 + slot
            self._adds.append(button)
        self.paint()

    def paint(self) -> None:
        self.platforms.placeholder = platform_label("", self.platform)
        self.platforms.options = _options(self.platform)
        start, stop, current, pages = _window(len(self.tracks), self.page)
        self.page = current
        shown = self.tracks[start:stop]
        box = discord.ui.Container(accent_colour=ACCENT_COLOR, id=1)
        box.add_item(discord.ui.Section(
            discord.ui.TextDisplay(TITLE, id=3),
            accessory=self._search,
            id=2,
        ))
        box.add_item(discord.ui.Separator(id=5))
        box.add_item(_row(self.platforms, id=6))
        box.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small, id=7))
        if shown:
            for offset, track in enumerate(shown):
                index = start + offset
                button = self._adds[offset]
                button.index = index
                box.add_item(discord.ui.Section(
                    discord.ui.TextDisplay(
                        song_row(
                            index + 1,
                            _plain(getattr(track, "title", None), "Unknown"),
                            _plain(getattr(track, "author", None)),
                            _ms(track),
                        ),
                        id=40 + offset,
                    ),
                    accessory=button,
                    id=30 + offset,
                ))
        else:
            box.add_item(discord.ui.TextDisplay(self.note or "No songs matched that.", id=16))
        box.add_item(discord.ui.Separator(id=60))
        box.add_item(discord.ui.TextDisplay(
            _hold_card(page_line(current + 1, pages, len(self.tracks))),
            id=61,
        ))
        self._back.disabled = current == 0
        self._next.disabled = current >= pages - 1
        box.add_item(_row(
            self._back,
            self._next,
            self._player,
            self._queue_button,
            self._close,
            id=62,
        ))
        if self._box is not None:
            self.remove_item(self._box)
        self._box = box
        self.add_item(box)

    async def _show(self, interaction: discord.Interaction) -> None:
        if self._closed:
            await self._ack(interaction)
            return
        await self._restore(interaction)

    async def on_previous(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, self.page - 1)

    async def on_next(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, self.page + 1)

    async def _turn(self, interaction: discord.Interaction, page: int) -> None:
        # A site change replaces these rows. Turning the page would paint the old ones back.
        if self._closed or self._pending:
            await self._ack(interaction)
            return
        self.page = page
        self.paint()
        await self._show(interaction)

    async def on_add(self, interaction: discord.Interaction, index: int) -> None:
        if index < 0 or index >= len(self.tracks):
            if not interaction.response.is_done():
                await interaction.response.defer()
            return
        # The site can change while this song joins the queue. Keep the one on the row.
        await self._queue(interaction, self.tracks[index], self.platform)

    async def _queue(self, interaction: discord.Interaction, track: object, platform: str) -> None:
        if await self._busy(interaction):
            return
        cold = getattr(interaction.guild, "voice_client", None) is None
        if cold:
            warm_voice(interaction.user, interaction.guild)
        title = _plain(getattr(track, "title", None), "that song")
        line = "Something went wrong while starting the song."
        try:
            # Already on a song, so this one only waits. "Starting" is for the song that begins now.
            state = _pkg().now_playing(interaction.guild)
            if state is None or not state.has_track:
                await show_note(interaction, f"Starting {title}")
            elif not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            try:
                line = await _pkg().queue_tracks(
                    interaction.user, interaction.guild, [track], platform,
                )
            except Exception:
                log.exception("music play failed")
            await finish_note(interaction, line)
            if line.startswith("Playing ") and self.on_playing is not None:
                await self.on_playing(interaction)
            if cold and not line.startswith(("Playing ", "Added ")):
                asyncio.create_task(leave_if_idle(interaction.guild), name="music-idle")
        finally:
            self._working = False

    async def on_player(self, interaction: discord.Interaction) -> None:
        if await self._busy(interaction):
            return
        try:
            state = _pkg().now_playing(interaction.guild)
            if state is None or not state.has_track:
                await _note(interaction, "Nothing is playing.")
                return
            # The Player button asks for the card. Starting a song uses on_started.
            opener = self.open_player if self.open_player is not None else self.on_started
            await opener(interaction)
        finally:
            self._working = False

    async def on_queue(self, interaction: discord.Interaction) -> None:
        if await self._busy(interaction):
            return
        try:
            if self.open_queue is None:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                return
            await self.open_queue(interaction)
        finally:
            self._working = False

    async def on_search(self, interaction: discord.Interaction) -> None:
        if not await self._can_search(interaction):
            return
        await interaction.response.send_modal(
            SearchSong(self._searched, title="Search again", filled=self.query),
        )

    async def _searched(self, interaction: discord.Interaction, query: str) -> None:
        await self._hand_off(interaction, query, self.platform)

    def _pause(self) -> None:
        self._closed = True
        self._switch += 1

    def _resume(self) -> None:
        self._closed = False

    def _retire(self) -> None:
        # A site click still in flight must not paint over the new search.
        self._closed = True
        self._switch += 1
        super()._retire()

    async def on_close(self, interaction: discord.Interaction) -> None:
        if self._closed:
            await self._ack(interaction)
            return
        self._closed = True
        self.stop()
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except discord.HTTPException:
            log.warning("music search could not close", exc_info=True)

    async def on_platform(self, interaction: discord.Interaction) -> None:
        if self._closed:
            await self._ack(interaction)
            return
        chosen = _selected(self.platforms)
        if chosen is None:
            await self._ack(interaction)
            return
        chosen = normalize_platform(chosen)
        self._switch += 1
        token = self._switch
        self._pending += 1
        try:
            await interaction.response.defer()
            found = await self._find(interaction, chosen)
            if token != self._switch or self._closed:
                return
            if isinstance(found, str) and not _swappable(found):
                await _note(interaction, found)
            else:
                self._take(found, chosen)
            if token != self._switch or self._closed:
                return
            self.paint()
            await self._show(interaction)
        except discord.HTTPException:
            log.warning("music search could not switch platform", exc_info=True)
        finally:
            self._pending -= 1

    async def _find(self, interaction: discord.Interaction, platform: str):
        try:
            return await _pkg().find_songs(interaction.guild, self.query, platform)
        except Exception:
            log.exception("music search failed")
            return "Something went wrong while starting the song."

    def _take(self, found: object, chosen: str) -> None:
        if isinstance(found, str):
            self.platform = chosen
            self.tracks = []
            self.note = found
        else:
            self.platform = found.platform
            self.tracks = list(found.tracks)
            self.note = None
        self.page = 0
