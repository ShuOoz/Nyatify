"""The queue tab."""

from __future__ import annotations

import asyncio
import logging
import discord

from music.panel import (
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
    can_edit_queue,
    leave_if_idle,
    move_queued,
    now_playing,
    played,
    paused_away,
    play_saved,
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
    MARK_MINUS,
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
            # A paused song is still current. Joining would sit there in silence.
            cold = (
                getattr(interaction.guild, "voice_client", None) is None
                and not paused_away(interaction.guild)
            )
            if cold:
                warm_voice(interaction.user, interaction.guild)
            await _defer(interaction)
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
            await self._release()
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
            if not await self._in_room(interaction):
                return
            saved = self.opened
            guild_id = _guild_num(self.guild)
            shown = saved.songs[index] if saved is not None and 0 <= index < len(saved.songs) else None
            if saved is None or shown is None or self.panel != PLAYLISTS or guild_id is None:
                await _defer(interaction)
                return
            await interaction.response.defer()
            # The card can still show the list from before a song was saved at
            # the top. Drop this row only when that same song is still here.
            current = find_playlist(self.store, guild_id, saved.name)
            moved = (
                current is not None
                and (index >= len(current.songs) or current.songs[index].url != shown.url)
            )
            if moved:
                self.opened = current
                self._removing = bool(current.songs)
                self.paint()
                await self._push()
                await _note(interaction, "That song is gone.")
                return
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
            await self._release()


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
            await self._release()


    async def on_empty(self, interaction: discord.Interaction) -> None:
        if not await _can_click(self, interaction):
            return
        if self.panel not in (QUEUE, HISTORY):
            await _defer(interaction)
            return
        yours = self.panel == HISTORY and self._mine_id is not None
        if self.panel == QUEUE:
            count = self._waiting_count()
        elif yours:
            count = sum(
                1 for row in (played(self.guild) or []) if row.requester_id == self._mine_id
            )
        else:
            count = self._played_count()
        if not count:
            await _defer(interaction)
            return
        # The box remembers whose songs were on screen. A later @Your does not change it.
        mine = self._mine_id if yours else None
        await interaction.response.send_modal(EmptyList(
            lambda press, panel: self.finish_empty(press, panel, mine),
            self.panel,
            yours=yours,
        ))


    async def finish_empty(
        self, interaction: discord.Interaction, panel: str, mine_id: int | None = None,
    ) -> None:
        """Clear the list on screen. A live queue still needs the voice channel."""
        if await self._busy(interaction):
            return
        try:
            if panel == QUEUE and not await self._in_room(interaction):
                return
            await interaction.response.defer()
            if panel == HISTORY:
                await clear_played(self.guild, mine_id)
            elif panel == QUEUE:
                await clear_queued(self.guild)
            self.paint()
            await self._push()
        finally:
            await self._release()


    def _waiting_count(self) -> int:
        return sum(1 for row in (queued(self.guild) or []) if not row.playing)


    async def _in_room(self, interaction: discord.Interaction) -> bool:
        # The card may still hold the guild from before a reconnect.
        live = getattr(interaction, "guild", None)
        if isinstance(live, discord.Guild) and live.id == _guild_num(self.guild):
            self.guild = live
        if can_edit_queue(interaction.user, self.guild):
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
        if not matches:
            return None
        if hint in matches:
            return hint
        if len(matches) == 1 or hint is None:
            return matches[0] if len(matches) == 1 else None
        # The row moved, and more than one copy is still here. Take the closest.
        return min(matches, key=lambda place: (abs(place - hint), place))


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
            note = "That song is gone." if place is None else await drop_queued(
                self.guild, place, interaction.user,
            )
            if note:
                await _note(interaction, note)
            self.paint()
            await self._push()
        finally:
            await self._release()


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
        state = now_playing(self.guild)
        shuffling = state is not None and state.shuffling and current is not None
        box.add_item(discord.ui.TextDisplay(now_text(current, shuffling=shuffling), id=12))
        moving = self._moving_line(waiting, base)
        if moving:
            box.add_item(discord.ui.TextDisplay(moving, id=13))
        _start, stop, pages = self._slice(len(waiting))
        if waiting:
            box.add_item(_rule(14))
            for offset, row in enumerate(waiting[_start:stop]):
                index = base + _start + offset
                if self._ordering:
                    label, method, style = MARK_MOVE, "move_at", _lit(self._moving == index)
                else:
                    label, method, style = MARK_MINUS, "remove_at", discord.ButtonStyle.secondary
                self._show_slot(box, offset, queue_row(_start + offset + 1, row), label, method, index, style)
        notice = skipped_songs(self.guild)
        if notice:
            box.add_item(discord.ui.TextDisplay(f"-# {notice}", id=15))
        return pages, len(waiting), _span(waiting), _queued_noun(len(waiting))


    def _moving_line(self, waiting: list[Queued], base: int) -> str | None:
        """The song chosen on another page, so every page still names it."""
        if not self._ordering or self._moving is None:
            return None
        index = self._moving - base
        if index < 0 or index >= len(waiting):
            return None
        title = _clip(" ".join(waiting[index].title.split()) or "Unknown")
        return f"-# Moving **{index + 1}\\.** {title}"
