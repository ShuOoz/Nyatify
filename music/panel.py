"""Shared music card: the words, the width, and the buttons."""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Awaitable, Callable

import discord

from announce import notice_text, quote_text, respond

log = logging.getLogger(__name__)

# Five songs on a page. The footer holds the card at 32 wide columns.
PAGE = 5
_CARD = 64
# One row beside the side button, in half a narrow letter. The order mark is included.
_LINE = 58
_BAR = 24
PANEL_SECONDS = 180.0
_QUERY = 4000

# Fullwidth marks. The phone draws ◀ and ▶ as wide buttons, so these stay one column.
MARK_BACK = "＜"
MARK_NEXT = "＞"
MARK_ADD = "＋"
MARK_ANY = "꩜"
MARK_SEARCH = "✎"
MARK_CLOSE = "⛌"
# The phone drops both fillers. Player keeps a halfwidth filler too, so that button is wider.
_PAD = "\u3164"
_HALF = "\uffa0"
LABEL_PLAYER = f"{_PAD}{_HALF}Player{_HALF}{_PAD}"
LABEL_QUEUE = f"{_PAD}Queue{_PAD}"
RADIO_MARK = "-# Radio"
_DOTS = "…"

# Narrow letters and digits. m and w are wider. The rest of the set is under one narrow letter.
_NARROW = frozenset("abcdefghijklnopqrstuvxyz0123456789")
_THIN = frozenset(" .,;:'\"!?()[]{}<>/\\|+-=*`~_")
_SILENT = discord.AllowedMentions.none()

Started = Callable[[discord.Interaction], Awaitable[None]]
# The platform is the site already on the card. None starts a name search.
Search = Callable[[discord.Interaction, str, str | None], Awaitable[None]]


def _half_slow(char: str) -> int:
    """2 narrow, 4 capital or another script, 6 emoji, 0 for a mark that does not draw."""
    category = unicodedata.category(char)
    if category in {"Mn", "Me", "Cf"}:
        return 0
    code = ord(char)
    if 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF or 0x2B00 <= code <= 0x2BFF:
        return 6
    if char in _NARROW:
        return 2
    if char in _THIN or (category == "Zs" and char != "\u2007"):
        return 1
    if char == "\u2007":
        return 2
    return 4


_ASCII_HALF = tuple(_half_slow(chr(code)) for code in range(128))
_HALF_CACHE: dict[str, int] = {}
_COL_CACHE: dict[str, int] = {}
_DOTS_W = _half_slow(_DOTS)
_BARS = tuple("━" * index + "●" + "─" * (_BAR - 1 - index) for index in range(_BAR))


def _half(char: str) -> int:
    code = ord(char)
    if code < 128:
        return _ASCII_HALF[code]
    size = _HALF_CACHE.get(char)
    if size is None:
        size = _half_slow(char)
        _HALF_CACHE[char] = size
    return size


def _clip(text: str, limit: int = _LINE) -> str:
    """Cut a line to `limit` half-columns. A line that fits is returned unchanged."""
    length = len(text)
    # Nothing is wider than 6, and nothing ASCII is wider than 4.
    if length * 6 <= limit or (length * 4 <= limit and text.isascii()):
        return text
    room = limit - _DOTS_W
    width = 0
    cut: int | None = None
    for index, char in enumerate(text):
        size = _half(char)
        if cut is None and width + size > room:
            cut = index
        width += size
        if width > limit:
            break
    if width <= limit:
        return text
    return f"{text[:0 if cut is None else cut].rstrip()}{_DOTS}"


def _columns(text: str) -> int:
    if text.isascii():
        return len(text)
    total = 0
    for char in text:
        if ord(char) < 128:
            total += 1
            continue
        unit = _COL_CACHE.get(char)
        if unit is None:
            unit = 2 if unicodedata.east_asian_width(char) in "WF" else 1
            _COL_CACHE[char] = unit
        total += unit
    return total


