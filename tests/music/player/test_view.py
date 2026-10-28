"""The player card. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord

from announce import ACCENT_COLOR
from cogs.osa_musify import OsaMusify
from music.panel import RADIO_MARK, progress_bar, progress_line, progress_text
from music.player.view import MusicPlayer
from music.playlist.card import LiveLine
from music.playback import (
    advance_from_end,
    change_volume,
    next_track,
    play_now,
    now_playing,
    nudge,
    previous_track,
    queue_tracks,
    queued,
    stop_track,
    suggest_next,
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

async def test_an_idle_player_leaves_only_close():
    guild = MagicMock()
    guild.id = 31
    channel = MagicMock()
    channel.id = 19
    player = _Player(channel)
    guild.voice_client = player
    view = MusicPlayer(guild)
    assert view.close.disabled is False
    idle = (
        view.previous,
        view.pause,
        view.skip,
        view.quieter,
        view.louder,
        view.rewind,
        view.forward,
        view.radio,
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
    assert view.quieter.disabled is False
    assert view.louder.disabled is False
    assert view.radio.disabled is False
    assert view.rewind.disabled is False

    track.is_stream = True
    track.length = 0
    assert view.paint() is True
    assert view.skip.disabled is False
    assert view.rewind.disabled is True
    assert view.radio.disabled is False

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
        assert "**[I Ain't Worried](https://example.com/song)**" in text
        assert "OneRepublic" in text
        assert "Volume 100" in text
        assert "Up next" not in text
        assert progress_line(0, 148_000) in text
        assert not any(isinstance(item, discord.ui.MediaGallery) for item in view.walk_children())
        assert view.children[0].accent_colour == ACCENT_COLOR
        thumbs = [item for item in view.walk_children() if isinstance(item, discord.ui.Thumbnail)]
        assert [thumb.media.url for thumb in thumbs] == ["https://example.com/art.jpg"]
        assert view.pause.disabled is False
        assert view.close.emoji.name == "❌"
        assert view.repeat.emoji.name == "🔂"
        assert view.repeat.disabled is False
        assert view.repeat.style == discord.ButtonStyle.secondary

        track.artwork = None
        plain = MusicPlayer(guild)
        assert not any(isinstance(item, discord.ui.Thumbnail) for item in plain.walk_children())

        order = []

        async def edit(**kwargs):
            order.append("edit")
            assert view.pause.emoji.name == "▶"

        async def flip(value):
            order.append("pause")
            player._paused = value

        player._position = 12_000
        player.pause.side_effect = flip
        interaction = _press(player)
        interaction.response.edit_message.side_effect = edit
        await view.pause.callback(interaction)
        assert order == ["edit", "pause"]
        player.pause.assert_awaited_once_with(True)
        interaction.response.edit_message.assert_awaited()
        assert "attachments" not in interaction.response.edit_message.await_args.kwargs
        paused = _card_text(view)
        assert view.pause.emoji.name == "▶"
        assert progress_line(12_000, 148_000) in paused

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
        assert "Up next: After" in panel.meta.content
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

        monkeypatch.setattr("music.player.view.asyncio.sleep", no_wait)
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

async def test_a_radio_station_has_no_time_bar_and_can_be_skipped():
    guild, player, track = await _playing()
    try:
        track.is_stream = True
        track.length = 0
        player._position = 20_000
        view = MusicPlayer(guild)
        text = _card_text(view)
        assert "Volume 100" in text
        assert RADIO_MARK in text
        assert "━" not in text
        assert view.rewind.disabled is True
        assert view.forward.disabled is True
        assert view.repeat.disabled is True
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
        assert song.rewind.disabled is False
        assert song.skip.disabled is False
        later.length = 0
        missing = MusicPlayer(guild)
        assert missing.rewind.disabled is True
        assert missing.forward.disabled is True
        assert missing.skip.disabled is False
        later.length = 90_000

        track.is_stream = False
        track.length = 8 * 60 * 60 * 1000
        _stored(guild).current.videos = ("https://www.youtube.com/watch?v=long",)
        held = MusicPlayer(guild)
        held_text = _card_text(held)
        assert RADIO_MARK in held_text
        assert "━" not in held_text
        assert held.rewind.disabled is True
        assert held.skip.disabled is False
        held_line = LiveLine(guild, MagicMock())
        assert RADIO_MARK in _card_text(held_line)
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
        assert "Volume 100" in _card_text(view)
        assert "Up next: Midnight City - M83" in _card_text(view)
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

        player._position = 10_000
        await nudge(guild, -15_000)
        player.seek.assert_awaited_with(0)
        player._position = 140_000
        await nudge(guild, 15_000)
        player.seek.assert_awaited_with(148_000)

        track.is_seekable = False
        assert await nudge(guild, -15_000) == "That song cannot move."

        track.is_seekable = True
        track.length = 0
        assert progress_text(0, 0) == "live"
        assert "━" not in progress_bar(0, 0)
        assert await nudge(guild, 15_000) == "That song cannot move."
        track.length = 148_000

        assert await change_volume(guild, 10) is None
        assert now_playing(guild).volume == 100
        assert await change_volume(guild, -10) is None
        player.set_volume.assert_awaited_with(90)
        assert now_playing(guild).volume == 90
        view.paint()
        assert "Volume 90" in _card_text(view)
        await change_volume(guild, -100)
        assert now_playing(guild).volume == 0

        same = MagicMock()
        same.title = "I Ain't Worried"
        same.identifier = "same"
        other = MagicMock()
        other.title = "Counting Stars"
        other.author = "OneRepublic"
        other.identifier = "other"
        track.identifier = "same"
        monkeypatch.setattr(
            "music.source.search_track", AsyncMock(return_value=[same, other]),
        )
        asker = MagicMock()
        asker.id = 21
        assert await suggest_next(guild, asker) is None
        assert now_playing(guild).upcoming == "Midnight City - M83"
        assert _stored(guild).later[-1].track.title == "Counting Stars"
        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[same]))
        assert await suggest_next(guild, asker) == "Nothing similar came up."

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
