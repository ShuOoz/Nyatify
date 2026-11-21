"""Remote for the song already playing in a voice channel."""

from __future__ import annotations

import asyncio, inspect, io, logging
from collections.abc import Awaitable, Callable

import discord
from announce import notice_text, respond
from music import source
from music.panel import (
    MARK_ANY, MARK_BACK, MARK_CLOSE, MARK_NEXT, MARK_SAVE, delete_panel, format_clock,
)
from music.playback import (
    PlayState, _stored, leave_voice, member_in_voice, next_track, now_playing,
    previous_track, queued, repeat_track, shuffle_queue, toggle_pause,
    apply_playback, unwatch_line, watch_line,
)
from music.playback.remember import bot_connected, note_bot_in
from music.player.album import _Album, _AlbumCard, _playlists, _record_id
from music.player.layout import PAUSE_MARK, _OFF, _Face, _details, _fit, _words
from music.player.playbar import _BarClock, _bar_key
from music.player.radio import _Radio

log = logging.getLogger(__name__)
VOLUME_MARK, SHUFFLE_MARK, REPEAT_MARK = "𖦤", "֎", "𖣠"
INFO_MARK = "ᝰ"
_QUIET = discord.AllowedMentions.none()

def _typed_level(text: str) -> int | None:
    cleaned = _words(text, "")
    level = int(cleaned) if cleaned.isdecimal() else None
    return level if level is not None and level <= 100 else None

def _typed_clock(text: str) -> int | None:
    parts = text.split(":")
    numbers = [int(part) for part in parts] if 1 <= len(parts) <= 3 and all(part.isdecimal() for part in parts) else None
    if numbers is None or any(number >= 60 for number in numbers[1:]):
        return None
    units = (3600, 60, 1)[-len(numbers):]
    return 1000 * sum(number * unit for number, unit in zip(numbers, units))

def _menu_value(menu: discord.ui.Select, fallback: str) -> str:
    picked = menu.values[0] if menu.values else None
    if isinstance(picked, str) and picked:
        return source.normalize_platform(picked)
    chosen = next((option.value for option in menu.options if option.default and isinstance(option.value, str)), None)
    return source.normalize_platform(chosen) if isinstance(chosen, str) else fallback

def _current_row(guild: object):
    return next((row for row in queued(guild) or [] if row.playing), None)

def _field(text: str, placeholder: str, limit: int, **extra) -> discord.ui.Label:
    return discord.ui.Label(text=text, component=discord.ui.TextInput(placeholder=placeholder, max_length=limit, **extra))

class _Volume(discord.ui.Modal):

    def __init__(self, apply: Callable[..., Awaitable[None]], *, level: int, platform: str, length: int, position: int) -> None:
        super().__init__(title="Tune")
        self._apply = apply
        self._platform = source.normalize_platform(platform)
        self.level = _field("Volume", "From 0 to 100", 3, default=str(level))
        clock = f"Up to {format_clock(length)}" if length > 0 else "Live"
        # The box opens on the current time. Clearing it reveals the grey limit.
        self._stamp = format_clock(position) if length > 0 else ""
        self.place = _field("Duration", clock, 8, required=False, default=self._stamp or None)
        self.sites = discord.ui.Select(
            custom_id="music-now-platform", placeholder=source.platform_label("", self._platform),
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=label, value=key, default=key == self._platform) for key, label in source.platform_pairs()],
        )
        for item in (self.level, self.place, discord.ui.Label(text="Platform", component=self.sites)):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        level = _typed_level(str(self.level.component))
        typed = _words(str(self.place.component), "")
        # Leaving the time that was already there does not rewind the song.
        same_time = bool(self._stamp) and typed == self._stamp
        position = _typed_clock(typed) if typed and not same_time else None
        if level is None or (typed and not same_time and position is None):
            text = "Type a volume from 0 to 100." if level is None else "Type a time like 1:30."
            await respond(interaction, notice_text(text), ephemeral=True)
            return
        chosen = _menu_value(self.sites, self._platform)
        await self._apply(interaction, level, None if chosen == self._platform else chosen, position)

class _Control(discord.ui.Button):

    def __init__(self, name: str, *, label: str) -> None:
        super().__init__(label=label, style=_OFF, custom_id=f"music-{name}")
        self.method = f"on_{name}"

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if isinstance(panel, MusicPlayer):
            await getattr(panel, self.method)(interaction)
_staged: dict[int, object] = {}
_cards: dict[int, object] = {}
# The card on screen. A preview uses _cards before it is posted, and must not take this slot.
_open: dict[int, object] = {}

