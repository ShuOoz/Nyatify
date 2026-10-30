"""The discover card: a genre, a radio station, or a saved list."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import discord

from announce import ACCENT_COLOR
from music.archive import list_playlists
from music.panel import (
    MARK_ANY,
    PANEL_SECONDS,
    Search,
    SearchSong,
    Started,
    _Menu,
    _Search,
    _SearchCard,
    _button_rows,
    _note,
    _row,
    _selected,
)
from music.playback import leave_if_idle, playing_note, warm_voice
from music.source import GENRES, RADIOS, normalize_platform, station_videos

log = logging.getLogger(__name__)

# The empty card is only a chooser, so it leaves sooner than a card you keep using.
EMPTY_SECONDS = 15.0
TITLE = "## Osaka Play"
PICK_SONG = "**Pick a random song**"
PICK_RADIO = "**Pick a radio station**"
PICK_LIST = "**Pick a playlist**"

# Spotify refreshes a genre list about once a week, so one open lasts the day.
_DAY = 24 * 60 * 60
_kept: dict[str, tuple[float, object]] = {}

# Short names stay on the first row, so a phone can keep that row on one line.
_GENRE_ROWS = (
    ("Pop", "Jazz", "Rock", "R&B"),
    ("Lo-fi", "Classical", "Hip hop", "Electronic", "Ambient"),
)


def _pkg():
    import sys
    return sys.modules["music.play"]


class _Pick(discord.ui.Button):
    """A genre button. No list means any genre."""

    def __init__(self, label: str, phrase: str | None = None) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.phrase = phrase

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, GenreList):
            return
        if self.phrase is None:
            await view.on_any(interaction)
            return
        await view.on_genre(interaction, self.phrase)


def _genre_buttons() -> list[_Pick]:
    phrases = dict(GENRES)
    labels = [label for row in _GENRE_ROWS for label in row]
    if set(labels) != set(phrases) or len(labels) != len(phrases):
        raise ValueError("genre rows do not match the genre list")
    return [_Pick(label, phrases[label]) for label in labels]


def _saved_list(url: str):
    """The songs opened earlier today. A later press does not read the list again."""
    row = _kept.get(url)
    if row is None:
        return None
    opened, found = row
    if time.monotonic() - opened >= _DAY:
        _kept.pop(url, None)
        return None
    return found


def _saved_lists(guild: object | None, store: object | None) -> list:
    guild_id = getattr(guild, "id", None)
    if store is None or isinstance(guild_id, bool) or not isinstance(guild_id, int):
        return []
    return list_playlists(store, guild_id)


def _list_options(lists: list) -> list[discord.SelectOption]:
    if not lists:
        return [discord.SelectOption(label="\u200b", value="none")]
    return [
        discord.SelectOption(
            label=saved.name,
            value=saved.name,
            description=f"{len(saved.songs)} {'song' if len(saved.songs) == 1 else 'songs'}",
        )
        for saved in lists[:25]
    ]


def _keep_trying(line: str) -> bool:
    """Another video might play. A queue, a full line, or a room error applies to all of them."""
    if playing_note(line) or line.endswith("The queue is full."):
        return False
    if "voice channel" in line or line.startswith("Use this command"):
        return False
    return line != "Something went wrong while starting the song."


async def _queue(
    interaction: discord.Interaction,
    tracks: list,
    platform: str,
    **extra,
) -> str:
    try:
        return await _pkg().queue_tracks(
            interaction.user, interaction.guild, tracks, platform, **extra,
        )
    except Exception:
        log.exception("music play failed")
        return "Something went wrong while starting the song."


async def _play_or_note(
    interaction: discord.Interaction,
    tracks: list,
    platform: str,
    on_started: Started,
    **extra,
) -> None:
    line = await _queue(interaction, tracks, platform, **extra)
    # Joining the queue leaves this card up. Starting playback opens the player.
    if not playing_note(line) or line.startswith("Added "):
        await _note(interaction, line)
        return
    await on_started(interaction)


class GenreList(_SearchCard):
    """A random song from a genre, a station, or a saved list."""

    def __init__(
        self,
        platform: str,
        on_started: Started,
        on_search: Search | None = None,
        guild: object | None = None,
        store: object | None = None,
    ) -> None:
        super().__init__()
        self.timeout = EMPTY_SECONDS
        self.platform = normalize_platform(platform)
        self.on_started = on_started
        self.open_search = on_search
        self.guild = guild
        self.store = store
        self.rows = discord.ui.TextDisplay(PICK_SONG)
        self.any = _Pick(MARK_ANY)
        self.buttons = _genre_buttons()
        self.stations = _Menu(
            "Choose",
            "on_radio",
            [discord.SelectOption(label=label, value=key) for label, key in RADIOS],
            "music-station",
        )
        saved = _saved_lists(guild, store)
        self.lists = _Menu("Pick a playlist", "on_list", _list_options(saved), "music-play-playlist")
        self.lists.disabled = not saved
        self._mount()

    def _mount(self) -> None:
        box = discord.ui.Container(accent_colour=ACCENT_COLOR)
        box.add_item(discord.ui.Section(TITLE, accessory=_Search()))
        box.add_item(discord.ui.Separator())
        box.add_item(self.rows)
        controls = [self.any, *self.buttons]
        start = 0
        for size in _button_rows(len(controls)):
            box.add_item(_row(*controls[start:start + size]))
            start += size
        # A blank gap, then the rule. Widening the rule would push the radio heading down.
        box.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(PICK_RADIO))
        box.add_item(_row(self.stations))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(PICK_LIST))
        box.add_item(_row(self.lists))
        self.add_item(box)

    @asynccontextmanager
    async def _session(self, interaction: discord.Interaction):
        """Hold the card open for one start, then let it idle again.

        Yields a function that joins voice early, or None when another start is already running.
        """
        if await self._busy(interaction):
            yield None
            return
        self.timeout = PANEL_SECONDS
        cold = False

        def arm() -> None:
            nonlocal cold
            if getattr(interaction.guild, "voice_client", None) is not None:
                return
            cold = True
            warm_voice(interaction.user, interaction.guild)

        try:
            yield arm
        finally:
            self._working = False
            if not self.is_finished():
                self.timeout = EMPTY_SECONDS
            if cold:
                asyncio.create_task(leave_if_idle(interaction.guild), name="music-idle")

    def _chosen_list(self, name: str | None):
        key = (name or "").casefold()
        return next(
            (item for item in _saved_lists(self.guild, self.store) if item.name.casefold() == key),
            None,
        )

    async def on_search(self, interaction: discord.Interaction) -> None:
        if not await self._can_search(interaction):
            return
        # The box stays open after this click. The idle timer would delete the card first.
        self.timeout = PANEL_SECONDS
        await interaction.response.send_modal(SearchSong(self._searched, title="Search"))

    async def _searched(self, interaction: discord.Interaction, query: str) -> None:
        try:
            await self._hand_off(interaction, query, None)
        finally:
            if not self.is_finished():
                self.timeout = EMPTY_SECONDS

    async def on_list(self, interaction: discord.Interaction) -> None:
        async with self._session(interaction) as arm:
            if arm is None:
                return
            try:
                saved = self._chosen_list(_selected(self.lists))
                if saved is None:
                    await self._ack(interaction)
                    return
                if not saved.songs:
                    if not interaction.response.is_done():
                        await interaction.response.defer()
                    await _note(interaction, f"{saved.name} has no songs.")
                    return
                arm()
                self.lists.placeholder = "Starting…"
                self.lists.disabled = True
                await self._restore(interaction)
                try:
                    line = await _pkg().play_saved(
                        interaction.user,
                        interaction.guild,
                        list(saved.songs),
                        replace=True,
                    )
                except Exception:
                    log.exception("music playlist failed")
                    line = "Something went wrong while starting the song."
                if not playing_note(line):
                    await _note(interaction, line)
                    return
                if " Skipped " in line:
                    await _note(interaction, line)
                await self.on_started(interaction)
            finally:
                self.lists.placeholder = "Pick a playlist"
                self.lists.disabled = not bool(_saved_lists(self.guild, self.store))
                await self._restore(interaction)

    async def on_radio(self, interaction: discord.Interaction) -> None:
        async with self._session(interaction) as arm:
            if arm is None:
                return
            try:
                key = _selected(self.stations)
                links = station_videos(key or "")
                if not links:
                    if not interaction.response.is_done():
                        await interaction.response.defer()
                    return
                arm()
                self.stations.placeholder = "Starting…"
                self.stations.disabled = True
                await self._restore(interaction)
                line = await self._station(interaction, key or "", links)
                if line:
                    await _note(interaction, line)
            finally:
                self.stations.placeholder = "Choose"
                self.stations.disabled = False
                await self._restore(interaction)

    async def _station(
        self,
        interaction: discord.Interaction,
        key: str,
        links: tuple[str, ...],
    ) -> str | None:
        """Play one video from the station. None means the player opened."""
        start = _pkg().random.randrange(len(links))
        for step in range(len(links)):
            index = (start + step) % len(links)
            track = await self._video(interaction, links[index])
            if track is None:
                continue
            line = await _queue(
                interaction,
                [track],
                "youtube",
                videos=links,
                video_at=index,
                station=key,
            )
            if line.startswith("Playing "):
                await self.on_started(interaction)
                return None
            if _keep_trying(line):
                continue
            return line
        return "That station did not start."

    async def _video(self, interaction: discord.Interaction, link: str):
        try:
            found = await _pkg().find_songs(interaction.guild, link, "youtube")
        except Exception:
            log.exception("music radio failed")
            return None
        if isinstance(found, str) or not found.tracks:
            return None
        return found.tracks[0]

    async def _genre_list(self, guild: object, url: str):
        """The weekly list. A list opened earlier today is not read again."""
        found = _saved_list(url)
        if found is not None:
            return found
        try:
            found = await _pkg().find_songs(guild, url)
        except Exception:
            log.exception("music genre list failed")
            return "Something went wrong while starting the song."
        if not isinstance(found, str):
            _kept[url] = (time.monotonic(), found)
        return found

    async def on_any(self, interaction: discord.Interaction) -> None:
        async with self._session(interaction) as arm:
            if arm is None:
                return
            arm()
            self.rows.content = "Picking a song…"
            try:
                await self._restore(interaction)
                # One song from the weekly lists, whichever genre it belongs to.
                opened = await asyncio.gather(
                    *(self._genre_list(interaction.guild, url) for _label, url in GENRES)
                )
                songs = []
                platform = "spotify"
                trouble = "Nothing on Spotify matched that."
                for found in opened:
                    if isinstance(found, str):
                        trouble = found
                        continue
                    platform = found.platform
                    songs.extend(found.tracks)
                if not songs:
                    await _note(interaction, trouble)
                    return
                await _play_or_note(
                    interaction,
                    [_pkg().random.choice(songs)],
                    platform,
                    self.on_started,
                )
            finally:
                self.rows.content = PICK_SONG
                await self._restore(interaction)

    async def on_genre(self, interaction: discord.Interaction, phrase: str) -> None:
        async with self._session(interaction) as arm:
            if arm is None:
                return
            arm()
            self.rows.content = "Picking a song…"
            try:
                await self._restore(interaction)
                found = await self._genre_list(interaction.guild, phrase)
                if isinstance(found, str):
                    await _note(interaction, found)
                    return
                await _play_or_note(
                    interaction,
                    [_pkg().random.choice(found.tracks)],
                    found.platform,
                    self.on_started,
                )
            finally:
                self.rows.content = PICK_SONG
                await self._restore(interaction)
