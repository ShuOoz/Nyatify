"""Remote for the song already playing in a voice channel."""

from __future__ import annotations

import logging
import asyncio
import discord

from announce import ACCENT_COLOR, notice_text, respond

from music.panel import (
    RADIO_MARK,
    delete_panel,
    meta_line,
    progress_line,
    song_label,
    song_line,
)

from music.playback import (
    JUMP_MS,
    VOLUME_STEP,
    change_volume,
    member_in_voice,
    next_track,
    now_playing,
    nudge,
    previous_track,
    repeat_track,
    suggest_next,
    toggle_pause,
    unwatch_line,
    watch_line,
)

log = logging.getLogger(__name__)

# One edit every five seconds stays inside the channel's message-edit budget.
REFRESH_SECONDS = 5
_PAUSE = "⏸"
_PLAY = "▶"



def _status(state: PlayState, volume: int | None = None) -> str:
    lines = [f"Volume {state.volume if volume is None else volume}"]
    if state.upcoming:
        lines.append(f"Up next: {state.upcoming}")
    return "\n".join(lines)



class _Control(discord.ui.Button):
    def __init__(self, emoji: str, custom_id: str, method: str) -> None:
        super().__init__(emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=custom_id)
        self.method = method

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if isinstance(panel, MusicPlayer):
            await getattr(panel, self.method)(interaction)



