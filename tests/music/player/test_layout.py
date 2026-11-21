"""The player face: ids, holds, and the up-next line. No Discord login."""

from __future__ import annotations

from unittest.mock import MagicMock

from music.player.card import MusicPlayer
from music.player.layout import (
    _kept,
    face_ids,
    held_buttons,
    paint_holds,
    part_id,
)
from music.playback import queue_tracks, stop_track

from tests.music.support import _named, _playing

def case_a_press_holds_the_buttons_and_each_part_has_one_id():
    """A press in flight greys every button. Each face part has its own id."""
    live = held_buttons(
        held=False, pause_off=False, repeat_off=False, shuffle_off=False, controls_off=False,
    )
    frozen = held_buttons(
        held=True, pause_off=False, repeat_off=False, shuffle_off=False, controls_off=False,
    )
    assert all(off is False for off in live.values())
    assert all(frozen.values())
    released = held_buttons(
        held=False, pause_off=False, repeat_off=True, shuffle_off=True, controls_off=False,
    )
    assert released["repeat"] is True and released["shuffle"] is True and released["pause"] is False
    assert released["volume"] is False and released["close"] is False and released["leave"] is False
    idle = held_buttons(
        held=True, pause_off=True, repeat_off=True, shuffle_off=True, controls_off=True,
    )
    assert idle["volume"] is True and idle["pause"] is True
    assert idle["close"] is True and idle["leave"] is True
    assert paint_holds(True, held=False, was=False) is True
    assert paint_holds(True, held=True, was=True) is True
    assert paint_holds(True, held=False, was=True) is False
    assert paint_holds(True, held=True, was=False) is False
    assert paint_holds(False, held=False, was=False) is False

    assert part_id("cover") != part_id("pause")
    assert part_id("missing") is None
    assert _kept(("pause", "pause")) is None
    assert _kept(("missing",)) is None
    assert _kept(("pause",)) == {"pause": part_id("pause")}
    playing = face_ids(playing=True, cover=True, status=True, bar=True)
    quiet = face_ids(playing=False, cover=False, status=True, bar=False)
    plain = face_ids(playing=True, cover=False, status=True, bar=False)
    assert playing is not None and quiet is not None and plain is not None
    assert len(set(playing.values())) == len(playing)
    assert "heading" not in playing and "status" not in playing
    assert "played" in playing and "cover" in playing and "song" in playing and "bar" in playing
    assert "heading" in quiet and "status" in quiet and "song" not in quiet and "bar" not in quiet
    assert "song" in plain and "status" in plain and "played" not in plain and "cover" not in plain
    assert playing["pause"] == part_id("pause")
    assert quiet["heading"] != playing["song"]
    assert all(number >= 1 for number in (*playing.values(), *quiet.values(), *plain.values()))


def _component_ids(view: MusicPlayer) -> list[int]:
    found: list[int] = []

    def walk(payload: object) -> None:
        if not isinstance(payload, dict) or "type" not in payload:
            return
        number = payload.get("id")
        if isinstance(number, int):
            found.append(number)
        for child in payload.get("components") or ():
            walk(child)
        walk(payload.get("accessory"))

    for component in view.to_components():
        walk(component)
    return found

async def test_a_button_keeps_the_same_component_ids():
    """A click patches the card. Each part keeps its id, and a cleared id is put back."""
    case_a_press_holds_the_buttons_and_each_part_has_one_id()
    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        await queue_tracks(member, guild, [_named("雨" * 40)])
        coming = MusicPlayer(guild)
        line = coming.song.content.split("Up next:", 1)[1].strip()
        assert line.startswith("雨")
        assert line.endswith("…")
        assert len(line) < 40
        view = MusicPlayer(guild)
        first = _component_ids(view)
        assert first
        assert len(first) == len(set(first))
        assert all(number >= 1 for number in first)
        assert part_id("pause") in first
        assert part_id("cover") in first
        assert part_id("bar") in first
        assert part_id("heading") not in first
        assert part_id("status") not in first
        pause = view.pause.id
        gallery = view._gallery
        assert gallery is not None and gallery.id == part_id("bar")
        view._hint = ("pause", True)
        assert view.paint(move_bar=False) is True
        assert gallery is view._gallery
        assert _component_ids(view) == first
        view.pause.id = None
        gallery.id = None
        view._keep()
        assert view.pause.id == pause
        assert gallery.id == part_id("bar")
        assert _component_ids(view) == first

        view._hint = None
        track.artwork = None
        assert view.paint() is True
        bare = _component_ids(view)
        assert bare
        assert len(bare) == len(set(bare))
        assert part_id("pause") in bare
        assert part_id("song") in bare
        assert part_id("cover") not in bare
        assert part_id("played") not in bare
        assert view.pause.id == pause
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)
