"""The player card's layout, and the paint that fills it."""

from __future__ import annotations

import logging

import discord
from discord.components import MediaGalleryItem

from announce import ACCENT_COLOR
from music.panel import (
    RADIO_MARK, _HALF, _LINE, _clip, _half, _mode, _row, meta_line, song_label, song_line,
)
from music.playback import PlayState, _stored, now_playing
from music.playback.remember import _coming, _song_name, cover_url
from music.player.rule import hinted_modes, next_title

log = logging.getLogger(__name__)
TITLE = "## Osaka Player"
# ► is 0.990 em beside the fillers; the square is 1.000 em. U+FE0E keeps the square a mark.
PAUSE_MARK, PLAY_MARK = "\u25fc\ufe0e", "\u25ba"
# The lower row is 0.178 em short. A hair space is 0.070 em, one on each side of close.
_HAIR = "\u200a"
_OFF = discord.ButtonStyle.secondary

def _words(value: object, fallback: str) -> str:
    return " ".join(value.split()) if isinstance(value, str) and value.split() else fallback

def _http(value: object) -> str | None:
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None

def _fit(mark: str, *, hair: int = 0) -> str:
    # Two halfwidth fillers each side. Thin spaces wrap on Windows and lean on a phone.
    return f"{_HALF * 2}{_HAIR * hair}{mark}{_HAIR * hair}{_HALF * 2}"

def _lit(on: bool) -> discord.ButtonStyle:
    return discord.ButtonStyle.primary if on else _OFF

def _next_line(upcoming: str) -> str:
    prefix = "Up next: "
    name = _clip(_words(upcoming, ""), _LINE - sum(_half(char) for char in prefix))
    return f"-# {prefix}{name}"

def _status(state: PlayState, *, repeating: bool, shuffling: bool, radio_on: bool = False) -> str:
    mode = _mode(repeating, shuffling, radio_on)
    # A station is On Radio. The Radio line under On Queue would say it twice.
    if state.stream and mode == "On Queue":
        mode = "On Radio"
    mark = "" if not state.stream or "Radio" in mode else f"\n{RADIO_MARK}"
    # Up next is the queue. Repeat does not rename it to the song that is on.
    upcoming = next_title(state.upcoming)
    return f"-# {mode}{mark}\n" + _next_line(upcoming)

def _details(track: object, title: str = "…", author: str = "", uri: str | None = None, artwork: str | None = None):
    return (
        _words(getattr(track, "title", None), title), _words(getattr(track, "author", None), author),
        _http(getattr(track, "uri", None)) or _http(getattr(track, "url", None)) or uri,
        cover_url(getattr(track, "artwork", None)) or cover_url(artwork),
    )

def _face(heading, song, status, *, pause=_OFF, pause_off=True, repeat_off=True, repeat=_OFF,
          controls_off=True, shuffle=_OFF, radio=_OFF, shuffle_off=True, radio_off=True) -> tuple:
    # Slots the refresh writes: 2 status, 7 repeat style, 9 shuffle style, 10 radio style, 11 shuffle off.
    return (heading, song, status, "", pause, pause_off, repeat_off, repeat,
            controls_off, shuffle, radio, shuffle_off, radio_off)

def held_buttons(
    *,
    held: bool,
    pause_off: bool,
    repeat_off: bool,
    shuffle_off: bool,
    controls_off: bool,
    radio_off: bool = False,
) -> dict[str, bool]:
    """Disabled flags for this paint.

    A press in flight greys every button, so the card looks stopped. One that
    was already off stays off when the press lets go. Close and leave are on
    again then.
    """
    flags = {
        "previous": controls_off,
        "pause": pause_off,
        "next": controls_off,
        "repeat": repeat_off,
        "shuffle": shuffle_off,
        "radio": controls_off or radio_off,
        "volume": controls_off,
        "save": controls_off,
        "info": controls_off,
        "close": False,
        "leave": False,
    }
    if held:
        for name in flags:
            flags[name] = True
    return flags


