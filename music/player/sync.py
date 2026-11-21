"""Keep the player card matched to the song."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable

import discord

from announce import notice_text, respond
from music.panel import _mode, click_answer
from music.playback import PlayState, _stored, now_playing
from music.playback.remember import _movable, _on_air, _song_name
from music.player.playbar import _bar_key
from music.player.rule import card_first, hinted_modes, mode_press, skip_step

log = logging.getLogger(__name__)

# One edit every five seconds stays inside the channel's message-edit budget.
REFRESH_SECONDS = 5

_MODE_LINES = (
    "-# On Queue", "-# On Repeat", "-# On Shuffle", "-# On Radio", "-# On Repeat + Radio",
)


def card_stays(holding: bool, mode_press: bool) -> bool:
    """The gap between songs, or a mode press while nothing is on, keeps the card."""
    return holding or mode_press


def spend_mode(pressed: bool, spare: bool) -> tuple[bool, bool]:
    """Whether this refresh keeps the card, and whether one sibling refresh still must."""
    if pressed:
        return True, True
    return spare, False


def shows_mode(kind: str | None) -> bool:
    """Repeat, shuffle, and radio rewrite the grey line while the song stays."""
    return kind in ("repeat", "shuffle", "radio")


def after_quiet(kind: str | None, *, failed: bool, showing: bool, holding: bool) -> str:
    """stay, restore, or dismiss.

    A mode press on a quiet player keeps the card. A failed press while a song
    is still coming puts the real buttons back. An empty room takes the card down.
    """
    if kind in ("repeat", "shuffle") and not failed and showing:
        return "stay"
    if failed and showing and holding:
        return "restore"
    return "stay" if holding else "dismiss"


def _replace_mode(text: str, repeating: bool, shuffling: bool, radio: bool = False) -> str:
    fresh = f"-# {_mode(repeating, shuffling, radio)}"
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line in _MODE_LINES:
            lines[index] = fresh
            return "\n".join(lines)
    return text


def _replace_next(text: str, line: str) -> str:
    """Swap the up-next line. A card with no such line stays as it is."""
    prefix = "-# Up next: "
    lines = text.split("\n")
    for index, item in enumerate(lines):
        if item.startswith(prefix):
            lines[index] = line
            return "\n".join(lines)
    return text


class _Preview:
    """Modes, the queued song, and the line under the one that is on."""

    def _modes(self) -> tuple[bool, bool, bool]:
        """Repeat, shuffle, and radio on the line. A quiet player still has them."""
        line = _stored(self.guild)
        if line is not None:
            return line.repeat, line.shuffle, line.radio
        state = now_playing(self.guild)
        if state is None:
            return False, False, False
        return state.repeating, state.shuffling, state.radio

    def _hinted(
        self, hint: tuple[str, object] | None, state: PlayState | None,
    ) -> tuple[bool, bool, bool, bool]:
        """Pause and the mode triple, with a press painted before Lavalink answers."""
        repeating, shuffling, radio = self._modes()
        paused = bool(state is not None and state.paused)
        if hint is not None:
            kind, value = hint
            if kind == "pause" and isinstance(value, bool):
                paused = value
            elif shows_mode(kind) and isinstance(value, bool):
                repeating, shuffling, radio = hinted_modes(repeating, shuffling, radio, kind, value)
        return paused, repeating, shuffling, radio

    def _shown_modes(self) -> tuple[bool, bool, bool, bool]:
        return self._hinted(self._hint, now_playing(self.guild))

    def _apply_mode_hint(self, hint: tuple[str, object]) -> None:
        self._show_mode(*self._hinted(hint, None)[1:])

    def _show_mode(self, repeating: bool, shuffling: bool, radio: bool = False) -> None:
        """The grey line and the mode buttons. The song and the queue stay."""
        styles = tuple(
            discord.ButtonStyle.primary if on else discord.ButtonStyle.secondary
            for on in (repeating, shuffling, radio)
        )
        self.repeat.style, self.shuffle.style, self.radio.style = styles
        # On Radio turns shuffle off, the same as a full paint.
        self.shuffle.disabled = radio
        named = self._named_next()

        def write(text: str) -> str:
            return self._replace_next(self._replace_mode(text, repeating, shuffling, radio), named)

        self.song.content = write(self.song.content)
        self.meta.content = write(self.meta.content)
        if self._shown is None:
            return
        shown = list(self._shown)
        shown[2] = write(str(shown[2]))
        shown[7], shown[9], shown[10], shown[11] = *styles, radio
        # Nothing is on, so a station is not holding the Last.fm button.
        if len(shown) > 12:
            shown[12] = False
        self._shown = tuple(shown)

    def _preview(self, kind: str) -> tuple[str, object] | None:
        """The control's next frame, so the card can move before Lavalink answers."""
        state = now_playing(self.guild)
        line = _stored(self.guild)
        paused, repeating, shuffling, radio = self._shown_modes()
        live = (state is not None and state.has_track) or getattr(self, "_coming", None) is not None
        if kind == "pause":
            if not live or state is None or not state.has_track:
                return None
            return ("pause", not paused)
        if kind == "next":
            # A radio skip is not the queue title. Repeating this song is not either.
            if (radio and not repeating) or line is None:
                return None
            song = line.current
            length = state.length if state is not None and state.has_track else 0
            if song is not None and length <= 0:
                raw = getattr(song.track, "length", None)
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    length = raw
            waiting = bool(line.later or line.held)
            if skip_step(
                repeating=repeating,
                videos=bool(song is not None and song.videos),
                length=length,
                waiting=waiting,
            ) == "repeat":
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
        station = state is not None and state.stream
        return mode_press(kind, repeating, shuffling, radio, live=live, stuck=stuck, station=station)

    def _can_post(self) -> bool:
        """The card is already in the channel, so the next song can be its own message."""
        message = getattr(self, "message", None)
        send = getattr(getattr(message, "channel", None), "send", None)
        return inspect.iscoroutinefunction(send)

    def _queued_song(self, kind: str | None, hint: tuple[str, object] | None) -> object | None:
        """The song this press will start, when that song is already known."""
        if kind == "next" and isinstance(hint, tuple) and hint[0] == "next":
            song = hint[1]
        elif kind == "previous":
            line = _stored(self.guild)
            song = None if line is None or not line.earlier else line.earlier[-1]
        else:
            return None
        title = getattr(getattr(song, "track", None), "title", None)
        return song if isinstance(title, str) and title.strip() else None

    def _after(self, song: object) -> str | None:
        """The name under Up next once this song is the one on."""
        line = _stored(self.guild)
        if line is None:
            return None
        later = line.later
        for index, item in enumerate(later):
            if item is song:
                return _song_name(later[index + 1]) if index + 1 < len(later) else None
        # On Radio the song just left waits as the next radio song. The queue stays.
        if line.radio and not line.repeat:
            return _song_name(later[0]) if later else None
        current = line.current
        return None if current is None or current is song else _song_name(current)

    def _frame(self, song: object) -> PlayState:
        """The card for a song that has not started. The slider and the modes stay."""
        from music.player.card import preview_state

        track = getattr(song, "track", song)
        shown = preview_state(track)
        line = _stored(self.guild)
        live = now_playing(self.guild)
        who = getattr(song, "requester_id", None)
        if isinstance(who, bool) or not isinstance(who, int):
            who = None if live is None else live.requester_id
        modes = (False, False, False) if line is None else (line.repeat, line.shuffle, line.radio)
        return PlayState(
            shown.title, shown.author, shown.uri, shown.artwork,
            0, shown.length, False, who,
            None if live is None else live.channel_id,
            100 if line is None else line.volume,
            self._after(song), True, _on_air(line, track),
            *modes,
            None if line is None or line.radio_next is None else line.radio_next[1],
        )


