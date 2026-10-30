"""The player card. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock

import discord

from announce import ACCENT_COLOR
from cogs.osa_musify import OsaMusify
from music.panel import (
    LABEL_PLAYER,
    LABEL_QUEUE,
    LABEL_ADD_ALL,
    LABEL_LIKED,
    MARK_ADD,
    MARK_BACK,
    MARK_CLOSE,
    MARK_NEXT,
    MARK_SAVE,
    RADIO_MARK,
    page_line,
    progress_text,
)
from music.player.album import ALBUM_SECONDS, ALBUM_TITLE, ALL_HINT, _AlbumCard
from music.player.playbar import (
    _BAR, _BAR_TAIL_MS, _COLORS, _FRAMES, _SHOWN, _WIDTH, _paint, _place, _rest, _timed,
    bar_gif, bar_still,
)
from music.player.card import (
    CLOSE_MARK, INFO_MARK, NEXT_MARK, PAUSE_MARK, PLAY_MARK,
    PREVIOUS_MARK, REPEAT_MARK, SHUFFLE_MARK, TITLE, VOLUME_MARK, MusicPlayer,
)
from music.playlist.card import LiveLine
from music.playback import (
    advance_from_end,
    change_volume,
    next_track,
    play_now,
    now_playing,
    previous_track,
    queue_tracks,
    queued,
    stop_track,
    _stored,
)

from tests.music.support import (
    _Player,
    _ask,
    _card_text,
    _cards_caught_up,
    _named,
    _playing,
    _press,
)


def _fitted(mark: str, *, hair: int = 0) -> str:
    """Two halfwidth fillers on each side, so the mark stays centered."""
    side = "\uffa0" * 2
    gap = "\u200a" * hair
    return f"{side}{gap}{mark}{gap}{side}"

async def test_an_idle_player_leaves_only_close():
    guild = MagicMock()
    guild.id = 31
    channel = MagicMock()
    channel.id = 19
    player = _Player(channel)
    guild.voice_client = player
    view = MusicPlayer(guild)
    heading = view.children[0].children[0]
    assert isinstance(heading, discord.ui.Section)
    assert heading.children[0].content == TITLE
    assert heading.accessory is view.leave
    assert view.leave.label == "⏻"
    assert view.leave.disabled is False
    assert isinstance(view.children[0].children[1], discord.ui.Separator)
    assert view.close.disabled is False
    assert view.pause.label == _fitted(PAUSE_MARK)
    assert view.pause.style == discord.ButtonStyle.secondary
    idle = (
        view.previous,
        view.pause,
        view.skip,
        view.volume,
        view.radio,
        view.save,
        view.info,
        view.shuffle,
        view.repeat,
    )
    assert all(button.disabled for button in idle)

    track = _named("Ocean Drive")
    track.length = 148_000
    track.is_stream = False
    player._track = track
    assert view.paint() is True
    assert view.close.disabled is False
    assert view.skip.disabled is False
    assert view.previous.disabled is False
    assert view.volume.disabled is False
    assert view.volume.label == _fitted(VOLUME_MARK)
    assert view.radio.disabled is False
    assert view.shuffle.disabled is False

    track.is_stream = True
    track.length = 0
    assert view.paint() is True
    assert view.skip.disabled is False
    assert view.radio.disabled is False
    assert view.shuffle.disabled is False
    assert view.repeat.disabled is True

async def test_player_card_shows_the_current_song():
    guild, player, track = await _playing()
    try:
        state = now_playing(guild)
        assert state is not None
        assert state.requester_id == 17
        assert state.channel_id == 19
        assert state.position == 0
        assert state.length == 148_000

        view = MusicPlayer(guild)
        text = _card_text(view)
        parts = view.children[0].children
        assert isinstance(parts[0], discord.ui.Section)
        assert parts[0].children[0].content == TITLE
        assert parts[0].accessory is view.leave
        assert isinstance(parts[1], discord.ui.Separator)
        assert isinstance(parts[2], discord.ui.Section)
        assert isinstance(parts[3], discord.ui.MediaGallery)
        assert parts[3].items[0].media.url.startswith("attachment://bar-")
        assert isinstance(parts[4], discord.ui.Separator)
        assert parts[4].visible is True
        assert len(parts[2].children) == 1
        assert parts[2].children[0].content == (
            "### [I Ain't Worried](https://example.com/song)\nOneRepublic\n"
            "-# On Queue\n-# Up next: Nothing"
        )
        assert "OneRepublic" in text
        assert "Volume" not in text
        assert progress_text(0, 148_000) not in text
        galleries = [item for item in view.walk_children() if isinstance(item, discord.ui.MediaGallery)]
        assert len(galleries) == 1
        assert view.children[0].accent_colour == ACCENT_COLOR
        thumbs = [item for item in view.walk_children() if isinstance(item, discord.ui.Thumbnail)]
        assert [thumb.media.url for thumb in thumbs] == ["https://example.com/art.jpg"]
        assert view.pause.disabled is False
        assert view.previous.label == _fitted(PREVIOUS_MARK)
        assert view.pause.label == _fitted(PAUSE_MARK)
        assert view.pause.style == discord.ButtonStyle.secondary
        assert view.skip.label == _fitted(NEXT_MARK)
        assert view.close.label == _fitted(CLOSE_MARK, hair=1)
        rows = [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]
        assert rows[0].children == [
            view.shuffle, view.previous, view.pause, view.skip, view.repeat,
        ]
        assert rows[-1].children[-1] is view.close
        assert view.shuffle.label == _fitted(SHUFFLE_MARK)
        assert view.shuffle.disabled is False
        assert view.shuffle.style == discord.ButtonStyle.secondary
        assert view.repeat.label == _fitted(REPEAT_MARK)
        assert view.repeat.disabled is False
        assert view.repeat.style == discord.ButtonStyle.secondary

        track.artwork = None
        plain = MusicPlayer(guild)
        assert not any(isinstance(item, discord.ui.Thumbnail) for item in plain.walk_children())
        assert any(isinstance(item, discord.ui.MediaGallery) for item in plain.walk_children())

        order = []
        started = asyncio.Event()

        async def edit(**kwargs):
            await asyncio.wait_for(started.wait(), timeout=1)
            order.append("edit")
            assert view.pause.label == _fitted(PLAY_MARK)
            assert view.pause.style == discord.ButtonStyle.primary

        async def flip(value):
            order.append("pause")
            player._paused = value
            started.set()

        async def defer():
            # A real defer spends the response. The button edit has to happen first.
            interaction.response.is_done.return_value = True

        player._position = 12_000
        player.pause.side_effect = flip
        interaction = _press(player)
        interaction.response.edit_message.side_effect = edit
        interaction.response.defer.side_effect = defer
        await asyncio.wait_for(view.pause.callback(interaction), timeout=1)
        assert order == ["pause", "edit"]
        interaction.response.defer.assert_not_awaited()
        player.pause.assert_awaited_once_with(True)
        interaction.response.edit_message.assert_awaited()
        paused_file = interaction.response.edit_message.await_args.kwargs["attachments"]
        assert len(paused_file) == 1
        assert paused_file[0].fp.read() == bar_still(12_000, 148_000)
        paused = _card_text(view)
        assert view.pause.label == _fitted(PLAY_MARK)
        assert view.pause.style == discord.ButtonStyle.primary
        assert progress_text(12_000, 148_000) not in paused

        outsider = MagicMock()
        outsider.send = AsyncMock()
        outsider.user.voice = None
        await view.on_close(outsider)
        assert view._closed is False
        assert "Join the voice channel first" in outsider.send.await_args.args[0]

        closer = _press(player)
        closed = []

        async def answer():
            closed.append("defer")
            closer.response.is_done.return_value = True

        closer.response.defer.side_effect = answer
        closer.delete_original_response = AsyncMock()
        await view.on_close(closer)
        assert closed == ["defer"]
        player.stop.assert_not_awaited()
        player.disconnect.assert_not_awaited()
        closer.delete_original_response.assert_awaited()
        assert view._closed is True
        assert now_playing(guild).has_track is True
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_a_second_press_answers_while_the_card_is_still_updating():
    """Play on one device, then pause on either. The second click cannot wait on the first edit."""
    guild, player, _track = await _playing()
    try:
        view = MusicPlayer(guild)
        held = asyncio.Event()
        release = asyncio.Event()

        async def slow_edit(**kwargs):
            held.set()
            await release.wait()

        first = _press(player)
        first.response.edit_message.side_effect = slow_edit
        started = asyncio.create_task(view.pause.callback(first))
        await asyncio.wait_for(held.wait(), timeout=1)

        second = _press(player)
        answered = asyncio.Event()

        async def defer():
            answered.set()
            second.response.is_done.return_value = True

        second.response.defer.side_effect = defer
        again = asyncio.create_task(view.pause.callback(second))
        await asyncio.wait_for(answered.wait(), timeout=1)
        release.set()
        await started
        await again
        second.response.defer.assert_awaited()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_an_open_player_refreshes_when_the_line_changes():
    guild, player, _track = await _playing()
    panel = None
    try:
        cog = OsaMusify(MagicMock())
        panel = MusicPlayer(guild, discard=cog._forget_panel)
        cog._panels.append(panel)
        message = MagicMock()
        message.edit = AsyncMock()
        panel.follow(message)
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        await _cards_caught_up()
        assert panel.meta.content == "-# On Queue\n-# Up next: After"
        message.edit.assert_awaited()

        assert await play_now(member, guild, _named("Echo"), "youtube") == "Playing Echo."
        await _cards_caught_up()
        assert "Echo" in panel.song.content
        press = _ask()
        press.guild = guild
        press.response.is_done.return_value = True
        press.channel.send = AsyncMock()
        await cog._open_beside(press)
        press.channel.send.assert_not_awaited()
    finally:
        if panel is not None:
            panel.shutdown()
            await asyncio.sleep(0)
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_a_failed_refresh_keeps_the_buttons(monkeypatch):
    guild, _player, _track = await _playing()
    try:
        async def no_wait(_seconds):
            return None

        monkeypatch.setattr("music.player.sync.asyncio.sleep", no_wait)
        view = MusicPlayer(guild)
        message = MagicMock()
        message.delete = AsyncMock()
        view.message = message
        calls = []

        async def redraw(interaction=None):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise discord.HTTPException(MagicMock(status=400), "bad artwork")
            guild.voice_client = None

        view.redraw = redraw
        view.follow(message)
        await view._task
        assert calls == [1, 2]
        assert view._closed is True
        message.delete.assert_awaited()
    finally:
        guild.voice_client = None

async def test_repeat_plays_the_song_again_without_queueing_it():
    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        view = MusicPlayer(guild)
        player.play.reset_mock()
        press = _press(player)
        press.response.defer = AsyncMock()
        await view.repeat.callback(press)
        assert _stored(guild).repeat is True
        assert _stored(guild).earlier == []
        assert [item.track.title for item in _stored(guild).later] == ["After"]
        player.play.assert_not_awaited()
        assert view.repeat.style == discord.ButtonStyle.primary
        assert view.meta.content == "-# On Repeat\n-# Up next: After"

        player._guild = guild
        player._last_position = 148_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "I Ain't Worried"
        assert now_playing(guild).repeating is True
        assert [item.track.title for item in _stored(guild).later] == ["After"]
        assert _stored(guild).earlier == []
        player.play.assert_awaited_with(track)

        await view.repeat.callback(press)
        assert _stored(guild).repeat is False
        assert view.repeat.style == discord.ButtonStyle.secondary
        player._track = None
        player._last_position = 148_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "After"
        assert [item.track.title for item in _stored(guild).later] == []
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_repeat_and_shuffle_only_change_the_mode_when_nothing_is_on():
    guild, player, _track = await _playing()
    view = None
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        view = MusicPlayer(guild)
        message = MagicMock()
        message.edit = AsyncMock()
        message.delete = AsyncMock()
        view.follow(message)
        player._track = None
        press = _press(player)
        press.response.send_message = AsyncMock()
        press.followup.send = AsyncMock()

        await view.repeat.callback(press)
        await _cards_caught_up()
        assert _stored(guild).repeat is True
        assert view._closed is False
        assert view.repeat.style == discord.ButtonStyle.primary
        assert view.shuffle.style == discord.ButtonStyle.secondary
        text = _card_text(view)
        assert "On Repeat" in text
        assert "I Ain't Worried" in text
        assert "Nothing is playing" not in text
        press.response.send_message.assert_not_awaited()
        press.followup.send.assert_not_awaited()

        await view.shuffle.callback(press)
        await _cards_caught_up()
        assert _stored(guild).shuffle is True
        assert _stored(guild).repeat is False
        assert view._closed is False
        assert view.shuffle.style == discord.ButtonStyle.primary
        assert view.repeat.style == discord.ButtonStyle.secondary
        shuffled = _card_text(view)
        assert "On Shuffle" in shuffled
        assert "I Ain't Worried" in shuffled
        assert "Nothing is playing" not in shuffled
        press.response.send_message.assert_not_awaited()
    finally:
        if view is not None:
            task = view._task
            view.shutdown()
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_shuffle_mixes_the_queue_until_repeat(monkeypatch):
    guild, player, _track = await _playing()
    card = None
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        added = await queue_tracks(member, guild, [_named("One"), _named("Two"), _named("Three")])
        assert added == "Added 3 songs."
        view = MusicPlayer(guild)
        press = _press(player)
        press.response.defer = AsyncMock()

        def reverse(songs):
            songs.reverse()

        card = LiveLine(guild, MagicMock())
        message = MagicMock()
        message.edit = AsyncMock()
        card.follow(message)
        assert "**1\\.** One" in _card_text(card)
        assert "On shuffle" not in _card_text(card)

        monkeypatch.setattr("music.playback.control.random.shuffle", reverse)
        await view.shuffle.callback(press)
        await _cards_caught_up()
        assert _stored(guild).shuffle is True
        assert _stored(guild).repeat is False
        assert [item.track.title for item in _stored(guild).later] == ["Three", "Two", "One"]
        assert [row.title for row in queued(guild) if not row.playing] == ["Three", "Two", "One"]
        assert now_playing(guild).shuffling is True
        assert view.shuffle.style == discord.ButtonStyle.primary
        assert view.repeat.style == discord.ButtonStyle.secondary
        assert view.meta.content.startswith("-# On Shuffle\n")
        shuffled = _card_text(card)
        assert "### Now Playing - On shuffle" in shuffled
        assert "**1\\.** Three" in shuffled
        assert "**2\\.** Two" in shuffled
        assert "**3\\.** One" in shuffled

        monkeypatch.setattr("music.playback.remember.random.randrange", lambda _n: 0)
        assert await queue_tracks(member, guild, [_named("Front")]) == "Added Front."
        await _cards_caught_up()
        assert [item.track.title for item in _stored(guild).later] == ["Front", "Three", "Two", "One"]
        assert "**1\\.** Front" in _card_text(card)

        await view.shuffle.callback(press)
        await _cards_caught_up()
        assert _stored(guild).shuffle is False
        assert [item.track.title for item in _stored(guild).later] == ["One", "Two", "Three", "Front"]
        assert [row.title for row in queued(guild) if not row.playing] == ["One", "Two", "Three", "Front"]
        restored = _card_text(card)
        assert "On shuffle" not in restored
        assert "**1\\.** One" in restored
        assert view.shuffle.style == discord.ButtonStyle.secondary
        assert await queue_tracks(member, guild, [_named("End")]) == "Added End."
        assert [item.track.title for item in _stored(guild).later] == ["One", "Two", "Three", "Front", "End"]

        await view.repeat.callback(press)
        assert _stored(guild).repeat is True
        assert _stored(guild).shuffle is False
        assert view.repeat.style == discord.ButtonStyle.primary
        assert view.shuffle.style == discord.ButtonStyle.secondary
        assert [item.track.title for item in _stored(guild).later][-1] == "End"
    finally:
        if card is not None:
            card.stop()
        if guild.voice_client is not None:
            await stop_track(guild)

def test_a_tick_opens_on_the_frame_already_showing():
    """An edit starts the picture over, so it opens on the frame already up."""
    started = _timed(0, 180_000, tail=_BAR_TAIL_MS)
    assert started is not None
    index, _into = _place(started[1], 5_000)
    kept = _rest(*started, 5_000)
    fresh = _timed(5_000, 180_000, tail=_BAR_TAIL_MS)
    assert fresh is not None
    assert kept[0][0] == started[0][index]
    # One pixel of travel. A new cut at this moment is not a jump back.
    assert abs(fresh[0][0] - kept[0][0]) <= 2


def test_the_bar_is_the_whole_button_row():
    frame = _paint(_FRAMES - 1)
    ink = [index % _WIDTH for index, pixel in enumerate(frame) if pixel]
    # The upper row is 14.452 em and the lower row is 14.423 em. The picture
    # is 350px so the drawn bar meets the end of the longer row. Each frame
    # moves the knob one pixel.
    picture = bar_gif(0, 8_000)
    width, height = struct.unpack_from("<HH", picture, 6)
    assert _FRAMES == _WIDTH - _BAR + 1
    assert width == 350
    assert width == _WIDTH
    assert height == 16
    assert min(ink) <= 1
    assert max(ink) >= _WIDTH - 2
    # Windows loads a 256-color picture. One frame for every pixel stays blank.
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
    assert frames == _SHOWN
    assert frames < _FRAMES


def test_the_bar_moves_forward_one_pixel_at_a_time():
    """A jump of several pixels is the hitch. The knob never steps backward."""
    timed = _timed(0, 180_000, tail=_BAR_TAIL_MS)
    assert timed is not None
    steps, delays = timed
    assert len(steps) == _SHOWN
    assert len(steps) < _FRAMES
    assert max(b - a for a, b in zip(steps, steps[1:])) <= 2
    assert all(delay >= 2 for delay in delays)
    edge = -1
    for step in steps:
        frame = _paint(step)
        filled = [index % _WIDTH for index, pixel in enumerate(frame) if pixel >= 2]
        assert filled
        lead = max(filled)
        assert lead >= edge
        edge = lead


async def test_the_play_bar_follows_the_song_and_stays_on_a_color_change(monkeypatch):
    guild, player, _track = await _playing()
    clock = {"now": 1_000.0}
    monkeypatch.setattr("music.player.playbar.time.monotonic", lambda: clock["now"])
    try:
        view = MusicPlayer(guild)
        opening = view.take_bar()
        assert opening is not None
        assert opening.filename == "bar-1.gif"
        assert opening.fp.read() == bar_still(0, 148_000)
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
        assert moved_on.fp.read() == bar_still(3_000, 148_000)
        assert gallery().items[0].media.url == "attachment://bar-2.gif"
        player._position = 0
        assert view.paint() is True
        restarted = view.take_bar()
        assert restarted is not None
        assert restarted.fp.read() == bar_still(0, 148_000)
        assert gallery().items[0].media.url == "attachment://bar-3.gif"

        clock["now"] += 6
        player._position = 6_000
        assert view.paint() is True
        later = view.take_bar()
        assert later is not None
        assert later.fp.read() == bar_still(6_000, 148_000)
        assert gallery().items[0].media.url == "attachment://bar-4.gif"

        press = _press(player)
        await view.shuffle.callback(press)
        assert "attachments" not in press.response.edit_message.await_args.kwargs
        assert gallery().items[0].media.url == "attachment://bar-4.gif"

        player._position = 90_000
        assert view.paint() is True
        moved = view.take_bar()
        assert moved is not None
        assert moved.fp.read() == bar_still(90_000, 148_000)
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
        assert reopened.fp.read() == bar_still(40_000, 148_000)
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_a_late_reading_does_not_pull_the_bar_backward(monkeypatch):
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
        assert caught.fp.read() == bar_still(4_000, 148_000)
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_shuffle_and_repeat_keep_the_paused_place():
    """A paused mark can fall back to the start. The song stays where it was paused."""
    guild, player, _track = await _playing()
    try:
        player._position = 90_000
        player._last_position = 0
        player._paused_mark = True
        view = MusicPlayer(guild)

        async def flip(value):
            player._paused = value

        player.pause.side_effect = flip
        press = _press(player)
        await view.pause.callback(press)
        assert now_playing(guild).paused is True
        assert now_playing(guild).position == 90_000
        assert _stored(guild).spot == 90_000

        # The next update from the player is the start of the song.
        player._last_position = 0
        player._position = 0
        assert now_playing(guild).position == 90_000
        player.seek.reset_mock()

        mode = _press(player)
        await view.shuffle.callback(mode)
        assert "attachments" not in mode.response.edit_message.await_args.kwargs
        assert now_playing(guild).paused is True
        assert now_playing(guild).shuffling is True
        # The song itself is put back, and it stays paused.
        player.seek.assert_awaited_once_with(90_000)
        assert player.paused is True
        assert player.position == 90_000

        player.seek.reset_mock()
        again = _press(player)
        await view.repeat.callback(again)
        assert "attachments" not in again.response.edit_message.await_args.kwargs
        assert now_playing(guild).repeating is True
        assert now_playing(guild).position == 90_000
        assert player.position == 90_000
        player.seek.assert_not_awaited()

        resume = _press(player)
        await view.pause.callback(resume)
        player.seek.assert_not_awaited()
        assert now_playing(guild).paused is False
        assert now_playing(guild).position == 90_000
        assert player.position == 90_000
        assert "attachments" not in resume.response.edit_message.await_args.kwargs
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_a_radio_station_has_no_time_bar_and_can_be_skipped():
    guild, player, track = await _playing()
    try:
        track.is_stream = True
        track.length = 0
        player._position = 20_000
        view = MusicPlayer(guild)
        text = _card_text(view)
        assert "Volume" not in text
        assert RADIO_MARK in text
        assert "━" not in text
        assert not any(isinstance(item, discord.ui.MediaGallery) for item in view.walk_children())
        assert view.repeat.disabled is True
        assert view.shuffle.disabled is False
        assert view.skip.disabled is False
        player.seek.reset_mock()
        assert await previous_track(guild) == "Nothing is before this."
        player.seek.assert_not_awaited()

        later = _named("After")
        later.length = 90_000
        later.is_stream = False
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [later]) == "Added After."
        line = LiveLine(guild, MagicMock())
        text = _card_text(line)
        assert "### Now Playing" in text
        assert "I Ain't Worried" in text
        assert "After" in text
        assert text.count(RADIO_MARK) == 1

        assert await next_track(guild) is None
        assert now_playing(guild).title == "After"
        assert now_playing(guild).stream is False
        song = MusicPlayer(guild)
        assert RADIO_MARK not in _card_text(song)
        assert song.skip.disabled is False
        later.length = 0
        missing = MusicPlayer(guild)
        assert missing.skip.disabled is False
        later.length = 90_000

        track.is_stream = False
        track.length = 8 * 60 * 60 * 1000
        _stored(guild).current.videos = ("https://www.youtube.com/watch?v=long",)
        held = MusicPlayer(guild)
        held_text = _card_text(held)
        assert RADIO_MARK in held_text
        assert "━" not in held_text
        assert any(isinstance(item, discord.ui.MediaGallery) for item in held.walk_children())
        assert held.skip.disabled is False
        held_line = LiveLine(guild, MagicMock())
        assert RADIO_MARK in _card_text(held_line)
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_repeat_can_turn_off_while_a_stream_is_on():
    """A livestream cannot start repeat. One already on has to be able to stop."""
    guild, player, track = await _playing()
    try:
        view = MusicPlayer(guild)
        press = _press(player)
        press.response.defer = AsyncMock()
        await view.repeat.callback(press)
        assert _stored(guild).repeat is True

        track.is_stream = True
        track.length = 0
        assert view.paint() is True
        assert view.repeat.disabled is False
        assert view.repeat.style == discord.ButtonStyle.primary

        await view.repeat.callback(press)
        assert _stored(guild).repeat is False
        assert view.repeat.style == discord.ButtonStyle.secondary
        assert view.repeat.disabled is True
        press.send.assert_not_called()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_the_power_button_leaves_the_voice_channel():
    guild, player, _track = await _playing()
    try:
        view = MusicPlayer(guild)
        outsider = MagicMock()
        outsider.send = AsyncMock()
        outsider.user.voice = None
        await view.leave.callback(outsider)
        player.disconnect.assert_not_awaited()
        assert "Join the voice channel first" in outsider.send.await_args.args[0]
        assert now_playing(guild).has_track is True

        press = _press(player)
        press.response.defer = AsyncMock()
        press.delete_original_response = AsyncMock()
        await view.leave.callback(press)
        player.stop.assert_awaited()
        player.disconnect.assert_awaited()
        press.delete_original_response.assert_awaited()
        assert view._closed is True
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_the_controls_start_before_discord_answers():
    guild, player, track = await _playing()
    try:
        later = MagicMock()
        later.title = "Midnight City"
        later.author = "M83"
        later.length = 240_000
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [later]) == "Added Midnight City."
        view = MusicPlayer(guild)
        started = asyncio.Event()
        order = []

        async def play(song):
            order.append("next")
            player._track = song
            started.set()

        async def acknowledge():
            await asyncio.wait_for(started.wait(), timeout=1)
            order.append("ack")
            skip.response.is_done.return_value = True

        player.play.side_effect = play
        skip = _press(player)
        skip.response.defer.side_effect = acknowledge
        await asyncio.wait_for(view.skip.callback(skip), timeout=1)
        assert order == ["next", "ack"]
        assert now_playing(guild).title == "Midnight City"

        started.clear()
        order.clear()

        async def seek(position):
            order.append("previous")
            player._position = position
            started.set()

        async def acknowledge_back():
            await asyncio.wait_for(started.wait(), timeout=1)
            order.append("ack")
            back.response.is_done.return_value = True

        player._position = 20_000
        player.seek.side_effect = seek
        back = _press(player)
        back.response.defer.side_effect = acknowledge_back
        await asyncio.wait_for(view.previous.callback(back), timeout=1)
        assert order == ["previous", "ack"]
        player.seek.assert_awaited_with(0)

        async def edited(**kwargs):
            await asyncio.sleep(0)
            assert _stored(guild).repeat is True
            order.append("edit")

        order.clear()
        again = _press(player)
        again.response.edit_message.side_effect = edited
        await view.repeat.callback(again)
        assert order == ["edit"]
        assert view.repeat.style == discord.ButtonStyle.primary

        started.clear()
        order.clear()

        async def disconnect(**kwargs):
            order.append("leave")
            started.set()

        async def acknowledge_leave():
            await asyncio.wait_for(started.wait(), timeout=1)
            order.append("ack")
            power.response.is_done.return_value = True

        player.disconnect.side_effect = disconnect
        power = _press(player)
        power.response.defer.side_effect = acknowledge_leave
        power.delete_original_response = AsyncMock()
        await asyncio.wait_for(view.leave.callback(power), timeout=1)
        assert order == ["leave", "ack"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)
    guild, player, _track = await _playing()
    try:
        view = MusicPlayer(guild)
        rows = [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]
        assert rows[1].children == [view.save, view.info, view.radio, view.volume, view.close]
        assert view.save.label == _fitted(MARK_SAVE)

        outsider = MagicMock()
        outsider.send = AsyncMock()
        outsider.user.voice = None
        outsider.response.send_modal = AsyncMock()
        await view.volume.callback(outsider)
        outsider.response.send_modal.assert_not_awaited()
        assert "Join the voice channel first" in outsider.send.await_args.args[0]

        press = _press(player)
        press.response.send_modal = AsyncMock()
        await view.volume.callback(press)
        modal = press.response.send_modal.await_args.args[0]
        assert modal.title == "Tune"
        assert [item.text for item in modal.children] == ["Volume", "Duration", "Platform"]
        assert modal.level.component.default == "100"
        assert modal.level.component.placeholder == "From 0 to 100"
        assert modal.level.description is None
        assert not modal.place.component.default
        assert modal.place.component.placeholder == "Up to 2:28"
        assert modal.place.text == "Duration"
        assert modal.place.description is None
        assert [option.value for option in modal.sites.options if option.default] == ["youtube"]

        bad = _press(player)
        bad.send = AsyncMock()
        modal.level.component._value = "loud"
        await modal.on_submit(bad)
        assert "0 to 100" in bad.send.await_args.args[0]
        player.set_volume.assert_not_awaited()

        mistimed = _press(player)
        mistimed.send = AsyncMock()
        modal.level.component._value = "40"
        modal.place.component._value = "noon"
        await modal.on_submit(mistimed)
        assert "1:30" in mistimed.send.await_args.args[0]
        player.set_volume.assert_not_awaited()
        player.seek.assert_not_awaited()

        submit = _press(player)
        submit.response.defer = AsyncMock()
        modal.level.component._value = "40"
        modal.place.component._value = ""
        await modal.on_submit(submit)
        player.set_volume.assert_awaited_with(40)
        player.seek.assert_not_awaited()
        assert now_playing(guild).volume == 40
        assert "Volume" not in _card_text(view)
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_the_volume_panel_switches_site_and_moves(monkeypatch):
    guild, player, _track = await _playing()
    try:
        from music.lavalink import Found

        other = _named("I Ain't Worried", "OneRepublic")
        other.length = 180_000
        other.uri = "https://example.com/spotify"

        async def search(_guild, query, platform="youtube", mode=None):
            assert "I Ain't Worried" in query
            assert platform == "spotify"
            return Found(query, platform, [other], False)

        monkeypatch.setattr("music.playback.find_songs", search)
        view = MusicPlayer(guild)
        press = _press(player)
        press.response.send_modal = AsyncMock()
        await view.volume.callback(press)
        modal = press.response.send_modal.await_args.args[0]
        modal.sites._values = ["spotify"]
        modal.place.component._value = "1:05"
        submit = _press(player)
        submit.response.defer = AsyncMock()
        await modal.on_submit(submit)
        player.seek.assert_awaited_with(65_000)
        assert now_playing(guild).position == 65_000
        assert _stored(guild).current.platform == "spotify"
        assert player.play.await_args.args[0] is other
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_radio_without_a_key_opens_the_key_box(monkeypatch):
    guild, player, _track = await _playing()
    try:
        from config import settings

        view = MusicPlayer(guild)
        outsider = MagicMock()
        outsider.send = AsyncMock()
        outsider.user.voice = None
        outsider.response.send_modal = AsyncMock()
        await view.radio.callback(outsider)
        outsider.response.send_modal.assert_not_awaited()
        assert "Join the voice channel first" in outsider.send.await_args.args[0]

        press = _press(player)
        press.response.send_modal = AsyncMock()
        await view.radio.callback(press)
        modal = press.response.send_modal.await_args.args[0]
        assert modal.title == "Last.fm"

        empty = _press(player)
        empty.send = AsyncMock()
        modal.key.component._value = "short"
        await modal.on_submit(empty)
        assert "Paste a Last.fm API key" in empty.send.await_args.args[0]
        assert settings.lastfm_api_key is None

        written = {}
        monkeypatch.setattr(
            "music.player.radio.upsert_config_values",
            lambda _path, values: written.update(values),
        )
        monkeypatch.setattr("music.playback.control.similar_songs", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[]))
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        submit = _press(player)
        modal.key.component._value = "abc123abc123abc1"
        await modal.on_submit(submit)
        assert written == {"LASTFM_API_KEY": "abc123abc123abc1"}
        assert settings.lastfm_api_key == "abc123abc123abc1"
        assert _stored(guild).radio is True
        assert view.radio.style == discord.ButtonStyle.primary
        assert "On Radio" not in view.meta.content
        assert now_playing(guild).title == "I Ain't Worried"
        assert view.meta.content == "-# On Queue\n-# Up next: After"
        assert view.shuffle.disabled is True
        assert view.repeat.disabled is False
        assert [item.track.title for item in _stored(guild).later] == ["After"]

        await view.repeat.callback(submit)
        assert _stored(guild).repeat is True
        assert _stored(guild).radio is True
        assert view.repeat.style == discord.ButtonStyle.primary
        assert view.meta.content == "-# On Repeat\n-# Up next: After"

        await view.shuffle.callback(submit)
        assert _stored(guild).shuffle is False

        again = _press(player)
        again.response.send_modal = AsyncMock()
        await view.radio.callback(again)
        again.response.send_modal.assert_not_awaited()
        assert _stored(guild).radio is False
        assert _stored(guild).repeat is True
        assert view.radio.style == discord.ButtonStyle.secondary
        assert view.shuffle.disabled is False
        assert view.meta.content == "-# On Repeat\n-# Up next: After"
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_the_remote_moves_the_line(monkeypatch):
    guild, player, track = await _playing()
    try:
        outsider = MagicMock()
        outsider.send = AsyncMock()
        outsider.user.voice = None
        view = MusicPlayer(guild)
        await view.pause.callback(outsider)
        player.pause.assert_not_awaited()
        assert "Join the voice channel first" in outsider.send.await_args.args[0]

        later = MagicMock()
        later.title = "Midnight City"
        later.author = "M83"
        later.length = 240_000
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [later], "spotify") == "Added Midnight City."
        player.play.assert_awaited_once()
        assert now_playing(guild).upcoming == "Midnight City - M83"
        view.paint()
        assert "Volume" not in _card_text(view)
        song = view.children[0].children[2]
        assert isinstance(song, discord.ui.Section)
        assert song.children[-1].content == (
            "### [I Ain't Worried](https://example.com/song)\nOneRepublic\n"
            "-# On Queue\n-# Up next: Midnight City - M83"
        )
        assert [row.title for row in queued(guild) if not row.playing] == ["Midnight City"]

        player._position = 20_000
        assert await previous_track(guild) is None
        player.seek.assert_awaited_once_with(0)
        assert now_playing(guild).position == 0

        player._position = 1_000
        await next_track(guild)
        assert now_playing(guild).title == "Midnight City"
        assert now_playing(guild).requester_id == 18
        player._position = 1_000
        await previous_track(guild)
        assert now_playing(guild).title == "I Ain't Worried"
        assert now_playing(guild).upcoming == "Midnight City - M83"

        assert await change_volume(guild, 10) is None
        assert now_playing(guild).volume == 100
        assert await change_volume(guild, -10) is None
        player.set_volume.assert_awaited_with(90)
        assert now_playing(guild).volume == 90
        view.paint()
        assert "Volume" not in _card_text(view)
        await change_volume(guild, -100)
        assert now_playing(guild).volume == 0

        other = MagicMock()
        other.title = "Counting Stars"
        other.author = "OneRepublic"
        other.identifier = "other"
        asker = MagicMock()
        asker.id = 21
        asker.voice.channel = player.channel
        assert await queue_tracks(asker, guild, [other]) == "Added Counting Stars."
        assert now_playing(guild).upcoming == "Midnight City - M83"
        assert _stored(guild).later[-1].track.title == "Counting Stars"

        player._guild = guild
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "Midnight City"
        await advance_from_end(type("End", (), {"reason": "replaced", "player": player})())
        assert now_playing(guild).title == "Midnight City"

        await next_track(guild)
        assert now_playing(guild).title == "Counting Stars"
        await next_track(guild)
        player.disconnect.assert_not_awaited()
        assert now_playing(guild) is not None
        assert now_playing(guild).has_track is False
        assert now_playing(guild).upcoming is None
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_the_star_saves_the_song_that_is_playing(tmp_path):
    from guild import GuildConfig
    from music.archive import create_playlist, find_playlist

    guild, player, track = await _playing()
    store = GuildConfig(tmp_path / "player.json")
    try:
        view = MusicPlayer(guild, store=store)
        assert view.save.label == _fitted(MARK_SAVE)
        outsider = MagicMock()
        outsider.send = AsyncMock()
        outsider.user.voice = None
        outsider.response.send_modal = AsyncMock()
        await view.save.callback(outsider)
        outsider.response.send_modal.assert_not_awaited()
        assert "Join the voice channel first" in outsider.send.await_args.args[0]

        press = _press(player)
        press.user.id = 17
        press.send = AsyncMock()
        await view.save.callback(press)
        assert "There is no playlist yet" in press.send.await_args.args[0]

        create_playlist(store, guild.id, "Evening", 17)
        press = _press(player)
        press.user.id = 17
        press.response.send_modal = AsyncMock()
        await view.save.callback(press)
        modal = press.response.send_modal.await_args.args[0]
        assert modal.title == "Save"
        modal.menu._values = ["Evening"]
        submit = _press(player)
        submit.user.id = 17
        submit.send = AsyncMock()
        await modal.on_submit(submit)
        assert "Saved to Evening" in submit.send.await_args.args[0]
        saved = find_playlist(store, guild.id, "Evening")
        assert [(song.title, song.url) for song in saved.songs] == [(track.title, track.uri)]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


def _album_rows(view):
    return [
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.Section) and getattr(item.accessory, "label", None) == MARK_ADD
    ]


def _page_text(view) -> str:
    for item in view.walk_children():
        if isinstance(item, discord.ui.TextDisplay) and item.content.startswith("Page "):
            return item.content
    return ""


async def test_a_finished_song_removes_the_player():
    guild, player, _track = await _playing()
    try:
        view = MusicPlayer(guild)
        message = MagicMock()
        message.delete = AsyncMock()
        message.edit = AsyncMock()
        view.message = message
        player._track = None
        await view.redraw()
        assert view._closed is True
        message.delete.assert_awaited_once()
        message.edit.assert_not_awaited()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


def _player_message():
    message = MagicMock()
    message.delete = AsyncMock()
    message.edit = AsyncMock()
    sent = MagicMock()
    sent.edit = AsyncMock()
    message.channel.send = AsyncMock(return_value=sent)
    return message, sent


async def test_repeat_keeps_the_card_when_the_song_starts_again():
    guild, player, track = await _playing()
    try:
        view = MusicPlayer(guild)
        message, sent = _player_message()
        view.message = message
        _stored(guild).repeat = True

        player._track = None
        await view.redraw()
        assert view._closed is False
        message.delete.assert_not_awaited()
        message.channel.send.assert_not_awaited()

        player._track = track
        player._position = 0
        await view.redraw()
        message.channel.send.assert_not_awaited()
        message.delete.assert_not_awaited()
        assert view.message is message
        assert view._closed is False
        assert "I Ain't Worried" in view.song.content

        other = _named("After", "OneRepublic")
        other.uri = "https://example.com/after"
        other.length = 120_000
        player._track = other
        await view.redraw()
        message.channel.send.assert_not_awaited()
        message.delete.assert_not_awaited()
        assert view._closed is False
        assert "After" in view.song.content
    finally:
        view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_a_different_song_posts_a_new_player_message():
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        view = MusicPlayer(guild)
        message, sent = _player_message()
        view.message = message
        player._guild = guild
        player._last_position = 148_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        await view.redraw()
        message.channel.send.assert_awaited()
        assert message.channel.send.await_args.kwargs["view"] is view
        message.delete.assert_awaited_once()
        assert view.message is sent
        assert view._closed is False
        assert "After" in view.song.content
        message.edit.assert_not_awaited()
    finally:
        view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_the_next_song_posts_a_picture_the_other_client_can_load():
    guild, player, _track = await _playing()
    try:
        view = MusicPlayer(guild)
        assert view.take_bar() is not None
        message, sent = _player_message()
        view.message = message
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        nxt = _named("After", "Someone")
        nxt.uri = "https://example.com/after"
        nxt.artwork = "https://example.com/after.jpg"
        nxt.length = 180_000
        assert await queue_tracks(member, guild, [nxt]) == "Added After."

        async def send(**kwargs):
            upload = kwargs["file"]
            item = MagicMock()
            item.filename = upload.filename
            item.url = f"https://cdn.discordapp.com/attachments/19/8/{upload.filename}"
            sent.attachments = [item]
            return sent

        message.channel.send = send
        press = _press(player)
        await view.skip.callback(press)
        press.response.edit_message.assert_not_awaited()
        sent.edit.assert_awaited()
        assert "attachments" not in sent.edit.await_args.kwargs
        assert "After" in view.song.content
        gallery = next(item for item in view.walk_children() if isinstance(item, discord.ui.MediaGallery))
        assert gallery.items[0].media.url == "https://cdn.discordapp.com/attachments/19/8/bar-2.gif"
        message.delete.assert_awaited()
    finally:
        view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_an_album_add_stays_quiet_while_a_song_is_playing(monkeypatch):
    from types import SimpleNamespace

    notes = []

    async def show(interaction, line, quote=False):
        notes.append(line)

    monkeypatch.setattr("music.player.album.show_note", show)
    monkeypatch.setattr("music.player.album.now_playing", lambda guild: SimpleNamespace(has_track=True))
    monkeypatch.setattr("music.player.album.queue_tracks", AsyncMock(return_value="Added After."))
    card = _AlbumCard("Dreaming Out Loud", None, "OneRepublic", None, [_named("After")], None, "spotify")
    press = _ask()
    await card.on_add(press, 0)
    assert notes == []
    press.response.defer.assert_awaited()
    press.send.assert_not_awaited()

    notes.clear()
    monkeypatch.setattr("music.player.album.now_playing", lambda guild: None)
    monkeypatch.setattr("music.player.album.queue_tracks", AsyncMock(return_value="Playing After."))
    press = _ask()
    await card.on_add(press, 0)
    assert notes == ["Starting After"]


class _AlbumClick(discord.Interaction):
    """A real component interaction. A plain mock never takes the delete path."""

    def __init__(self) -> None:
        response = MagicMock()
        response.is_done.return_value = False

        async def defer(*args, **kwargs):
            response.is_done.return_value = True

        response.defer = AsyncMock(side_effect=defer)
        self._state = None
        self._cs_response = response
        followup = MagicMock()
        followup.send = AsyncMock()
        self._cs_followup = followup
        self.delete_original_response = AsyncMock()
        self.edit_original_response = AsyncMock()
        self.send = AsyncMock()
        guild = MagicMock()
        guild.voice_client = MagicMock()
        self.user = MagicMock()
        self.user.guild = guild


async def test_adding_while_a_song_is_on_leaves_the_album_up(monkeypatch):
    """A button ack's original message is the album. Deleting it closes the card."""
    from types import SimpleNamespace

    monkeypatch.setattr("music.player.album.now_playing", lambda guild: SimpleNamespace(has_track=True))
    monkeypatch.setattr("music.player.album.queue_tracks", AsyncMock(return_value="Added After."))
    card = _AlbumCard("Dreaming Out Loud", None, "OneRepublic", None, [_named("After")], None, "spotify")
    press = _AlbumClick()
    await card.on_add(press, 0)
    press.delete_original_response.assert_not_awaited()
    press.edit_original_response.assert_not_awaited()
    press.followup.send.assert_not_awaited()
    assert card._closed is False

    monkeypatch.setattr("music.player.album.queue_tracks", AsyncMock(return_value="The queue is full."))
    full = _AlbumClick()
    await card.on_add(full, 0)
    full.delete_original_response.assert_not_awaited()
    full.edit_original_response.assert_not_awaited()
    full.followup.send.assert_awaited()
    sent = full.followup.send.await_args.kwargs
    assert sent["ephemeral"] is True
    assert "queue is full" in sent["content"]


