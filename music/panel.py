"""Shared music card: the words, the width, and the buttons."""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from collections.abc import Awaitable, Callable
from functools import cache

import discord

from announce import notice_text, quote_text, respond

log = logging.getLogger(__name__)

# Five songs on a page. The footer holds the card at 32 wide columns.
PAGE = 5
_CARD = 64
# One row beside the side button, in half a narrow letter. The order mark is included.
_LINE = 58
PANEL_SECONDS = 180.0
_QUERY = 4000

# Fullwidth marks. The phone draws ◀ and ▶ as wide buttons, so these stay one column.
MARK_BACK = "＜"
MARK_NEXT = "＞"
MARK_ADD = "＋"
MARK_ANY = "꩜"
MARK_SEARCH = "✎"
MARK_CLOSE = "⛌"
# The history card and the player use this to save the current song.
MARK_SAVE = "\u2726"
# The queue uses this to add a saved playlist.
MARK_LIST = "\u2261"
# The phone drops both fillers. Player keeps a halfwidth filler too, so that button is wider.
_PAD = "\u3164"
_HALF = "\uffa0"
LABEL_PLAYER = f"{_PAD}{_HALF}Player{_HALF}{_PAD}"
LABEL_QUEUE = f"{_PAD}Queue{_PAD}"
# Album only. Add All queues every song. Liked turns + into save.
LABEL_ADD_ALL = f"{_PAD}{_HALF}Add All{_HALF}{_PAD}"
LABEL_LIKED = f"{_PAD}Liked{_PAD}"
RADIO_MARK = "-# Radio"
_DOTS = "…"
_FULL = "The queue is full."
_FULL_SUFFIX = f" {_FULL}"

# Narrow letters and digits. m and w are wider. The rest of the set is under one narrow letter.
_NARROW = frozenset("abcdefghijklnopqrstuvxyz0123456789")
_THIN = frozenset(" .,;:'\"!?()[]{}<>/\\|+-=*`~_")

@cache
def _half(char: str) -> int:
    """2 narrow, 4 capital or another script, 6 emoji, 0 for a mark that does not draw."""
    category = unicodedata.category(char)
    if category in {"Mn", "Me", "Cf"}:
        return 0
    code = ord(char)
    if 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF or 0x2B00 <= code <= 0x2BFF:
        return 6
    if char in _NARROW or char == "\u2007":
        return 2
    if char in _THIN or category == "Zs":
        return 1
    return 4


def _width(text: str) -> int:
    return sum(_half(char) for char in text)


def _clip(text: str, limit: int = _LINE) -> str:
    """Cut a line to `limit` half-columns. A line that fits is returned unchanged."""
    room = limit - _half(_DOTS)
    width = cut = 0
    for index, char in enumerate(text):
        width += _half(char)
        if width > limit:
            return f"{text[:cut].rstrip()}{_DOTS}"
        if width <= room:
            cut = index + 1
    return text