class MusicPlayer(discord.ui.LayoutView):
    """Title and artist beside the art, the bar between the times, then the controls."""

    def __init__(
        self,
        guild: object,
        *,
        discard: Callable[[MusicPlayer], None] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.guild = guild
        self.message: discord.Message | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._discard = discard
        self._lock = asyncio.Lock()
        self._shown: tuple | None = None
        self._watch = None
        self._hint: tuple[str, object] | None = None
        self._box: discord.ui.Container | None = None
        self.heading = discord.ui.TextDisplay("**Nothing is playing**")
        self.song = discord.ui.TextDisplay("…")
        self.meta = discord.ui.TextDisplay("")
        self.time = discord.ui.TextDisplay("…")
        self.previous = _Control("⏮", "music-previous", "on_previous")
        self.pause = _Control(_PAUSE, "music-pause", "on_pause")
        self.skip = _Control("⏭", "music-next", "on_next")
        self.quieter = _Control("🔉", "music-quieter", "on_quieter")
        self.louder = _Control("🔊", "music-louder", "on_louder")
        self.rewind = _Control("⏪", "music-rewind", "on_rewind")
        self.forward = _Control("⏩", "music-forward", "on_forward")
        self.radio = _Control("📻", "music-radio", "on_radio")
        self.repeat = _Control("🔂", "music-repeat", "on_repeat")
        self.close = _Control("❌", "music-close", "on_close")
        self.paint()

    def follow(self, message: discord.Message) -> None:
        self.message = message
        if self._watch is None:
            self._watch = self._on_line
            watch_line(self._watch)
        self._task = asyncio.create_task(self._tick(), name="music-player")

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop()
        watch = self._watch
        self._watch = None
        if watch is not None:
            unwatch_line(watch)
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        drop = self._discard
        self._discard = None
        if drop is not None:
            drop(self)

    async def on_timeout(self) -> None:
        await self.dismiss()

    async def dismiss(self) -> None:
        """Remove the card. The song keeps playing."""
        message = self.message
        self.shutdown()
        await delete_panel(message)

    def paint(self) -> bool:
        state = now_playing(self.guild)
        shown = self._parts(state)
        if shown == self._shown:
            return False
        self._shown = shown
        self.heading.content = shown[0]
        self.song.content = shown[1]
        self.meta.content = shown[2]
        self.time.content = shown[3]
        self.pause.emoji = shown[4]
        self.pause.disabled = shown[5]
        self.rewind.disabled = shown[6]
        self.forward.disabled = shown[6]
        self.repeat.disabled = shown[7]
        self.repeat.style = shown[8]
        self.previous.disabled = shown[9]
        self.skip.disabled = shown[9]
        self.quieter.disabled = shown[9]
        self.louder.disabled = shown[9]
        self.radio.disabled = shown[9]
        self._mount(state)
        return True

    async def on_previous(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: previous_track(self.guild))

    async def on_rewind(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: nudge(self.guild, -JUMP_MS), "rewind")

    async def on_pause(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: toggle_pause(self.guild), "pause")

    async def on_forward(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: nudge(self.guild, JUMP_MS), "forward")

    async def on_next(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: next_track(self.guild))

    async def on_quieter(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: change_volume(self.guild, -VOLUME_STEP), "quieter")

    async def on_louder(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: change_volume(self.guild, VOLUME_STEP), "louder")

    async def on_radio(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: suggest_next(self.guild, interaction.user))

    async def on_repeat(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: repeat_track(self.guild), "repeat")

    async def on_close(self, interaction: discord.Interaction) -> None:
        """Drop the card. Stopping the song stays on /music stop."""
        await self._ack(interaction)
        self.shutdown()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            log.warning("music player could not close", exc_info=True)

    async def redraw(self, interaction: discord.Interaction | None = None) -> None:
        async with self._lock:
            if self._closed and interaction is None:
                return
            if not self._closed and self.paint():
                await self._push(interaction)
                return
            await self._ack(interaction)

    def _parts(self, state: PlayState | None) -> tuple:
        if state is None or not state.has_track:
            channel = None if state is None else state.channel_id
            return (
                "**Nothing is playing**",
                "",
                meta_line(None, channel),
                "",
                _PLAY,
                True,
                True,
                True,
                discord.ButtonStyle.secondary,
                True,
            )
        paused = state.paused
        volume = state.volume
        position = state.position
        repeating = state.repeating
        hint = self._hint
        if hint is not None:
            kind, value = hint
            if kind == "pause" and isinstance(value, bool):
                paused = value
            elif kind == "volume" and isinstance(value, int):
                volume = value
            elif kind == "seek" and isinstance(value, int):
                position = value
            elif kind == "repeat" and isinstance(value, bool):
                repeating = value
        return (
            "",
            song_line(state.title, state.author, state.uri),
            _status(state, volume),
            RADIO_MARK if state.stream else progress_line(position, state.length),
            _PLAY if paused else _PAUSE,
            False,
            state.stream or state.length <= 0,
            state.stream or state.length <= 0,
            discord.ButtonStyle.primary if repeating else discord.ButtonStyle.secondary,
            False,
        )

    def _mount(self, state: PlayState | None) -> None:
        box = self._box
        if box is not None:
            self.remove_item(box)
        box = discord.ui.Container(accent_colour=ACCENT_COLOR)
        playing = state is not None and state.has_track
        if playing and state.artwork:
            thumb = discord.ui.Thumbnail(
                state.artwork,
                description=song_label(state.title, state.author)[:256],
            )
            box.add_item(discord.ui.Section(self.song, self.time, self.meta, accessory=thumb))
        elif playing:
            box.add_item(self.song)
            box.add_item(self.time)
            box.add_item(self.meta)
        else:
            box.add_item(self.heading)
            if self.meta.content:
                box.add_item(self.meta)
        box.add_item(self._row(self.previous, self.pause, self.skip, self.quieter, self.louder))
        box.add_item(self._row(self.rewind, self.forward, self.radio, self.repeat, self.close))
        self._box = box
        self.add_item(box)

    def _row(self, *buttons: _Control) -> discord.ui.ActionRow:
        row = discord.ui.ActionRow()
        for button in buttons:
            row.add_item(button)
        return row

    def _preview(self, kind: str) -> tuple[str, object] | None:
        """The control's next frame, so the card can move before Lavalink answers."""
        state = now_playing(self.guild)
        if state is None or not state.has_track:
            return None
        if kind == "pause":
            return ("pause", not state.paused)
        if kind == "quieter":
            return ("volume", max(0, state.volume - VOLUME_STEP))
        if kind == "louder":
            return ("volume", min(100, state.volume + VOLUME_STEP))
        if kind in ("rewind", "forward"):
            if state.stream or state.length <= 0:
                return None
            step = -JUMP_MS if kind == "rewind" else JUMP_MS
            return ("seek", min(max(0, state.position + step), state.length))
        if kind == "repeat":
            if state.stream or state.length <= 0:
                return None
            return ("repeat", not state.repeating)
        return None

    async def _act(
        self,
        interaction: discord.Interaction,
        call: Callable[[], Awaitable[str | None]],
        kind: str | None = None,
    ) -> None:
        if not member_in_voice(interaction.user, self.guild):
            await respond(interaction, notice_text("Join the voice channel first."), ephemeral=True)
            return
        async with self._lock:
            shown = False
            hint = None if kind is None or interaction.response.is_done() else self._preview(kind)
            if hint is not None:
                self._hint = hint
                self.paint()
                self._hint = None
                try:
                    await self._push(interaction)
                    shown = True
                except discord.HTTPException:
                    log.warning("music player could not show the control", exc_info=True)
                    self.paint()
            if not shown:
                await self._ack(interaction)
            try:
                note = await call()
            except Exception:
                log.exception("music control failed")
                note = "Something went wrong while using the player."
            if note:
                await respond(interaction, notice_text(note), ephemeral=True)
            if self._closed:
                return
            try:
                if self.paint():
                    await self._push(interaction)
                elif not shown:
                    await self._ack(interaction)
            except discord.HTTPException:
                log.exception("music player could not update")
                if not interaction.response.is_done():
                    await interaction.response.defer()

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
                if now_playing(self.guild) is None:
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

    async def _push(self, interaction: discord.Interaction | None) -> None:
        mentions = discord.AllowedMentions.none()
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(view=self, allowed_mentions=mentions)
            return
        if self.message is not None:
            await self.message.edit(view=self, allowed_mentions=mentions)

    async def _ack(self, interaction: discord.Interaction | None) -> None:
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.defer()
