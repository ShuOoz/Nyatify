"""Mode buttons and the shared ending rule.

The card, the refresh loop, and playback ask the same question here: what a
mode press does, and what a finished song does next.
"""

from __future__ import annotations


def hinted_modes(
    repeating: bool,
    shuffling: bool,
    radio: bool,
    kind: str,
    value: bool,
) -> tuple[bool, bool, bool]:
    """Buttons after this press, before the line has answered.

    Repeat and shuffle cancel each other. On Radio turns shuffle off and
    leaves repeat. The grey line names both when they are on together.
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
    station: bool = False,
) -> tuple[str, bool] | None:
    """The next frame for a mode button. None when that press does nothing.

    A stream can leave repeat. It cannot enter it. A station can leave
    Last.fm radio. It cannot enter it.
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
        if not live or (station and not radio):
            return None
        return ("radio", not radio)
    return None


def song_repeats(*, repeating: bool, videos: bool, length: int) -> bool:
    """This recording plays again.

    Backup videos rotate instead. A stream has no length, so it does not either.
    """
    return repeating and not videos and length > 0


def track_ending(
    *,
    reason: str,
    repeating: bool,
    videos: bool,
    length: int,
    short: bool,
    waiting: bool,
) -> str:
    """What a finished song does: repeat, videos, or advance.

    Repeat of this recording wins, so On Radio waits with the queue. Backup
    videos reload on a short drop, and they move on when nothing is waiting.
    Advance is the line, and On Radio lives there.
    """
    if reason == "finished" and song_repeats(repeating=repeating, videos=videos, length=length):
        return "repeat"
    if videos and (short or not waiting):
        return "videos"
    return "advance"


def next_title(queued: str | None) -> str:
    """The song named as up next.

    The queue is that song. An empty queue stays Nothing. Repeat plays this
    recording again when it ends, and that replay is not a second name here.
    A radio pick is not this line either.
    """
    return queued or "Nothing"


def card_first(kind: str | None) -> bool:
    """Whether a picture already going out finishes before this song starts.

    Skip and back start with the card. A bar that is already uploading finishes
    first, so the song and that picture are not sent as two edits at once.
    """
    return kind in ("next", "previous")


def skip_step(*, repeating: bool, videos: bool, length: int, waiting: bool = False) -> str:
    """repeat or advance.

    A song that is waiting plays next. Repeat starts this one again only when
    nothing is waiting. Backup videos and a stream still move on.
    """
    if waiting or not song_repeats(repeating=repeating, videos=videos, length=length):
        return "advance"
    return "repeat"
