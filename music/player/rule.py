"""Rules shared by the card, the saved line, and the play bar.

The player keeps those three in step by asking here. A press, a new song,
and a jump in the bar each have one answer.
"""

from __future__ import annotations

# A late reading inside this window stays on the picture. A typed seek does not.
SEEK_MS = 1_000


def _words_in(name: str, text: str) -> bool:
    """True when every word of `name` is a whole-word run inside `text`."""
    words = name.casefold().split()
    hay = text.casefold().split()
    size = len(words)
    if not size or size > len(hay):
        return False
    return any(hay[index:index + size] == words for index in range(len(hay) - size + 1))


def hinted_modes(
    repeating: bool,
    shuffling: bool,
    radio: bool,
    kind: str,
    value: bool,
) -> tuple[bool, bool, bool]:
    """Buttons after this press, before the line has answered.

    Repeat and shuffle cancel each other. Radio on turns shuffle off and
    leaves repeat, because the station is the button, not the grey line.
    """
    if kind == "repeat":
        repeating = value
        if repeating:
            shuffling = False
        return repeating, shuffling, radio
    if kind == "shuffle":
        shuffling = value
        if shuffling:
            repeating = False
        return repeating, shuffling, radio
    if kind == "radio":
        radio = value
        if radio:
            shuffling = False
        return repeating, shuffling, radio
    return repeating, shuffling, radio


def line_modes(
    repeating: bool,
    shuffling: bool,
    radio: bool,
    kind: str,
) -> tuple[bool, bool, bool] | None:
    """The saved flags after the press. None when radio ignores shuffle."""
    if kind == "shuffle" and radio:
        return None
    if kind == "repeat":
        return hinted_modes(repeating, shuffling, radio, kind, not repeating)
    if kind == "shuffle":
        return hinted_modes(repeating, shuffling, radio, kind, not shuffling)
    if kind == "radio":
        return hinted_modes(repeating, shuffling, radio, kind, not radio)
    return None


def mode_press(
    kind: str,
    repeating: bool,
    shuffling: bool,
    radio: bool,
    *,
    live: bool,
    stuck: bool,
) -> tuple[str, bool] | None:
    """The next frame for a mode button. None when that press does nothing.

    A stream can leave repeat. It cannot enter it.
    """
    if kind == "repeat":
        if stuck and not repeating:
            return None
        return ("repeat", not repeating)
    if kind == "shuffle":
        if radio:
            return None
        return ("shuffle", not shuffling)
    if kind == "radio":
        if not live:
            return None
        return ("radio", not radio)
    return None


def card_stays(holding: bool, mode_press: bool) -> bool:
    """The gap between songs, or a mode press while nothing is on, keeps the card."""
    return holding or mode_press


def waiting_kept(count: int, *, cap: int) -> int:
    """How many waiting songs stay. Previous puts one back, and the cap still holds."""
    if count < cap:
        return count
    return cap


def bar_jumped(
    reported: int,
    shown: int,
    heard: int | None,
    clock: int,
    *,
    seek: bool,
    frozen: bool,
) -> bool:
    """Whether the picture is cut again.

    A typed seek moves to the spot that was asked for. A late reading, or a
    pause sitting on the same pixel, stays where the picture already is.
    """
    if seek:
        return reported != shown
    if frozen:
        return abs(reported - shown) > SEEK_MS
    if reported > clock + SEEK_MS:
        return True
    return heard is not None and reported + SEEK_MS < heard


def repeat_after_replacement(repeating: bool, *, replaced: bool) -> bool:
    """Another site's copy is a new song. Repeat belonged to the one it replaced.

    Skip, next, and previous keep it. Only that replacement turns it off.
    """
    if replaced:
        return False
    return repeating


def track_ending(
    *,
    reason: str,
    repeating: bool,
    station: bool,
    length: int,
    short: bool,
    waiting: bool,
) -> str:
    """What a finished song does: repeat, station, or advance.

    Repeat of this song wins, so radio waits with the queue. A short station
    drop reloads the station. Advance is the line, and radio lives there.
    """
    if reason == "finished" and repeating and not station and length > 0:
        return "repeat"
    if station and (short or not waiting):
        return "station"
    return "advance"


def previous_step(
    *,
    movable: bool,
    earlier: bool,
    playing: bool,
) -> str:
    """restart, back, resume, or stay.

    An earlier song plays when there is one. With none, a song that can move
    starts again.
    """
    if earlier:
        return "back"
    if not playing:
        return "resume"
    if not movable:
        return "stay"
    return "restart"


def pause_position(reported: int, saved: int) -> int:
    """The place to keep. A paused player can report the start, so the saved position wins when it is ahead."""
    if saved > reported:
        return saved
    return reported


def click_answer(kind: str, *, busy: bool = False, playing: bool = True) -> str:
    """How this click is accepted: edit, defer, aside, or note.

    A free card's edit is the acceptance, and deferring first would throw it
    away. A busy card defers, because Discord gives up after three seconds.
    An album add while a song is on defers and leaves that message up. An album
    add on a quiet room says which song is starting.
    """
    if kind == "album":
        if playing:
            return "aside"
        return "note"
    if busy:
        return "defer"
    return "edit"


def spend_mode(pressed: bool, spare: bool) -> tuple[bool, bool]:
    """Whether this refresh keeps the card, and whether one sibling refresh still must.

    The press spends itself and leaves a spare, so the tick and the redraw can
    both be the refresh that follows. The refresh after those two can close it.
    """
    if pressed:
        return True, True
    if spare:
        return True, False
    return False, False


def after_quiet(kind: str | None, *, failed: bool, showing: bool, holding: bool) -> str:
    """stay, restore, or dismiss.

    A mode press on a quiet player keeps the card. A failed press while a song
    is still coming puts the real buttons back. An empty room takes the card down.
    """
    if kind in ("repeat", "shuffle") and not failed and showing:
        return "stay"
    if failed and showing and holding:
        return "restore"
    if holding:
        return "stay"
    return "dismiss"


# The status line the card and the refresh loop both write.
_MODE_LINES = ("-# On Queue", "-# On Repeat", "-# On Shuffle")


def _mode(repeating: bool, shuffling: bool) -> str:
    return "On Repeat" if repeating else ("On Shuffle" if shuffling else "On Queue")


def _replace_mode(text: str, repeating: bool, shuffling: bool) -> str:
    fresh = f"-# {_mode(repeating, shuffling)}"
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line in _MODE_LINES:
            lines[index] = fresh
            return "\n".join(lines)
    return text
