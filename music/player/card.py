"""Remote for the song already playing in a voice channel."""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
from collections.abc import Awaitable, Callable

import discord
from discord.components import MediaGalleryItem

from announce import ACCENT_COLOR, notice_text, respond
from music import source
from music.panel import (
    MARK_ANY,
    MARK_BACK,
    MARK_CLOSE,
    MARK_NEXT,
    MARK_SAVE,
    RADIO_MARK,
    _HALF,
    _LINE,
    _clip,
    _half,
    delete_panel,
    format_clock,
    meta_line,
    song_label,
    song_line,
)
from music.playback import (
    _stored,
    member_in_voice,
    next_track,
    now_playing,
    previous_track,
    queued,
    repeat_track,
    shuffle_queue,
    stop_track,
    toggle_pause,
    tune_track,
    unwatch_line,
    watch_line,
)
from music.player.album import _Album, _AlbumCard, _playlists, _record_id
from music.player.playbar import _BarClock, _bar_key
from music.player.radio import _Radio

log = logging.getLogger(__name__)

TITLE = "## Osaka Player"
VOLUME_MARK = "𖦤"
SHUFFLE_MARK = "֎"
REPEAT_MARK = "𖣠"
# A filled square while the song plays. U+FE0E keeps it a plain mark, not an emoji picture.
# ► while it is paused. It has no emoji picture, and beside the fillers it is 0.990 em
# against the square's 1.000 em.
PAUSE_MARK = "\u25fc\ufe0e"
PLAY_MARK = "\u25ba"
INFO_MARK = "ᝰ"
# The pointers and close are the shared panel marks, so the player matches the other cards.
PREVIOUS_MARK = MARK_BACK
NEXT_MARK = MARK_NEXT
CLOSE_MARK = MARK_CLOSE


# Two halfwidth fillers on each side, the same count, so the mark stays in
# the middle. A thin space is 0.139 em here and about 0.39 em on Windows,
# which wrapped each row of five. The phone drops the fillers and was keeping
# only the left thin spaces, so those marks leaned right. One more filler is
# 1.0 em and the Windows row wraps again, so the buttons differ by the marks
# alone. The upper row is 14.452 em and the lower row is 14.423 em.
def _fit(mark: str, *, hair: int = 0) -> str:
    side = _HALF * 2
    gap = _HAIR * hair
    return f"{side}{gap}{mark}{gap}{side}"


# Beside the same fillers the marks are 4.601 em in the upper row and 4.423 em in
# the lower row, so the lower row ran 0.178 em short. A hair space is 0.070 em on the
# Mac, and one on each side of close adds 0.140 em, which leaves 0.038 em.
_HAIR = "\u200a"


def _mode(repeating: bool, shuffling: bool) -> str:
    """Repeat, shuffle, or the queue's own order. Radio is only the button."""
    if repeating:
        return "On Repeat"
    if shuffling:
        return "On Shuffle"
    return "On Queue"


_MODE_LINES = ("-# On Queue", "-# On Repeat", "-# On Shuffle")


def _replace_mode(text: str, repeating: bool, shuffling: bool) -> str:
    """Swap the mode line. The song around it stays."""
    fresh = f"-# {_mode(repeating, shuffling)}"
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line in _MODE_LINES:
            lines[index] = fresh
            return "\n".join(lines)
    return text


def _status(state: PlayState, *, repeating: bool, shuffling: bool) -> str:
    """Grey lines under the artist. The mode line is the one directly above Up next."""
    lines = [f"-# {_mode(repeating, shuffling)}"]
    if state.stream:
        lines.append(RADIO_MARK)
    lines.append(_next_line(state.upcoming or "Nothing"))
    return "\n".join(lines)


def _cover_text(song: str, status: str) -> str:
    """Lines beside the cover. The title is a heading, so the gap sits under it.

    The artist is on the line above On Repeat.
    """
    if song and status:
        return f"{song}\n{status}"
    return song or status