def paint_holds(same: bool, *, held: bool, was: bool) -> bool:
    """Skip this paint only when the picture and the hold are both unchanged.

    Letting the buttons go is a change even when the song on the card is not,
    or the grey buttons would stay after the player has answered.
    """
    return same and held == was


# One id per part of the player card. Discord rebuilds a component whose id
# changed, and that rebuild reloads the cover and the bar.
_PARTS = (
    "container",
    "header",
    "title",
    "leave",
    "rule",
    "played",
    "song",
    "cover",
    "heading",
    "status",
    "bar",
    "split",
    "transport",
    "shuffle",
    "previous",
    "pause",
    "next",
    "repeat",
    "tools",
    "save",
    "info",
    "radio",
    "volume",
    "close",
)


def part_id(part: str) -> int | None:
    """The id this part keeps across edits. None when it is not part of the card."""
    try:
        return _PARTS.index(part) + 1
    except ValueError:
        return None


def _kept(parts: tuple[str, ...]) -> dict[str, int] | None:
    """Ids for these parts. None when one is missing or two would share an id.

    A shared id makes Discord paint one component over another. Sending no
    ids keeps the edit the card already survived.
    """
    found: dict[str, int] = {}
    for part in parts:
        number = part_id(part)
        if number is None or number in found.values():
            return None
        found[part] = number
    return found


def face_ids(*, playing: bool, cover: bool, status: bool, bar: bool) -> dict[str, int] | None:
    """The parts on this face, each with its own id.

    A cover and a quiet line are different parts. The one that leaves is
    dropped, and its id is not given to whatever takes its place. A cover
    hides the separate status line, so that id stays off this face too.
    """
    parts = ["container", "header", "title", "leave", "rule"]
    if playing and cover:
        parts.extend(("played", "song", "cover"))
    elif playing:
        parts.append("song")
    else:
        parts.append("heading")
    if status and not (playing and cover):
        parts.append("status")
    if bar:
        parts.append("bar")
    parts.extend((
        "split",
        "transport", "shuffle", "previous", "pause", "next", "repeat",
        "tools", "save", "info", "radio", "volume", "close",
    ))
    return _kept(tuple(parts))


