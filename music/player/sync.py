"""Keep the player card matched to the song."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable

import discord

from announce import notice_text, respond
from music.panel import delete_panel
from music.playback import _stored, now_playing
from music.playback.remember import _movable, _on_air
from music.player.playbar import _bar_key
from music.player.card import _replace_mode

log = logging.getLogger(__name__)

# One edit every five seconds stays inside the channel's message-edit budget.
REFRESH_SECONDS = 5


class _Refresh:
    """Buttons, the tick, and a new message when the song changes."""

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
        kind, value = hint
        if kind == "repeat" and isinstance(value, bool):
            repeating = value
            if value:
                shuffling = False
        elif kind == "shuffle" and isinstance(value, bool):
            shuffling = value
            if value:
                repeating = False
        self._show_mode(repeating, shuffling)

    def _preview(self, kind: str) -> tuple[str, object] | None:
        """The control's next frame, so the card can move before Lavalink answers."""
        state = now_playing(self.guild)
        line = _stored(self.guild)
        repeating, shuffling = self._modes()
        radio = state.radio if state is not None else bool(line is not None and line.radio)
        live = state is not None and state.has_track
        if kind == "pause":
            if not live or state is None:
                return None
            return ("pause", not state.paused)
        if kind == "repeat":
            stuck = live and state is not None and (state.stream or state.length <= 0)
            track = None if line is None or line.current is None else line.current.track
            if not live and track is not None and (_on_air(line, track) or not _movable(track)):
                stuck = True
            # Turning repeat off is still allowed. The stream only blocks turning it on.
            if stuck and not repeating:
                return None
            return ("repeat", not repeating)
        if kind == "shuffle":
            if radio:
                return None
            return ("shuffle", not shuffling)
        if kind == "radio":
            if not live or state is None:
                return None
            return ("radio", not state.radio)
        return None

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
        hint = None if kind is None else self._preview(kind)
        pending = asyncio.create_task(call())
        if self._lock.locked() and not interaction.response.is_done():
            await interaction.response.defer()
        async with self._lock:
            shown = False
            # A quiet player keeps the song on screen. Only the mode line moves.
            quiet = self._current() is None and self._song is not None
            if hint is not None and not interaction.response.is_done():
                self._hint = hint
                if quiet and kind in ("repeat", "shuffle"):
                    self._apply_mode_hint(hint)
                else:
                    self.paint()
                try:
                    await self._push(interaction)
                    shown = True
                except discord.HTTPException:
                    log.warning("music player could not show the control", exc_info=True)
                    if not quiet:
                        self.paint()
            if not shown:
                await self._ack(interaction)
            try:
                note = await pending
            except Exception:
                log.exception("music control failed")
                note = "Something went wrong while using the player."
            self._hint = None
            if note:
                await respond(interaction, notice_text(note), ephemeral=True)
            if self._closed:
                return
            if self._current() is None:
                if kind in ("repeat", "shuffle") and not note and self._song is not None:
                    # The refresh that follows this press must not take the card down.
                    self._mode_press = True
                    self._show_mode(*self._modes())
                    try:
                        await self._push(interaction)
                    except discord.HTTPException:
                        log.exception("music player could not update")
                    return
                if not self._holding():
                    await self.dismiss()
                return
            state = self._current()
            key = None if state is None else _bar_key(state)
            # A different song is its own message, with its own picture.
            switched = (
                state is not None
                and key is not None
                and self._song is not None
                and key != self._song
                and not state.repeating
            )
            self._mode_press = False
            if switched:
                self._song = key
                if not interaction.response.is_done():
                    await self._ack(interaction)
                if await self._renew():
                    return
            elif key is not None:
                self._song = key
            try:
                if self.paint():
                    await self._push(interaction)
                elif not shown:
                    await self._ack(interaction)
            except discord.HTTPException:
                log.exception("music player could not update")
                if not interaction.response.is_done():
                    await interaction.response.defer()

    async def _renew(self) -> bool:
        """Post this card again, then remove the previous message."""
        message = self.message
        send = getattr(getattr(message, "channel", None), "send", None)
        if not inspect.iscoroutinefunction(send):
            return False
        # The new message needs its own picture. The old file stays on the old message.
        self._bars.bar = None
        self.paint()
        upload = self.take_bar()
        extra = {"file": upload} if upload is not None else {}
        try:
            sent = await send(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
                **extra,
            )
        except discord.HTTPException:
            log.warning("music player could not post the next song", exc_info=True)
            self._bars.bar = None
            return False
        self.message = sent
        await self._cite(sent)
        await delete_panel(message)
        return True

    async def redraw(self, interaction: discord.Interaction | None = None) -> None:
        async with self._lock:
            if self._closed and interaction is None:
                return
            state = self._current()
            if state is None:
                pressed = self._mode_press
                self._mode_press = False
                if self._holding() or pressed:
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
            key = _bar_key(state)
            # A new message is how the channel learns a different song started,
            # and it is the card they can change right then. Repeat is that
            # same song, so the message already on screen stays.
            notify = (
                interaction is None
                and self._song is not None
                and key != self._song
                and not state.repeating
            )
            self._song = key
            if notify and await self._renew():
                return
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
                if self._left():
                    await self.dismiss()
                    return
                if self._current() is None:
                    pressed = self._mode_press
                    self._mode_press = False
                    if self._holding() or pressed:
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
