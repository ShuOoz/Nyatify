"""The search card and the genre buttons for /music play."""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable

import discord

from announce import ACCENT_COLOR, notice_text, respond
from music.lavalink import find_songs
from music.playback import now_playing, playing_note, queue_tracks
from music.commands.player import PANEL_SECONDS, delete_panel, format_clock
from music.source import (
    GENRES,
    RADIOS,
    normalize_platform,
    platform_label,
    platform_pairs,
    radio_lives,
)

log = logging.getLogger(__name__)

Started = Callable[[discord.Interaction], Awaitable[None]]
# The third value is the site already on the card. None starts a name on Apple Music.
Search = Callable[[discord.Interaction, str, str | None], Awaitable[None]]


# Five songs on a page.
PAGE = 5

TITLE = "## Osaka Play"

# Short names share the first row with the shuffle button, so a phone can keep
# that row on one line. The longer names share the next row and wrap once.
_GENRE_ROWS = (
    ("Pop", "Jazz", "Rock", "R&B"),
    ("Lo-fi", "Classical", "Hip hop", "Electronic", "Ambient"),
)

# ◀ and ▶ are emoji on the phone, so that client draws them as wide blue buttons.
MARK_BACK = "＜"
MARK_NEXT = "＞"
MARK_ADD = "＋"
MARK_ANY = "꩜"
MARK_SEARCH = "✎"
# Both words keep one Hangul filler on each side. The phone drops it.
# Player also has a halfwidth filler on each side, the same on both, so the
# word stays centered and that button is the wider one.
_PAD = "\u3164"
_HALF = "\uffa0"
LABEL_PLAYER = _PAD + _HALF + "Player" + _HALF + _PAD
LABEL_PLAYLIST = _PAD + "Queue" + _PAD
MARK_CLOSE = "⛌"


# Beside the add button, a phone line fits about this many title characters.
# A longer name wraps, so the rest is an ellipsis.
_TITLE = 32


def song_row(index: int, title: str, author: str, length: int) -> str:
    """Bold order mark, plain title, and the artist starting under the title.

    The period is escaped. Otherwise Discord reads "1. " as a list, and that
    block drops the artist below the button.
    """
    name = _clip(" ".join(title.split()) or "Unknown", _TITLE)
    head = f"**{index}\\.** {name}"
    detail = _detail(" ".join(author.split()), length)
    if not detail:
        return head
    return f"{head}\n-# {_order_gap(index)}{detail}"


def _order_gap(index: int) -> str:
    """As wide as the bold order mark and the space before the title."""
    return "\u2007" * len(str(index)) + "\u2008\u00a0"


def page_line(page: int, pages: int, count: int) -> str:
    """The page number and the result count on one line."""
    noun = "Result" if count == 1 else "Results"
    return f"Page {page}/{pages}  |  {count} {noun}"


def genre_text() -> str:
    return "**Pick a random song**"


def radio_text() -> str:
    return "**Pick a radio station**"


# The empty play card is only a chooser, so it leaves sooner than a card you keep using.
EMPTY_SECONDS = 15.0


def _detail(author: str, length: int) -> str:
    parts: list[str] = []
    if author:
        parts.append(_clip(author))
    if length > 0:
        parts.append(format_clock(length))
    return " • ".join(parts)


