"""Remote for the song already playing in a voice channel."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord

from announce import ACCENT_COLOR, notice_text, respond
from music.playback import (
    JUMP_MS,
    VOLUME_STEP,
    PlayState,
    change_volume,
    member_in_voice,
    next_track,
    now_playing,
    nudge,
    previous_track,
    suggest_next,
    toggle_pause,
)

log = logging.getLogger(__name__)

# One edit every five seconds stays inside the channel's message-edit budget.
REFRESH_SECONDS = 5
# Discord stops a card this long after the last click. A later press only
# says the bot did not answer, so the card is removed instead.
PANEL_SECONDS = 180.0
_BAR = 24
_PAUSE = "⏸"
_PLAY = "▶"


async def delete_panel(message: discord.Message | None) -> None:
    """Remove a music card. A message that is already gone is left alone."""
    if message is None:
        return
    try:
        await message.delete()
    except discord.HTTPException:
        log.warning("music panel could not close", exc_info=True)


def format_clock(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def song_label(title: str, author: str) -> str:
    name = title.strip() or "Unknown"
    artist = author.strip()
    if not artist or artist.casefold() in name.casefold():
        return name
    return f"{name} - {artist}"


def _status(state: PlayState) -> str:
    lines = [f"Volume {state.volume}"]
    if state.upcoming:
        lines.append(f"Up next: {state.upcoming}")
    return "\n".join(lines)


def song_line(title: str, author: str, uri: str | None) -> str:
    """The title, linked when the address is a normal web address.

    The artist is a second line, and is left out when that name is already in the title.
    """
    name = title.strip() or "Unknown"
    linked = f"**{_link(name, uri)}**"
    artist = author.strip()
    if not artist or artist.casefold() in name.casefold():
        return linked
    return f"{linked}\n{artist}"


def meta_line(requester_id: int | None, channel_id: int | None) -> str:
    parts: list[str] = []
    if requester_id is not None:
        parts.append(f"<@{requester_id}>")
    if channel_id is not None:
        parts.append(f"<#{channel_id}>")
    return " | ".join(parts)


# A station has no length, so this stands in for the time bar.
RADIO_MARK = "-# Radio"


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


def progress_bar(position: int, length: int) -> str:
    """A text bar. A live stream has no length, so the bar stays empty."""
    if length <= 0:
        return "●" + "─" * (_BAR - 1)
    ratio = min(1.0, max(0.0, position / length))
    index = min(_BAR - 1, max(0, round(ratio * (_BAR - 1))))
    return "━" * index + "●" + "─" * (_BAR - 1 - index)


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
        self.close = _Control("❌", "music-close", "on_close")
        self.paint()

    def follow(self, message: discord.Message) -> None:
        self.message = message
        self._task = asyncio.create_task(self._tick(), name="music-player")

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop()
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
        self._mount(state)
        return True

    async def on_previous(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: previous_track(self.guild))

    async def on_rewind(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: nudge(self.guild, -JUMP_MS))

    async def on_pause(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: toggle_pause(self.guild))

    async def on_forward(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: nudge(self.guild, JUMP_MS))

    async def on_next(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: next_track(self.guild))

    async def on_quieter(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: change_volume(self.guild, -VOLUME_STEP))

    async def on_louder(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: change_volume(self.guild, VOLUME_STEP))

    async def on_radio(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: suggest_next(self.guild, interaction.user))

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
                False,
            )
        return (
            "",
            song_line(state.title, state.author, state.uri),
            _status(state),
            RADIO_MARK if state.stream else progress_line(state.position, state.length),
            _PLAY if state.paused else _PAUSE,
            False,
            state.stream,
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
        box.add_item(self._row(self.rewind, self.forward, self.radio, self.close))
        self._box = box
        self.add_item(box)

    def _row(self, *buttons: _Control) -> discord.ui.ActionRow:
        row = discord.ui.ActionRow()
        for button in buttons:
            row.add_item(button)
        return row

    async def _act(
        self,
        interaction: discord.Interaction,
        call: Callable[[], Awaitable[str | None]],
    ) -> None:
        if not member_in_voice(interaction.user, self.guild):
            await respond(interaction, notice_text("Join the voice channel first."), ephemeral=True)
            return
        await self._ack(interaction)
        try:
            note = await call()
        except Exception:
            log.exception("music control failed")
            note = "Something went wrong while using the player."
        if note:
            await respond(interaction, notice_text(note), ephemeral=True)
            return
        try:
            await self.redraw(interaction)
        except discord.HTTPException:
            log.exception("music player could not update")
            if not interaction.response.is_done():
                await interaction.response.defer()

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
