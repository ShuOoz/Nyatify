"""The playlist card: queue, history, and saved lists on one frame."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord

from announce import ACCENT_COLOR
from music.archive import PLAYLIST_LIMIT, SONG_LIMIT, SavedSong
from music.panel import (
    MARK_BACK, MARK_CLOSE, MARK_NEXT, MARK_SEARCH, PAGE, RADIO_MARK,
    _Card, _Mark, _Menu, _clip, _hold_card, _link, _name_in_title, _note, _row,
    format_clock, song_row,
)
from music.playback import played, queued, skipped_songs, unwatch_line, watch_line
from music.source import platform_label

_READ_AT_ONCE = 4
TITLE = "## Osaka Playlist"
QUEUE, HISTORY, PLAYLISTS = "Queue", "History", "Playlist"
MARK_DELETE, MARK_MINUS, MARK_LINE, MARK_SAVE, MARK_MOVE = "⌫", "－", "☰", "\u2605", "⇵"
PICK = "Pick a playlist"
_ENTRY = 4000
_PAD, _HALF, _GAP = "\u3164", "\uffa0", "\u00a0"
LABEL_EMPTY = f"{_PAD}Empty{_PAD}"
LABEL_YOUR = f"{_PAD}{_HALF}@Your{_HALF}{_PAD}"
LABEL_ADJUST = f"{_PAD}{_HALF}Adjust{_HALF}{_PAD}"
LABEL_NEW_LIST = f"{_PAD}{_HALF}Create{_HALF}{_PAD}"
LABEL_REMOVE = f"{_PAD}Delete{_PAD}"
_BUSY = "Already working on that."
_MENTIONS = discord.AllowedMentions.none()


def _plain(value: str) -> str:
    return " ".join(value.split())


def _lit(on: bool) -> discord.ButtonStyle:
    return discord.ButtonStyle.primary if on else discord.ButtonStyle.secondary


def _clock(line: str, duration: int | None) -> str:
    if duration is None:
        return line
    return f"{line}  |  {format_clock(duration)}"


def _one(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def now_text(row: Queued | None) -> str:
    if row is None:
        return "### Nothing is playing\n-# Use /music play to play a song"
    title = _plain(row.title) or "Unknown"
    artist = "" if _name_in_title(_plain(row.author), title) else _clip(_plain(row.author))
    parts = [artist, format_clock(row.length) if row.length > 0 else ""]
    lines = ["### Now Playing", f"**{_link(_clip(title), row.url)}**"]
    detail = " · ".join(part for part in parts if part)
    if detail:
        lines.append(detail)
    if row.stream:
        lines.append(RADIO_MARK)
    return "\n".join(lines)


def queue_row(index: int, row: Queued) -> str:
    text = song_row(index, row.title, row.author, row.length, platform_label("", row.platform), sep=" · ")
    return f"{text}\n{RADIO_MARK}" if row.stream else text


def saved_row(index: int, song: SavedSong, *, site: bool) -> str:
    extra = platform_label("", song.platform) if site else ""
    return song_row(index, song.title, song.author, song.length, extra, sep=" · ")


def _mixed_platforms(songs: tuple[SavedSong, ...]) -> bool:
    return bool(songs) and any(song.platform != songs[0].platform for song in songs)


def pick_text() -> str:
    return (
        '-# Use the "Create" button for a new playlist\n'
        f"-# A server keeps {PLAYLIST_LIMIT} playlists, and each keeps {SONG_LIMIT} songs"
    )


def empty_text(name: str) -> str:
    return f"-# {name} has no songs"


def _tab_label(name: str, widened: bool) -> str:
    side = _PAD if name == PLAYLISTS else _PAD + _HALF
    if widened:
        side += _HALF
        return f"{side}{_GAP}{name}{_GAP}{side}"
    return f"{side}{name}{side}"


class _Tab(discord.ui.Button):
    def __init__(self, panel: str, selected: bool, widened: bool = False) -> None:
        super().__init__(
            label=_tab_label(panel, selected or widened),
            style=_lit(selected),
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = getattr(self.view, "on_tab", None)
        if handler is not None:
            await handler(interaction, self.panel)


def _status(page: int, pages: int, count: int, noun: str, duration: int | None) -> str:
    return _clock(f"Page {page}/{pages}  |  {count} {noun}", duration)


def _queued_noun(count: int) -> str:
    return _one(count, "song in queue", "songs in queue")


def _song_noun(count: int) -> str:
    return _one(count, "song", "songs")


def _list_noun(count: int) -> str:
    return _one(count, "playlist", "playlists")


def _span(rows: list[Queued]) -> int | None:
    total = sum(row.length for row in rows if row.length > 0)
    if rows and total == 0:
        return None
    return total


def _song_span(songs: tuple[SavedSong, ...]) -> int | None:
    if any(song.length <= 0 for song in songs):
        return None
    return sum(song.length for song in songs)


def _saved_span(lists: list[SavedList]) -> int | None:
    spans = [_song_span(saved.songs) for saved in lists]
    if any(span is None for span in spans):
        return None
    return sum(spans)


def _rule() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def _menu_gap() -> discord.ui.Separator:
    return discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small)


def _as_saved(row: Queued) -> SavedSong | None:
    url = row.url or ""
    if not url.startswith(("http://", "https://")):
        return None
    return SavedSong(
        _plain(row.title) or "Unknown",
        row.platform or "youtube",
        url,
        _plain(row.author),
        row.length if row.length > 0 else 0,
    )


def _id(obj: object) -> int | None:
    value = getattr(obj, "id", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


_person = _guild_num = _id


def _track_author(track: object) -> str:
    author = getattr(track, "author", None)
    return _plain(author) if isinstance(author, str) else ""


def _track_length(track: object) -> int:
    length = getattr(track, "length", None)
    if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
        return 0
    return length


def _heard(line: str) -> bool:
    quiet = line.startswith(("Playing ", "Added ")) and " Skipped " not in line
    return quiet and not line.endswith("The queue is full.")


async def _defer(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()


def _song_snap(row: Queued) -> tuple:
    return (
        row.title, row.author, row.length, row.url, row.platform,
        row.playing, row.stream, row.requester_id,
    )


# Words above are bound first. The tabs import them, then this card imports the tabs.
from music.playlist.history import _History
from music.playlist.lists import _Lists
from music.playlist.queue import _Queue

log = logging.getLogger(__name__)


class LiveLine(_Queue, _Lists, _History, _Card):
    def __init__(
        self,
        guild: object,
        store: object,
        on_started: Started | None = None,
        opened: SavedList | None = None,
        on_search: Callable[..., Awaitable[object]] | None = None,
        on_playing: Started | None = None,
    ) -> None:
        super().__init__()
        self.guild = guild
        self.store = store
        self.on_started = on_started
        self.on_playing = on_playing
        self.opened = opened
        self.open_search = on_search
        self.panel = PLAYLISTS if opened is not None else QUEUE
        self.page = 0
        self._query: str | None = None
        self._ordering = False
        self._moving: int | None = None
        self._moving_key: tuple | None = None
        self._removing = False
        self._saving = False
        self._mine_id: int | None = None
        self._history_draw: tuple[tuple, ...] = ()
        self._filled: set[str] = set()
        self._box: discord.ui.Container | None = None
        self._shown: tuple | None = None
        self._watch: Callable[[object], Awaitable[None]] | None = None
        self._draw = asyncio.Lock()
        self.menu = _Menu(
            PICK,
            "on_pick",
            [discord.SelectOption(label="Playlist", value="0")],
            "music-playlist",
        )
        self.paint()

    def follow(self, message: discord.Message) -> None:
        self.message = message
        if self._watch is not None:
            return
        self._watch = self._on_line
        watch_line(self._watch)

    def stop(self) -> None:
        self._silence()
        super().stop()

    async def on_timeout(self) -> None:
        self._silence()
        await super().on_timeout()

    def _silence(self) -> None:
        watch, self._watch = self._watch, None
        if watch is not None:
            unwatch_line(watch)

    async def _on_line(self, guild: object) -> None:
        if _id(guild) != _id(self.guild) or self.is_finished() or self._working:
            return
        if self.message is None or self._snap() == self._shown:
            return
        self.paint()
        await self._push()

    def _snap(self) -> tuple:
        if self.panel == PLAYLISTS:
            saved = self.opened
            if saved is not None:
                songs = tuple((song.title, song.url) for song in saved.songs)
                return (PLAYLISTS, saved.name, songs, self.page, self._query)
            shelf = tuple((item.name, len(item.songs), item.saver_id) for item in self._saved_lists())
            return (PLAYLISTS, shelf, self.page)
        rows = played(self.guild) if self.panel == HISTORY else queued(self.guild)
        if self.panel == HISTORY and self._mine_id is not None:
            rows = [row for row in (rows or []) if row.requester_id == self._mine_id]
        notice = skipped_songs(self.guild) if self.panel == QUEUE else None
        return (
            self.panel,
            tuple(_song_snap(row) for row in (rows or [])),
            self.page,
            self._mine_id,
            notice,
        )

    def paint(self) -> None:
        if self._box is not None:
            self.remove_item(self._box)
        self._settle_query()
        box = discord.ui.Container(accent_colour=ACCENT_COLOR)
        box.add_item(discord.ui.Section(TITLE, accessory=self._corner()))
        box.add_item(discord.ui.Separator())
        box.add_item(_row(
            _Tab(QUEUE, self.panel == QUEUE),
            _Tab(HISTORY, self.panel == HISTORY),
            _Tab(PLAYLISTS, self.panel == PLAYLISTS),
            self._side(),
        ))
        box.add_item(_rule())
        if self.panel == PLAYLISTS and self.opened is not None:
            summary = self._open_list(box)
        elif self.panel == PLAYLISTS:
            summary = self._lists(box)
        elif self.panel == HISTORY:
            summary = self._played(box)
        else:
            summary = self._queue(box)
        self._footer(box, *summary)
        self._box = box
        self.add_item(box)
        self._shown = self._snap()

    async def on_tab(self, interaction: discord.Interaction, panel: str) -> None:
        if self._working:
            await _note(interaction, _BUSY)
            return
        if panel == self.panel and self.opened is None:
            await _defer(interaction)
            return
        self.opened = None
        self.panel = panel
        self.page = 0
        self._query = None
        self._removing = False
        if panel != QUEUE:
            self._ordering = False
            self._clear_move()
        if panel != HISTORY:
            self._saving = False
        self.paint()
        await self._edit(interaction)

    async def on_back(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, -1)

    async def on_forward(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, 1)

    async def on_close(self, interaction: discord.Interaction) -> None:
        self.stop()
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except discord.HTTPException:
            log.warning("music playlist could not close", exc_info=True)

    def _corner(self) -> discord.ui.Button:
        filtering = self.panel == PLAYLISTS and self.opened is not None and bool(self._query)
        return _Mark(MARK_SEARCH, "on_add", style=_lit(filtering))

    def _side(self) -> discord.ui.Button:
        if self.panel == PLAYLISTS:
            return _Mark(MARK_MINUS, "on_remove_ask", disabled=self.opened is None)
        if self.panel == QUEUE:
            return _Mark(MARK_LINE, "on_queue_list", disabled=not self._saved_lists())
        ready = bool(played(self.guild))
        if not ready:
            self._saving = False
        return _Mark(MARK_SAVE, "on_save", disabled=not ready, style=_lit(self._saving))

    def _footer(
        self,
        box: discord.ui.Container,
        pages: int,
        count: int,
        duration: int | None,
        noun: str,
    ) -> None:
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(_hold_card(self._page_line(pages, count, noun, duration))))
        box.add_item(_row(
            _Mark(MARK_BACK, "on_back", disabled=self.page <= 0),
            _Mark(MARK_NEXT, "on_forward", disabled=self.page >= pages - 1),
            self._wide(count),
            self._narrow(count),
            _Mark(MARK_CLOSE, "on_close"),
        ))

    def _wide(self, count: int) -> discord.ui.Button:
        if self.panel == QUEUE:
            return _Mark(LABEL_ADJUST, "on_order", disabled=count < 2, style=_lit(self._ordering))
        if self.panel == HISTORY:
            on = self._mine_id is not None
            return _Mark(LABEL_YOUR, "on_mine", disabled=not played(self.guild), style=_lit(on))
        return _Mark(LABEL_NEW_LIST, "on_new")

    def _narrow(self, count: int) -> discord.ui.Button:
        if self.panel in (QUEUE, HISTORY):
            return _Mark(LABEL_EMPTY, "on_empty", disabled=count == 0)
        ready = self.opened is not None and count > 0
        return _Mark(LABEL_REMOVE, "on_remove", disabled=not ready, style=_lit(self._removing and ready))

    def _page_line(self, pages: int, count: int, noun: str, duration: int | None) -> str:
        if self.panel == PLAYLISTS and self.opened is None:
            return _clock(f"{count} {_list_noun(count)}", duration)
        line = _status(self.page + 1, pages, count, noun, duration)
        saved = self.opened
        if self.panel == PLAYLISTS and saved is not None and saved.saver_id is not None:
            return f"{line}  |  <@{saved.saver_id}>"
        return line

    def _slice(self, count: int) -> tuple[int, int, int]:
        pages = 1 if count <= 0 else (count + PAGE - 1) // PAGE
        self.page = min(max(0, self.page), pages - 1)
        start = self.page * PAGE
        return start, min(count, start + PAGE), pages

    async def _turn(self, interaction: discord.Interaction, step: int) -> None:
        if self._working:
            await _note(interaction, _BUSY)
            return
        self.page += step
        await self._show_songs(interaction)

    async def _send(self, edit: Callable[[], Awaitable[None]]) -> None:
        if self.is_finished():
            return
        async with self._draw:
            if self.is_finished():
                return
            try:
                await edit()
            except discord.HTTPException:
                log.warning("music playlist could not refresh", exc_info=True)

    async def _edit(self, interaction: discord.Interaction) -> None:
        async def edit() -> None:
            if interaction.response.is_done():
                await interaction.edit_original_response(view=self, allowed_mentions=_MENTIONS)
            else:
                await interaction.response.edit_message(view=self, allowed_mentions=_MENTIONS)
        await self._send(edit)

    async def _push(self) -> None:
        message = self.message
        if message is None:
            return

        async def edit() -> None:
            await message.edit(view=self, allowed_mentions=_MENTIONS)

        await self._send(edit)