class Wish:

    def __init__(self) -> None:
        self.token = self.ahead = 0

    def claim(self) -> int:
        self.token += 1
        return self.token

    def owns(self, token: int) -> bool:
        return token == self.token

    def slot(self) -> int:
        self.ahead += 1
        return self.ahead - 1

    def rewind(self) -> None:
        self.ahead = max(0, self.ahead - 1)

    def rest(self) -> None:
        self.ahead = 0

def _guild_id(guild: object | None) -> int | None:
    guild_id = getattr(guild, "id", None)
    return guild_id if isinstance(guild_id, int) and not isinstance(guild_id, bool) else None

def _put(table: dict[int, object], guild: object | None, value: object) -> None:
    if (guild_id := _guild_id(guild)) is not None: table[guild_id] = value

def _read(table: dict[int, object], guild: object | None) -> object | None:
    guild_id = _guild_id(guild)
    return None if guild_id is None else table.get(guild_id)

def _drop(table: dict[int, object], guild: object | None) -> None:
    if (guild_id := _guild_id(guild)) is not None: table.pop(guild_id, None)

def preview_state(track: object) -> PlayState:
    length = getattr(track, "length", None)
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        length = 0
    title, author, uri, artwork = _details(track)
    return PlayState(title, author, uri, artwork, 0, length, False, None, None, 100, None, True, False)

def stage(guild: object | None, track: object) -> None:
    _put(_staged, guild, track)

def staged(guild: object | None) -> object | None:
    return _read(_staged, guild)

def clear_stage(guild: object | None) -> None:
    _drop(_staged, guild)

def remember(panel: object) -> None:
    guild = getattr(panel, "guild", None)
    _put(_cards, guild, panel)
    # Posted once it has a message. A preview is remembered earlier, before that.
    if getattr(panel, "message", None) is not None and getattr(panel, "_closed", True) is False:
        _put(_open, guild, panel)

def forget(panel: object) -> None:
    guild = getattr(panel, "guild", None)
    if _read(_cards, guild) is panel:
        _drop(_cards, guild)
    if _read(_open, guild) is panel:
        _drop(_open, guild)

def card_open(guild: object | None) -> bool:
    """The player on screen. A preview that has not been posted yet is not."""
    panel = _read(_open, guild)
    return panel is not None and getattr(panel, "_closed", True) is False

async def settle(guild: object | None) -> bool:
    clear_stage(guild)
    panel = _read(_cards, guild)
    if panel is None or getattr(panel, "_closed", True):
        return False
    return await panel.land()


def _tap(kind: str, fn: str, *, room: bool = True, user: bool = False):

    async def handler(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: globals()[fn](self.guild, interaction.user) if user else globals()[fn](self.guild), kind, room=room)

    handler.__name__ = f"on_{kind}"
    return handler

from music.player.sync import _Refresh

