"""Songs already played."""

from __future__ import annotations

import asyncio
import logging
import discord

from music.panel import (
    MARK_ADD,
    _note,
    reopen_player,
    shows_player,
)

from music.playback import (
    earlier_at,
    leave_if_idle,
    paused_away,
    played,
    playing_note,
    queue_tracks,
    warm_voice,
)

from music.playlist.card import (
    _can_click,
    _defer,
    _heard,
    _person,
    _song_noun,
    _span,
    queue_row,
)

log = logging.getLogger(__name__)


def _row_key(row) -> tuple:
    return (row.title, row.url, row.author, row.platform, row.requester_id)


class _History:
    """Songs already played."""


    async def on_again(self, interaction: discord.Interaction, index: int) -> None:
        if await self._busy(interaction):
            return
        cold = False
        try:
            place = self._played_index(index)
            if place is None:
                await _note(interaction, "That song is gone.")
                self.paint()
                await self._edit(interaction)
                return
            found = earlier_at(self.guild, place)
            if found is None:
                await _defer(interaction)
                return
            # A paused song is still current. Joining would sit there in silence.
            cold = (
                getattr(interaction.guild, "voice_client", None) is None
                and not paused_away(interaction.guild)
            )
            if cold:
                warm_voice(interaction.user, interaction.guild)
            await interaction.response.defer()
            track, platform = found
            try:
                line = await queue_tracks(interaction.user, interaction.guild, [track], platform)
            except Exception:
                log.exception("music queue failed")
                line = "Something went wrong while starting the song."
            # This button is still working, so the line watcher will not paint the card.
            if playing_note(line):
                self.paint()
                await self._edit(interaction)
            # The playlist stays. A start opens the player, and so does an add when none is up.
            if shows_player(line, interaction.guild) and self.on_playing is not None:
                await self.on_playing(interaction, reopen=reopen_player(line))
            # Joining the queue hides the row. Say so, or the song looks deleted.
            if not _heard(line) or line.startswith("Added "):
                await _note(interaction, line)
        finally:
            await self._release()
            if cold:
                asyncio.create_task(leave_if_idle(interaction.guild), name="music-idle")


    async def on_mine(self, interaction: discord.Interaction) -> None:
        if not await _can_click(self, interaction, closed=False):
            return
        user_id = _person(interaction.user)
        if user_id is None:
            await _defer(interaction)
            return
        self._mine_id = None if self._mine_id == user_id else user_id
        self.page = 0
        self.paint()
        await self._edit(interaction)


    def _played_count(self) -> int:
        return len(played(self.guild) or [])


    def _played_index(self, index: int) -> int | None:
        """The played song this button was drawn for, wherever it sits now."""
        drawn = self._history_draw
        if not 0 <= index < len(drawn):
            return None
        key = drawn[index]
        matches = [place for place, row in enumerate(played(self.guild) or []) if _row_key(row) == key]
        if index in matches:
            return index
        if len(matches) == 1:
            return matches[0]
        if not matches:
            return None
        # The row moved, and more than one copy is still here. Take the closest.
        return min(matches, key=lambda place: (abs(place - index), place))


    def _played(self, box: discord.ui.Container) -> tuple[int, int, int | None, str]:
        rows = played(self.guild) or []
        # Nothing has played, so @Your must not stay on and claim the empty list.
        if not rows:
            self._mine_id = None
        self._history_draw = tuple(_row_key(row) for row in rows)
        mine = self._mine_id
        picked = [
            (index, row)
            for index, row in enumerate(rows)
            if mine is None or row.requester_id == mine
        ]
        method = "on_keep" if self._saving else "on_again"
        visible = [row for _index, row in picked]
        empty = None if picked else (
            "-# Nothing of yours yet" if mine is not None else "-# Nothing has played yet"
        )

        def present(start: int, offset: int, item: tuple) -> tuple:
            index, row = item
            return (
                queue_row(start + offset + 1, row), MARK_ADD, method, index,
                discord.ButtonStyle.secondary,
            )

        return self._paged(
            box, picked, present, _span(visible), _song_noun(len(visible)), empty=empty,
        )
