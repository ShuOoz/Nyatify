"""The play bar. No Discord login and no Lavalink node."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, MagicMock

import discord

from music.playback import _stored, stop_track
from music.player.card import PAUSE_MARK, PLAY_MARK, MusicPlayer
from music.player.playbar import (
    _BAR, _COLORS, _FRAMES, _WIDTH, _gif, _paint, _step,
)
from tests.music.player.support import _fitted
from tests.music.support import _playing, _press, _scene


def _still(position: int, length: int) -> bytes:
    """The one frame the player uploads for this spot in the song."""
    return _gif([_step(min(position, length), length)], [100])


async def test_the_bar_follows_the_song(monkeypatch):
    """The picture fills the row. It follows the song, a typed spot moves it, and a failed link edit puts it back."""
    case_the_bar_picture()
    await _scene(case_the_play_bar_follows_the_song_and_stays_on_a_color_change, monkeypatch=monkeypatch)
    await _scene(case_a_late_reading_does_not_pull_the_bar_backward, monkeypatch=monkeypatch)
    await _scene(case_a_typed_spot_moves_the_bar, monkeypatch=monkeypatch)
    await _scene(case_a_failed_cite_puts_the_bar_back_on_the_attachment)

def case_the_bar_picture():
    """The still picture fills the button row, and a finished song fills the track."""
    frame = _paint(_FRAMES - 1)
    ink = [index % _WIDTH for index, pixel in enumerate(frame) if pixel]
    # The upper row is 14.452 em and the lower row is 14.423 em. The picture
    # is 350px so the drawn bar meets the end of the longer row.
    picture = _still(0, 8_000)
    width, height = struct.unpack_from("<HH", picture, 6)
    assert _FRAMES == _WIDTH - _BAR + 1
    assert width == 350
    assert width == _WIDTH
    assert height == 16
    assert min(ink) <= 1
    assert max(ink) >= _WIDTH - 2
    # Windows loads a 256-color picture.
    assert picture[10] == 0xF7
    index = 13 + _COLORS * 3
    image = picture.index(0x2C, index)
    assert picture[image + 10] == 8
    frames = 0
    while picture[index] != 0x3B:
        if picture[index] == 0x21:
            if picture[index + 1] == 0xF9:
                frames += 1
            index += 3 + picture[index + 2]
        else:
            index += 11
        while picture[index] != 0:
            index += 1 + picture[index]
        index += 1
    assert frames == 1

async def case_the_play_bar_follows_the_song_and_stays_on_a_color_change(monkeypatch):
    guild, player, _track = await _playing()
    clock = {"now": 1_000.0}
    monkeypatch.setattr("music.player.playbar.time.monotonic", lambda: clock["now"])
    try:
        view = MusicPlayer(guild)
        opening = view.take_bar()
        assert opening is not None
        assert opening.filename == "bar-1.gif"
        assert opening.fp.read() == _still(0, 148_000)
        parts = view.children[0].children
        assert isinstance(parts[2], discord.ui.Section)
        assert isinstance(parts[3], discord.ui.MediaGallery)
        assert isinstance(parts[4], discord.ui.Separator)
        assert isinstance(parts[5], discord.ui.ActionRow)
        assert view.paint() is False
        assert view.take_bar() is None

        def gallery():
            return next(item for item in view.walk_children() if isinstance(item, discord.ui.MediaGallery))

        # The tick moves the one frame forward. Switching windows has no
        # earlier frame to restart. A seek still draws the new place.
        clock["now"] += 3
        player._position = 3_000
        assert view.paint() is True
        moved_on = view.take_bar()
        assert moved_on is not None
        assert moved_on.fp.read() == _still(3_000, 148_000)
        assert gallery().items[0].media.url == "attachment://bar-2.gif"
        player._position = 0
        assert view.paint() is True
        restarted = view.take_bar()
        assert restarted is not None
        assert restarted.fp.read() == _still(0, 148_000)
        assert gallery().items[0].media.url == "attachment://bar-3.gif"

        clock["now"] += 6
        player._position = 6_000
        assert view.paint() is True
        later = view.take_bar()
        assert later is not None
        assert later.fp.read() == _still(6_000, 148_000)
        assert gallery().items[0].media.url == "attachment://bar-4.gif"

        press = _press(player)
        await view.shuffle.callback(press)
        assert "attachments" not in press.response.edit_message.await_args.kwargs
        assert gallery().items[0].media.url == "attachment://bar-4.gif"

        player._position = 90_000
        assert view.paint() is True
        moved = view.take_bar()
        assert moved is not None
        assert moved.fp.read() == _still(90_000, 148_000)
        assert gallery().items[0].media.url == "attachment://bar-5.gif"
        placed = view.children[0].children
        song_at = next(i for i, item in enumerate(placed) if isinstance(item, discord.ui.Section) and item.accessory is not view.leave)
        assert isinstance(placed[song_at + 1], discord.ui.MediaGallery)
        assert isinstance(placed[song_at + 2], discord.ui.Separator)
        assert isinstance(placed[song_at + 3], discord.ui.ActionRow)

        player._paused = True
        assert view.paint() is True
        assert view.pause.label == _fitted(PLAY_MARK)
        assert view.pause.style == discord.ButtonStyle.primary
        assert view.take_bar() is None
        assert gallery().items[0].media.url == "attachment://bar-5.gif"

        player._position = 40_000
        player._paused = False
        again = MusicPlayer(guild)
        assert again.pause.label == _fitted(PAUSE_MARK)
        assert again.pause.style == discord.ButtonStyle.secondary
        reopened = again.take_bar()
        assert reopened is not None
        assert reopened.fp.read() == _still(40_000, 148_000)
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_a_late_reading_does_not_pull_the_bar_backward(monkeypatch):
    """A reading behind the frame stays there. A later one moves it forward."""
    guild, player, _track = await _playing()
    clock = {"now": 1_000.0}
    monkeypatch.setattr("music.player.playbar.time.monotonic", lambda: clock["now"])
    try:
        view = MusicPlayer(guild)
        assert view.take_bar() is not None

        clock["now"] += 2
        player._position = 0
        assert view.paint() is False
        assert view.take_bar() is None

        clock["now"] += 4
        player._position = 4_000
        assert view.paint() is True
        caught = view.take_bar()
        assert caught is not None
        assert caught.fp.read() == _still(4_000, 148_000)
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_a_typed_spot_moves_the_bar(monkeypatch):
    """A typed spot moves the bar, paused or not. A reading behind the picture stays."""
    from music.playback import toggle_pause

    guild, player, _track = await _playing()
    clock = {"now": 1_000.0}
    monkeypatch.setattr("music.player.playbar.time.monotonic", lambda: clock["now"])
    view = None
    try:
        view = MusicPlayer(guild)
        assert view.take_bar() is not None
        player._position = 10_000
        assert view.paint() is True
        assert view._bars.bar.position == 10_000
        view.take_bar()

        player._position = 9_500
        assert view.paint() is False
        assert view._bars.bar.position == 10_000

        press = _press(player)
        press.response.send_modal = AsyncMock()
        await view.volume.callback(press)
        modal = press.response.send_modal.await_args.args[0]
        modal.level.component._value = "100"
        modal.place.component._value = "0:09"
        submit = _press(player)
        submit.response.defer = AsyncMock()
        await modal.on_submit(submit)
        assert player.seek.await_args.args[0] == 9_000
        assert view._bars.bar.position == 9_000
        uploaded = submit.response.edit_message.await_args.kwargs["attachments"]
        assert uploaded[0].fp.read() == _still(9_000, 148_000)
    finally:
        if view is not None:
            view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)

    guild, player, _track = await _playing()
    clock = {"now": 1_000.0}
    monkeypatch.setattr("music.player.playbar.time.monotonic", lambda: clock["now"])
    view = None
    try:
        view = MusicPlayer(guild)
        assert view.take_bar() is not None
        player._position = 10_000
        assert view.paint() is True
        view.take_bar()
        assert await toggle_pause(guild) is None
        view.paint()
        player._position = 9_000
        _stored(guild).position = 9_000
        assert view.paint() is False
        assert view._bars.bar.position == 10_000

        press = _press(player)
        press.response.send_modal = AsyncMock()
        await view.volume.callback(press)
        modal = press.response.send_modal.await_args.args[0]
        modal.level.component._value = "100"
        modal.place.component._value = "0:09"
        submit = _press(player)
        submit.response.defer = AsyncMock()
        await modal.on_submit(submit)
        assert view._bars.bar.position == 9_000
    finally:
        if view is not None:
            view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_a_failed_cite_puts_the_bar_back_on_the_attachment():
    """The gallery stays on the uploaded file when the link edit does not land."""
    from types import SimpleNamespace

    guild, _player, _track = await _playing()
    view = None
    try:
        view = MusicPlayer(guild)
        bar = view._bars.bar
        assert bar is not None
        assert str(view._gallery.items[0].media.url).startswith("attachment://")
        message = MagicMock()
        message.attachments = [
            SimpleNamespace(filename=bar.name, url="https://cdn.example/bar.gif"),
        ]
        message.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "no"))
        await view._cite(message)
        assert str(view._gallery.items[0].media.url).startswith("attachment://")
        assert view._closed is False
    finally:
        if view is not None:
            view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)
