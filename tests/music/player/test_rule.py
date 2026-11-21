"""One answer for a press, a new song, and the end of a song. No Discord login."""

from __future__ import annotations

from music.player.rule import (
    card_first, hinted_modes, line_modes, mode_press, next_title, skip_step, track_ending,
)


def test_a_press_and_a_new_song_each_have_one_answer():
    """Mode buttons and the gap between songs each take one path."""
    assert hinted_modes(False, True, True, "repeat", True) == (True, False, True)
    assert hinted_modes(True, False, False, "repeat", False) == (False, False, False)
    assert hinted_modes(True, False, False, "shuffle", True) == (False, True, False)
    assert hinted_modes(False, True, False, "shuffle", False) == (False, False, False)
    assert hinted_modes(True, True, False, "radio", True) == (True, False, True)
    assert hinted_modes(False, False, True, "radio", False) == (False, False, False)
    assert hinted_modes(True, True, True, "volume", True) == (True, True, True)

    assert line_modes(False, True, False, "repeat") == (True, False, False)
    assert line_modes(True, False, False, "repeat") == (False, False, False)
    assert line_modes(False, False, True, "shuffle") is None
    assert line_modes(False, False, False, "shuffle") == (False, True, False)
    assert line_modes(False, True, False, "radio") == (False, False, True)
    assert line_modes(False, False, True, "radio") == (False, False, False)
    assert line_modes(False, False, False, "volume") is None

    assert mode_press("repeat", False, False, False, live=True, stuck=True) is None
    assert mode_press("repeat", True, False, False, live=True, stuck=True) == ("repeat", False)
    assert mode_press("repeat", False, False, False, live=False, stuck=False) == ("repeat", True)
    assert mode_press("shuffle", False, False, True, live=True, stuck=False) is None
    assert mode_press("shuffle", False, False, False, live=False, stuck=False) == ("shuffle", True)
    assert mode_press("radio", False, False, False, live=False, stuck=False) is None
    assert mode_press("radio", False, False, False, live=True, stuck=False) == ("radio", True)
    assert mode_press("volume", False, False, False, live=True, stuck=False) is None

    assert track_ending(
        reason="finished", repeating=True, videos=False, length=1_000, short=False, waiting=True,
    ) == "repeat"
    assert track_ending(
        reason="finished", repeating=True, videos=True, length=1_000, short=False, waiting=False,
    ) == "videos"
    assert track_ending(
        reason="finished", repeating=True, videos=True, length=1_000, short=False, waiting=True,
    ) == "advance"
    assert track_ending(
        reason="finished", repeating=True, videos=False, length=0, short=True, waiting=False,
    ) == "advance"
    assert track_ending(
        reason="replaced", repeating=True, videos=False, length=1_000, short=False, waiting=False,
    ) == "advance"
    assert skip_step(repeating=True, videos=False, length=1_000) == "repeat"
    assert skip_step(repeating=True, videos=False, length=1_000, waiting=True) == "advance"
    assert skip_step(repeating=True, videos=True, length=1_000) == "advance"
    assert skip_step(repeating=True, videos=False, length=0) == "advance"
    assert skip_step(repeating=False, videos=False, length=1_000) == "advance"
    assert card_first("next") is True
    assert card_first("previous") is True
    assert card_first("pause") is False
    assert card_first("seek") is False
    assert card_first(None) is False
    assert next_title("After") == "After"
    assert next_title(None) == "Nothing"
    assert next_title("") == "Nothing"