class _Picker(discord.ui.LayoutView):
    on_started: Started

    def __init__(self) -> None:
        super().__init__(timeout=PANEL_SECONDS)
        self.message: discord.Message | None = None
        self._working = False
        self.open_search: Search | None = None

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

    async def _ack(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()

    async def _can_search(self, interaction: discord.Interaction) -> bool:
        if self.is_finished():
            await self._ack(interaction)
            return False
        if self._working:
            await _note(interaction, "Already working on that.")
            return False
        return True

    def _pause(self) -> None:
        """Drop a click that started before this search and would paint over it."""

    def _resume(self) -> None:
        return

    async def _restore(self, interaction: discord.Interaction) -> None:
        mentions = discord.AllowedMentions.none()
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(view=self, allowed_mentions=mentions)
            else:
                await interaction.response.edit_message(view=self, allowed_mentions=mentions)
        except discord.HTTPException:
            log.warning("music search could not restore the card", exc_info=True)

    async def _hand_off(
        self,
        interaction: discord.Interaction,
        query: str,
        platform: str | None,
    ) -> None:
        opener = self.open_search
        if opener is None:
            await self._ack(interaction)
            return
        # A song may already have replaced this message. Editing it would take that away.
        if self.is_finished():
            await _note(interaction, "That card has closed.")
            return
        if self._working:
            await _note(interaction, "Already working on that.")
            return
        self._working = True
        self._pause()
        replaced = False
        try:
            replaced = (await opener(interaction, query, platform, commit=self._retire)) is True
        except Exception:
            log.exception("music search failed")
            if not interaction.response.is_done():
                await _note(interaction, "Something went wrong while starting the song.")
        self._working = False
        if replaced or self.is_finished():
            return
        self._resume()
        if interaction.response.is_done():
            await self._restore(interaction)

    def _retire(self) -> None:
        self.stop()


class _Menu(discord.ui.Select):
    def __init__(
        self,
        placeholder: str,
        method: str,
        options: list[discord.SelectOption],
        custom_id: str,
    ) -> None:
        super().__init__(
            custom_id=custom_id,
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
        )
        self.method = method

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        handler = getattr(view, self.method, None)
        if handler is not None:
            await handler(interaction)


class _AnyGenre(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label=MARK_ANY, style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, GenreList):
            await view.on_any(interaction)


class _Genre(discord.ui.Button):
    def __init__(self, label: str, phrase: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.phrase = phrase

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, GenreList):
            await view.on_genre(interaction, self.phrase)


# Discord rejects a modal whose prefilled text is longer than the field allows.
_QUERY = 4000


class SearchSong(discord.ui.Modal):
    """An artist, a song, or a link. Search again fills in the one already shown."""

    query = discord.ui.Label(
        text="Artist, song, or link",
        component=discord.ui.TextInput(placeholder="Name or link", max_length=_QUERY),
    )

    def __init__(
        self,
        apply: Callable[[discord.Interaction, str], Awaitable[None]],
        *,
        title: str,
        filled: str = "",
    ) -> None:
        super().__init__(title=title)
        self._apply = apply
        if filled:
            self.query.component.default = filled[:_QUERY]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = " ".join(str(self.query.component).split())
        if not query:
            await _note(interaction, "Name an artist or a song, or paste a link.")
            return
        await self._apply(interaction, query)


class _Search(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label=MARK_SEARCH, style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        handler = getattr(view, "on_search", None)
        if handler is not None:
            await handler(interaction)


class _Close(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label=MARK_CLOSE, style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, MatchList):
            await view.on_close(interaction)


class _Add(discord.ui.Button):
    def __init__(self, index: int) -> None:
        super().__init__(label=MARK_ADD, style=discord.ButtonStyle.secondary)
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, MatchList):
            await view.on_add(interaction, self.index)


class _Move(discord.ui.Button):
    def __init__(self, method: str, *, label: str, disabled: bool = False) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)
        self.method = method

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, MatchList):
            await getattr(view, self.method)(interaction)