async def test_an_idle_album_deletes_itself():
    card = _AlbumCard("Dreaming Out Loud", None, "OneRepublic", None, [], None, "spotify")
    assert card.timeout == ALBUM_SECONDS == 45
    message = MagicMock()
    message.delete = AsyncMock()
    card.message = message
    released = []
    card._release = released.append
    await card.on_timeout()
    message.delete.assert_awaited_once()
    assert released == [card]
    assert card._closed is True


async def test_the_album_button_stays_on_the_song_that_is_playing():
    """A mirror can lose its album. The previous song's album is not a substitute."""
    from types import SimpleNamespace

    guild, player, track = await _playing()
    try:
        view = MusicPlayer(guild)
        track.album = SimpleNamespace(name="Dreaming Out Loud", url=None)
        mirror = MagicMock()
        mirror.title = track.title
        mirror.author = track.author
        mirror.uri = "https://www.youtube.com/watch?v=mirror"
        mirror.album = None
        mirror.raw_data = {}
        mirror.encoded = ""
        player._track = mirror
        press = _press(player)
        press.response.send_message = AsyncMock()
        await view.info.callback(press)
        card = press.response.send_message.await_args.kwargs["view"]
        assert "Dreaming Out Loud" in _card_text(card)

        other = MagicMock()
        other.title = "Stop and Stare"
        other.author = "OneRepublic"
        other.uri = "https://example.com/other"
        other.album = None
        other.raw_data = {}
        other.encoded = ""
        player._track = other
        missed = _press(player)
        missed.send = AsyncMock()
        await view.info.callback(missed)
        assert "no album" in missed.send.await_args.args[0]
        missed.response.send_message.assert_not_called()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_the_info_button_shows_the_album(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from music.lavalink import Found

    guild, player, track = await _playing()
    try:
        view = MusicPlayer(guild)
        assert view.info.label == _fitted(INFO_MARK)
        outsider = MagicMock()
        outsider.send = AsyncMock()
        outsider.user.voice = None
        await view.info.callback(outsider)
        assert "Join the voice channel first" in outsider.send.await_args.args[0]

        missing = _press(player)
        missing.send = AsyncMock()
        await view.info.callback(missing)
        assert "no album" in missing.send.await_args.args[0]

        track.album = SimpleNamespace(name="Dreaming Out Loud", url=None)
        named = _press(player)
        named.response.send_message = AsyncMock()
        await view.info.callback(named)
        card = named.response.send_message.await_args.kwargs["view"]
        assert named.response.send_message.await_args.kwargs["ephemeral"] is True
        text = _card_text(card)
        assert ALBUM_TITLE in text
        assert "### Dreaming Out Loud" in text
        assert "By OneRepublic" in text
        assert "I Ain't Worried" in text
        assert "Playing" in text
        assert "OneRepublic" in text
        assert _album_rows(card)[0].accessory.label == MARK_ADD

        other = MagicMock()
        other.title = "Stop and Stare"
        other.author = "OneRepublic"
        other.length = 201_000
        other.uri = "https://example.com/other"
        other.artwork = None
        track.album = SimpleNamespace(
            name="Dreaming Out Loud",
            url="https://open.spotify.com/album/abc",
        )

        async def search(guild_id, song, platform="youtube"):
            assert song == "https://open.spotify.com/album/abc"
            return Found(song, "spotify", [track, other], True, "Dreaming Out Loud")

        monkeypatch.setattr("music.player.album.find_songs", search)
        from guild import GuildConfig
        from music.archive import create_playlist, find_playlist

        store = GuildConfig(tmp_path / "album.json")
        create_playlist(store, guild.id, "Evening", 17)
        view._lists = store
        press = _press(player)
        press.followup.send = AsyncMock()
        await view.info.callback(press)
        press.response.defer.assert_awaited()
        opened = press.followup.send.await_args.kwargs
        assert opened["ephemeral"] is True
        shown = _card_text(opened["view"])
        assert ALBUM_TITLE in shown
        assert "### [Dreaming Out Loud](https://open.spotify.com/album/abc)" in shown
        assert "By OneRepublic" in shown
        assert "Stop and Stare" in shown
        assert shown.count("Playing") == 1
        rows = _album_rows(opened["view"])
        assert [row.accessory.label for row in rows] == [MARK_ADD, MARK_ADD]
        assert _page_text(opened["view"]).startswith(page_line(1, 1, 2, "Track"))
        footer = [
            item for item in opened["view"].walk_children() if isinstance(item, discord.ui.ActionRow)
        ][-1]
        assert LABEL_ADD_ALL.split("Add All") == LABEL_PLAYER.split("Player")
        assert LABEL_LIKED.split("Liked") == LABEL_QUEUE.split("Queue")
        assert [button.label for button in footer.children] == [
            MARK_BACK, MARK_NEXT, LABEL_ADD_ALL, LABEL_LIKED, MARK_CLOSE,
        ]
        assert len(ALL_HINT) <= 100
        album = opened["view"]
        ask = _press(player)
        ask.guild = guild
        ask.response.send_modal = AsyncMock()
        await album._all.callback(ask)
        modal = ask.response.send_modal.await_args.args[0]
        assert modal.title == "Add All"
        assert modal.query.text == "This adds every song on the album."
        assert modal.query.description == ALL_HINT
        assert 'Type "yes" to confirm' in modal.query.description
        assert modal.query.component.placeholder == "yes"
        modal.query.component._value = "no"
        declined = _press(player)
        declined.guild = guild
        declined.send = AsyncMock()
        await modal.on_submit(declined)
        declined.send.assert_not_awaited()
        assert [row.title for row in queued(guild) if not row.playing] == []
        modal.query.component._value = "Yes"
        accepted = _press(player)
        accepted.guild = guild
        accepted.send = AsyncMock()
        await modal.on_submit(accepted)
        assert [row.title for row in queued(guild) if not row.playing] == [
            "I Ain't Worried",
            "Stop and Stare",
        ]
        thumbs = [
            item for item in opened["view"].walk_children() if isinstance(item, discord.ui.Thumbnail)
        ]
        assert [thumb.media.url for thumb in thumbs] == ["https://example.com/art.jpg"]
        heading = opened["view"].children[0].children[0]
        assert isinstance(heading, discord.ui.Section)
        assert heading.accessory is thumbs[0]
        album = opened["view"]
        assert album._queue.label == LABEL_LIKED
        assert album._queue.style == discord.ButtonStyle.secondary
        switch = _press(player)
        await album._queue.callback(switch)
        assert album._queue.label == LABEL_LIKED
        assert album._queue.style == discord.ButtonStyle.primary
        assert [album._adds[0].label, album._adds[1].label] == [MARK_ADD, MARK_ADD]
        assert album._adds[0].method == "on_keep"
        keep = _press(player)
        keep.user.id = 17
        keep.response.send_modal = AsyncMock()
        await album._adds[0].callback(keep)
        modal = keep.response.send_modal.await_args.args[0]
        assert modal.title == "Save"
        modal.menu._values = ["Evening"]
        submit = _press(player)
        submit.user.id = 17
        submit.send = AsyncMock()
        await modal.on_submit(submit)
        assert "Saved to Evening" in submit.send.await_args.args[0]
        saved = find_playlist(store, guild.id, "Evening")
        assert saved.songs[0].title == track.title
        back = _press(player)
        await album._queue.callback(back)
        assert album._queue.label == LABEL_LIKED
        assert album._queue.style == discord.ButtonStyle.secondary
        assert [album._adds[0].label, album._adds[1].label] == [MARK_ADD, MARK_ADD]
        assert album._adds[0].method == "on_add"

        long = []
        for number in range(200):
            row = MagicMock()
            row.title = f"Song {number}"
            row.author = "OneRepublic"
            row.length = 180_000
            row.uri = f"https://example.com/{number}"
            long.append(row)

        async def many(guild_id, song, platform="youtube"):
            return Found(song, "spotify", long, True, "Dreaming Out Loud")

        monkeypatch.setattr("music.player.album.find_songs", many)
        again = _press(player)
        again.followup.send = AsyncMock()
        await view.info.callback(again)
        packed_view = again.followup.send.await_args.kwargs["view"]
        packed = _card_text(packed_view)
        assert "Song 0" in packed
        assert "Song 4" in packed
        assert "Song 5" not in packed
        assert "Song 199" not in packed
        assert _page_text(packed_view).startswith(page_line(1, 40, 200, "Track"))
        turn = _press(player)
        await packed_view._next.callback(turn)
        turned = _card_text(packed_view)
        assert "Song 5" in turned
        assert "Song 0" not in turned

        track.album = SimpleNamespace(name=None, url=None)
        track.raw_data = {}
        track.uri = "https://music.apple.com/us/album/chet-baker-sings/1440812340?i=1440811111"

        async def apple(guild_id, song, platform="youtube"):
            assert song == "https://music.apple.com/us/album/chet-baker-sings/1440812340"
            return Found(song, "apple", [track], True, "Chet Baker Sings")

        monkeypatch.setattr("music.player.album.find_songs", apple)
        apple_press = _press(player)
        apple_press.followup.send = AsyncMock()
        await view.info.callback(apple_press)
        apple_card = _card_text(apple_press.followup.send.await_args.kwargs["view"])
        assert "Chet Baker Sings" in apple_card

        first = AsyncMock()
        first.delete = AsyncMock()
        one = _press(player)
        one.user.id = 17
        one.followup.send = AsyncMock(return_value=first)
        await view.info.callback(one)
        first.delete.assert_not_awaited()

        second = AsyncMock()
        second.delete = AsyncMock()
        two = _press(player)
        two.user.id = 17
        two.followup.send = AsyncMock(return_value=second)
        await view.info.callback(two)
        first.delete.assert_awaited_once()
        assert one.followup.send.await_args.kwargs["view"].is_finished()
        second.delete.assert_not_awaited()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


def test_a_spotify_album_id_stops_at_twenty_two_characters():
    import base64

    from music.player.album import _encoded_album

    album_id = "37PoN4qrak5WenNfs1NcMr"
    page = f"https://open.spotify.com/album/{album_id}"
    track = MagicMock()
    track.encoded = base64.b64encode(page.encode() + b"extraALNUM99").decode()
    assert _encoded_album(track) == page


async def test_spotify_album_art_is_not_the_album_page(monkeypatch):
    import base64
    from types import SimpleNamespace

    from music.lavalink import Found

    guild, player, track = await _playing()
    try:
        view = MusicPlayer(guild)
        album_id = "37PoN4qrak5WenNfs1NcMr"
        page = f"https://open.spotify.com/album/{album_id}"
        track.album = SimpleNamespace(name=None, url="https://i.scdn.co/image/ab67616d")
        track.raw_data = {
            "pluginInfo": {
                "albumName": "Dreaming Out Loud",
                "albumArtUrl": "https://i.scdn.co/image/ab67616d",
                "save_uri": "https://open.spotify.com/track/1OQSLNZtIHsIseGlmwJBxG",
            },
        }
        track.uri = "https://open.spotify.com/track/1OQSLNZtIHsIseGlmwJBxG"
        # Lavalink stores the album address in the track bytes, not in pluginInfo.
        raw = b"\x00" + bytes([len(page)]) + page.encode()
        track.encoded = base64.b64encode(raw).decode()
        other = MagicMock()
        other.title = "Stop and Stare"
        other.author = "OneRepublic"
        other.length = 201_000
        other.uri = "https://open.spotify.com/track/other"
        other.artwork = None

        async def search(guild_id, song, platform="youtube", mode=None):
            assert song == page
            return Found(song, "spotify", [track, other], True, "Dreaming Out Loud")

        monkeypatch.setattr("music.player.album.find_songs", search)
        press = _press(player)
        press.followup.send = AsyncMock()
        await view.info.callback(press)
        text = _card_text(press.followup.send.await_args.kwargs["view"])
        assert "Dreaming Out Loud" in text
        assert "Stop and Stare" in text
        assert page in text
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_a_spotify_song_is_loaded_again_to_open_its_album(monkeypatch):
    from types import SimpleNamespace

    from music.lavalink import Found

    guild, player, track = await _playing()
    try:
        view = MusicPlayer(guild)
        page = "https://open.spotify.com/album/0ETFjACtuP2ADo6LFhL6HN"
        track.album = SimpleNamespace(name="Abbey Road", url=None)
        track.raw_data = {"pluginInfo": {"save_uri": "https://open.spotify.com/track/2EqlS6tkEnglzr7tkKAAYD"}}
        track.uri = "https://open.spotify.com/track/2EqlS6tkEnglzr7tkKAAYD"
        track.encoded = ""
        fresh = MagicMock()
        fresh.album = SimpleNamespace(name="Abbey Road", url=page)
        fresh.uri = track.uri
        fresh.author = "The Beatles"
        fresh.title = track.title
        fresh.artwork = None
        fresh.encoded = ""
        fresh.raw_data = {}
        fresh.length = track.length
        other = MagicMock()
        other.title = "Something"
        other.author = "The Beatles"
        other.length = 182_000
        other.uri = "https://open.spotify.com/track/other"
        other.artwork = None
        calls = []

        async def search(guild_id, song, platform="youtube", mode=None):
            calls.append((song, mode))
            if "/track/" in song:
                return Found(song, "spotify", [fresh], True, None)
            return Found(song, "spotify", [fresh, other], True, "Abbey Road")

        monkeypatch.setattr("music.player.album.find_songs", search)
        press = _press(player)

        async def defer(*args, **kwargs):
            press.response.is_done.return_value = True

        press.response.defer.side_effect = defer
        press.followup.send = AsyncMock()
        await view.info.callback(press)
        assert calls[0] == ("https://open.spotify.com/track/2EqlS6tkEnglzr7tkKAAYD", "track")
        assert calls[1] == (page, None)
        text = _card_text(press.followup.send.await_args.kwargs["view"])
        assert "Something" in text
        assert page in text
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)