class _Refresh(_Preview):
    """Buttons and the tick keep the open card on the song."""

    _replace_mode = staticmethod(_replace_mode)
    _replace_next = staticmethod(_replace_next)

    async def _send_with_song(
        self,
        interaction: discord.Interaction,
        call: Callable[[], Awaitable[str | None]],
        song: object,
    ) -> tuple[str | None, bool]:
        """Post the next song and start it together.

        The message in the channel is the panel, so it has to leave with the song.
        """
        frame = self._frame(song)
        self._ahead = frame
        posting: asyncio.Task[bool] | None = None
        note: str | None = None
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
            posting = asyncio.create_task(self._renew())
            await asyncio.sleep(0)
            self._song = _bar_key(frame)
            self._ahead = None
            note = await self._run_control(call)
        finally:
            self._ahead = None
            shown = False
            if posting is not None:
                try:
                    shown = await posting
                except discord.HTTPException:
                    log.warning("music player could not show the control", exc_info=True)
                    shown = False
        return note, shown

    async def _act(
        self,
        interaction: discord.Interaction,
        call: Callable[[], Awaitable[str | None]],
        kind: str | None = None,
        *,
        room: bool = True,
    ) -> None:
        allowed = self._can_edit(interaction) if not room else self._inside(interaction)
        if not allowed:
            await respond(interaction, notice_text("Join the voice channel first."), ephemeral=True)
            return
        # A second tap of a press that is already landing stops here.
        if click_answer(kind or "control", pressing=self._pressing) == "drop":
            await interaction.response.defer()
            return
        claimed = click_answer(kind or "control", pressing=True) == "drop"
        if claimed:
            self._pressing = True
        try:
            if claimed and self._lock.locked():
                self._draw += 1
                if not interaction.response.is_done():
                    await interaction.response.defer()
                # Skip and back wait for this picture. Pause can change the song now.
                play_now = not card_first(kind)
                if play_now:
                    note = await self._run_control(call)
                async with self._lock:
                    if not play_now:
                        note = await self._run_control(call)
                    await self._show_after(
                        interaction, kind, note, token=self._wish.claim(), shown=False,
                    )
                return
            if self._lock.locked() and not interaction.response.is_done():
                await interaction.response.defer()
            async with self._lock:
                token = self._wish.claim()
                hint = None if kind is None else self._preview(kind)
                coming = self._queued_song(kind, hint)
                if coming is not None and self._can_post():
                    note, shown = await self._send_with_song(interaction, call, coming)
                    await self._show_after(interaction, kind, note, token=token, shown=shown)
                    return
                shown = False
                early = kind in ("pause", "next", "previous")
                quiet = self._current() is None and self._song is not None
                answering: asyncio.Task[None] | None = None
                pushed = False
                # A press paints before Lavalink answers.
                if hint is not None and (early or not interaction.response.is_done()):
                    self._hint = hint
                    if quiet and shows_mode(kind):
                        self._apply_mode_hint(hint)
                        self._apply_offs(True)
                    else:
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
                if early and answering is not None:
                    await asyncio.sleep(0)
                control = self._run_control(call)
                note = await asyncio.create_task(control) if early else await control
                if answering is not None and await self._finish_draw(answering, pushed=pushed, quiet=quiet):
                    shown = True
                await self._show_after(interaction, kind, note, token=token, shown=shown)
        finally:
            if claimed:
                self._pressing = False

    async def _finish_draw(self, answering: asyncio.Task[None], *, pushed: bool, quiet: bool) -> bool:
        """True when the preview reached the card."""
        try:
            await answering
        except discord.HTTPException:
            if not pushed:
                raise
            log.warning("music player could not show the control", exc_info=True)
            if not quiet:
                self.paint(move_bar=False)
            return False
        return pushed

    async def _run_control(self, call: Callable[[], Awaitable[str | None]]) -> str | None:
        try:
            return await call()
        except Exception:
            log.exception("music control failed")
            return "Something went wrong while using the player."

    async def _show_after(
        self,
        interaction: discord.Interaction,
        kind: str | None,
        note: str | None,
        *,
        token: int,
        shown: bool,
    ) -> None:
        if not self._wish.owns(token):
            self._hint = None
            await self._ungrey(interaction)
            return
        self._hint = None
        self._wish.rest()
        if note:
            await respond(interaction, notice_text(note), ephemeral=True)
        if self._closed:
            return
        # The guild on the card can still be the one from before the bot returned.
        if self._current() is None and getattr(self.guild, "voice_client", None) is None:
            self._adopt_guild()
        if self._current() is None:
            # Play just brought the song back. Closing here kills the button.
            if kind == "pause" and not note:
                await self._ungrey(interaction)
                return
            step = after_quiet(
                kind,
                failed=bool(note),
                showing=self._song is not None,
                holding=self._holding(),
            )
            kept = step == "stay" and shows_mode(kind) and not note and self._song is not None
            if kept or (step == "restore" and self._shown is not None):
                if kept:
                    # The refresh after this press must not take the card down.
                    self._mode_press = True
                self._show_mode(*self._modes())
                self._apply_offs(False)
                try:
                    await self._push(interaction)
                except discord.HTTPException:
                    log.exception("music player could not update")
                return
            if step == "dismiss":
                # The message still shows the song. Paint the empty player, then take it down.
                self._song = None
                self.paint()
                try:
                    await self._push(interaction)
                except discord.HTTPException:
                    log.warning("music player could not show the end", exc_info=True)
                await self.dismiss()
            await self._ungrey(interaction)
            return
        state = self._current()
        key = None if state is None else _bar_key(state)
        song_moved = key is not None and self._song is not None and key != self._song
        # Repeat is the same song, so the message already up stays.
        switched = song_moved and state is not None and not state.repeating
        move_bar = kind in ("next", "previous", "seek") or song_moved
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
            away = self._current() is None
            if not away and self.paint():
                try:
                    await self._push(None)
                except discord.HTTPException:
                    log.warning("music player could not show the song", exc_info=True)
        if away:
            await self.dismiss()
        return not away

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
            key = _bar_key(state)
            # A different song is a new message. Repeat keeps the one already up.
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