def _next_line(upcoming: str) -> str:
    """Up next stays on one line. Wide characters take more of it than narrow ones."""
    prefix = "Up next: "
    name = _clip(" ".join(upcoming.split()), _LINE - sum(_half(char) for char in prefix))
    return f"-# {prefix}{name}"


def _hosted(message: object, name: str) -> str | None:
    """The link Discord stored for this upload."""
    found = getattr(message, "attachments", None)
    if not isinstance(found, (list, tuple)):
        return None
    for item in found:
        if getattr(item, "filename", None) != name:
            continue
        url = getattr(item, "url", None)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    return None


def _typed_level(text: str) -> int | None:
    cleaned = " ".join(text.split())
    if not cleaned.isdecimal():
        return None
    level = int(cleaned)
    if level > 100:
        return None
    return level


def _typed_clock(text: str) -> int | None:
    """Milliseconds from 1:30 or 1:02:03. None when the text is not a time."""
    parts = text.split(":")
    if not parts or len(parts) > 3 or any(not part.isdecimal() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    if any(part >= 60 for part in numbers[1:]):
        return None
    if len(numbers) == 1:
        total = numbers[0]
    elif len(numbers) == 2:
        total = numbers[0] * 60 + numbers[1]
    else:
        total = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    return total * 1000


def _menu_value(menu: discord.ui.Select, fallback: str) -> str:
    values = menu.values
    if values and isinstance(values[0], str) and values[0]:
        return source.normalize_platform(values[0])
    for option in menu.options:
        if option.default and isinstance(option.value, str):
            return source.normalize_platform(option.value)
    return fallback


def _playing_platform(guild: object) -> str:
    rows = queued(guild) or []
    current = next((row for row in rows if row.playing), None)
    site = None if current is None else current.platform
    return source.normalize_platform(site)


class _Volume(discord.ui.Modal):
    """Volume and the site start filled in. Duration stays empty until a time is typed."""

    def __init__(
        self,
        apply: Callable[
            [discord.Interaction, int, str | None, int | None],
            Awaitable[None],
        ],
        *,
        level: int,
        platform: str,
        length: int,
    ) -> None:
        super().__init__(title="Tune")
        self._apply = apply
        self._platform = source.normalize_platform(platform)
        self.level = discord.ui.Label(
            text="Volume",
            component=discord.ui.TextInput(
                placeholder="From 0 to 100",
                max_length=3,
                default=str(level),
            ),
        )
        self.sites = discord.ui.Select(
            custom_id="music-now-platform",
            placeholder=source.platform_label("", self._platform),
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=key, default=key == self._platform)
                for key, label in source.platform_pairs()
            ],
        )
        self.place = discord.ui.Label(
            text="Duration",
            component=discord.ui.TextInput(
                placeholder=f"Up to {format_clock(length)}" if length > 0 else "Live",
                max_length=8,
                required=False,
            ),
        )
        self.add_item(self.level)
        self.add_item(self.place)
        self.add_item(discord.ui.Label(text="Platform", component=self.sites))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        level = _typed_level(str(self.level.component))
        if level is None:
            await respond(interaction, notice_text("Type a volume from 0 to 100."), ephemeral=True)
            return
        typed = " ".join(str(self.place.component).split())
        position = None if not typed else _typed_clock(typed)
        if typed and position is None:
            await respond(interaction, notice_text("Type a time like 1:30."), ephemeral=True)
            return
        chosen = _menu_value(self.sites, self._platform)
        platform = None if chosen == self._platform else chosen
        await self._apply(interaction, level, platform, position)


class _Control(discord.ui.Button):
    def __init__(self, custom_id: str, method: str, *, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=custom_id)
        self.method = method

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if isinstance(panel, MusicPlayer):
            await getattr(panel, self.method)(interaction)


from music.player.sync import _Refresh