class MatchList(_Picker):
    """One platform, five songs a page, and a menu to switch platform."""

    def __init__(
        self,
        query: str,
        platform: str,
        tracks: list,
        on_started: Started,
        requester_id: int | None = None,
        on_playlist: Started | None = None,
        on_playing: Started | None = None,
        note: str | None = None,
        on_search: Search | None = None,
    ) -> None:
        super().__init__()
        self.query = query
        self.platform = normalize_platform(platform)
        self.tracks = list(tracks)
        self.on_started = on_started
        self.open_playlist = on_playlist
        self.on_playing = on_playing
        self.open_search = on_search
        self.requester_id = requester_id
        self.page = 0
        self._closed = False
        self._switch = 0
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
            _platform_options(self.platform),
            "music-platform",
        )
        self.paint()

    def paint(self) -> None:
        self.platforms.placeholder = platform_label("", self.platform)
        self.platforms.options = _platform_options(self.platform)
        self._mount()

    async def on_previous(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, self.page - 1)

    async def on_next(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, self.page + 1)

    async def on_add(self, interaction: discord.Interaction, index: int) -> None:
        entries = self._entries()
        if index < 0 or index >= len(entries):
            if not interaction.response.is_done():
                await interaction.response.defer()
            return
        await self._queue(interaction, [entries[index]])

    async def on_player(self, interaction: discord.Interaction) -> None:
        if await self._busy(interaction):
            return
        try:
            if now_playing(interaction.guild) is None:
                await _note(interaction, "Nothing is playing.")
                return
            # A new message. Replacing this card would drop the search.
            await self.on_started(interaction)
        finally:
            self._working = False

    async def on_playlist(self, interaction: discord.Interaction) -> None:
        if await self._busy(interaction):
            return
        try:
            opener = self.open_playlist
            if opener is None:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                return
            await opener(interaction)
        finally:
            self._working = False

    async def on_search(self, interaction: discord.Interaction) -> None:
        # The site already chosen stays. A link still starts playing.
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
        # A platform click still in flight must not paint over the new search.
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
            log.warning("music results could not close", exc_info=True)

    async def on_platform(self, interaction: discord.Interaction) -> None:
        if self._closed:
            await self._ack(interaction)
            return
        # Read before the first wait. The next click replaces this menu's values.
        chosen = normalize_platform(_selected(self.platforms))
        self._switch += 1
        token = self._switch
        try:
            await interaction.response.defer()
            try:
                found = await find_songs(interaction.guild, self.query, chosen)
            except Exception:
                log.exception("music search failed")
                found = "Something went wrong while starting the song."
            # A newer click owns the card. Painting this answer would bring the old one back.
            if token != self._switch or self._closed:
                return
            if isinstance(found, str):
                # An empty or unanswered platform still replaces the rows. A dead node leaves them.
                if found.startswith("Nothing on ") or found.endswith("did not answer."):
                    self.platform = chosen
                    self.tracks = []
                    self.page = 0
                    self.note = found
                    self.paint()
                    await self._edit(interaction)
                    return
                await _note(interaction, found)
                return
            self.platform = found.platform
            self.tracks = list(found.tracks)
            self.page = 0
            self.note = None
            self.paint()
            await self._edit(interaction)
        except discord.HTTPException:
            log.warning("music results could not switch platform", exc_info=True)

    def _entries(self) -> list:
        return self.tracks

    def _empty(self) -> str:
        if self.note:
            return self.note
        return "No songs matched that."

    def _mount(self) -> None:
        entries = self._entries()
        start, stop, current = _page(len(entries), self.page)
        self.page = current
        pages = _page_count(len(entries))
        box = self._box
        if box is not None:
            self.remove_item(box)
        box = discord.ui.Container(accent_colour=ACCENT_COLOR)
        box.add_item(_heading(_Search()))
        box.add_item(discord.ui.Separator())
        box.add_item(_row(self.platforms))
        box.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))
        shown = entries[start:stop]
        if shown:
            for offset, track in enumerate(shown):
                index = start + offset
                box.add_item(discord.ui.Section(
                    song_row(
                        index + 1,
                        _text(getattr(track, "title", None), "Unknown"),
                        _text(getattr(track, "author", None)),
                        _length(track),
                    ),
                    accessory=_Add(index),
                ))
        else:
            box.add_item(discord.ui.TextDisplay(self._empty()))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(page_line(current + 1, pages, len(entries))))
        last = current >= pages - 1
        box.add_item(_row(
            _Move("on_previous", label=MARK_BACK, disabled=current == 0),
            _Move("on_next", label=MARK_NEXT, disabled=last),
            _Move("on_player", label=LABEL_PLAYER),
            _Move("on_playlist", label=LABEL_PLAYLIST),
            _Close(),
        ))
        self._box = box
        self.add_item(box)

    async def _turn(self, interaction: discord.Interaction, page: int) -> None:
        if self._closed:
            await self._ack(interaction)
            return
        self.page = page
        self.paint()
        await self._edit(interaction)

    async def _queue(self, interaction: discord.Interaction, tracks: list) -> None:
        if await self._busy(interaction):
            return
        try:
            await interaction.response.defer()
            try:
                line = await queue_tracks(interaction.user, interaction.guild, tracks, self.platform)
            except Exception:
                log.exception("music play failed")
                line = "Something went wrong while starting the song."
            if not playing_note(line):
                await _note(interaction, line)
                return
            await _note(interaction, line)
            # The search card stays. A new song still gets a player beside it.
            if line.startswith("Playing ") and self.on_playing is not None:
                await self.on_playing(interaction)
        finally:
            self._working = False

    async def _edit(self, interaction: discord.Interaction) -> None:
        if self._closed:
            await self._ack(interaction)
            return
        mentions = discord.AllowedMentions.none()
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(view=self, allowed_mentions=mentions)
            else:
                await interaction.response.edit_message(view=self, allowed_mentions=mentions)
        except discord.HTTPException:
            log.warning("music results could not refresh", exc_info=True)


