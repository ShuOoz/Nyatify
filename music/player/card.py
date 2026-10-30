"""Remote for the song already playing in a voice channel."""

from __future__ import annotations

import asyncio, inspect, io, logging
from collections.abc import Awaitable, Callable

import discord
from discord.components import MediaGalleryItem

from announce import ACCENT_COLOR, notice_text, respond
from music import source
from music.panel import (
    MARK_ANY, MARK_BACK, MARK_CLOSE, MARK_NEXT, MARK_SAVE, RADIO_MARK, _HALF, _LINE,
    _clip, _half, _row, delete_panel, format_clock, meta_line, song_label, song_line,
)
from music.playback import (
    PlayState, _stored, leave_track, member_in_voice, next_track, now_playing,
    previous_track, queued, repeat_track, shuffle_queue, toggle_pause,
    apply_playback, unwatch_line, watch_line,
)
from music.player.album import _Album, _AlbumCard, _playlists, _record_id
from music.player.rule import _mode, hinted_modes
from music.player.playbar import _BarClock, _bar_key
from music.player.radio import _Radio

log = logging.getLogger(__name__)

TITLE = "## Osaka Player"
VOLUME_MARK, SHUFFLE_MARK, REPEAT_MARK = "𖦤", "֎", "𖣠"
# ► is 0.990 em beside the fillers; the square is 1.000 em. U+FE0E keeps the square a mark.
PAUSE_MARK, PLAY_MARK, INFO_MARK = "\u25fc\ufe0e", "\u25ba", "ᝰ"
# The lower row is 0.178 em short. A hair space is 0.070 em, one on each side of close.
_HAIR = "\u200a"
_QUIET = discord.AllowedMentions.none()
_OFF = discord.ButtonStyle.secondary
# Refresh writes these slots: 2 status, 7 repeat style, 9 shuffle style.
def _words(value: object, fallback: str) -> str:
    cleaned = " ".join(value.split()) if isinstance(value, str) else ""
    return cleaned or fallback
def _http(value: object) -> str | None:
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None
def _fit(mark: str, *, hair: int = 0) -> str:
    # Two halfwidth fillers each side. Thin spaces wrap on Windows and lean on a phone.
    side, gap = _HALF * 2, _HAIR * hair
    return f"{side}{gap}{mark}{gap}{side}"
def _lit(on: bool) -> discord.ButtonStyle:
    return discord.ButtonStyle.primary if on else _OFF
def _next_line(upcoming: str) -> str:
    prefix = "Up next: "
    name = _clip(_words(upcoming, ""), _LINE - sum(_half(char) for char in prefix))
    return f"-# {prefix}{name}"
def _status(state: PlayState, *, repeating: bool, shuffling: bool, radio_on: bool = False) -> str:
    mark = f"\n{RADIO_MARK}" if state.stream else ""
    # Radio plays a similar song and leaves the queue where it is. Repeat still names it.
    upcoming = "A similar song" if radio_on and not repeating else (state.upcoming or "Nothing")
    return f"-# {_mode(repeating, shuffling)}{mark}\n" + _next_line(upcoming)
def _details(track: object, title: str = "…", author: str = "", uri: str | None = None, artwork: str | None = None):
    return (
        _words(getattr(track, "title", None), title),
        _words(getattr(track, "author", None), author),
        _http(getattr(track, "uri", None)) or _http(getattr(track, "url", None)) or uri,
        _http(getattr(track, "artwork", None)) or artwork,
    )
def _hosted(message: object, name: str) -> str | None:
    found = getattr(message, "attachments", None)
    if not isinstance(found, (list, tuple)):
        return None
    for item in found:
        if getattr(item, "filename", None) == name and (url := _http(getattr(item, "url", None))):
            return url
    return None
def _typed_level(text: str) -> int | None:
    cleaned = _words(text, "")
    level = int(cleaned) if cleaned.isdecimal() else None
    return level if level is not None and level <= 100 else None