def _columns(text: str) -> int:
    if text.isascii():
        return len(text)
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _hold_card(text: str) -> str:
    """Pad out to 32 wide columns. The phone drops the filler."""
    gap = _CARD - _columns(text)
    return text if gap <= 0 else text + _PAD * (gap // 2)


def progress_text(position: int, length: int) -> str:
    if length <= 0:
        return "live"
    return f"{format_clock(position)} / {format_clock(length)}"


def format_clock(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _name_in_title(artist: str, title: str) -> bool:
    """True when the artist is already a whole-word run inside the title."""
    name = " ".join(artist.casefold().split())
    words = " ".join(title.casefold().split())
    return bool(name) and f" {name} " in f" {words} "


def _shown(title: str, author: str) -> tuple[str, str]:
    name = title.strip() or "Unknown"
    artist = author.strip()
    if artist and not _name_in_title(artist, name):
        return name, artist
    return name, ""


def song_label(title: str, author: str) -> str:
    name, artist = _shown(title, author)
    return f"{name} - {artist}" if artist else name


def song_line(title: str, author: str, uri: str | None) -> str:
    """Title as a heading, linked when the address is a normal web address. Artist under it.

    A wide character takes more of the line than a narrow one.
    """
    name, artist = _shown(title, author)
    head = f"### {_link(_clip(name), uri)}"
    return head if not artist else f"{head}\n{_clip(artist)}"


def song_row(
    index: int,
    title: str,
    author: str,
    length: int,
    extra: str = "",
    sep: str = " • ",
    *,
    fit: bool = False,
) -> str:
    """Bold order mark, plain title, then artist and length under the title.

    The period is escaped so Discord does not read "1. " as a list and drop the line below.
    `fit` also keeps the line under the title inside the same width. A wide character
    takes more of that width than a narrow one. The clock and the note stay.
    """
    prefix = f"{index}. "
    name = _clip(" ".join(title.split()) or "Unknown", _LINE - _width(prefix))
    head = f"**{index}\\.** {name}"
    who = " ".join(author.split())
    clock = format_clock(length) if length > 0 else ""
    tail = [part for part in (clock, " ".join(extra.split())) if part]
    if not who and not tail:
        return head
    gap = "\u2007" * len(str(index)) + "\u2008\u00a0"
    if not fit:
        return f"{head}\n-# {gap}{sep.join([who, *tail])}"
    detail = _fit_detail(who, tail, sep, _LINE - _width(gap))
    return f"{head}\n-# {gap}{detail}"


def _fit_detail(who: str, tail: list[str], sep: str, room: int) -> str:
    """Keep the clock and the note. The artist takes whatever width is left."""
    joined = sep.join(tail)
    if not who or not joined:
        return _clip(who or joined, room)
    reserved = _width(sep + joined)
    if reserved < room:
        return f"{_clip(who, room - reserved)}{sep}{joined}"
    return _clip(f"{who}{sep}{joined}", room)


def page_line(page: int, pages: int, count: int, noun: str = "Result") -> str:
    word = noun if count == 1 else f"{noun}s"
    return f"Page {page}/{pages}  |  {count} {word}"


def meta_line(requester_id: int | None, channel_id: int | None) -> str:
    who = f"<@{requester_id}>" if requester_id is not None else ""
    where = f"<#{channel_id}>" if channel_id is not None else ""
    return " | ".join(part for part in (who, where) if part)


def _link(label: str, uri: str | None) -> str:
    if not uri or not uri.startswith(("http://", "https://")) or ")" in uri or " " in uri:
        return label
    safe = label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return f"[{safe}]({uri})"


async def _quiet(call: Callable[..., Awaitable[object]], warning: str, /, **kwargs) -> None:
    try:
        await call(**kwargs)
    except discord.HTTPException:
        log.warning(warning, exc_info=True)


def _noted(line: str) -> str:
    """Drop a full-queue sentence that was added after another note."""
    if line.endswith(_FULL_SUFFIX) and line[: -len(_FULL_SUFFIX)].endswith("."):
        return line[: -len(_FULL_SUFFIX)]
    return line


def queue_is_full(line: str) -> bool:
    """The full-queue sentence was added after the note. A song can be named that."""
    return line == _FULL or line != _noted(line)


def _several(line: str) -> bool:
    """Added 3 songs. The count stays when the queue is also full."""
    note = _noted(line)
    if not note.startswith("Added ") or not note.endswith(" songs."):
        return False
    return note.removeprefix("Added ").removesuffix(" songs.").isdigit()


def visible_note(line: str) -> str | None:
    """A one-song add stays quiet. Several songs, a paused song, and a full queue still show."""
    if not line.startswith("Added "):
        return line
    if "is still paused." in line or _several(line):
        return line
    return _FULL if queue_is_full(line) else None


async def _note(interaction: discord.Interaction, line: str) -> None:
    shown = visible_note(line)
    if shown is None:
        if isinstance(interaction, discord.Interaction) and not interaction.response.is_done():
            await _quiet(interaction.response.defer, "music could not close the add note", ephemeral=True)
        return
    await respond(interaction, notice_text(shown), ephemeral=True)


def _button_rows(count: int, widest: int = 5) -> list[int]:
    """Row sizes of at most `widest`. Spare buttons spread so a row is not left with one."""
    if count <= 0:
        return []
    rows = max(1, (count + widest - 1) // widest)
    base, extra = divmod(count, rows)
    return [base + (index < extra) for index in range(rows)]


def _row(*items: discord.ui.Item, id: int | None = None) -> discord.ui.ActionRow:
    return discord.ui.ActionRow(*items, id=id)



_SILENT = discord.AllowedMentions.none()

Started = Callable[[discord.Interaction], Awaitable[None]]
# The platform is the site already on the card. None starts a name search.
Search = Callable[[discord.Interaction, str, str | None], Awaitable[None]]


async def delete_panel(message: discord.Message | None) -> None:
    """Remove a music card. A message that is already gone is left alone."""
    if message is not None:
        await _quiet(message.delete, "music panel could not close")


async def show_note(interaction: discord.Interaction, line: str, *, quote: bool = False) -> None:
    """Put a line on screen before the slow work.

    A real interaction is answered here, so the click does not sit on a spinner.
    Anything that is not an interaction waits and sends the final line instead.
    """
    if not isinstance(interaction, discord.Interaction):
        return
    text = quote_text(notice_text(line)) if quote else notice_text(line)
    # The deferred slash reply. A card must not call this once it is on screen.
    done = interaction.response.is_done()
    send = interaction.edit_original_response if done else interaction.response.send_message
    extra = {} if done else {"ephemeral": True}
    await _quiet(send, "music could not show a note", content=text, **extra)


async def finish_note(interaction: discord.Interaction, line: str) -> None:
    """Replace the line from show_note, or send it when that call did not answer.

    A plain add removes that line. Nothing stays on screen for it.
    """
    shown = visible_note(line)
    if isinstance(interaction, discord.Interaction) and interaction.response.is_done():
        if shown is None:
            await _quiet(interaction.delete_original_response, "music could not update the note")
        else:
            await _quiet(
                interaction.edit_original_response,
                "music could not update the note",
                content=notice_text(shown),
            )
        return
    await _note(interaction, line)


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
    if handler is not None:
        await (handler(interaction) if index is None else handler(interaction, index))


class Press:
    """The clicks on one card. The latest one owns the picture."""

    def __init__(self) -> None:
        self.token = self.depth = 0

    def begin(self) -> int:
        self.token += 1
        self.depth += 1
        return self.token

    def finish(self) -> None:
        if self.depth:
            self.depth -= 1

    def live(self, token: int) -> bool:
        return token == self.token

    @property
    def busy(self) -> bool:
        return self.depth > 0


class _Card(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=PANEL_SECONDS)
        self.message: discord.Message | None = None
        self._working = False
        self._press = Press()
        self._task_token: dict[asyncio.Task, int] = {}
        # The latest press asked for a refresh once every click has finished.
        self._catch_due = False

    async def on_timeout(self) -> None:
        await delete_panel(self.message)

    def _begin_press(self) -> int:
        """Count this click. A second one is kept, and it becomes the latest."""
        token = self._press.begin()
        self._working = True
        if (task := asyncio.current_task()) is not None:
            self._task_token[task] = token
        return token

    def _end_press(self) -> None:
        """This click has finished. Another one that is still running keeps the card busy."""
        task = asyncio.current_task()
        token = None if task is None else self._task_token.pop(task, None)
        if token is not None and self._press.live(token):
            self._catch_due = True
        self._press.finish()
        self._working = self._press.busy

    async def _busy(self, _interaction: discord.Interaction) -> bool:
        """Accept the click. A newer one keeps the picture when this one finishes."""
        self._begin_press()
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
        if not self.is_finished():
            return True
        await self._ack(interaction)
        return False

    def _pause(self) -> None:
        """Drop a click that started before this search and would paint over it."""

    def _resume(self) -> None:
        """Paint again when the search left this card on screen."""

    async def _restore(self, interaction: discord.Interaction) -> None:
        done = interaction.response.is_done()
        edit = interaction.edit_original_response if done else interaction.response.edit_message
        await _quiet(
            edit, "music search could not restore the card", view=self, allowed_mentions=_SILENT,
        )

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
        self._begin_press()
        replaced = False
        try:
            self._pause()
            replaced = (await opener(interaction, query, platform, replace_card=self._retire)) is True
        except Exception:
            log.exception("music search failed")
            if not interaction.response.is_done():
                await _note(interaction, "Something went wrong while starting the song.")
        finally:
            # Unlock even if the search is cancelled or the error note fails.
            # Leave a replaced or stopped card paused so an old click cannot paint over it.
            self._end_press()
            if not replaced and not self._working and not self.is_finished():
                self._resume()
        if not replaced and not self.is_finished() and interaction.response.is_done():
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
        super().__init__(custom_id=custom_id, placeholder=placeholder, options=options)
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
        id: int | None = None,
    ) -> None:
        super().__init__(label=label, style=style, disabled=disabled, id=id)
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
