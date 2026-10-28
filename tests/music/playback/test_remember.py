"""The line kept for one server, including across a restart. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from music.lavalink import Found
from music.playback import (
    next_track,
    played,
    queue_tracks,
    queued,
    release,
    unwatch_line,
    watch_line,
    stop_track,
    _stored,
)

from tests.music.support import _Player, _named, _playing

async def test_a_slow_queue_card_does_not_hold_up_play():
    guild, player, track = await _playing()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(_guild):
        started.set()
        await release.wait()

    watch_line(slow)
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        queued = asyncio.create_task(queue_tracks(member, guild, [_named("Next")]))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert queued.done() is True
        assert queued.result() == "Added Next."
    finally:
        release.set()
        unwatch_line(slow)
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_a_restart_reads_the_saved_queue_and_history():
    import music.playback as playback

    guild = MagicMock()
    guild.id = 79
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    player = _Player(channel)
    channel.connect = AsyncMock(return_value=player)

    async def play(track):
        player._track = track

    player.play.side_effect = play
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel
    one, two, three = _named("One", "Mac"), _named("Two"), _named("Three")
    one.uri = "https://example.com/one"
    one.length = 180_000
    try:
        assert await queue_tracks(member, guild, [one, two, three]) == "Playing One."
        guild.voice_client = player
        assert await next_track(guild) is None
        release(guild)
        assert playback._stored(guild) is None

        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        rows = queued(guild)
        assert [row.title for row in rows] == ["Two", "Three"]
        assert [row.playing for row in rows] == [False, False]
        assert rows[0].author == ""
        assert [row.title for row in played(guild)] == ["One"]
        assert played(guild)[0].author == "Mac"
        assert played(guild)[0].length == 180_000

        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        await stop_track(guild)
        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        assert queued(guild) is None
        assert [row.title for row in played(guild)] == ["One"]
    finally:
        await stop_track(guild)

def test_a_saved_station_still_reads_the_old_video_list():
    import music.playback as playback

    song = playback._load_song({
        "title": "Lofi",
        "url": "https://www.youtube.com/watch?v=one",
        "platform": "youtube",
        "lives": [
            "https://www.youtube.com/watch?v=one",
            "https://www.youtube.com/watch?v=two",
        ],
        "live_at": 1,
        "station": "lofi",
    })
    assert song is not None
    assert song.videos == (
        "https://www.youtube.com/watch?v=one",
        "https://www.youtube.com/watch?v=two",
    )
    assert song.video_at == 1
    assert song.station == "lofi"
    saved = playback._dump_song(song)
    assert saved["videos"] == list(song.videos)
    assert saved["video_at"] == 1
    assert "lives" not in saved
    assert "live_at" not in saved

async def test_a_reload_keeps_the_platform_of_the_song_still_playing():
    import music.playback as playback

    guild = MagicMock()
    guild.id = 81
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    player = _Player(channel)
    channel.connect = AsyncMock(return_value=player)

    async def play(track):
        player._track = track

    player.play.side_effect = play
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel
    song = _named("Slow Down", "Mac Ayres")
    song.uri = "https://www.youtube.com/watch?v=slow"
    song.length = 234_000
    waiting = _named("Next", "Someone")
    waiting.uri = "https://music.apple.com/us/song/next/2"
    try:
        assert await queue_tracks(member, guild, [song, waiting], "apple") == "Playing Slow Down."
        guild.voice_client = player
        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        playback.load_saved_lines()

        guild.voice_client = None
        parked = queued(guild)
        assert [(row.title, row.platform, row.playing) for row in parked] == [
            ("Slow Down", "apple", False),
            ("Next", "apple", False),
        ]

        guild.voice_client = player
        rows = queued(guild)
        assert [(row.title, row.platform, row.playing) for row in rows] == [
            ("Slow Down", "apple", True),
            ("Next", "apple", False),
        ]
        line = _stored(guild)
        assert line is not None and line.current is not None
        assert line.current.platform == "apple"
        assert [item.track.title for item in line.later] == ["Next"]
    finally:
        await stop_track(guild)

async def test_a_song_added_after_a_restart_stays_on_the_end(monkeypatch):
    import music.playback as playback

    guild = MagicMock()
    guild.id = 80
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    player = _Player(channel)
    channel.connect = AsyncMock(return_value=player)

    async def play(track):
        player._track = track

    player.play.side_effect = play
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel

    def named(title: str):
        track = _named(title)
        track.uri = f"https://example.com/{title.casefold()}"
        return track

    async def search(_guild, query, platform="youtube", mode=None):
        track = named(query.rsplit("/", 1)[-1].title())
        return Found(query, platform, [track], True)

    monkeypatch.setattr("music.playback.find_songs", search)
    one, two, three = named("One"), named("Two"), named("Three")
    try:
        assert await queue_tracks(member, guild, [one, two, three]) == "Playing One."
        guild.voice_client = player
        assert await next_track(guild) is None
        release(guild)
        plays = player.play.await_count
        connects = channel.connect.await_count

        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        assert await queue_tracks(member, guild, [named("Four")]) == "Playing Two."
        assert player.play.await_count == plays + 1
        assert channel.connect.await_count == connects + 1
        guild.voice_client = player
        line = _stored(guild)
        assert line is not None and line.current is not None
        assert line.current.track.title == "Two"
        assert [song.track.title for song in line.later] == ["Three", "Four"]

        assert await queue_tracks(member, guild, [named("Five"), named("Six")]) == "Added 2 songs."
        assert player.play.await_count == plays + 1
        assert [song.track.title for song in _stored(guild).later] == [
            "Three", "Four", "Five", "Six",
        ]

        guild.voice_client = None
        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        rows = queued(guild)
        assert [row.title for row in rows] == ["Two", "Three", "Four", "Five", "Six"]
        assert [row.playing for row in rows] == [False, False, False, False, False]
        assert [row.title for row in played(guild)] == ["One"]
    finally:
        await stop_track(guild)