class MusicPlayer(_Album, _Radio, _Refresh, _Face, discord.ui.LayoutView):

    def __init__(self, guild: object, *, discard: Callable[[MusicPlayer], None] | None = None,
                 conceal: Callable[[MusicPlayer], None] | None = None, store: object | None = None) -> None:
        super().__init__(timeout=None)
        self.guild, self.message, self._task = guild, None, None
        self._closed = False
        # Set once the bot is seen in voice. The tick closes the card after that if the bot is out.
        self._voiced = self._bot_present()
        self._discard, self._conceal, self._lists = discard, conceal, store
        self._lock, self._album_lock = asyncio.Lock(), asyncio.Lock()
        self._albums: dict[int, tuple[object, _AlbumCard]] = {}
        self._shown = self._face = self._watch = self._hint = self._ahead = None
        self._held = False
        # A press is landing. The bar upload is not one, and it must not eat the tap.
        self._pressing = False
        # Bumped when a press arrives during a picture that is already going out.
        self._draw = 0
        self._wish, self._coming = Wish(), staged(self.guild)
        self._cover = self._song = self._box = self._gallery = None
        # A quiet repeat or shuffle. The spare keeps the card for the refresh that follows.
        self._mode_press = self._mode_spare = False
        self._bars = _BarClock()
        self.heading, self.song, self.meta = map(discord.ui.TextDisplay, ("**Nothing is playing**", "…", ""))
        for name, label in (
            ("previous", _fit(MARK_BACK)), ("pause", _fit(PAUSE_MARK)), ("next", _fit(MARK_NEXT)),
            ("volume", _fit(VOLUME_MARK)), ("radio", _fit(MARK_ANY)), ("save", _fit(MARK_SAVE)),
            ("info", _fit(INFO_MARK)), ("shuffle", _fit(SHUFFLE_MARK)), ("repeat", _fit(REPEAT_MARK)),
            ("close", _fit(MARK_CLOSE, hair=1)), ("leave", "⏻"),
        ):
            setattr(self, name, _Control(name, label=label))
        if self._coming is not None:
            remember(self)
        self.paint()
        playing = self._current()
        if playing is not None:
            self._song = _bar_key(playing)

    def _inside(self, interaction: discord.Interaction) -> bool:
        live = getattr(interaction, "guild", None)
        if isinstance(live, discord.Guild) and live.id == _record_id(self.guild):
            self.guild = live
        return member_in_voice(interaction.user, self.guild)

    def _can_edit(self, interaction: discord.Interaction) -> bool:
        """A live room answers from inside it. After the bot leaves, the other buttons still change the card."""
        from music.lavalink import joining
        live = bot_connected(self.guild, getattr(interaction, "guild", None)) or joining(self.guild) is not None
        return self._inside(interaction) if live else True

    async def _require_room(self, interaction: discord.Interaction, *, edit: bool = False) -> bool:
        if (self._can_edit if edit else self._inside)(interaction):
            return True
        await respond(interaction, notice_text("Join the voice channel first."), ephemeral=True)
        return False

    def _adopt_guild(self) -> None:
        current = _record_id(self.guild)
        if current is None:
            return
        posted = getattr(self.message, "guild", None) if self.message is not None else None
        getter = getattr(getattr(self.guild, "_state", None), "get_guild", None)
        try:
            found = getter(current) if callable(getter) else None
        except Exception:
            found = None
        cached = found if _record_id(found) == current else None
        # A room with the bot wins. Otherwise the message's guild comes before the cache.
        same = lambda guild: _record_id(guild) == current
        voiced = lambda guild: same(guild) and getattr(guild, "voice_client", None) is not None
        live = next((g for g in (cached, posted) if voiced(g)), None) or next((g for g in (posted, cached) if same(g)), None)
        if live is not None:
            self.guild = live

    def follow(self, message: discord.Message) -> None:
        self.message = message
        remember(self)
        if self._watch is None:
            self._watch = self._on_line
            watch_line(self._watch)
        self._task = asyncio.create_task(self._tick(), name="music-player")

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        forget(self)
        self.stop()
        watch, self._watch = self._watch, None
        if watch is not None:
            unwatch_line(watch)
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        drop, self._discard, self._conceal = self._discard, None, None
        if drop is not None:
            drop(self)

    async def on_timeout(self) -> None:
        await self.dismiss()

    async def dismiss(self) -> None:
        message = self.message
        self.shutdown()
        await delete_panel(message)

    def _take_jump(self) -> bool:
        player = getattr(self.guild, "voice_client", None)
        if getattr(player, "_jumped", None) is not True:
            return False
        player._jumped = False
        return True

    def take_bar(self) -> discord.File | None:
        fresh = self._bars.take()
        if fresh is None:
            return None
        return discord.File(io.BytesIO(fresh[1]), filename=fresh[0])

    def _hearing(self) -> bool:
        return getattr(getattr(self.guild, "voice_client", None), "current", None) is not None

    def _attachments(self) -> dict:
        upload = self.take_bar()
        if upload is not None:
            return {"attachments": [upload]}
        # The song is gone. Leave the old progress picture off the card.
        return {"attachments": []} if self._bars.bar is None else {}

    async def _push(self, interaction: discord.Interaction | None) -> None:
        while not self._closed:
            # A press during this send makes the picture old. Paint the player once it lands.
            token = self._draw
            self._keep()
            extra = {"view": self, "allowed_mentions": _QUIET, **self._attachments()}
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.edit_message(**extra)
            elif self.message is not None:
                await self.message.edit(**extra)
            else:
                return
            if token == self._draw or self._closed:
                return
            interaction = None
            if not self.paint():
                return

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
        try:
            sent = await send(view=self, allowed_mentions=_QUIET, **({"file": upload} if upload else {}))
        except discord.HTTPException:
            log.warning("music player could not post the next song", exc_info=True)
            # The new song was painted here, but this message never received it.
            self._bars.bar = None
            self._shown = None
            return False
        self.message = sent
        await delete_panel(message)
        return True

    async def _close_message(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            log.warning("music player could not close", exc_info=True)
    on_previous = _tap("previous", "previous_track", user=True)
    on_pause = _tap("pause", "toggle_pause", user=True)
    on_next = _tap("next", "next_track", user=True)
    on_repeat = _tap("repeat", "repeat_track", room=False)

    async def on_shuffle(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: shuffle_queue(self.guild), "shuffle", room=False)

    async def on_leave(self, interaction: discord.Interaction) -> None:
        if not await self._require_room(interaction):
            return
        # Leave starts before the card answers, so the room does not wait on Discord.
        leaving = asyncio.create_task(leave_voice(self.guild))
        await self._ack(interaction)
        try:
            note = await leaving
        except Exception:
            log.exception("music player could not leave")
            await respond(interaction, notice_text("Something went wrong while leaving the voice channel."), ephemeral=True)
            return
        if note in ("Stopped.", "Nothing is playing."):
            self.shutdown()
            await self._close_message(interaction)
            return
        await respond(interaction, notice_text(note), ephemeral=True)
        if self._closed or not self.paint():
            return
        try:
            await self._push(interaction)
        except discord.HTTPException:
            log.warning("music player could not update", exc_info=True)

    async def on_close(self, interaction: discord.Interaction) -> None:
        if not await self._require_room(interaction, edit=True):
            return
        conceal, self._conceal = self._conceal, None
        if conceal is not None:
            conceal(self)
        self.shutdown()
        await self._ack(interaction)
        await self._close_message(interaction)

    def _current(self) -> PlayState | None:
        # A skip draws the song it is about to start. The player still has the one before it.
        if self._ahead is not None:
            return self._ahead
        state = now_playing(self.guild)
        if state is not None and state.has_track:
            self._coming = None
            return state
        return preview_state(self._coming) if self._coming is not None else None

    def _bot_present(self) -> bool:
        posted = getattr(self.message, "guild", None) if self.message is not None else None
        return bot_connected(self.guild, posted)

    def _left(self) -> bool:
        # Leaving voice takes the card, paused or not. A card opened while the bot was out stays.
        if self._bot_present():
            self._voiced = True
            note_bot_in(self.guild)
            return False
        return self._voiced

    def _holding(self) -> bool:
        state = now_playing(self.guild)
        if state is None or state.has_track:
            return False
        line = _stored(self.guild)
        # Radio is picking the next song while the queue is empty. Keep the card.
        return state.repeating or (line is not None and (bool(line.later) or line.radio))

    async def on_volume(self, interaction: discord.Interaction) -> None:
        if not await self._require_room(interaction, edit=True):
            return
        state = now_playing(self.guild)
        row = _current_row(self.guild)
        level, length, position = (100, 0, 0) if state is None else (state.volume, state.length, state.position)
        platform = source.normalize_platform(None if row is None else row.platform)
        await interaction.response.send_modal(_Volume(
            self._apply_volume, level=level, platform=platform, length=length, position=position,
        ))

    async def _apply_volume(
        self, interaction: discord.Interaction, level: int, platform: str | None, position: int | None,
    ) -> None:
        await self._act(
            interaction, lambda: apply_playback(self.guild, level, platform, position, interaction.user),
            None if position is None else "seek", room=False,
        )

    async def on_save(self, interaction: discord.Interaction) -> None:
        if not await self._require_room(interaction, edit=True):
            return
        from music.playlist.card import _as_saved
        from music.playlist.popups import SaveSong
        current = _current_row(self.guild)
        song = None if current is None else _as_saved(current)
        lists = _playlists(self._lists, self.guild)
        if current is not None and song is not None and lists:
            await interaction.response.send_modal(SaveSong(lists, lambda press, name: self._kept(press, name, song)))
            return
        if current is None:
            note = "Nothing is playing."
        elif song is None:
            note = "That song has no link to save."
        else:
            note = "There is no playlist yet."
        await respond(interaction, notice_text(note), ephemeral=True)

    async def _kept(self, interaction: discord.Interaction, name: str, song: object) -> None:
        from music.archive import find_playlist, save_song
        from music.lavalink import warm_saved
        guild_id = _record_id(self.guild)
        if self._lists is None or guild_id is None or find_playlist(self._lists, guild_id, name) is None:
            await respond(interaction, notice_text("That playlist is gone."), ephemeral=True)
            return
        updated = save_song(self._lists, guild_id, name, song, _record_id(interaction.user))
        if not isinstance(updated, str):
            warm_saved(self.guild, song)
            updated = f"Saved to {updated.name}."
        await respond(interaction, notice_text(updated), ephemeral=True)