def _typed_clock(text: str) -> int | None:
    parts = text.split(":")
    if not 1 <= len(parts) <= 3 or any(not part.isdecimal() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    if any(part >= 60 for part in numbers[1:]):
        return None
    units = (3600, 60, 1)[-len(numbers):]
    return 1000 * sum(number * unit for number, unit in zip(numbers, units))
def _menu_value(menu: discord.ui.Select, fallback: str) -> str:
    picked = menu.values[0] if menu.values else None
    if isinstance(picked, str) and picked:
        return source.normalize_platform(picked)
    for option in menu.options:
        if option.default and isinstance(option.value, str):
            return source.normalize_platform(option.value)
    return fallback
def _current_row(guild: object):
    return next((row for row in queued(guild) or [] if row.playing), None)
def _field(text: str, placeholder: str, limit: int, **extra) -> discord.ui.Label:
    return discord.ui.Label(
        text=text, component=discord.ui.TextInput(placeholder=placeholder, max_length=limit, **extra),
    )
def _face(
    heading, song, status, *, pause=_OFF, pause_off=True, repeat_off=True, repeat=_OFF,
    controls_off=True, shuffle=_OFF, radio=_OFF, shuffle_off=True,
) -> tuple:
    return (
        heading, song, status, "", pause, pause_off, repeat_off, repeat,
        controls_off, shuffle, radio, shuffle_off,
    )


class _Volume(discord.ui.Modal):
    def __init__(self, apply: Callable[..., Awaitable[None]], *, level: int, platform: str, length: int) -> None:
        super().__init__(title="Tune")
        self._apply = apply
        self._platform = source.normalize_platform(platform)
        self.level = _field("Volume", "From 0 to 100", 3, default=str(level))
        clock = f"Up to {format_clock(length)}" if length > 0 else "Live"
        self.place = _field("Duration", clock, 8, required=False)
        self.sites = discord.ui.Select(
            custom_id="music-now-platform",
            placeholder=source.platform_label("", self._platform),
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label=label, value=key, default=key == self._platform)
                for key, label in source.platform_pairs()
            ],
        )
        for item in (self.level, self.place, discord.ui.Label(text="Platform", component=self.sites)):
            self.add_item(item)
    async def on_submit(self, interaction: discord.Interaction) -> None:
        level = _typed_level(str(self.level.component))
        typed = _words(str(self.place.component), "")
        position = _typed_clock(typed) if typed else None
        if level is None or (typed and position is None):
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


class Wish:
    def __init__(self) -> None:
        self.token = self.ahead = 0
    def claim(self) -> int:
        self.token += 1
        return self.token
    def owns(self, token: int) -> bool:
        return token == self.token
    def slot(self) -> int:
        slot = self.ahead
        self.ahead += 1
        return slot
    def rewind(self) -> None:
        if self.ahead:
            self.ahead -= 1
    def rest(self) -> None:
        self.ahead = 0
def _guild_id(guild: object | None) -> int | None:
    guild_id = getattr(guild, "id", None)
    return guild_id if isinstance(guild_id, int) and not isinstance(guild_id, bool) else None
def _put(table: dict[int, object], guild: object | None, value: object) -> None:
    if (guild_id := _guild_id(guild)) is not None:
        table[guild_id] = value
def _read(table: dict[int, object], guild: object | None) -> object | None:
    guild_id = _guild_id(guild)
    return None if guild_id is None else table.get(guild_id)
def _drop(table: dict[int, object], guild: object | None) -> None:
    if (guild_id := _guild_id(guild)) is not None:
        table.pop(guild_id, None)
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
    _put(_cards, getattr(panel, "guild", None), panel)
def forget(panel: object) -> None:
    guild = getattr(panel, "guild", None)
    if _read(_cards, guild) is panel:
        _drop(_cards, guild)
async def settle(guild: object | None) -> bool:
    clear_stage(guild)
    panel = _read(_cards, guild)
    if panel is None or getattr(panel, "_closed", True):
        return False
    return await panel.land()

from music.player.sync import _Refresh