class _Face:

    def _ids(self, state: PlayState | None) -> dict[str, int]:
        playing = state is not None and state.has_track
        covered = bool(playing and self._cover)
        return face_ids(playing=playing, cover=covered, status=bool(self.meta.content) and not covered, bar=self._bars.bar is not None) or {}

    def _keep(self) -> None:
        """Put each part's id back before the edit. A collided face sends none."""
        ids = self._ids(self._current() or now_playing(self.guild))
        named = (
            ("leave", self.leave), ("shuffle", self.shuffle), ("previous", self.previous), ("pause", self.pause),
            ("next", self.next), ("repeat", self.repeat), ("save", self.save), ("info", self.info),
            ("radio", self.radio), ("volume", self.volume), ("close", self.close), ("heading", self.heading),
            ("song", self.song), ("status", self.meta), ("container", self._box), ("bar", self._gallery),
        )
        for name, item in named:
            if item is not None and (number := ids.get(name)) is not None:
                item.id = number

    def _named_next(self) -> str:
        """Up next for a mode change. The queue stays. The song on screen is not it."""
        line = _stored(self.guild)
        track = None if line is None or line.current is None else line.current.track
        coming = _coming(line, track)
        queued = None if coming is None else _song_name(coming)
        return _next_line(next_title(queued))

    def _aim(self) -> None:
        # Keep the uploaded file's name. A CDN link makes Discord proxy a new picture that stays failed.
        bar, gallery = self._bars.bar, self._gallery
        if bar is not None and gallery is not None:
            gallery.items = [MediaGalleryItem(f"attachment://{bar.name}")]

    def _apply_offs(self, held: bool) -> None:
        shown = self._shown
        if not isinstance(shown, tuple) or len(shown) < 13:
            self._held = False
            return
        pause_off, repeat_off, controls_off = shown[5], shown[6], shown[8]
        shuffle_off, radio_off = shown[11], shown[12]
        for name, off in held_buttons(
            held=held, pause_off=bool(pause_off), repeat_off=bool(repeat_off),
            controls_off=bool(controls_off), shuffle_off=bool(shuffle_off),
            radio_off=bool(radio_off),
        ).items():
            getattr(self, name).disabled = off
        self._held = held

    async def _ungrey(self, interaction: discord.Interaction | None) -> None:
        if not self._held:
            return
        self._apply_offs(False)
        try:
            await self._push(interaction)
        except discord.HTTPException:
            log.exception("music player could not update")

    def paint(self, *, move_bar: bool = True) -> bool:
        state = self._current() or now_playing(self.guild)
        shown = self._parts(state)
        heading, song, status, _, pause_style, _, _, repeat_style, _, shuffle_style, radio_style, _, _ = shown
        playing = state is not None and state.has_track
        paused = pause_style == discord.ButtonStyle.primary
        # Drop a picture this paint will not send. The tick, a seek, and a new song cut the next frame.
        self._bars.fresh = None
        bar_changed = move_bar and self._bars.sync(state, paused=playing and paused, hearing=self._hearing(), seek=self._take_jump())
        held = self._hint is not None
        if paint_holds(shown == self._shown and not bar_changed, held=held, was=self._held):
            return False
        self._shown = shown
        self.heading.content, self.meta.content = heading, status
        # A cover has no status row, so the song text carries the line under the art.
        self.song.content = (f"{song}\n{status}" if song and status else song or status) if playing and self._cover else song
        self.pause.style, self.pause.label = pause_style, _fit(PLAY_MARK if paused else PAUSE_MARK)
        self.repeat.style, self.shuffle.style, self.radio.style = repeat_style, shuffle_style, radio_style
        self._apply_offs(held)
        shape = (playing, self._cover, bool(self.meta.content), self._bars.bar is not None)
        if self._box is None or shape != self._face:
            self._face = shape
            self._mount(state, playing)
        elif bar_changed:
            self._aim()
        return True

    def _parts(self, state: PlayState | None) -> tuple:
        if state is None or not state.has_track:
            self._cover = None
            return _face("**Nothing is playing**", "", meta_line(None, None if state is None else state.channel_id))
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
        # A livestream cannot start repeat. A station cannot start Last.fm radio.
        # Leave each on when it is already on, so it can still be turned off.
        stuck = (state.stream or state.length <= 0) and not repeating
        return _face(
            "", song_line(title, author, uri), _status(state, repeating=repeating, shuffling=shuffling, radio_on=radio),
            pause=_lit(paused), pause_off=False, repeat_off=stuck, repeat=_lit(repeating), controls_off=False,
            shuffle=_lit(shuffling), radio=_lit(radio), shuffle_off=radio,
            radio_off=state.stream and not radio,
        )

    def _mount(self, state: PlayState | None, playing: bool) -> None:
        if self._box is not None:
            self.remove_item(self._box)
        covered = bool(playing and self._cover)
        ids = self._ids(state)
        box = discord.ui.Container(accent_colour=ACCENT_COLOR, id=ids.get("container"))
        add = box.add_item
        add(discord.ui.Section(discord.ui.TextDisplay(TITLE, id=ids.get("title")), accessory=self.leave, id=ids.get("header")))
        add(discord.ui.Separator(id=ids.get("rule")))
        if covered and state is not None:
            thumb = discord.ui.Thumbnail(self._cover, description=song_label(state.title, state.author)[:256], id=ids.get("cover"))
            add(discord.ui.Section(self.song, accessory=thumb, id=ids.get("played")))
        else:
            add(self.song if playing else self.heading)
            if self.meta.content:
                add(self.meta)
        self._gallery = discord.ui.MediaGallery(id=ids.get("bar")) if self._bars.bar is not None else None
        if self._gallery is not None:
            self._aim()
            add(self._gallery)
        add(discord.ui.Separator(id=ids.get("split")))
        add(_row(self.shuffle, self.previous, self.pause, self.next, self.repeat, id=ids.get("transport")))
        add(_row(self.save, self.info, self.radio, self.volume, self.close, id=ids.get("tools")))
        self._box = box
        self.add_item(box)
        self._keep()