def _hold_card(text: str) -> str:
    """Pad out to 32 wide columns. The phone drops the filler."""
    gap = _CARD - _columns(text)
    if gap <= 0:
        return text
    return text + _PAD * (gap // 2)


def format_clock(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _name_in_title(artist: str, title: str) -> bool:
    """True when the artist is already a whole-word run inside the title."""
    name = artist.casefold().split()
    words = title.casefold().split()
    size = len(name)
    if not size or size > len(words):
        return False
    if size == 1:
        return name[0] in words
    return f" {' '.join(name)} " in f" {' '.join(words)} "


def _shown(title: str, author: str) -> tuple[str, str]:
    name = title.strip() or "Unknown"
    artist = author.strip()
    if not artist or _name_in_title(artist, name):
        return name, ""
    return name, artist


def song_label(title: str, author: str) -> str:
    name, artist = _shown(title, author)
    return f"{name} - {artist}" if artist else name


def song_line(title: str, author: str, uri: str | None) -> str:
    """Bold title, linked when the address is a normal web address. Artist on the next line."""
    name, artist = _shown(title, author)
    linked = f"**{_link(name, uri)}**"
    return f"{linked}\n{artist}" if artist else linked


def song_row(
    index: int,
    title: str,
    author: str,
    length: int,
    extra: str = "",
    sep: str = " • ",
) -> str:
    """Bold order mark, plain title, then artist and length under the title.

    The period is escaped so Discord does not read "1. " as a list and drop the line below.
    """
    prefix = f"{index}. "
    name = _clip(" ".join(title.split()) or "Unknown", _LINE - sum(_half(char) for char in prefix))
    head = f"**{index}\\.** {name}"
    parts = [
        part
        for part in (
            " ".join(author.split()),
            format_clock(length) if length > 0 else "",
            " ".join(extra.split()),
        )
        if part
    ]
    if not parts:
        return head
    gap = "\u2007" * len(str(index)) + "\u2008\u00a0"
    return f"{head}\n-# {gap}{sep.join(parts)}"


def page_line(page: int, pages: int, count: int) -> str:
    noun = "Result" if count == 1 else "Results"
    return f"Page {page}/{pages}  |  {count} {noun}"


def meta_line(requester_id: int | None, channel_id: int | None) -> str:
    parts: list[str] = []
    if requester_id is not None:
        parts.append(f"<@{requester_id}>")
    if channel_id is not None:
        parts.append(f"<#{channel_id}>")
    return " | ".join(parts)


def _link(label: str, uri: str | None) -> str:
    if (
        uri
        and uri.startswith(("http://", "https://"))
        and "[" not in label
        and "]" not in label
        and ")" not in uri
        and " " not in uri
    ):
        return f"[{label}]({uri})"
    return label


def progress_bar(position: int, length: int) -> str:
    """A text bar. A live stream has no length, so the bar stays empty."""
    last = _BAR - 1
    if length <= 0 or position <= 0:
        return _BARS[0]
    if position >= length:
        return _BARS[last]
    return _BARS[min(last, max(0, round(position / length * last)))]


def progress_text(position: int, length: int) -> str:
    if length <= 0:
        return "live"
    return f"{format_clock(position)} / {format_clock(length)}"


def progress_line(position: int, length: int) -> str:
    """Elapsed time, the bar, then the duration, on one line."""
    bar = progress_bar(position, length)
    if length <= 0:
        return f"live  {bar}"
    return f"{format_clock(position)}  {bar}  {format_clock(length)}"


async def delete_panel(message: discord.Message | None) -> None:
    """Remove a music card. A message that is already gone is left alone."""
    if message is None:
        return
    try:
        await message.delete()
    except discord.HTTPException:
        log.warning("music panel could not close", exc_info=True)


async def _note(interaction: discord.Interaction, line: str) -> None:
    await respond(interaction, notice_text(line), ephemeral=True)


async def show_note(interaction: discord.Interaction, line: str, *, quote: bool = False) -> None:
    """Put a line on screen before the slow work.

    A real interaction is answered here, so the click does not sit on a spinner.
    Anything that is not an interaction waits and sends the final line instead.
    """
    if not isinstance(interaction, discord.Interaction):
        return
    text = quote_text(notice_text(line)) if quote else notice_text(line)
    try:
        if interaction.response.is_done():
            # The deferred slash reply. A card must not call this once it is on screen.
            await interaction.edit_original_response(content=text)
        else:
            await interaction.response.send_message(content=text, ephemeral=True)
    except discord.HTTPException:
        log.warning("music could not show a note", exc_info=True)


async def finish_note(interaction: discord.Interaction, line: str) -> None:
    """Replace the line from show_note, or send it when that call did not answer."""
    if isinstance(interaction, discord.Interaction) and interaction.response.is_done():
        try:
            await interaction.edit_original_response(content=notice_text(line))
        except discord.HTTPException:
            log.warning("music could not update the note", exc_info=True)
        return
    await _note(interaction, line)


def _button_rows(count: int, widest: int = 5) -> list[int]:
    """Row sizes of at most `widest`. Spare buttons spread so a row is not left with one."""
    if count <= 0:
        return []
    rows = max(1, (count + widest - 1) // widest)
    base, extra = divmod(count, rows)
    return [base + (index < extra) for index in range(rows)]


def _row(*items: discord.ui.Item) -> discord.ui.ActionRow:
    return discord.ui.ActionRow(*items)


def _selected(menu: discord.ui.Select) -> str | None:
    values = menu.values
    if not values:
        return None
    value = values[0]
    return value if isinstance(value, str) and value else None


async def _dispatch(
    view: object,
    method: str,
    interaction: discord.Interaction,
    index: int | None = None,
) -> None:
    handler = getattr(view, method, None)
    if handler is None:
        return
    if index is None:
        await handler(interaction)
    else:
        await handler(interaction, index)


class _Card(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=PANEL_SECONDS)
        self.message: discord.Message | None = None
        self._working = False

    async def on_timeout(self) -> None:
        await delete_panel(self.message)

    async def _busy(self, interaction: discord.Interaction) -> bool:
        if self._working:
            await _note(interaction, "Already working on that.")
            return True
        self._working = True
        return False


class _SearchCard(_Card):
    """A card that can hand a search off to the slash command."""

    on_started: Started

    def __init__(self) -> None:
        super().__init__()
        self.open_search: Search | None = None

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
        edit = (
            interaction.edit_original_response
            if interaction.response.is_done()
            else interaction.response.edit_message
        )
        try:
            await edit(view=self, allowed_mentions=_SILENT)
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
        replaced = False
        try:
            self._pause()
            replaced = (await opener(interaction, query, platform, commit=self._retire)) is True
        except Exception:
            log.exception("music search failed")
            if not interaction.response.is_done():
                await _note(interaction, "Something went wrong while starting the song.")
        finally:
            # Unlock even if the search is cancelled or the error note fails.
            # Leave a replaced or stopped card paused so an old click cannot paint over it.
            self._working = False
            if not replaced and not self.is_finished():
                self._resume()
        if replaced or self.is_finished():
            return
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
        await _dispatch(self.view, self.method, interaction)


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


class _Press(discord.ui.Button):
    def __init__(
        self,
        label: str,
        method: str,
        index: int | None = None,
        *,
        disabled: bool = False,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        super().__init__(label=label, style=style, disabled=disabled)
        self.method = method
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        await _dispatch(self.view, self.method, interaction, self.index)


class _Search(_Press):
    def __init__(self) -> None:
        super().__init__(MARK_SEARCH, "on_search")


class _Close(_Press):
    def __init__(self) -> None:
        super().__init__(MARK_CLOSE, "on_close")


class _Add(_Press):
    def __init__(self, index: int) -> None:
        super().__init__(MARK_ADD, "on_add", index)


class _Move(_Press):
    def __init__(self, method: str, *, label: str, disabled: bool = False) -> None:
        super().__init__(label, method, disabled=disabled)


class _Mark(_Press):
    pass
