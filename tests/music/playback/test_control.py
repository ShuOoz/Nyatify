"""Pause, skip, a similar song, and a station that ends. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import wavelink

from music.lavalink import Found
from music.playback import (
    advance_from_end,
    next_track,
    now_playing,
    queue_tracks,
    stop_track,
    suggest_next,
    _stored,
    toggle_pause,
)

from tests.music.support import _named, _playing

async def test_pause_needs_a_song():
    assert await toggle_pause(None) == "Use this command in a server."
    guild = MagicMock()
    guild.voice_client = None
    assert await toggle_pause(guild) == "Nothing is playing."
    assert now_playing(guild) is None
    assert now_playing(None) is None

async def test_a_failed_skip_leaves_the_song_playing():
    guild, player, _track = await _playing()
    try:
        player.stop.side_effect = wavelink.WavelinkException("no")
        assert await next_track(guild) == "Something went wrong while changing the song."
        assert now_playing(guild).title == "I Ain't Worried"
        assert _stored(guild).current.track.title == "I Ain't Worried"
        assert _stored(guild).repeat is False
    finally:
        player.stop.side_effect = None
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_a_similar_song_skips_a_preview_and_waits(monkeypatch):
    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("Queued")]) == "Added Queued."
        song = _stored(guild).current
        song.platform = "soundcloud"
        track.identifier = "now"
        preview = MagicMock(title="Clip", length=30_000, is_preview=False, identifier="clip")
        full = MagicMock(title="Full", length=180_000, is_preview=False, identifier="full")
        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[preview, full]))
        assert await suggest_next(guild, member) is None
        assert [item.track.title for item in _stored(guild).later] == ["Queued", "Full"]

        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[preview]))
        assert await suggest_next(guild, member) == "Nothing similar came up."
        assert [item.track.title for item in _stored(guild).later] == ["Queued", "Full"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_a_finished_station_plays_the_queue(monkeypatch):
    guild, player, _track = await _playing()
    try:
        song = _stored(guild).current
        song.videos = (
            "https://www.youtube.com/watch?v=one",
            "https://www.youtube.com/watch?v=two",
        )
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        called = []

        async def search(*args, **kwargs):
            called.append(args[1] if len(args) > 1 else kwargs.get("song"))
            return Found(args[1], "youtube", [_named("Station")], True)

        monkeypatch.setattr("music.playback.find_songs", search)
        player._guild = guild
        player._last_position = 4_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert called == ["https://www.youtube.com/watch?v=one"]
        assert now_playing(guild).title == "Station"
        assert [item.track.title for item in _stored(guild).later] == ["After"]

        player._last_position = 60_000
        called.clear()
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert called == []
        assert now_playing(guild).title == "After"
        assert _stored(guild).current.videos == ()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_a_radio_that_ends_plays_another_address(monkeypatch):
    links = (
        "https://www.youtube.com/live/one",
        "https://www.youtube.com/live/two",
        "https://www.youtube.com/live/three",
    )

    async def play_station():
        guild, player, track = await _playing()
        track.is_stream = True
        song = _stored(guild).current
        song.videos = links
        song.video_at = 0
        player._guild = guild
        return guild, player

    def ended(player, reason="finished"):
        return type("End", (), {"reason": reason, "player": player})()

    guild, player = await play_station()
    try:
        again = _named("Lofi Girl")
        again.is_stream = True
        seen = []

        async def reload_same(guild, song, platform="youtube", mode=None):
            seen.append(song)
            return Found(song, platform, [again], True)

        monkeypatch.setattr("music.playback.find_songs", reload_same)
        player._last_position = 4_000
        await advance_from_end(ended(player))
        assert seen == [links[0]]
        assert now_playing(guild).title == "Lofi Girl"
        assert _stored(guild).current.video_at == 0
        assert _stored(guild).current.drops == 1
        player.stop.assert_not_awaited()

        seen.clear()
        nxt = _named("Chillhop")
        nxt.is_stream = True

        async def next_address(guild, song, platform="youtube", mode=None):
            seen.append(song)
            if song == links[1]:
                return Found(song, platform, [nxt], True)
            return "Nothing on YouTube matched that."

        monkeypatch.setattr("music.playback.find_songs", next_address)
        player._last_position = 4_000
        await advance_from_end(ended(player))
        assert seen == [links[1]]
        assert now_playing(guild).title == "Chillhop"
        assert _stored(guild).current.video_at == 1
        player.stop.assert_not_awaited()
    finally:
        await stop_track(guild)

    guild, player = await play_station()
    try:
        held = _named("Piano")
        held.is_stream = True
        seen = []

        async def after_a_long_play(guild, song, platform="youtube", mode=None):
            seen.append(song)
            if song == links[1]:
                return Found(song, platform, [held], True)
            return Found(song, platform, [_named("clip")], True)

        monkeypatch.setattr("music.playback.find_songs", after_a_long_play)
        player._last_position = 60_000
        await advance_from_end(ended(player))
        assert seen == [links[1]]
        assert now_playing(guild).title == "Piano"
        assert _stored(guild).current.drops == 0
        player.stop.assert_not_awaited()
    finally:
        await stop_track(guild)

    guild, player = await play_station()
    try:
        five = tuple(f"https://www.youtube.com/watch?v={name}" for name in "abcde")
        song = _stored(guild).current
        song.videos = five
        song.video_at = 4
        song.drops = 0
        wrapped = _named("First again")
        seen = []

        async def back_to_the_first(guild, link, platform="youtube", mode=None):
            seen.append(link)
            if link == five[0]:
                return Found(link, platform, [wrapped], True)
            return "Nothing on YouTube matched that."

        monkeypatch.setattr("music.playback.find_songs", back_to_the_first)
        player._last_position = 3_600_000
        await advance_from_end(ended(player))
        assert seen == [five[0]]
        assert now_playing(guild).title == "First again"
        assert _stored(guild).current.video_at == 0
        player.stop.assert_not_awaited()
    finally:
        await stop_track(guild)

    guild, player = await play_station()
    try:
        async def dead(guild, song, platform="youtube", mode=None):
            return "Nothing on YouTube matched that."

        monkeypatch.setattr("music.playback.find_songs", dead)
        player._last_position = 4_000
        await advance_from_end(ended(player, "loadFailed"))
        player.stop.assert_awaited()
        assert now_playing(guild).has_track is False
    finally:
        await stop_track(guild)