class GenreList(_Picker):
    """A random song from a genre, or one long station."""

    def __init__(
        self,
        platform: str,
        on_started: Started,
        on_search: Search | None = None,
    ) -> None:
        super().__init__()
        self.timeout = EMPTY_SECONDS
        self.platform = normalize_platform(platform)
        self.on_started = on_started
        self.open_search = on_search
        self.rows = discord.ui.TextDisplay(genre_text())
        self.any = _AnyGenre()
        self.buttons = _genre_buttons()
        self.stations = _Menu(
            "Choose",
            "on_radio",
            [
                discord.SelectOption(label=label, value=key)
                for label, key in RADIOS
            ],
            "music-station",
        )
        box = discord.ui.Container(accent_colour=ACCENT_COLOR)
        box.add_item(_heading(_Search()))
        box.add_item(discord.ui.Separator())
        box.add_item(self.rows)
        # Five buttons stretch across the card. Ten of them are two full rows.
        controls = [self.any, *self.buttons]
        start = 0
        for size in _button_rows(len(controls)):
            box.add_item(_row(*controls[start : start + size]))
            start += size
        # Blank gap above the line. Widening the line itself would also push the radio heading down.
        box.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(radio_text()))
        box.add_item(_row(self.stations))
        self.add_item(box)

    async def on_search(self, interaction: discord.Interaction) -> None:
        # A song name replaces this chooser with the results card.
        if not await self._can_search(interaction):
            return
        await interaction.response.send_modal(SearchSong(self._searched, title="Search"))

    async def _searched(self, interaction: discord.Interaction, query: str) -> None:
        await self._hand_off(interaction, query, None)

    async def on_radio(self, interaction: discord.Interaction) -> None:
        if await self._busy(interaction):
            return
        opened = False
        try:
            key = _selected(self.stations)
            links = radio_lives(key or "")
            if not links:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                return
            await interaction.response.defer()
            start = random.randrange(len(links))
            for step in range(len(links)):
                index = (start + step) % len(links)
                link = links[index]
                try:
                    found = await find_songs(interaction.guild, link, "youtube")
                except Exception:
                    log.exception("music radio failed")
                    continue
                if isinstance(found, str) or not found.tracks:
                    continue
                station = found.tracks[0]
                started = await _queue_and_open(
                    interaction,
                    [station],
                    "youtube",
                    self.on_started,
                    self,
                    lives=links,
                    live_at=index,
                    station=key or "",
                )
                if started:
                    opened = True
                    return
            await _note(interaction, "That station did not start.")
        finally:
            self._working = False

    async def on_any(self, interaction: discord.Interaction) -> None:
        _label, phrase = random.choice(GENRES)
        await self.on_genre(interaction, phrase)

    async def on_genre(self, interaction: discord.Interaction, phrase: str) -> None:
        if await self._busy(interaction):
            return
        opened = False
        try:
            await interaction.response.defer()
            try:
                found = await find_songs(interaction.guild, phrase, self.platform, mode="draw")
            except Exception:
                log.exception("music genre search failed")
                await _note(interaction, "Something went wrong while starting the song.")
                return
            if isinstance(found, str):
                await _note(interaction, found)
                return
            opened = await _queue_and_open(
                interaction,
                [random.choice(found.tracks)],
                found.platform,
                self.on_started,
                self,
            )
        finally:
            self._working = False

    async def _dismiss(self) -> None:
        """Remove this chooser. A song that started has already taken the message."""
        message = self.message
        self.stop()
        await delete_panel(message)


