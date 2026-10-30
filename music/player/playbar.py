"""The play bar as one frame.

A moving picture starts again at its first frame when the window changes, so
the bar jumps back to where that picture began. One frame has nowhere to
restart. The tick moves it forward with the song. A late reading does not
pull it back. A seek does.
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass

from music.playback.remember import PlayState
from music.player.rule import bar_jumped

# Discord's dark card, the empty groove, the player pink, the knob.
# Windows loads the picture when the table has 256 slots and the code
# is 8 bits. Slot 0 stays clear, so the card shows through.
_CODE = 8
_COLORS = 1 << _CODE
_PALETTE = (
    (0x2B, 0x2D, 0x31),
    (0x4E, 0x50, 0x58),
    (0xFF, 0xB6, 0xC1),
    (0xFF, 0xFF, 0xFF),
)
# The upper button row is 14.452 em and the lower row is 14.423 em. The
# picture is 350px so the track runs the whole way, out to the longer row.
_WIDTH = 350
_HEIGHT = 16
_BAR = 6
# The knob's pixels, from an empty track to a full one.
_FRAMES = _WIDTH - _BAR + 1


def _step(position: int, length: int) -> int:
    """The pixel the knob is on. 0 is empty and the last pixel is full."""
    last = _FRAMES - 1
    if length <= 0:
        return 0
    return last * min(position, length) // length


def _paint(step: int) -> bytes:
    """One frame. `step` 0 is empty and the last step is full."""
    last = _FRAMES - 1
    progress = 0.0 if last == 0 else step / last
    pixels = bytearray(_WIDTH * _HEIGHT)
    radius = _BAR / 2
    cy = _HEIGHT / 2
    # The caps sit on the two ends, so the track is the whole picture.
    left = radius
    right = _WIDTH - radius
    head = radius + 3
    fill_x = left + (right - left) * progress
    bar_span = radius * radius
    head_span = head * head
    groove = bytes([1]) * _WIDTH
    pink = bytes([2]) * _WIDTH
    for y in range(_HEIGHT):
        dy2 = (y + 0.5 - cy) ** 2
        row = y * _WIDTH
        if dy2 <= bar_span:
            extent = math.sqrt(bar_span - dy2)
            x0 = max(0, math.ceil(left - extent - 0.5 - 1e-9))
            x1 = min(_WIDTH - 1, math.floor(right + extent - 0.5 + 1e-9))
            if x0 <= x1:
                pixels[row + x0 : row + x1 + 1] = groove[: x1 - x0 + 1]
                if progress >= 1:
                    pixels[row + x0 : row + x1 + 1] = pink[: x1 - x0 + 1]
                elif progress > 0:
                    filled = min(x1, math.floor(fill_x - 0.5 + 1e-9))
                    if filled >= x0:
                        pixels[row + x0 : row + filled + 1] = pink[: filled - x0 + 1]
        if dy2 <= head_span:
            reach = math.sqrt(head_span - dy2)
            hx0 = max(0, math.ceil(fill_x - reach - 0.5 - 1e-9))
            hx1 = min(_WIDTH - 1, math.floor(fill_x + reach - 0.5 + 1e-9))
            for x in range(hx0, hx1 + 1):
                if pixels[row + x] and (x + 0.5 - fill_x) ** 2 + dy2 <= head_span:
                    pixels[row + x] = 3
    return bytes(pixels)


def _lzw(data: bytes) -> bytes:
    """Pack frame indexes. A GIF picture is LZW with a clear code in front."""
    clear = _COLORS
    end = clear + 1
    size = _CODE + 1
    nxt = end + 1
    table: dict[bytes, int] = {bytes([index]): index for index in range(clear)}
    acc = 0
    bits = 0
    out = bytearray()

    def emit(code: int) -> None:
        nonlocal acc, bits
        acc |= code << bits
        bits += size
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8

    emit(clear)
    prefix = bytes([data[0]])
    for raw in data[1:]:
        joined = prefix + bytes([raw])
        if joined in table:
            prefix = joined
            continue
        emit(table[prefix])
        if nxt < 4096:
            table[joined] = nxt
            nxt += 1
            if nxt > (1 << size) and size < 12:
                size += 1
        else:
            emit(clear)
            table = {bytes([index]): index for index in range(clear)}
            size = _CODE + 1
            nxt = end + 1
        prefix = bytes([raw])
    emit(table[prefix])
    emit(end)
    if bits:
        out.append(acc & 0xFF)
    return bytes(out)


def _chunks(data: bytes) -> bytes:
    parts = bytearray()
    for start in range(0, len(data), 255):
        block = data[start : start + 255]
        parts.append(len(block))
        parts.extend(block)
    parts.append(0)
    return bytes(parts)


# Color 0 is clear, so the card shows through everywhere the track is not.
_IMAGE = struct.pack("<BHHHHB", 0x2C, 0, 0, _WIDTH, _HEIGHT, 0) + bytes((_CODE,))
_BLOCKS: list[bytes] | None = None


def _blocks() -> list[bytes]:
    """Each knob position, packed once. A song only chooses which one is sent."""
    global _BLOCKS
    if _BLOCKS is None:
        _BLOCKS = [_chunks(_lzw(_paint(step))) for step in range(_FRAMES)]
    return _BLOCKS


def _gif(steps: list[int], delays: list[int]) -> bytes:
    colors = bytearray()
    for red, green, blue in _PALETTE:
        colors.extend((red, green, blue))
    colors.extend(bytes(3 * (_COLORS - len(_PALETTE))))
    # 0xF7: the table is present, and both size fields say 256 colors.
    screen = struct.pack("<HHBBB", _WIDTH, _HEIGHT, 0xF7, 0, 0)
    packed = _blocks()
    # Loop count 1 plays once. Count 0 repeats forever, back at the first frame.
    once = bytes((
        0x21, 0xFF, 0x0B,
        *b"NETSCAPE2.0",
        0x03, 0x01, 0x01, 0x00,
        0x00,
    ))
    parts = [b"GIF89a", screen, bytes(colors), once]
    for step, delay in zip(steps, delays):
        # The low bit marks color 0 as clear.
        parts.append(bytes((0x21, 0xF9, 0x04, 0x05, delay & 0xFF, (delay >> 8) & 0xFF, 0, 0)))
        parts.append(_IMAGE)
        parts.append(packed[step])
    parts.append(b"\x3b")
    return b"".join(parts)


@dataclass
class _Bar:
    """The picture already on the card."""

    key: str
    position: int
    length: int
    frozen: bool
    name: str
    mark: float
    step: int


def _bar_key(state: PlayState) -> str:
    if state.uri:
        return state.uri
    return f"{state.title}\n{state.author}\n{state.length}"


class _BarClock:
    """The picture on the card, and the reading it was cut from."""

    def __init__(self) -> None:
        self.bar: _Bar | None = None
        self.fresh: tuple[str, bytes] | None = None
        self.heard: int | None = None
        self.n = 0

    def take(self) -> tuple[str, bytes] | None:
        fresh = self.fresh
        self.fresh = None
        return fresh

    def _elapsed(self, held: _Bar) -> int:
        return max(0, int((time.monotonic() - held.mark) * 1000))

    def _clock(self, held: _Bar) -> int:
        """Where the song is on the picture's own clock."""
        return min(held.length, held.position + self._elapsed(held))

    def _seeked(self, held: _Bar, position: int, heard: int | None, *, seek: bool) -> bool:
        """A typed seek cuts a new frame. A late reading leaves the one on screen."""
        clock = held.position if held.frozen else self._clock(held)
        return bar_jumped(
            position, held.position, heard, clock, seek=seek, frozen=held.frozen,
        )

    def _store(
        self,
        key: str,
        position: int,
        length: int,
        frozen: bool,
        steps: list[int],
        delays: list[int],
    ) -> bool:
        picture = _gif(steps, delays)
        self.n += 1
        name = f"bar-{self.n}.gif"
        self.bar = _Bar(key, position, length, frozen, name, time.monotonic(), steps[0])
        self.fresh = (name, picture)
        return True

    def sync(
        self,
        state: PlayState | None,
        *,
        paused: bool,
        hearing: bool,
        seek: bool = False,
    ) -> bool:
        """Move the one frame forward with the song.

        The same pixel stays put, including when the card is edited and when
        the reading falls behind. A seek draws the pixel the song jumped to.
        """
        show = state is not None and state.has_track and state.length > 0
        if not show or state is None:
            changed = self.bar is not None
            self.bar = None
            self.heard = None
            return changed
        frozen = paused or not hearing
        key = _bar_key(state)
        position = min(max(0, state.position), state.length)
        held = self.bar
        heard = None if held is None or held.key != key else self.heard
        self.heard = position
        same = held is not None and held.key == key and held.length == state.length
        seeked = False if held is None else self._seeked(held, position, heard, seek=seek)
        if same and held is not None and not seeked and position < held.position:
            position = held.position
        pixel = _step(position, state.length)
        if same and held is not None and held.step == pixel:
            held.frozen = frozen
            held.position = position
            return False
        return self._store(key, position, state.length, frozen, [pixel], [100])
