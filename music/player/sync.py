"""Keep the player card matched to the song."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord

from announce import notice_text, respond
from music.playback import _stored, now_playing
from music.playback.remember import _movable, _on_air
from music.player.rule import (
    _replace_mode,
    after_quiet,
    card_stays,
    click_answer,
    hinted_modes,
    mode_press,
    spend_mode,
)
from music.player.playbar import _bar_key

log = logging.getLogger(__name__)

# One edit every five seconds stays inside the channel's message-edit budget.
REFRESH_SECONDS = 5


class _Refresh:
    """Buttons and the tick keep the open card on the song."""

    def _modes(self) -> tuple[bool, bool]:
        """Repeat and shuffle on the line. A quiet player still has them."""
        line = _stored(self.guild)
        if line is not None:
            return line.repeat, line.shuffle
        state = now_playing(self.guild)
        if state is None:
            return False, False
        return state.repeating, state.shuffling

    def _show_mode(self, repeating: bool, shuffling: bool) -> None:
        """Change On Queue, On Repeat, or On Shuffle. The song stays where it is."""
        repeat_style = discord.ButtonStyle.primary if repeating else discord.ButtonStyle.secondary
        shuffle_style = discord.ButtonStyle.primary if shuffling else discord.ButtonStyle.secondary
        self.repeat.style = repeat_style
        self.shuffle.style = shuffle_style
        self.song.content = _replace_mode(self.song.content, repeating, shuffling)
        self.meta.content = _replace_mode(self.meta.content, repeating, shuffling)
        if self._shown is None:
            return
        shown = list(self._shown)
        shown[2] = _replace_mode(str(shown[2]), repeating, shuffling)
        shown[7] = repeat_style
        shown[9] = shuffle_style
        self._shown = tuple(shown)

    def _apply_mode_hint(self, hint: tuple[str, object]) -> None:
        repeating, shuffling = self._modes()
        line = _stored(self.guild)
        radio = bool(line is not None and line.radio)
        kind, value = hint
        if kind in ("repeat", "shuffle") and isinstance(value, bool):
            repeating, shuffling, _radio = hinted_modes(repeating, shuffling, radio, kind, value)
        self._show_mode(repeating, shuffling)

    def _shown_modes(self) -> tuple[bool, bool, bool, bool]:
        """Pause, repeat, shuffle, and radio, including a press Lavalink has not answered."""
        state = now_playing(self.guild)
        repeating, shuffling = self._modes()
        line = _stored(self.guild)
        radio = state.radio if state is not None else bool(line is not None and line.radio)
        paused = bool(state is not None and state.paused)
        hint = self._hint
        if hint is not None:
            kind, value = hint
            if kind == "pause" and isinstance(value, bool):
                paused = value
            elif kind in ("repeat", "shuffle", "radio") and isinstance(value, bool):
                repeating, shuffling, radio = hinted_modes(
                    repeating, shuffling, radio, kind, value,
                )
        return paused, repeating, shuffling, radio

    def _preview(self, kind: str) -> tuple[str, object] | None:
        """The control's next frame, so the card can move before Lavalink answers.

        A second press reads the frame already on the card. The line may still
        be on the press before this one.
        """
        state = now_playing(self.guild)
        line = _stored(self.guild)
        paused, repeating, shuffling, radio = self._shown_modes()
        live = (state is not None and state.has_track) or getattr(self, "_coming", None) is not None
        if kind == "pause":
            if not live or state is None or not state.has_track:
                return None
            return ("pause", not paused)
        if kind == "next":
            # Radio plays a similar song. The waiting line is not that title.
            if radio and not repeating:
                return None
            if line is None:
                return None
            slot = self._wish.slot()
            if slot >= len(line.later):
                self._wish.rewind()
                return None
            return ("next", line.later[slot])
        stuck = live and state is not None and (state.stream or state.length <= 0)
        track = None if line is None or line.current is None else line.current.track
        if not live and track is not None and (_on_air(line, track) or not _movable(track)):
            stuck = True
        return mode_press(kind, repeating, shuffling, radio, live=live, stuck=stuck)

    async def _act(
        self,
        interaction: discord.Interaction,
        call: Callable[[], Awaitable[str | None]],
        kind: str | None = None,
    ) -> None:
        if not self._inside(interaction):
            await respond(interaction, notice_text("Join the voice channel first."), ephemeral=True)
            return
        # The same card is open on every device. One press can still be editing
        # it when the next press arrives, and Discord gives up after three
        # seconds, so a busy card is accepted before it waits for that edit.
        # A free card edits the message instead. That edit is the acceptance,
        # and deferring first would throw it away: the response is already done.
        # The lock covers that edit. A pause, skip, or back runs with it, so
        # Lavalink is told while Discord is still drawing the card. The card
        # catches up before the lock lets the line refresh in.
        busy = self._lock.locked() and not interaction.response.is_done()
        if click_answer("control", busy=busy) == "defer":
            await interaction.response.defer()
        async with self._lock:
            token = self._wish.claim()
            hint = None if kind is None else self._preview(kind)
            shown = False
            # Preview first. This does not run until the hold below waits.
            early = kind in ("pause", "next", "previous")
            running = asyncio.create_task(call()) if early else None
            # A quiet player keeps the song on screen. Only the mode line moves.
            quiet = self._current() is None and self._song is not None
            answering: asyncio.Task[None] | None = None
            pushed = False
            if hint is not None and not interaction.response.is_done():
                self._hint = hint
                if quiet and kind in ("repeat", "shuffle"):
                    self._apply_mode_hint(hint)
                else:
                    # The progress picture stays. A button edit does not spend a new frame.
                    self.paint(move_bar=False)
                if early:
                    answering = asyncio.create_task(self._push(interaction))
                    pushed = True
                else:
                    try:
                        await self._push(interaction)
                        shown = True
                    except discord.HTTPException:
                        log.warning("music player could not show the control", exc_info=True)
                        if not quiet:
                            self.paint(move_bar=False)
            if answering is None and not shown:
                if early:
                    answering = asyncio.create_task(self._ack(interaction))
                else:
                    await self._ack(interaction)
            try:
                note = await running if running is not None else await call()
            except Exception:
                log.exception("music control failed")
                note = "Something went wrong while using the player."
            if answering is not None:
                try:
                    await answering
                except discord.HTTPException:
                    if not pushed:
                        raise
                    log.warning("music player could not show the control", exc_info=True)
                    if not quiet:
                        self.paint(move_bar=False)
                else:
                    if pushed:
                        shown = True
            if not self._wish.owns(token):
                return
            self._hint = None
            self._wish.rest()
            if note:
                await respond(interaction, notice_text(note), ephemeral=True)
            if self._closed:
                return
            # The bot can be back before this card's guild object can see it.
            if self._current() is None and getattr(self.guild, "voice_client", None) is None:
                self._adopt_guild()
            if self._current() is None:
                # Play just brought the song back. Closing here kills the button.
                if kind == "pause" and not note:
                    return
                step = after_quiet(
                    kind,
                    failed=bool(note),
                    showing=self._song is not None,
                    holding=self._holding(),
                )
                if step == "stay" and kind in ("repeat", "shuffle") and not note and self._song is not None:
                    # The refresh that follows this press must not take the card down.
                    self._mode_press = True
                    self._show_mode(*self._modes())
                    try:
                        await self._push(interaction)
                    except discord.HTTPException:
                        log.exception("music player could not update")
                    return
                if step == "restore" and self._shown is not None:
                    self._show_mode(*self._modes())
                    try:
                        await self._push(interaction)
                    except discord.HTTPException:
                        log.exception("music player could not update")
                    return
                if step == "dismiss":
                    # The message still has the song. Show the empty player, then take it down.
                    self._song = None
                    self.paint()
                    try:
                        await self._push(interaction)
                    except discord.HTTPException:
                        log.warning("music player could not show the end", exc_info=True)
                    await self.dismiss()
                return
            state = self._current()
            key = None if state is None else _bar_key(state)
            # A different song stays on this message. The picture opens on the frame already up.
            song_moved = key is not None and self._song is not None and key != self._song
            # Skip, back, and a typed seek need a new frame. A mode button leaves the one up.
            move_bar = kind in ("next", "previous", "seek") or song_moved
            self._mode_press = False
            if key is not None:
                self._song = key
            try:
                if self.paint(move_bar=move_bar):
                    await self._push(interaction)
                elif not shown:
                    await self._ack(interaction)
            except discord.HTTPException:
                log.exception("music player could not update")
                if not interaction.response.is_done():
                    await interaction.response.defer()

    async def land(self) -> bool:
        """The preview has had its chance. True when a song is on and the card stayed."""
        async with self._lock:
            self._coming = None
            if self._current() is None:
                away = True
            else:
                away = False
                if self.paint():
                    try:
                        await self._push(None)
                    except discord.HTTPException:
                        log.warning("music player could not show the song", exc_info=True)
        if away:
            await self.dismiss()
            return False
        return True

    async def redraw(self, interaction: discord.Interaction | None = None) -> None:
        async with self._lock:
            if self._closed and interaction is None:
                return
            state = self._current()
            if state is None:
                pressed = self._mode_press
                stay, self._mode_spare = spend_mode(pressed, self._mode_spare)
                self._mode_press = False
                if card_stays(self._holding(), stay):
                    if pressed and self._song is not None:
                        self._show_mode(*self._modes())
                    if pressed:
                        try:
                            await self._push(interaction)
                        except discord.HTTPException:
                            log.warning("music player could not show the mode", exc_info=True)
                        return
                    await self._ack(interaction)
                    return
                if self._closed:
                    await self._ack(interaction)
                    return
                if interaction is not None and not interaction.response.is_done():
                    await self._ack(interaction)
                await self.dismiss()
                return
            self._song = _bar_key(state)
            if self.paint():
                await self._push(interaction)
                return
            await self._ack(interaction)

    async def _on_line(self, guild: object) -> None:
        """A song started or joined the queue. Paint now instead of waiting for the tick."""
        if self._closed or getattr(guild, "id", None) != getattr(self.guild, "id", None):
            return
        await self.redraw()

    async def _tick(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(REFRESH_SECONDS)
                if self._closed:
                    return
                self._adopt_guild()
                if self._left():
                    await self.dismiss()
                    return
                if self._current() is None:
                    async with self._lock:
                        if self._current() is None:
                            stay, self._mode_spare = spend_mode(self._mode_press, self._mode_spare)
                            self._mode_press = False
                            if card_stays(self._holding(), stay):
                                continue
                            await self.dismiss()
                            return
                try:
                    await self.redraw()
                except discord.NotFound:
                    self.shutdown()
                    return
                except discord.HTTPException as exc:
                    if exc.status != 429:
                        log.warning("music player could not update", exc_info=True)
                    continue
        except asyncio.CancelledError:
            raise

    async def _ack(self, interaction: discord.Interaction | None) -> None:
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.defer()
