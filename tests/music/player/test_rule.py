"""One answer for a press, a new song, and a jump in the bar. No Discord login."""

from __future__ import annotations

from music.player.rule import (
    after_quiet,
    bar_jumped,
    card_stays,
    click_answer,
    hinted_modes,
    line_modes,
    mode_press,
    pause_position,
    previous_step,
    repeat_after_replacement,
    spend_mode,
    track_ending,
    waiting_kept,
)


def test_a_press_a_new_song_and_a_jump_each_have_one_answer():
    """Mode buttons, the bar, and the gap between songs each take one path."""
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

    assert card_stays(False, False) is False
    assert card_stays(True, False) is True
    assert card_stays(False, True) is True
    assert waiting_kept(3, cap=15) == 3
    assert waiting_kept(16, cap=15) == 15

    assert bar_jumped(5_000, 5_000, None, 0, seek=True, frozen=False) is False
    assert bar_jumped(5_000, 4_000, None, 0, seek=True, frozen=False) is True
    assert bar_jumped(1_500, 1_000, None, 0, seek=False, frozen=True) is False
    assert bar_jumped(2_100, 1_000, None, 0, seek=False, frozen=True) is True
    assert bar_jumped(5_000, 0, None, 3_000, seek=False, frozen=False) is True
    assert bar_jumped(3_500, 0, None, 3_000, seek=False, frozen=False) is False
    assert bar_jumped(1_000, 0, 3_000, 5_000, seek=False, frozen=False) is True
    assert bar_jumped(2_500, 0, 3_000, 5_000, seek=False, frozen=False) is False

    assert repeat_after_replacement(True, replaced=True) is False
    assert repeat_after_replacement(True, replaced=False) is True
    assert track_ending(
        reason="finished", repeating=True, station=False, length=1_000, short=False, waiting=True,
    ) == "repeat"
    assert track_ending(
        reason="finished", repeating=True, station=True, length=1_000, short=False, waiting=False,
    ) == "station"
    assert track_ending(
        reason="finished", repeating=True, station=True, length=1_000, short=False, waiting=True,
    ) == "advance"
    assert track_ending(
        reason="finished", repeating=True, station=False, length=0, short=True, waiting=False,
    ) == "advance"
    assert track_ending(
        reason="replaced", repeating=True, station=False, length=1_000, short=False, waiting=False,
    ) == "advance"

    assert previous_step(movable=True, earlier=True, playing=False) == "back"
    assert previous_step(movable=True, earlier=False, playing=False) == "resume"
    assert previous_step(movable=False, earlier=False, playing=True) == "stay"
    assert previous_step(movable=True, earlier=False, playing=True) == "restart"
    assert pause_position(0, 5_000) == 5_000
    assert pause_position(8_000, 5_000) == 8_000

    assert click_answer("album", playing=True) == "aside"
    assert click_answer("album", playing=False) == "note"
    assert click_answer("album", busy=True, playing=True) == "aside"
    assert click_answer("pause", busy=True) == "defer"
    assert click_answer("pause") == "edit"
    assert spend_mode(True, False) == (True, True)
    assert spend_mode(False, True) == (True, False)
    assert spend_mode(False, False) == (False, False)
    assert after_quiet("repeat", failed=False, showing=True, holding=False) == "stay"
    assert after_quiet("shuffle", failed=True, showing=True, holding=True) == "restore"
    assert after_quiet("repeat", failed=False, showing=False, holding=True) == "stay"
    assert after_quiet(None, failed=False, showing=False, holding=False) == "dismiss"
    assert after_quiet("radio", failed=False, showing=True, holding=False) == "dismiss"
