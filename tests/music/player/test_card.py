"""The player card. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord

from announce import ACCENT_COLOR
from cogs.osa_musify import OsaMusify
from music.panel import (
    MARK_BACK,
    MARK_CLOSE,
    MARK_NEXT,
    MARK_SAVE,
    RADIO_MARK,
)
from music.player.card import (
    PAUSE_MARK,
    REPEAT_MARK,
    SHUFFLE_MARK,
    VOLUME_MARK,
    MusicPlayer,
)
from music.player.layout import PLAY_MARK, TITLE
from music.playlist.card import PlaylistCard
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

from tests.music.player.support import _fitted
from tests.music.support import (
    _Player,
    _ask,
    _card_text,
    _cards_caught_up,
    _named,
    _playing,
    _press,
    _scene,
)


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
        view.next,
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
    assert view.next.disabled is False
    assert view.previous.disabled is False
    assert view.volume.disabled is False
    assert view.volume.label == _fitted(VOLUME_MARK)
    assert view.radio.disabled is False
    assert view.shuffle.disabled is False

    track.is_stream = True
    track.length = 0
    assert view.paint() is True
    assert view.next.disabled is False
    assert view.radio.disabled is False
    assert view.shuffle.disabled is False
    assert view.repeat.disabled is True
    assert view.timeout is None
    message = MagicMock()
    message.delete = AsyncMock()
    view.message = message
    await view.dismiss()
    message.delete.assert_awaited_once()
    assert view._closed is True


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
        assert "0:00 / 2:28" not in text
        galleries = [item for item in view.walk_children() if isinstance(item, discord.ui.MediaGallery)]
        assert len(galleries) == 1
        assert view.children[0].accent_colour == ACCENT_COLOR
        thumbs = [item for item in view.walk_children() if isinstance(item, discord.ui.Thumbnail)]
        assert [thumb.media.url for thumb in thumbs] == ["https://example.com/art.jpg"]
        track.artwork = "http://p3.music.126.net/abc==/9.jpg?param=1024y1024&foo=1"
        covered = MusicPlayer(guild)
        netease = [item for item in covered.walk_children() if isinstance(item, discord.ui.Thumbnail)]
        assert [thumb.media.url for thumb in netease] == [
            "https://p3.music.126.net/abc==/9.jpg?foo=1&param=300y300"
        ]
        track.artwork = "https://example.com/art.jpg"
        assert view.pause.disabled is False
        assert view.previous.label == _fitted(MARK_BACK)
        assert view.pause.label == _fitted(PAUSE_MARK)
        assert view.pause.style == discord.ButtonStyle.secondary
        assert view.next.label == _fitted(MARK_NEXT)
        assert view.close.label == _fitted(MARK_CLOSE, hair=1)
        rows = [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]
        assert rows[0].children == [
            view.shuffle, view.previous, view.pause, view.next, view.repeat,
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
            # The button has already moved. The pause itself follows this edit.
            # A real edit spends the response, so the buttons coming back use the message.
            order.append("edit")
            interaction.response.is_done.return_value = True
            assert view.pause.label == _fitted(PLAY_MARK)
            assert view.pause.style == discord.ButtonStyle.primary
            assert "attachments" not in kwargs
            assert view.pause.disabled is True
            assert view.next.disabled is True
            assert view.previous.disabled is True
            assert view.repeat.disabled is True
            assert view.shuffle.disabled is True
            assert view.radio.disabled is True
            assert view.volume.disabled is True
            assert view.save.disabled is True
            assert view.info.disabled is True
            assert view.close.disabled is True
            assert view.leave.disabled is True

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
        assert order == ["edit", "pause"]
        interaction.response.defer.assert_not_awaited()
        player.pause.assert_awaited_once_with(True)
        interaction.response.edit_message.assert_awaited()
        paused = _card_text(view)
        assert view.pause.label == _fitted(PLAY_MARK)
        assert view.pause.style == discord.ButtonStyle.primary
        assert view.pause.disabled is False
        assert view.next.disabled is False
        assert view.volume.disabled is False
        assert "0:12 / 2:28" not in paused

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
    """Play on one device, then pause on either. The second click cannot wait on the first edit.

    It is accepted and dropped. The card stays on the first press, then the buttons come back.
    """
    guild, player, _track = await _playing()
    try:
        view = MusicPlayer(guild)
        held = asyncio.Event()
        release = asyncio.Event()

        async def slow_edit(**kwargs):
            # A real edit spends the response. The buttons coming back are a later edit.
            first.response.is_done.return_value = True
            assert view.pause.disabled is True
            assert view.next.disabled is True
            assert view.volume.disabled is True
            assert view.save.disabled is True
            assert view.info.disabled is True
            assert view.close.disabled is True
            assert view.leave.disabled is True
            held.set()
            await release.wait()

        async def flip(value):
            player._paused = value

        player.pause.side_effect = flip
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
        first.response.defer.assert_not_awaited()
        player.pause.assert_awaited_once_with(True)
        assert player._paused is True
        assert view.pause.style == discord.ButtonStyle.primary
        assert view.pause.label == _fitted(PLAY_MARK)
        assert view.pause.disabled is False
        assert view.next.disabled is False
        assert view.volume.disabled is False
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_a_pause_during_the_bar_still_pauses():
    """The bar upload does not eat a pause. The song stops while that picture is still going out.

    A second tap while that pause is landing does not start the song again. The card ends paused.
    """
    guild, player, _track = await _playing()
    try:
        view = MusicPlayer(guild)
        message = MagicMock()
        view.message = message
        held = asyncio.Event()
        release = asyncio.Event()
        paused = asyncio.Event()
        edits = []

        async def edit(**kwargs):
            edits.append(view.pause.style)
            if len(edits) == 1:
                held.set()
                await release.wait()

        async def flip(value):
            player._paused = value
            if value:
                paused.set()

        message.edit = AsyncMock(side_effect=edit)
        player.pause.side_effect = flip

        async def locked_upload():
            # The tick holds the card for the whole upload.
            async with view._lock:
                await view._push(None)

        bar = asyncio.create_task(locked_upload())
        await asyncio.wait_for(held.wait(), timeout=1)

        press = _press(player)

        async def defer():
            press.response.is_done.return_value = True

        press.response.defer.side_effect = defer
        pausing = asyncio.create_task(view.pause.callback(press))
        await asyncio.wait_for(paused.wait(), timeout=1)
        assert not release.is_set()
        player.pause.assert_awaited_once_with(True)

        second = _press(player)
        answered = asyncio.Event()

        async def defer_second():
            answered.set()
            second.response.is_done.return_value = True

        second.response.defer.side_effect = defer_second
        again = asyncio.create_task(view.pause.callback(second))
        await asyncio.wait_for(answered.wait(), timeout=1)
        release.set()
        await bar
        await pausing
        await again
        player.pause.assert_awaited_once_with(True)
        assert player._paused is True
        assert view.pause.style == discord.ButtonStyle.primary
        assert view.pause.label == _fitted(PLAY_MARK)
        assert view.pause.disabled is False
        assert edits[-1] == discord.ButtonStyle.primary
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_a_skip_starts_the_song_with_the_card():
    """The next song starts while the card is still drawing, so the two change together."""
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_later_song()]) == "Added Midnight City."
        view = MusicPlayer(guild)
        held = asyncio.Event()
        release = asyncio.Event()

        async def play(song):
            player._track = song

        async def edited(**kwargs):
            held.set()
            await release.wait()
            forward.response.is_done.return_value = True

        player.play.reset_mock()
        player.play.side_effect = play
        forward = _press(player)
        forward.response.edit_message.side_effect = edited
        skipping = asyncio.create_task(view.next.callback(forward))
        await asyncio.wait_for(held.wait(), timeout=1)
        for _ in range(20):
            if player.play.await_count:
                break
            await asyncio.sleep(0)
        player.play.assert_awaited()
        assert now_playing(guild).title == "Midnight City"
        release.set()
        await asyncio.wait_for(skipping, timeout=1)
        assert now_playing(guild).title == "Midnight City"
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_a_posted_skip_starts_the_song_while_the_new_card_goes_out():
    """The new message leaves with the song. The message already up stays as it was."""
    guild, player, _track = await _playing()
    view = None
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        nxt = _named("After", "Someone")
        nxt.uri = "https://example.com/after"
        nxt.artwork = "https://example.com/after.jpg"
        nxt.length = 180_000
        assert await queue_tracks(member, guild, [nxt]) == "Added After."
        view = MusicPlayer(guild)
        message, sent = _player_message()
        view.message = message
        held = asyncio.Event()
        release = asyncio.Event()
        sends = 0

        async def send(**kwargs):
            nonlocal sends
            sends += 1
            assert "After" in _card_text(kwargs["view"])
            held.set()
            await release.wait()
            return sent

        async def play(song):
            player._track = song

        async def defer():
            press.response.is_done.return_value = True

        message.channel.send = send
        player.play.reset_mock()
        player.play.side_effect = play
        press = _press(player)
        press.response.defer.side_effect = defer
        skipping = asyncio.create_task(view.next.callback(press))
        await asyncio.wait_for(held.wait(), timeout=1)
        for _ in range(20):
            if player.play.await_count:
                break
            await asyncio.sleep(0)
        player.play.assert_awaited()
        press.response.edit_message.assert_not_awaited()
        message.edit.assert_not_awaited()
        assert now_playing(guild).title == "After"
        release.set()
        await asyncio.wait_for(skipping, timeout=1)
        assert view.message is sent
        message.delete.assert_awaited()
        sent.edit.assert_not_awaited()
        assert "After" in view.song.content
        assert sends == 1
        await view.redraw()
        assert sends == 1
        sent.edit.assert_not_awaited()
    finally:
        if view is not None:
            view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_a_posted_back_starts_the_earlier_song_while_the_new_card_goes_out():
    """Back posts the earlier song with the audio."""
    guild, player, _track = await _playing()
    view = None
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_later_song()]) == "Added Midnight City."

        async def play(song):
            player._track = song

        player.play.side_effect = play
        await next_track(guild)
        assert now_playing(guild).title == "Midnight City"
        view = MusicPlayer(guild)
        message, sent = _player_message()
        view.message = message
        held = asyncio.Event()
        release = asyncio.Event()

        async def send(**_kwargs):
            held.set()
            await release.wait()
            return sent

        async def defer():
            press.response.is_done.return_value = True

        message.channel.send = send
        player.play.reset_mock()
        press = _press(player)
        press.response.defer.side_effect = defer
        skipping = asyncio.create_task(view.previous.callback(press))
        await asyncio.wait_for(held.wait(), timeout=1)
        for _ in range(20):
            if player.play.await_count:
                break
            await asyncio.sleep(0)
        player.play.assert_awaited()
        press.response.edit_message.assert_not_awaited()
        assert now_playing(guild).title == "I Ain't Worried"
        release.set()
        await asyncio.wait_for(skipping, timeout=1)
        assert view.message is sent
        sent.edit.assert_not_awaited()
        assert "I Ain't Worried" in view.song.content
    finally:
        if view is not None:
            view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_a_skip_during_the_bar_waits_for_the_picture():
    """The bar upload does not eat a skip, and the song waits until that picture lands."""
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_later_song()]) == "Added Midnight City."
        view = MusicPlayer(guild)
        message = MagicMock()
        view.message = message
        held = asyncio.Event()
        release = asyncio.Event()

        async def edit(**kwargs):
            held.set()
            await release.wait()

        async def play(song):
            player._track = song

        message.edit = AsyncMock(side_effect=edit)
        player.play.reset_mock()
        player.play.side_effect = play

        async def locked_upload():
            async with view._lock:
                await view._push(None)

        bar = asyncio.create_task(locked_upload())
        await asyncio.wait_for(held.wait(), timeout=1)
        press = _press(player)

        async def defer():
            press.response.is_done.return_value = True

        press.response.defer.side_effect = defer
        skipping = asyncio.create_task(view.next.callback(press))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        player.play.assert_not_awaited()
        assert now_playing(guild).title == "I Ain't Worried"
        release.set()
        await bar
        await asyncio.wait_for(skipping, timeout=1)
        assert now_playing(guild).title == "Midnight City"
        player.play.assert_awaited()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_an_open_card_refreshes(monkeypatch):
    """An open card refreshes when the line changes. A failed refresh keeps the buttons."""
    await case_an_open_player_refreshes_when_the_line_changes()
    with monkeypatch.context() as patch:
        await case_a_failed_refresh_keeps_the_buttons(patch)

async def case_an_open_player_refreshes_when_the_line_changes():
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
        await cog._redraw_player(press)
        press.channel.send.assert_not_awaited()
    finally:
        if panel is not None:
            panel.shutdown()
            await asyncio.sleep(0)
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_a_failed_refresh_keeps_the_buttons(monkeypatch):
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

async def test_a_quiet_mode_press(monkeypatch):
    """Repeat and shuffle only change the mode when nothing is on. A failed shuffle puts the real mode back."""
    await case_repeat_and_shuffle_only_change_the_mode_when_nothing_is_on()
    with monkeypatch.context() as patch:
        await case_a_failed_quiet_shuffle_puts_the_real_mode_back(patch)

async def case_repeat_and_shuffle_only_change_the_mode_when_nothing_is_on():
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
        assert "Up next: After" in text
        assert "Up next: I Ain't Worried" not in text
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
        assert "Up next: After" in shuffled
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

        card = PlaylistCard(guild, MagicMock())
        message = MagicMock()
        message.edit = AsyncMock()
        card.follow(message)
        assert "**1\\.** One" in _card_text(card)
        assert "On Shuffle" not in _card_text(card)

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
        assert "### Now Playing - On Shuffle" in shuffled
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
        assert "On Shuffle" not in restored
        assert "**1\\.** One" in restored
        assert view.shuffle.style == discord.ButtonStyle.secondary
        assert await queue_tracks(member, guild, [_named("End")]) == "Added End."
        assert [item.track.title for item in _stored(guild).later] == ["One", "Two", "Three", "Front", "End"]

        await view.repeat.callback(press)
        await _cards_caught_up()
        assert _stored(guild).repeat is True
        assert _stored(guild).shuffle is False
        assert view.repeat.style == discord.ButtonStyle.primary
        assert view.shuffle.style == discord.ButtonStyle.secondary
        assert [item.track.title for item in _stored(guild).later][-1] == "End"
        repeated = _card_text(card)
        assert "### Now Playing - On Repeat" in repeated
        assert "On Shuffle" not in repeated
    finally:
        if card is not None:
            card.stop()
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
        assert _stored(guild).position == 90_000

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


async def test_a_station_has_no_bar():
    """A station has no time bar and can be skipped. Repeat can still turn off."""
    await case_a_radio_station_has_no_time_bar_and_can_be_skipped()
    await case_repeat_can_turn_off_while_a_stream_is_on()

async def case_a_radio_station_has_no_time_bar_and_can_be_skipped():
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
        assert view.next.disabled is False
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
        line = PlaylistCard(guild, MagicMock())
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
        assert song.next.disabled is False
        later.length = 0
        missing = MusicPlayer(guild)
        assert missing.next.disabled is False
        later.length = 90_000

        track.is_stream = False
        track.length = 8 * 60 * 60 * 1000
        _stored(guild).current.videos = ("https://www.youtube.com/watch?v=long",)
        held = MusicPlayer(guild)
        held_text = _card_text(held)
        assert RADIO_MARK in held_text
        assert "━" not in held_text
        assert any(isinstance(item, discord.ui.MediaGallery) for item in held.walk_children())
        assert held.next.disabled is False
        held_line = PlaylistCard(guild, MagicMock())
        assert RADIO_MARK in _card_text(held_line)
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_repeat_can_turn_off_while_a_stream_is_on():
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

        waiting = _named("After")
        waiting.uri = "https://example.com/after"
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [waiting]) == "Added After."

        press = _press(player)
        press.response.defer = AsyncMock()
        press.delete_original_response = AsyncMock()
        await view.leave.callback(press)
        player.stop.assert_awaited()
        player.disconnect.assert_awaited()
        press.delete_original_response.assert_awaited()
        assert view._closed is True
        assert [row.title for row in queued(guild)] == ["I Ain't Worried", "After"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


def _outsider():
    """Someone looking at the card who is not in a voice channel."""
    press = _ask()
    press.user.voice = None
    press.user.id = 44
    press.guild.voice_client = None
    press.response.send_modal = AsyncMock()
    press.response.send_message = AsyncMock()
    return press


async def test_the_other_buttons_work_from_outside_the_voice_channel(monkeypatch):
    """Next, back, play, and leave need the voice channel. The other buttons still change the card."""
    from config import settings

    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        line = _stored(guild)
        line.paused = True
        line.position = 65_000
        player._paused = True
        player._position = 65_000
        view = MusicPlayer(guild)
        assert view.pause.label == _fitted(PLAY_MARK)
        player.play.reset_mock()
        player.pause.reset_mock()
        player.channel.connect.reset_mock()

        # The bot is still in the room. Every button answers only from inside it.
        for button in (
            view.previous, view.pause, view.next, view.leave, view.shuffle, view.repeat,
            view.volume, view.radio, view.save, view.info, view.close,
        ):
            press = _outsider()
            await button.callback(press)
            assert "Join the voice channel first" in press.send.await_args.args[0]
        player.play.assert_not_awaited()
        player.pause.assert_not_awaited()
        player.disconnect.assert_not_awaited()
        player.set_volume.assert_not_awaited()
        assert view._closed is False
        assert _stored(guild).shuffle is False
        assert _stored(guild).repeat is False

        # The bot has left. Volume and radio stay on the saved song and do not join.
        guild.voice_client = None
        parked = MusicPlayer(guild)
        assert parked.pause.label == _fitted(PLAY_MARK)
        for button in (parked.previous, parked.pause, parked.next, parked.leave):
            press = _outsider()
            await button.callback(press)
            assert "Join the voice channel first" in press.send.await_args.args[0]
        player.play.assert_not_awaited()
        player.pause.assert_not_awaited()
        player.disconnect.assert_not_awaited()

        await parked.shuffle.callback(_outsider())
        assert _stored(guild).shuffle is True
        assert "On Shuffle" in _card_text(parked)
        await parked.repeat.callback(_outsider())
        assert _stored(guild).repeat is True
        assert _stored(guild).shuffle is False
        assert "On Repeat" in _card_text(parked)
        tune = _outsider()
        await parked.volume.callback(tune)
        modal = tune.response.send_modal.await_args.args[0]
        modal.level.component._value = "25"
        modal.place.component._value = "0:10"
        await modal.on_submit(_outsider())
        player.set_volume.assert_not_awaited()
        player.play.assert_not_awaited()
        player.channel.connect.assert_not_awaited()
        assert guild.voice_client is None
        assert _stored(guild).volume == 25
        assert _stored(guild).position == 10_000

        settings.lastfm_api_key = "abc123abc123abc1"
        monkeypatch.setattr("music.player.radio.load_similar", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        await parked.radio.callback(_outsider())
        assert _stored(guild).radio is True
        assert parked.radio.style == discord.ButtonStyle.primary
        assert "On Repeat + Radio" in _card_text(parked)
        player.play.assert_not_awaited()
        player.channel.connect.assert_not_awaited()

        from types import SimpleNamespace

        track.album = SimpleNamespace(name="Dreaming Out Loud", url=None)
        _stored(guild).current.track.album = track.album
        info = _outsider()
        await parked.info.callback(info)
        assert "Dreaming Out Loud" in _card_text(info.response.send_message.await_args.kwargs["view"])

        closer = _outsider()
        await parked.close.callback(closer)
        closer.delete_original_response.assert_awaited()
        assert parked._closed is True
        assert guild.voice_client is None
    finally:
        settings.lastfm_api_key = None
        await stop_track(guild)


async def test_the_controls_start_before_discord_answers():
    """Next edits the card first. Previous answers first. Repeat turns on as the card answers. Leave starts first. The volume box takes a level."""
    await _scene(case_next_edits_the_card_before_the_song_changes)
    await _scene(case_previous_answers_before_the_song_changes)
    await _scene(case_repeat_turns_on_as_the_card_answers)
    await _scene(case_leave_starts_before_discord_answers)
    await _scene(case_the_volume_box_takes_a_level)


def _later_song():
    later = MagicMock()
    later.title = "Midnight City"
    later.author = "M83"
    later.length = 240_000
    return later


async def case_next_edits_the_card_before_the_song_changes():
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_later_song()]) == "Added Midnight City."
        view = MusicPlayer(guild)
        order = []

        async def play(song):
            order.append("next")
            player._track = song

        async def edited(**kwargs):
            order.append("edit")
            if "next" not in order:
                forward.response.is_done.return_value = True

        player.play.side_effect = play
        forward = _press(player)
        forward.response.edit_message.side_effect = edited
        await asyncio.wait_for(view.next.callback(forward), timeout=1)
        assert order == ["edit", "next"]
        forward.response.defer.assert_not_awaited()
        assert now_playing(guild).title == "Midnight City"
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_previous_answers_before_the_song_changes():
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_later_song()]) == "Added Midnight City."

        async def play(song):
            player._track = song

        player.play.side_effect = play
        await next_track(guild)
        assert now_playing(guild).title == "Midnight City"
        view = MusicPlayer(guild)
        order = []

        async def play_earlier(song):
            order.append("previous")
            player._track = song

        async def acknowledge_back():
            order.append("ack")
            back.response.is_done.return_value = True

        player._position = 20_000
        player.play.side_effect = play_earlier
        player.seek.reset_mock()
        back = _press(player)
        back.response.defer.side_effect = acknowledge_back
        await asyncio.wait_for(view.previous.callback(back), timeout=1)
        assert order == ["ack", "previous"]
        assert now_playing(guild).title == "I Ain't Worried"
        player.seek.assert_not_awaited()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_repeat_turns_on_as_the_card_answers():
    guild, player, _track = await _playing()
    try:
        view = MusicPlayer(guild)
        order = []

        async def edited(**kwargs):
            order.append("edit")
            again.response.is_done.return_value = True
            assert view.repeat.style == discord.ButtonStyle.primary

        again = _press(player)
        again.response.edit_message.side_effect = edited
        await view.repeat.callback(again)
        assert order == ["edit"]
        assert _stored(guild).repeat is True
        assert view.repeat.style == discord.ButtonStyle.primary
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_leave_starts_before_discord_answers():
    guild, player, _track = await _playing()
    try:
        view = MusicPlayer(guild)
        order = []
        started = asyncio.Event()

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


async def case_the_volume_box_takes_a_level():
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

        player._position = 65_000
        press = _press(player)
        press.response.send_modal = AsyncMock()
        await view.volume.callback(press)
        modal = press.response.send_modal.await_args.args[0]
        assert modal.title == "Tune"
        assert [item.text for item in modal.children] == ["Volume", "Duration", "Platform"]
        assert modal.level.component.default == "100"
        assert modal.level.component.placeholder == "From 0 to 100"
        assert modal.level.description is None
        assert modal.place.component.default == "1:05"
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

        held = _press(player)
        held.response.defer = AsyncMock()
        modal.level.component._value = "40"
        modal.place.component._value = "1:05"
        await modal.on_submit(held)
        player.set_volume.assert_awaited_with(40)
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
        _stored(guild).repeat = True
        view = MusicPlayer(guild)
        assert view.repeat.style == discord.ButtonStyle.primary
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
        assert _stored(guild).repeat is False
        assert view.repeat.style == discord.ButtonStyle.secondary
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
        player._position = 20_000
        player.seek.reset_mock()
        await previous_track(guild)
        assert now_playing(guild).title == "I Ain't Worried"
        player.seek.assert_not_awaited()
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

async def test_skipping_the_last_song_clears_the_player():
    """Next with nothing queued leaves the empty player, the same as a song that finished."""
    guild, player, track = await _playing()
    view = None
    line = None
    try:
        view = MusicPlayer(guild)
        message = MagicMock()
        message.delete = AsyncMock()
        message.edit = AsyncMock()
        view.follow(message)
        line = PlaylistCard(guild, MagicMock())
        queue_message = MagicMock()
        queue_message.edit = AsyncMock()
        line.follow(queue_message)

        async def stop(*_args, **_kwargs):
            # The stop request ends the audio. The end event can hand the track back.
            player._track = track
            return track

        player.stop.side_effect = stop
        press = _press(player)
        await view.next.callback(press)
        await _cards_caught_up()
        player._track = track
        state = now_playing(guild)
        assert state is not None and state.has_track is False
        text = _card_text(view)
        assert "Nothing is playing" in text
        assert "I Ain't Worried" not in text
        assert view.next.disabled is True
        assert view.pause.disabled is True
        assert view._closed is True
        message.delete.assert_awaited()
        shown = _card_text(line)
        assert "Nothing is playing" in shown
        assert "Now Playing" not in shown
    finally:
        if view is not None:
            view.shutdown()
        if line is not None:
            line.stop()
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


async def test_the_card_follows_the_song(monkeypatch):
    """An open card refreshes when the line changes. A finished song removes it. Repeat keeps it. A different song posts a new one."""
    await _scene(case_an_open_card_refreshes, monkeypatch=monkeypatch)
    await case_a_finished_song_removes_the_player()
    await case_repeat_keeps_the_card_when_the_song_starts_again()
    await case_a_different_song_posts_a_new_player_message()
    await case_a_failed_post_keeps_the_song_on_the_open_card()

async def case_a_finished_song_removes_the_player():
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


async def case_repeat_keeps_the_card_when_the_song_starts_again():
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


async def case_a_different_song_posts_a_new_player_message():
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


async def case_a_failed_post_keeps_the_song_on_the_open_card():
    """Discord refusing the new message leaves the open card on the new song."""
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        view = MusicPlayer(guild)
        message, _sent = _player_message()
        view.message = message
        message.channel.send = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "no"),
        )
        player._guild = guild
        player._last_position = 148_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        await view.redraw()
        assert view.message is message
        assert view._closed is False
        assert "After" in view.song.content
        message.delete.assert_not_awaited()
        message.edit.assert_awaited()
        from music.player.card import card_open, forget, remember
        remember(view)
        assert card_open(guild) is True
        preview = MagicMock(guild=guild, _closed=False, message=None)
        remember(preview)
        assert card_open(guild) is True
        preview._closed = True
        forget(preview)
        assert card_open(guild) is True
        view._closed = True
        forget(view)
        assert card_open(guild) is False
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

        async def edited(**kwargs):
            press.response.is_done.return_value = True

        async def send(**kwargs):
            assert kwargs["file"].filename == "bar-2.gif"
            return sent

        press = _press(player)
        press.response.edit_message = AsyncMock(side_effect=edited)
        message.channel.send = send
        await view.next.callback(press)
        assert view.message is sent
        message.delete.assert_awaited()
        sent.edit.assert_not_awaited()
        assert "After" in view.song.content
        gallery = next(item for item in view.walk_children() if isinstance(item, discord.ui.MediaGallery))
        assert gallery.items[0].media.url == "attachment://bar-2.gif"
    finally:
        view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_a_failed_quiet_shuffle_puts_the_real_mode_back(monkeypatch):
    """A failed press during the gap restores the buttons, and the card stays up."""
    guild, player, _track = await _playing()
    view = None
    try:
        view = MusicPlayer(guild)
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        player._track = None
        assert view._holding() is True

        async def broken(_guild):
            raise RuntimeError("shuffle failed")

        monkeypatch.setattr("music.player.card.shuffle_queue", broken)
        press = _press(player)
        press.send = AsyncMock()
        await view.shuffle.callback(press)
        assert view._closed is False
        assert view.shuffle.style == discord.ButtonStyle.secondary
        assert _stored(guild).shuffle is False
    finally:
        if view is not None:
            view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_radio_keeps_the_card_while_the_next_song_is_chosen(monkeypatch):
    """Radio looks up the next song with an empty queue. The card stays until radio is off."""
    guild, player, _track = await _playing()
    view = None
    try:
        view = MusicPlayer(guild)
        message, _sent = _player_message()
        view.message = message
        line = _stored(guild)
        line.radio = True
        line.current = None
        line.later.clear()
        player._track = None
        monkeypatch.setattr("music.player.sync.REFRESH_SECONDS", 0)
        view.follow(message)
        for _ in range(5):
            await asyncio.sleep(0)
        assert view._closed is False
        line.radio = False
        for _ in range(20):
            await asyncio.sleep(0)
            if view._closed:
                break
        assert view._closed is True
    finally:
        if view is not None:
            view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)