def _page(count: int, page: int) -> tuple[int, int, int]:
    pages = _page_count(count)
    current = min(max(0, page), pages - 1)
    start = current * PAGE
    return start, min(count, start + PAGE), current


def _page_count(count: int) -> int:
    if count <= 0:
        return 1
    return (count + PAGE - 1) // PAGE


def _platform_options(current: str) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label=label, value=key, default=key == current)
        for key, label in platform_pairs()
    ]


def _genre_buttons() -> list[_Genre]:
    by_label = {label: phrase for label, phrase in GENRES}
    labels = [label for row in _GENRE_ROWS for label in row]
    if len(labels) != len(set(labels)) or set(labels) != set(by_label):
        raise ValueError("genre rows do not match the genre list")
    return [_Genre(label, by_label[label]) for label in labels]


def _button_rows(count: int, widest: int = 5) -> list[int]:
    """Spread buttons across rows of at most `widest`, with no row of one."""
    if count <= 0:
        return []
    rows = max(1, (count + widest - 1) // widest)
    base, extra = divmod(count, rows)
    return [base + (1 if index < extra else 0) for index in range(rows)]


def _heading(button: discord.ui.Button) -> discord.ui.Section:
    """The title and the search button share one line."""
    return discord.ui.Section(TITLE, accessory=button)


def _row(*items: discord.ui.Item) -> discord.ui.ActionRow:
    row = discord.ui.ActionRow()
    for item in items:
        row.add_item(item)
    return row


def _selected(menu: discord.ui.Select) -> str | None:
    values = menu.values
    if not values:
        return None
    value = values[0]
    if isinstance(value, str) and value:
        return value
    return None


def _length(track: object) -> int:
    value = getattr(track, "length", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _text(value: object, fallback: str = "") -> str:
    if isinstance(value, str):
        cleaned = " ".join(value.split())
        if cleaned:
            return cleaned
    return fallback


def _clip(text: str, limit: int = 80) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


async def _note(interaction: discord.Interaction, line: str) -> None:
    await respond(interaction, notice_text(line), ephemeral=True)


async def _queue_and_open(
    interaction: discord.Interaction,
    tracks: list,
    platform: str,
    on_started: Started,
    view: discord.ui.LayoutView,
    lives: tuple[str, ...] = (),
    live_at: int = 0,
    station: str = "",
) -> bool:
    try:
        line = await queue_tracks(
            interaction.user,
            interaction.guild,
            tracks,
            platform,
            lives=lives,
            live_at=live_at,
            station=station,
        )
    except Exception:
        log.exception("music play failed")
        line = "Something went wrong while starting the song."
    if not playing_note(line):
        await _note(interaction, line)
        return False
    # Stopping later drops the player, which is stored on this same message.
    view.stop()
    await on_started(interaction)
    return True
