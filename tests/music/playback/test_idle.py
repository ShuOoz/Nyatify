"""Leave voice when nothing is playing, or a pause has lasted. No Discord login."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import wavelink

from music.lavalink import IDLE_SECONDS
from music.playback import (
    leave_if_idle,
    leave_if_quiet,
    load_saved_lines,
    now_playing,
    queue_tracks,
    queued,
    save_and_clear,
    stop_track,
    toggle_pause,
    warm_voice,
    _stored,
)

from tests.music.playback.test_remember import _paused_with_next
from tests.music.support import _Player, _named, _playing, _scene

async def test_an_unused_room_leaves(monkeypatch):
    """An early join leaves only when nothing started. Five quiet or paused minutes leave and keep the line. A failed disconnect still stores the song."""
    await _scene(case_an_early_join_leaves_only_when_nothing_started)
    await _scene(case_five_minutes_leave_and_keep_the_line, monkeypatch=monkeypatch)
    await case_a_pause_that_leaves_keeps_the_song_even_when_the_disconnect_fails()
    await case_a_disconnect_that_fails_while_still_in_voice_keeps_the_player()


async def case_an_early_join_leaves_only_when_nothing_started():
    channel = MagicMock()
    channel.id = 4
    player = _Player(channel)
    guild = MagicMock()
    guild.id = 92
    guild.voice_client = None

    async def connect(**kwargs):
        guild.voice_client = player
        return player

    channel.connect = connect
    member = MagicMock()
    member.id = 3
    member.voice.channel = channel
    try:
        warm_voice(member, guild)
        await asyncio.sleep(0)
        await leave_if_idle(guild)
        player.disconnect.assert_awaited_once()

        guild.voice_client = None
        player.disconnect.reset_mock()
        player.play.side_effect = wavelink.WavelinkException("no")
        warm_voice(member, guild)
        await asyncio.sleep(0)
        assert await queue_tracks(member, guild, [_named("Miss")]) == "YouTube did not start that song."
        await leave_if_idle(guild)
        player.disconnect.assert_awaited_once()

        guild.voice_client = None
        player.disconnect.reset_mock()
        player.play.side_effect = None
        warm_voice(member, guild)
        await asyncio.sleep(0)
        assert await queue_tracks(member, guild, [_named("Stay")]) == "Playing Stay."
        await leave_if_idle(guild)
        player.disconnect.assert_not_awaited()
    finally:
        player.play.side_effect = None
        guild.voice_client = player
        await stop_track(guild)

async def case_five_minutes_leave_and_keep_the_line(monkeypatch):
    """Five quiet minutes leave and keep the queue. Five paused minutes leave and keep the song."""
    await case_five_quiet_minutes_leave_and_keep_the_queue()
    with monkeypatch.context() as patch:
        await case_five_paused_minutes_leave_and_keep_the_song(patch)

async def case_five_quiet_minutes_leave_and_keep_the_queue():
    guild, player, _track = await _playing()
    try:
        assert await leave_if_quiet(guild) is False
        player.disconnect.assert_not_awaited()

        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("Next")]) == "Added Next."
        player._track = None
        _stored(guild).current = None
        assert await leave_if_quiet(guild) is True
        player.disconnect.assert_awaited_once()
        save_and_clear(guild)
        import music.playback as playback

        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        assert [row.title for row in queued(guild)] == ["Next"]
    finally:
        guild.voice_client = None
        await stop_track(guild)

async def case_five_paused_minutes_leave_and_keep_the_song(monkeypatch):
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("Next")]) == "Added Next."
        seen: list[float] = []

        async def no_wait(seconds):
            seen.append(seconds)
            return None

        monkeypatch.setattr("music.playback.idle._wait", no_wait)
        finished = asyncio.Event()

        def dispatch(event, target):
            assert event == "wavelink_inactive_player"
            assert target is player

            async def run():
                assert await leave_if_quiet(guild) is True
                finished.set()

            asyncio.create_task(run())

        player.client = MagicMock()
        player.client.dispatch = dispatch
        assert await toggle_pause(guild) is None
        await asyncio.wait_for(finished.wait(), timeout=1)
        assert seen == [IDLE_SECONDS]
        player.disconnect.assert_awaited_once()

        import music.playback as playback

        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        state = now_playing(guild)
        assert state is not None and state.title == "I Ain't Worried" and state.paused is True
        assert [(row.title, row.playing) for row in queued(guild)] == [
            ("I Ain't Worried", True),
            ("Next", False),
        ]
    finally:
        guild.voice_client = None
        await stop_track(guild)

async def case_a_pause_that_leaves_keeps_the_song_even_when_the_disconnect_fails():
    import music.playback as playback
    from music.playback import idle

    guild, _member, player = await _paused_with_next(9812)
    try:
        async def disconnect():
            # Discord took the bot out, then Lavalink had nothing to answer.
            guild.voice_client = None
            raise RuntimeError("no session")

        player.disconnect.side_effect = disconnect
        idle._pause_ready.add(9812)
        assert await leave_if_quiet(guild) is True
        assert _stored(guild) is None
        assert now_playing(guild).title == "Slow Down"

        # Then the process restarts. The song and the queue come back.
        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        load_saved_lines()
        state = now_playing(guild)
        assert state is not None and state.has_track is True and state.paused is True
        assert [(row.title, row.playing) for row in queued(guild)] == [
            ("Slow Down", True),
            ("Next", False),
        ]
    finally:
        guild.voice_client = None
        await stop_track(guild)

async def case_a_disconnect_that_fails_while_still_in_voice_keeps_the_player():
    from music.playback import idle

    guild, _member, player = await _paused_with_next(9813)
    try:
        player.disconnect.side_effect = RuntimeError("busy")
        idle._pause_ready.add(9813)
        assert await leave_if_quiet(guild) is False
        assert guild.voice_client is player
        assert _stored(guild) is not None
    finally:
        player.disconnect.side_effect = None
        await stop_track(guild)
