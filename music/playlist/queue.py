"""The queue tab."""

from __future__ import annotations

import asyncio
import logging
import discord

from music.panel import (
    _Mark,
    _clip,
    _note,
)

from music.archive import (
    SavedList,
    drop_song,
    find_playlist,
)

from music.playback import (
    Queued,
    clear_played,
    clear_queued,
    drop_queued,
    edits_queue,
    leave_if_idle,
    move_queued,
    playing_note,
    queued,
    skipped_songs,
    warm_voice,
)

from music.playlist.lists import _can_click
from music.playlist.popups import (
    EmptyList,
    QueueList,
)

from music.playlist.card import (
    HISTORY,
    MARK_DELETE,
    MARK_MOVE,
    PLAYLISTS,
    QUEUE,
    _defer,
    _guild_num,
    _heard,
    _lit,
    _queued_noun,
    _rule,
    _span,
    now_text,
    queue_row,
)

log = logging.getLogger(__name__)


def _queue_key(row: Queued) -> tuple:
    return (row.title, row.url, row.author, row.platform, row.requester_id)


class _Queue:
    """The queue tab."""

    async def on_queue_list(self, interaction: discord.Interaction) -> None:
        if not await _can_click(self, interaction):
            return
        lists = self._saved_lists()
        if not lists:
            await _note(interaction, "There is no playlist yet.")
            return
        await interaction.response.send_modal(QueueList(lists, self._queue_list))


    async def _queue_list(self, interaction: discord.Interaction, name: str) -> None:
        """Add this playlist from the first song. Every song joins, in order."""
        if await self._busy(interaction):
            return
        cold = False
        try:
            found = self._named_list(name)
            if isinstance(found, str):
                await _note(interaction, found)
                return
            if not found.songs:
                await _note(interaction, f"{found.name} has no songs.")
                return
            cold = getattr(interaction.guild, "voice_client", None) is None
            if cold:
                warm_voice(interaction.user, interaction.guild)
            await _defer(interaction)
            from music.playlist import play_saved
            try:
                line = await play_saved(
                    interaction.user,
                    interaction.guild,
                    list(found.songs),
                    replace=False,
                )
            except Exception:
                log.exception("music playlist could not be queued")
                line = "Something went wrong while starting the song."
            if not playing_note(line):
                await _note(interaction, line)
                return
            self.paint()
            await self._edit(interaction)
            if line.startswith("Playing ") and self.on_playing is not None:
                await self.on_playing(interaction)
            if _heard(line):
                return
            await _note(interaction, line)
        finally:
            self._working = False
            if cold:
                asyncio.create_task(leave_if_idle(interaction.guild), name="music-idle")


    def _named_list(self, text: str) -> SavedList | str:
        name = " ".join(text.split())
        if not name:
            return "Name the playlist."
        key = name.casefold()
        found = next((saved for saved in self._saved_lists() if saved.name.casefold() == key), None)
        return found if found is not None else "That playlist is not here."


    async def on_drop(self, interaction: discord.Interaction, index: int) -> None:
        if await self._busy(interaction):
            return
        try:
            saved = self.opened
            guild_id = _guild_num(self.guild)
            if saved is None or self.panel != PLAYLISTS or guild_id is None:
                await _defer(interaction)
                return
            await interaction.response.defer()
            updated = drop_song(self.store, guild_id, saved.name, index)
            if isinstance(updated, str):
                await _note(interaction, updated)
                updated = find_playlist(self.store, guild_id, saved.name)
            if updated is None:
                self.opened = None
                self.page = 0
                self._query = None
                self._removing = False
            else:
                self.opened = updated
                if not updated.songs:
                    self._removing = False
            self.paint()
            await self._push()
        finally:
            self._working = False


    async def on_order(self, interaction: discord.Interaction) -> None:
        if not await _can_click(self, interaction, closed=False):
            return
        self._clear_move()
        self._ordering = self._waiting_count() >= 2 and not self._ordering
        self.paint()
        await self._edit(interaction)


    async def move_at(self, interaction: discord.Interaction, index: int) -> None:
        if not self._ordering:
            await _defer(interaction)
            return
        if self._moving is None or self._moving == index:
            if self._working:
                await _note(interaction, "Already working on that.")
                return
            if self._moving == index:
                self._clear_move()
            elif not self._arm_move(index):
                await _note(interaction, "That song is gone.")
            self.paint()
            await self._edit(interaction)
            return
        if await self._busy(interaction):
            return
        try:
            if not await self._in_room(interaction):
                return
            await interaction.response.defer()
            rows = queued(self.guild) or []
            # The buttons still name the queue as it was drawn. A song that
            # ended since then has shifted every later row down by one.
            source = self._place_of(rows, self._moving_key, self._moving)
            dest = self._drawn_place(rows, index)
            gone = source is None or dest is None
            note = "That song is gone." if gone else await move_queued(self.guild, source, dest)
            self._clear_move()
            if note:
                await _note(interaction, note)
            self.paint()
            await self._push()
        finally:
            self._working = False


    async def on_empty(self, interaction: discord.Interaction) -> None:
        if not await _can_click(self, interaction):
            return
        if self.panel not in (QUEUE, HISTORY):
            await _defer(interaction)
            return
        count = self._waiting_count() if self.panel == QUEUE else self._played_count()
        if not count:
            await _defer(interaction)
            return
        await interaction.response.send_modal(EmptyList(self.finish_empty, self.panel))


    async def finish_empty(self, interaction: discord.Interaction, panel: str) -> None:
        """Clear the list on screen. A live queue still needs the voice channel."""
        if await self._busy(interaction):
            return
        try:
            if panel == QUEUE and not await self._in_room(interaction):
                return
            await interaction.response.defer()
            if panel == HISTORY:
                await clear_played(self.guild)
            elif panel == QUEUE:
                await clear_queued(self.guild)
            self.paint()
            await self._push()
        finally:
            self._working = False


    def _waiting_count(self) -> int:
        return sum(1 for row in (queued(self.guild) or []) if not row.playing)


    async def _in_room(self, interaction: discord.Interaction) -> bool:
        if edits_queue(interaction.user, self.guild):
            return True
        await _note(interaction, "Join the voice channel first.")
        return False


    def _clear_move(self) -> None:
        self._moving = None
        self._moving_key = None


    def _arm_move(self, index: int) -> bool:
        """Select the song this button was drawn for. False when that song is gone."""
        rows = queued(self.guild) or []
        place = self._drawn_place(rows, index)
        if place is None:
            self._clear_move()
            return False
        self._moving = place
        self._moving_key = _queue_key(rows[place])
        return True


    def _keep_move(self, rows: list[Queued]) -> None:
        """The armed song may have shifted. Follow it, or drop the arm if it is gone."""
        place = self._place_of(rows, self._moving_key, self._moving)
        if place is None:
            self._clear_move()
            return
        self._moving = place


    def _place_of(
        self, rows: list[Queued], key: tuple | None, hint: int | None, *, waiting: bool = True,
    ) -> int | None:
        """The old place wins when it still is that song. Waiting skips the one on now."""
        if key is None:
            return None
        matches = [
            place
            for place, row in enumerate(rows)
            if _queue_key(row) == key and not (waiting and row.playing)
        ]
        if hint in matches:
            return hint
        return matches[0] if len(matches) == 1 else None


    def _drawn_place(self, rows: list[Queued], index: int, *, waiting: bool = True) -> int | None:
        """The song this button was drawn for, wherever it sits now."""
        shown = self._shown
        if shown is None or shown[0] != QUEUE or not isinstance(shown[1], tuple):
            return None
        drawn = shown[1]
        if index < 0 or index >= len(drawn):
            return None
        title, author, _length, url, platform, playing, _stream, requester = drawn[index]
        if playing and waiting:
            return None
        key = (title, url, author, platform, requester)
        return self._place_of(rows, key, index, waiting=waiting)


    async def remove_at(self, interaction: discord.Interaction, index: int) -> None:
        if await self._busy(interaction):
            return
        try:
            if not await self._in_room(interaction):
                return
            await interaction.response.defer()
            rows = queued(self.guild) or []
            place = self._drawn_place(rows, index, waiting=False)
            note = "That song is gone." if place is None else await drop_queued(self.guild, place)
            if note:
                await _note(interaction, note)
            self.paint()
            await self._push()
        finally:
            self._working = False


    def _queue(self, box: discord.ui.Container) -> tuple[int, int, int | None, str]:
        rows = queued(self.guild) or []
        current = next((row for row in rows if row.playing), None)
        waiting = [row for row in rows if not row.playing]
        base = len(rows) - len(waiting)
        if len(waiting) < 2:
            self._ordering = False
            self._clear_move()
        elif self._ordering:
            self._keep_move(rows)
        box.add_item(discord.ui.TextDisplay(now_text(current)))
        moving = self._moving_line(waiting, base)
        if moving:
            box.add_item(discord.ui.TextDisplay(moving))
        _start, stop, pages = self._slice(len(waiting))
        if waiting:
            box.add_item(_rule())
            for offset, row in enumerate(waiting[_start:stop]):
                index = base + _start + offset
                accessory = (
                    _Mark(MARK_MOVE, "move_at", index, style=_lit(self._moving == index))
                    if self._ordering
                    else _Mark(MARK_DELETE, "remove_at", index)
                )
                box.add_item(discord.ui.Section(
                    queue_row(_start + offset + 1, row), accessory=accessory,
                ))
        notice = skipped_songs(self.guild)
        if notice:
            box.add_item(discord.ui.TextDisplay(f"-# {notice}"))
        return pages, len(waiting), _span(waiting), _queued_noun(len(waiting))


    def _moving_line(self, waiting: list[Queued], base: int) -> str | None:
        """The song chosen on another page, so every page still names it."""
        if not self._ordering or self._moving is None:
            return None
        spot = self._moving - base
        if spot < 0 or spot >= len(waiting):
            return None
        title = _clip(" ".join(waiting[spot].title.split()) or "Unknown")
        return f"-# Moving **{spot + 1}\\.** {title}"