class MusicPlayer(_Album, _Radio, _Refresh, discord.ui.LayoutView):
    def __init__(
        self, guild: object, *,
        discard: Callable[[MusicPlayer], None] | None = None,
        conceal: Callable[[MusicPlayer], None] | None = None,
        store: object | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.guild = guild
        self.message = self._task = None
        self._closed = False
        # Set once the bot is seen in voice. The tick closes the card after that if the bot is out.
        self._voiced = getattr(guild, "voice_client", None) is not None
        self._discard, self._conceal, self._lists = discard, conceal, store
        self._lock, self._album_lock = asyncio.Lock(), asyncio.Lock()
        self._albums: dict[int, tuple[object, _AlbumCard]] = {}
        self._shown = self._face = self._watch = self._hint = None
        self._wish = Wish()
        self._coming = staged(self.guild)
        self._cover = self._song = None
        # A quiet repeat or shuffle. The spare keeps the card for the refresh that follows.
        self._mode_press = self._mode_spare = False
        self._box = self._gallery = None
        self._bars = _BarClock()
        self.heading, self.song, self.meta = map(
            discord.ui.TextDisplay, ("**Nothing is playing**", "…", ""),
        )
        self.previous = _Control("previous", label=_fit(MARK_BACK))
        self.pause = _Control("pause", label=_fit(PAUSE_MARK))
        self.next = _Control("next", label=_fit(MARK_NEXT))
        self.volume = _Control("volume", label=_fit(VOLUME_MARK))
        self.radio = _Control("radio", label=_fit(MARK_ANY))
        self.save = _Control("save", label=_fit(MARK_SAVE))
        self.info = _Control("info", label=_fit(INFO_MARK))
        self.shuffle = _Control("shuffle", label=_fit(SHUFFLE_MARK))
        self.repeat = _Control("repeat", label=_fit(REPEAT_MARK))
        self.close = _Control("close", label=_fit(MARK_CLOSE, hair=1))
        self.leave = _Control("leave", label="⏻")
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
    async def _require_room(self, interaction: discord.Interaction) -> bool:
        if self._inside(interaction):
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
        live = next((guild for guild in (cached, posted) if voiced(guild)), None)
        live = live or next((guild for guild in (posted, cached) if same(guild)), None)
        if live is not None:
            self.guild = live
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
        name, picture = fresh
        return discord.File(io.BytesIO(picture), filename=name)
    async def _cite(self, message: object) -> None:
        bar, gallery = self._bars.bar, self._gallery
        if bar is None or gallery is None or not gallery.items or not str(gallery.items[0].media.url).startswith("attachment://"):
            return
        link, edit = _hosted(message, bar.name), getattr(message, "edit", None)
        if link is None or not inspect.iscoroutinefunction(edit):
            return
        previous = list(gallery.items)
        gallery.items = [MediaGalleryItem(link)]
        try:
            await edit(view=self, allowed_mentions=_QUIET)
        except discord.HTTPException:
            gallery.items = previous
            log.warning("music player could not show the bar", exc_info=True)
    def _hearing(self) -> bool:
        return getattr(getattr(self.guild, "voice_client", None), "current", None) is not None
    def _attachments(self) -> dict:
        upload = self.take_bar()
        if upload is not None:
            return {"attachments": [upload]}
        # The song is gone. Leave the old progress picture off the card.
        return {"attachments": []} if self._bars.bar is None else {}
    async def _push(self, interaction: discord.Interaction | None) -> None:
        extra = {"view": self, "allowed_mentions": _QUIET, **self._attachments()}
        if interaction is not None and not interaction.response.is_done():
            result = await interaction.response.edit_message(**extra)
            await self._cite(getattr(result, "resource", None) or self.message)
            return
        if self.message is None:
            return
        edited = await self.message.edit(**extra)
        await self._cite(edited if edited is not None else self.message)
    async def _close_message(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            log.warning("music player could not close", exc_info=True)
    async def on_previous(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: previous_track(self.guild, interaction.user), "previous")
    async def on_pause(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: toggle_pause(self.guild, interaction.user), "pause")
    async def on_next(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: next_track(self.guild, interaction.user), "next")
    async def on_shuffle(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: shuffle_queue(self.guild), "shuffle")
    async def on_repeat(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, lambda: repeat_track(self.guild), "repeat")
    async def on_leave(self, interaction: discord.Interaction) -> None:
        if not await self._require_room(interaction):
            return
        # Leave starts before the card answers, so the room does not wait on Discord.
        leaving = asyncio.create_task(leave_track(self.guild))
        await self._ack(interaction)
        try:
            note = await leaving
        except Exception:
            log.exception("music player could not leave")
            await respond(
                interaction, notice_text("Something went wrong while leaving the voice channel."), ephemeral=True,
            )
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
        if not await self._require_room(interaction):
            return
        conceal, self._conceal = self._conceal, None
        if conceal is not None:
            conceal(self)
        self.shutdown()
        await self._ack(interaction)
        await self._close_message(interaction)
    def _current(self) -> PlayState | None:
        state = now_playing(self.guild)
        if state is not None and state.has_track:
            self._coming = None
            return state
        return preview_state(self._coming) if self._coming is not None else None
    def _left(self) -> bool:
        # Leaving voice takes the card, paused or not. A card opened while the bot was out stays.
        if getattr(self.guild, "voice_client", None) is not None:
            self._voiced = True
            return False
        return self._voiced
    def _holding(self) -> bool:
        state = now_playing(self.guild)
        if state is None or state.has_track:
            return False
        line = _stored(self.guild)
        return state.repeating or (line is not None and bool(line.later))
    async def on_volume(self, interaction: discord.Interaction) -> None:
        if not await self._require_room(interaction):
            return
        state = now_playing(self.guild)
        row = _current_row(self.guild)
        await interaction.response.send_modal(_Volume(
            self._apply_volume,
            level=100 if state is None else state.volume,
            platform=source.normalize_platform(None if row is None else row.platform),
            length=0 if state is None else state.length,
        ))
    async def _apply_volume(
        self, interaction: discord.Interaction, level: int, platform: str | None, position: int | None,
    ) -> None:
        await self._act(
            interaction,
            lambda: apply_playback(self.guild, level, platform, position, interaction.user),
            "seek" if position is not None else None,
        )
    async def on_save(self, interaction: discord.Interaction) -> None:
        if not await self._require_room(interaction):
            return
        from music.playlist.card import _as_saved
        from music.playlist.popups import SaveSong

        current = _current_row(self.guild)
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
        if not isinstance(updated, str):
            from music.playback.remember import warm_saved

            warm_saved(self.guild, song)
            updated = f"Saved to {updated.name}."
        await respond(interaction, notice_text(updated), ephemeral=True)
    def _aim(self) -> None:
        bar, gallery = self._bars.bar, self._gallery
        if bar is not None and gallery is not None:
            gallery.items = [MediaGalleryItem(f"attachment://{bar.name}")]
    def paint(self, *, move_bar: bool = True) -> bool:
        state = self._current()
        if state is None:
            state = now_playing(self.guild)
        shown = self._parts(state)
        (
            heading, song, status, _, pause_style, pause_off, repeat_off, repeat_style,
            controls_off, shuffle_style, radio_style, shuffle_off,
        ) = shown
        playing = state is not None and state.has_track
        # Drop a picture this paint will not send. The tick, a seek, and a new song cut the next frame.
        self._bars.fresh = None
        bar_changed = move_bar and self._bars.sync(
            state, paused=playing and pause_style == discord.ButtonStyle.primary,
            hearing=self._hearing(), seek=self._take_jump(),
        )
        if shown == self._shown and not bar_changed:
            return False
        self._shown = shown
        self.heading.content, self.meta.content = heading, status
        covered = f"{song}\n{status}" if song and status else song or status
        self.song.content = covered if playing and self._cover else song
        self.pause.style, self.pause.disabled = pause_style, pause_off
        self.pause.label = _fit(PLAY_MARK if pause_style == discord.ButtonStyle.primary else PAUSE_MARK)
        self.repeat.disabled, self.repeat.style = repeat_off, repeat_style
        self.shuffle.disabled, self.shuffle.style = shuffle_off, shuffle_style
        self.radio.style = radio_style
        for button in (self.previous, self.next, self.volume, self.radio, self.save, self.info):
            button.disabled = controls_off
        shape = (playing, self._cover, bool(self.meta.content), self._bars.bar is not None)
        if self._box is None or shape != self._face:
            self._face = shape
            self._mount(state)
        elif bar_changed:
            self._aim()
        return True
    def _parts(self, state: PlayState | None) -> tuple:
        if state is None or not state.has_track:
            self._cover = None
            channel = None if state is None else state.channel_id
            return _face("**Nothing is playing**", "", meta_line(None, channel))
        paused, repeating, shuffling, radio = state.paused, state.repeating, state.shuffling, state.radio
        title, author, uri, artwork = state.title, state.author, state.uri, state.artwork
        kind, value = self._hint or (None, None)
        if kind == "pause" and isinstance(value, bool):
            paused = value
        elif kind in ("repeat", "shuffle", "radio") and isinstance(value, bool):
            repeating, shuffling, radio = hinted_modes(repeating, shuffling, radio, kind, value)
        elif kind == "next":
            title, author, uri, artwork = _details(getattr(value, "track", value), title, author, uri, artwork)
        self._cover = artwork
        # A livestream cannot start repeat. Leave it on when repeat is already on.
        return _face(
            "", song_line(title, author, uri), _status(
                state, repeating=repeating, shuffling=shuffling, radio_on=radio,
            ),
            pause=_lit(paused), pause_off=False,
            repeat_off=(state.stream or state.length <= 0) and not repeating, repeat=_lit(repeating),
            controls_off=False, shuffle=_lit(shuffling), radio=_lit(radio), shuffle_off=radio,
        )
    def _mount(self, state: PlayState | None) -> None:
        if self._box is not None:
            self.remove_item(self._box)
        box = discord.ui.Container(accent_colour=ACCENT_COLOR)
        box.add_item(discord.ui.Section(TITLE, accessory=self.leave))
        box.add_item(discord.ui.Separator())
        playing = state is not None and state.has_track
        if playing and self._cover and state is not None:
            box.add_item(discord.ui.Section(
                self.song,
                accessory=discord.ui.Thumbnail(self._cover, description=song_label(state.title, state.author)[:256]),
            ))
        else:
            box.add_item(self.song if playing else self.heading)
            if self.meta.content:
                box.add_item(self.meta)
        bar, self._gallery = self._bars.bar, None
        if bar is not None:
            self._gallery = discord.ui.MediaGallery()
            self._aim()
            box.add_item(self._gallery)
        box.add_item(discord.ui.Separator())
        box.add_item(_row(self.shuffle, self.previous, self.pause, self.next, self.repeat))
        box.add_item(_row(self.save, self.info, self.radio, self.volume, self.close))
        self._box = box
        self.add_item(box)