class MusicPlayer(_Album, _Radio, _Refresh, discord.ui.LayoutView):
    """The Osaka Player heading, the song beside the art, then the controls.

    The power button on the heading leaves the voice channel.
    A moving picture of the song sits between the song and the buttons.
    """

    def __init__(
        self,
        guild: object,
        *,
        discard: Callable[[MusicPlayer], None] | None = None,
        conceal: Callable[[MusicPlayer], None] | None = None,
        store: object | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.guild = guild
        self.message: discord.Message | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        # Set once the bot is seen in voice. The tick closes the card after that if the bot is out.
        self._voiced = getattr(guild, "voice_client", None) is not None
        self._discard = discard
        # The cross uses this. Leaving voice only removes the card.
        self._conceal = conceal
        self._lists = store
        self._lock = asyncio.Lock()
        # One album panel per person. The next press removes the one already open.
        self._albums: dict[int, tuple[object, _AlbumCard]] = {}
        self._album_lock = asyncio.Lock()
        self._shown: tuple | None = None
        self._face: tuple | None = None
        self._watch = None
        self._hint: tuple[str, object] | None = None
        # Repeat or shuffle while the player is quiet. The card stays up for that change.
        self._mode_press = False
        self._box: discord.ui.Container | None = None
        self._bars = _BarClock()
        self._gallery: discord.ui.MediaGallery | None = None
        # The song this card is already showing. A different one posts a new message.
        self._song: str | None = None
        self.heading = discord.ui.TextDisplay("**Nothing is playing**")
        self.song = discord.ui.TextDisplay("…")
        self.meta = discord.ui.TextDisplay("")
        self.time = discord.ui.TextDisplay("…")
        self.previous = _Control("music-previous", "on_previous", label=_fit(PREVIOUS_MARK))
        self.pause = _Control("music-pause", "on_pause", label=_fit(PAUSE_MARK))
        self.skip = _Control("music-next", "on_next", label=_fit(NEXT_MARK))
        self.volume = _Control("music-volume", "on_volume", label=_fit(VOLUME_MARK))
        self.radio = _Control("music-radio", "on_radio", label=_fit(MARK_ANY))
        self.save = _Control("music-save", "on_save", label=_fit(MARK_SAVE))
        self.info = _Control("music-info", "on_info", label=_fit(INFO_MARK))
        self.shuffle = _Control("music-shuffle", "on_shuffle", label=_fit(SHUFFLE_MARK))
        self.repeat = _Control("music-repeat", "on_repeat", label=_fit(REPEAT_MARK))
        self.close = _Control("music-close", "on_close", label=_fit(CLOSE_MARK, hair=1))
        self.leave = _Control("music-leave", "on_leave", label="⏻")
        self.paint()
        playing = self._current()
        if playing is not None:
            self._song = _bar_key(playing)

    def _inside(self, interaction: discord.Interaction) -> bool:
        """Use this click's guild. A card can still hold the guild from before a reconnect."""
        live = getattr(interaction, "guild", None)
        if isinstance(live, discord.Guild) and live.id == _record_id(self.guild):
            self.guild = live
        return member_in_voice(interaction.user, self.guild)

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
        self._conceal = None
        if drop is not None:
            drop(self)

    async def on_timeout(self) -> None:
        await self.dismiss()

    async def dismiss(self) -> None:
        """Remove the card. The song keeps playing."""
        message = self.message
        self.shutdown()
        await delete_panel(message)

    def take_bar(self) -> discord.File | None:
        """The picture for the next send. A seek or a pause uploads another."""
        fresh = self._bars.take()
        if fresh is None:
            return None
        name, picture = fresh
        return discord.File(io.BytesIO(picture), filename=name)

    async def _cite(self, message: object) -> None:
        """Point the bar at the link Discord stored for this upload.

        The other client loads that link with the card.
        """
        bar = self._bars.bar
        gallery = self._gallery
        if bar is None or gallery is None or not gallery.items:
            return
        if not str(gallery.items[0].media.url).startswith("attachment://"):
            return
        link = _hosted(message, bar.name)
        if link is None:
            return
        gallery.items = [MediaGalleryItem(link)]
        edit = getattr(message, "edit", None)
        if not inspect.iscoroutinefunction(edit):
            return
        try:
            await edit(view=self, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("music player could not show the bar", exc_info=True)

    def _hearing(self) -> bool:
        player = getattr(self.guild, "voice_client", None)
        return getattr(player, "current", None) is not None

    def _aim(self) -> None:
        bar = self._bars.bar
        gallery = self._gallery
        if bar is None or gallery is None:
            return
        gallery.items = [MediaGalleryItem(f"attachment://{bar.name}")]

    def paint(self) -> bool:
        state = now_playing(self.guild)
        shown = self._parts(state)
        paused = state is not None and state.has_track and shown[4] == discord.ButtonStyle.primary
        # This paint's upload. A picture from an earlier paint was already sent, or it is replaced below.
        self._bars.fresh = None
        # The bar is one frame. An edit leaves that frame where it is.
        bar_changed = self._bars.sync(
            state, paused=paused, hearing=self._hearing(),
        )
        if shown == self._shown and not bar_changed:
            return False
        self._shown = shown
        self.heading.content = shown[0]
        self.meta.content = shown[2]
        self.time.content = shown[3]
        # Fold the grey lines into the song while the cover is up. The heading
        # on the title opens the gap under the song name.
        covered = state is not None and state.has_track and bool(state.artwork)
        self.song.content = _cover_text(shown[1], shown[2]) if covered else shown[1]
        # Blurple means paused, and the mark is the triangle. Grey means playing.
        self.pause.style = shown[4]
        self.pause.label = _fit(PLAY_MARK if shown[4] == discord.ButtonStyle.primary else PAUSE_MARK)
        self.pause.disabled = shown[5]
        self.repeat.disabled = shown[6]
        self.repeat.style = shown[7]
        self.shuffle.disabled = shown[11]
        # Blurple means that mode is on. The two marks stay.
        self.shuffle.style = shown[9]
        self.radio.style = shown[10]
        self.previous.disabled = shown[8]
        self.skip.disabled = shown[8]
        self.volume.disabled = shown[8]
        self.radio.disabled = shown[8]
        self.save.disabled = shown[8]
        self.info.disabled = shown[8]
        # Words and buttons update in place. A new picture, or a line appearing, builds the cover again.
        shape = (
            state is not None and state.has_track,
            None if state is None else state.artwork,
            bool(self.time.content),
            bool(self.meta.content),
            self._bars.bar is not None,
        )
        if self._box is None or shape != self._face:
            self._face = shape
            self._mount(state)
        elif bar_changed:
            self._aim()
        return True

    def _parts(self, state: PlayState | None) -> tuple:
        if state is None or not state.has_track:
            channel = None if state is None else state.channel_id
            return (
                "**Nothing is playing**",
                "",
                meta_line(None, channel),
                "",
                discord.ButtonStyle.secondary,
                True,
                True,
                discord.ButtonStyle.secondary,
                True,
                discord.ButtonStyle.secondary,
                discord.ButtonStyle.secondary,
                True,
            )
        paused = state.paused
        repeating = state.repeating
        shuffling = state.shuffling
        radio = state.radio
        hint = self._hint
        if hint is not None:
            kind, value = hint
            if kind == "pause" and isinstance(value, bool):
                paused = value
            elif kind == "repeat" and isinstance(value, bool):
                repeating = value
                if value:
                    shuffling = False
            elif kind == "shuffle" and isinstance(value, bool):
                shuffling = value
                if value:
                    repeating = False
            elif kind == "radio" and isinstance(value, bool):
                radio = value
                if value:
                    shuffling = False
        line = song_line(state.title, state.author, state.uri)
        return (
            "",
            line,
            _status(state, repeating=repeating, shuffling=shuffling),
            "",
            discord.ButtonStyle.primary if paused else discord.ButtonStyle.secondary,
            False,
            # A livestream cannot repeat. Leave the button on when repeat is already
            # on, so it can be turned off instead of staying stuck.
            (state.stream or state.length <= 0) and not repeating,
            discord.ButtonStyle.primary if repeating else discord.ButtonStyle.secondary,
            False,
            discord.ButtonStyle.primary if shuffling else discord.ButtonStyle.secondary,
            discord.ButtonStyle.primary if radio else discord.ButtonStyle.secondary,
            radio,
        )

    def _mount(self, state: PlayState | None) -> None:
        box = self._box
        if box is not None:
            self.remove_item(box)
        box = discord.ui.Container(accent_colour=ACCENT_COLOR)
        box.add_item(discord.ui.Section(TITLE, accessory=self.leave))
        box.add_item(discord.ui.Separator())
        playing = state is not None and state.has_track
        beside: list[discord.ui.TextDisplay] = []
        if playing and state.artwork:
            thumb = discord.ui.Thumbnail(
                state.artwork,
                description=song_label(state.title, state.author)[:256],
            )
            # One block. The grey lines are already in the song text.
            beside = [self.song]
            if self.time.content:
                beside.append(self.time)
            box.add_item(discord.ui.Section(*beside, accessory=thumb))
        elif playing:
            box.add_item(self.song)
        else:
            box.add_item(self.heading)
        if not beside:
            if self.time.content:
                box.add_item(self.time)
            if self.meta.content:
                box.add_item(self.meta)
        # Under the song, above the buttons. The picture meets the end of the lower row.
        bar = self._bars.bar
        if bar is not None:
            gallery = discord.ui.MediaGallery()
            gallery.add_item(media=f"attachment://{bar.name}")
            self._gallery = gallery
            box.add_item(gallery)
        else:
            self._gallery = None
        box.add_item(discord.ui.Separator())
        box.add_item(self._row(self.shuffle, self.previous, self.pause, self.skip, self.repeat))
        box.add_item(self._row(self.save, self.info, self.radio, self.volume, self.close))
        self._box = box
        self.add_item(box)

    def _row(self, *buttons: discord.ui.Button) -> discord.ui.ActionRow:
        row = discord.ui.ActionRow()
        for button in buttons:
            row.add_item(button)
        return row

    async def _push(self, interaction: discord.Interaction | None) -> None:
        mentions = discord.AllowedMentions.none()
        upload = self.take_bar()
        extra = {"attachments": [upload]} if upload is not None else {}
        if interaction is not None and not interaction.response.is_done():
            result = await interaction.response.edit_message(view=self, allowed_mentions=mentions, **extra)
            await self._cite(getattr(result, "resource", None) or self.message)
            return
        if self.message is not None:
            edited = await self.message.edit(view=self, allowed_mentions=mentions, **extra)
            await self._cite(edited if edited is not None else self.message)

    async def on_previous(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: previous_track(self.guild, interaction.user))

    async def on_pause(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: toggle_pause(self.guild, interaction.user), "pause")

    async def on_next(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: next_track(self.guild, interaction.user))

    async def on_shuffle(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: shuffle_queue(self.guild), "shuffle")

    async def on_repeat(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: repeat_track(self.guild), "repeat")

    async def on_leave(self, interaction: discord.Interaction) -> None:
        """Leave the voice channel. The card goes with the bot."""
        if not self._inside(interaction):
            await respond(interaction, notice_text("Join the voice channel first."), ephemeral=True)
            return
        # Leave starts before the card answers, so the room does not wait on Discord.
        leaving = asyncio.create_task(stop_track(self.guild))
        await self._ack(interaction)
        try:
            note = await leaving
        except Exception:
            log.exception("music player could not leave")
            await respond(
                interaction,
                notice_text("Something went wrong while leaving the voice channel."),
                ephemeral=True,
            )
            return
        if note not in ("Stopped.", "Nothing is playing."):
            await respond(interaction, notice_text(note), ephemeral=True)
            if not self._closed and self.paint():
                try:
                    await self._push(interaction)
                except discord.HTTPException:
                    log.warning("music player could not update", exc_info=True)
            return
        self.shutdown()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            log.warning("music player could not close", exc_info=True)

    async def on_close(self, interaction: discord.Interaction) -> None:
        """Remove the card and keep the next songs from posting another one.

        Opening the player again is what lets a new song bring it back.
        The card is the room's, so someone outside it cannot put it away.
        """
        if not self._inside(interaction):
            await respond(interaction, notice_text("Join the voice channel first."), ephemeral=True)
            return
        conceal = self._conceal
        self._conceal = None
        if conceal is not None:
            conceal(self)
        self.shutdown()
        await self._ack(interaction)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            log.warning("music player could not close", exc_info=True)

    def _current(self) -> PlayState | None:
        """The song on now. An empty room does not keep a player card."""
        state = now_playing(self.guild)
        if state is None or not state.has_track:
            return None
        return state

    def _left(self) -> bool:
        """The bot was in voice while this card was up, and now it is out.

        A song saved for later still reads as the current one, so that alone no
        longer takes the card down. A card opened while the bot was already out
        stays, because nothing ever put it in a room.
        """
        if getattr(self.guild, "voice_client", None) is not None:
            self._voiced = True
            return False
        return self._voiced

    def _holding(self) -> bool:
        """The track dropped, and a song is still coming.

        Repeat is the one that just ended. A song still waiting is the next one.
        """
        state = now_playing(self.guild)
        if state is None or state.has_track:
            return False
        if state.repeating:
            return True
        line = _stored(self.guild)
        return line is not None and bool(line.later)

    async def on_volume(self, interaction: discord.Interaction) -> None:
        """Open the box for the volume, the site, and the place in the song."""
        if not self._inside(interaction):
            await respond(interaction, notice_text("Join the voice channel first."), ephemeral=True)
            return
        state = now_playing(self.guild)
        level = 100 if state is None else state.volume
        length = 0 if state is None else state.length
        await interaction.response.send_modal(_Volume(
            self._apply_volume,
            level=level,
            platform=_playing_platform(self.guild),
            length=length,
        ))

    async def _apply_volume(
        self,
        interaction: discord.Interaction,
        level: int,
        platform: str | None,
        position: int | None,
    ) -> None:
        await self._act(
            interaction,
            lambda: tune_track(self.guild, level, platform, position, interaction.user),
        )

    async def on_save(self, interaction: discord.Interaction) -> None:
        """Save the song that is playing into a playlist."""
        if not self._inside(interaction):
            await respond(interaction, notice_text("Join the voice channel first."), ephemeral=True)
            return
        from music.playlist.card import _as_saved
        from music.playlist.popups import SaveSong

        rows = queued(self.guild) or []
        current = next((row for row in rows if row.playing), None)
        if current is None:
            await respond(interaction, notice_text("Nothing is playing."), ephemeral=True)
            return
        song = _as_saved(current)
        if song is None:
            await respond(interaction, notice_text("That song has no link to save."), ephemeral=True)
            return
        lists = _playlists(self._lists, self.guild)
        if not lists:
            await respond(interaction, notice_text("There is no playlist yet."), ephemeral=True)
            return
        await interaction.response.send_modal(SaveSong(lists, lambda press, name: self._kept(press, name, song)))

    async def _kept(self, interaction: discord.Interaction, name: str, song: object) -> None:
        from music.archive import find_playlist, save_song

        guild_id = _record_id(self.guild)
        if self._lists is None or guild_id is None or find_playlist(self._lists, guild_id, name) is None:
            await respond(interaction, notice_text("That playlist is gone."), ephemeral=True)
            return
        updated = save_song(self._lists, guild_id, name, song, _record_id(interaction.user))
        if isinstance(updated, str):
            await respond(interaction, notice_text(updated), ephemeral=True)
            return
        await respond(interaction, notice_text(f"Saved to {updated.name}."), ephemeral=True)
