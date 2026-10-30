"""The line kept for one server, including across a restart. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from music.lavalink import Found
from music.playback import (
    clear_played,
    clear_queued,
    leave_if_quiet,
    load_saved_lines,
    next_track,
    now_playing,
    played,
    queue_tracks,
    queued,
    release,
    save_open_lines,
    toggle_pause,
    unwatch_line,
    watch_line,
    stop_track,
    _stored,
)
from music.player.card import MusicPlayer

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
        assert [row.playing for row in rows] == [True, False]
        state = now_playing(guild)
        assert state is not None and state.title == "Two" and state.paused is False
        card = MusicPlayer(guild)
        assert "Two" in card.song.content
        assert "Nothing is playing" not in card.heading.content
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
        assert [row.title for row in played(guild)] == ["Two", "One"]
    finally:
        await stop_track(guild)

async def test_unloading_after_the_bot_leaves_keeps_the_queue():
    import music.playback as playback

    guild = MagicMock()
    guild.id = 87
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
    two.uri = "https://example.com/two"
    three.uri = "https://example.com/three"
    try:
        assert await queue_tracks(member, guild, [one, two, three]) == "Playing One."
        guild.voice_client = player
        assert await next_track(guild) is None
        release(guild)
        save_open_lines()

        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        rows = queued(guild)
        assert [row.title for row in rows] == ["Two", "Three"]
        assert [row.playing for row in rows] == [True, False]
        assert [row.title for row in played(guild)] == ["One"]
    finally:
        await stop_track(guild)


async def test_leaving_then_loading_puts_the_queue_back():
    """History stays after a leave. That must not hide the queue on the next load."""
    guild = MagicMock()
    guild.id = 88
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
    one, two, three = _named("One"), _named("Two"), _named("Three")
    one.uri = "https://example.com/one"
    two.uri = "https://example.com/two"
    three.uri = "https://example.com/three"
    try:
        assert await queue_tracks(member, guild, [one, two, three]) == "Playing One."
        guild.voice_client = player
        assert await next_track(guild) is None
        line = _stored(guild)
        line.volume = 40
        line.paused = True
        line.radio = True
        line.shuffle = True
        line.order = list(line.later)
        release(guild)
        assert _stored(guild) is None

        load_saved_lines()
        restored = _stored(guild)
        assert restored is not None and restored.resting is True
        assert restored.volume == 40 and restored.paused is True
        assert restored.radio is True and restored.shuffle is True
        assert [song.track.title for song in restored.later] == ["Three"]
        assert [song.track.title for song in restored.order] == ["Three"]
        assert restored.current is not None and restored.current.track.title == "Two"
        assert [row.title for row in played(guild)] == ["One"]
    finally:
        await stop_track(guild)


async def test_a_live_line_is_not_replaced_by_the_saved_one():
    import music.archive as archive

    guild = MagicMock()
    guild.id = 89
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
    song = _named("Live")
    song.uri = "https://example.com/live"
    try:
        assert await queue_tracks(member, guild, [song, _named("Next")]) == "Playing Live."
        guild.voice_client = player
        live = _stored(guild)
        live.resting = False
        archive.write_guild(89, {
            "current": {
                "title": "Stale",
                "url": "https://example.com/stale",
                "platform": "youtube",
            },
            "later": [],
            "history": [{
                "title": "Older",
                "url": "https://example.com/older",
                "platform": "youtube",
            }],
        })
        load_saved_lines()
        assert _stored(guild) is live
        assert live.resting is False
        assert live.current.track.title == "Live"
        assert [row.title for row in played(guild)] == ["Older"]
    finally:
        await stop_track(guild)


async def test_stopping_then_saving_does_not_bring_the_queue_back():
    import music.archive as archive
    import music.playback as playback

    guild = MagicMock()
    guild.id = 90
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
    one, two, three = _named("One"), _named("Two"), _named("Three")
    one.uri = "https://example.com/one"
    two.uri = "https://example.com/two"
    three.uri = "https://example.com/three"
    try:
        assert await queue_tracks(member, guild, [one, two, three]) == "Playing One."
        guild.voice_client = player
        assert await next_track(guild) is None
        await clear_queued(guild)
        assert [row.title for row in queued(guild) if not row.playing] == []
        await stop_track(guild)
        save_open_lines()

        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        load_saved_lines()
        saved = archive.read_guild(90)
        assert saved is not None
        assert saved["current"] is None and saved["later"] == []
        assert queued(guild) is None
        assert [row.title for row in played(guild)] == ["Two", "One"]
    finally:
        await stop_track(guild)


async def test_clearing_history_then_saving_keeps_the_queue():
    import music.playback as playback

    guild = MagicMock()
    guild.id = 91
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
    one, two = _named("One"), _named("Two")
    one.uri = "https://example.com/one"
    two.uri = "https://example.com/two"
    try:
        assert await queue_tracks(member, guild, [one, two]) == "Playing One."
        guild.voice_client = player
        assert await next_track(guild) is None
        await clear_played(guild)
        save_open_lines()

        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        load_saved_lines()
        assert [row.title for row in queued(guild)] == ["Two"]
        assert played(guild) == []
    finally:
        await stop_track(guild)


async def test_a_restart_still_shows_a_paused_song():
    import discord

    import music.playback as playback

    guild = MagicMock()
    guild.id = 86
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    channel.id = 19
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
    song.length = 200_000
    song.artwork = "https://example.com/art.jpg"
    try:
        assert await queue_tracks(member, guild, [song, _named("Next")]) == "Playing Slow Down."
        guild.voice_client = player
        player._track = song
        assert await toggle_pause(guild) is None
        assert player.paused is True
        release(guild)

        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        state = now_playing(guild)
        assert state is not None
        assert state.title == "Slow Down"
        assert state.paused is True
        assert state.has_track is True
        rows = queued(guild)
        assert [(row.title, row.playing) for row in rows] == [("Slow Down", True), ("Next", False)]

        card = MusicPlayer(guild)
        assert "Slow Down" in card.song.content
        assert "Nothing is playing" not in card.heading.content
        assert card.pause.style == discord.ButtonStyle.primary
        assert card.pause.disabled is False

        # The room is back, and the player has not been given the track yet.
        fresh = _Player(channel)
        guild.voice_client = fresh
        held = now_playing(guild)
        assert held is not None
        assert held.title == "Slow Down"
        assert held.paused is True
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
            ("Slow Down", "apple", True),
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
        assert [row.playing for row in rows] == [True, False, False, False, False]
        assert [row.title for row in played(guild)] == ["One"]
    finally:
        await stop_track(guild)


def _song(title: str):
    track = _named(title)
    track.uri = f"https://example.com/{title.casefold()}"
    track.length = 180_000
    return track


async def test_a_lost_current_song_keeps_the_next_one_on(monkeypatch):
    """The song on now can vanish. The queue stays, and someone in the channel still has a song."""
    from cogs.osa_musify import OsaMusify
    from music.playlist.card import now_text

    import music.playback as playback

    from tests.music.support import _ask

    guild = MagicMock()
    guild.id = 9402
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    channel.id = 19
    player = _Player(channel)

    async def connect(**_kwargs):
        guild.voice_client = player
        return player

    async def play(track, **_kwargs):
        player._track = track

    channel.connect = AsyncMock(side_effect=connect)
    player.play.side_effect = play
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel

    async def search(_guild, query, platform="youtube", mode=None):
        title = query.rsplit("/", 1)[-1].replace("-", " ").title()
        return Found(query, platform, [_song(title)], True)

    monkeypatch.setattr("music.playback.find_songs", search)
    card = None
    shown = None
    cog = None
    try:
        assert await queue_tracks(member, guild, [_song("One"), _song("Two"), _song("Three")]) == "Playing One."
        guild.voice_client = player
        # The song that was on is gone. The queue, and a pause from that song, stay.
        player._track = None
        held = _stored(guild)
        held.current = None
        held.resting = False
        held.paused = True
        held.spot = 40_000
        release(guild)
        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        # Someone is already in the channel. The bot is there too, with no track.
        guild.voice_client = player
        state = now_playing(guild)
        assert state is not None and state.title == "Two" and state.has_track is True
        assert state.paused is False and state.position == 0
        rows = queued(guild)
        assert [(row.title, row.playing) for row in rows] == [("Two", True), ("Three", False)]
        assert "Nothing is playing" not in now_text(rows[0])

        card = MusicPlayer(guild)
        assert "Two" in card.song.content
        assert "Nothing is playing" not in card.heading.content

        press = _ask()
        press.guild = guild
        press.user.voice.channel = channel
        press.response.is_done.return_value = False
        press.response.send_message = AsyncMock()
        cog = OsaMusify(MagicMock())
        await cog.player.callback(cog, press)
        shown = press.response.send_message.await_args.kwargs["view"]
        assert "Nothing is playing" not in shown.heading.content
        assert "Two" in shown.song.content

        assert await toggle_pause(guild, member) is None
        assert now_playing(guild).title == "Two"
        assert [song.track.title for song in _stored(guild).later] == ["Three"]
        assert await next_track(guild, member) is None
        assert now_playing(guild).title == "Three"
    finally:
        if card is not None:
            card.shutdown()
        if shown is not None:
            shown.shutdown()
        if cog is not None:
            for panel in list(cog._panels):
                panel.shutdown()
        guild.voice_client = None
        await stop_track(guild)


async def test_a_song_still_on_the_player_stays_ahead_of_the_queue():
    """A track that is actually on keeps its place. The queue does not step over it."""
    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 9
        member.voice.channel = player.channel
        waiting = _named("Next")
        waiting.uri = "https://example.com/next"
        assert await queue_tracks(member, guild, [waiting]) == "Added Next."
        line = _stored(guild)
        song = line.current
        line.later = [line.later[0], song]
        line.current = None
        line.resting = False
        state = now_playing(guild)
        assert state is not None and state.title == track.title and state.has_track is True
        assert line.current is song
        assert [item.track.title for item in line.later] == ["Next"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def _paused_with_next(guild_id: int):
    """A paused song with one waiting song, and a person in the voice room."""
    guild = MagicMock()
    guild.id = guild_id
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    channel.id = 19
    player = _Player(channel)
    player._guild = guild
    channel.connect = AsyncMock(return_value=player)

    async def play(track):
        player._track = track

    player.play.side_effect = play
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel
    song = _named("Slow Down", "Mac Ayres")
    song.uri = "https://example.com/slow"
    song.length = 200_000
    assert await queue_tracks(member, guild, [song, _named("Next")]) == "Playing Slow Down."
    guild.voice_client = player
    player._track = song
    assert await toggle_pause(guild) is None
    return guild, member, player


async def test_a_paused_song_stays_current_when_the_link_drops_without_a_release():
    """The bot is out, the line was never released. It is the saved song, not an empty player."""
    from music.playback import drop_queued

    guild, _member, _player = await _paused_with_next(9810)
    try:
        guild.voice_client = None
        state = now_playing(guild)
        assert state is not None and state.has_track is True
        assert state.title == "Slow Down" and state.paused is True
        assert [(row.title, row.playing) for row in queued(guild)] == [
            ("Slow Down", True),
            ("Next", False),
        ]
        # Removing a waiting song works with nobody in voice, and takes the right one.
        assert await drop_queued(guild, 1) is None
        assert [(row.title, row.playing) for row in queued(guild)] == [("Slow Down", True)]
    finally:
        guild.voice_client = None
        await stop_track(guild)


async def test_the_player_card_still_opens_for_a_paused_song_after_the_bot_leaves():
    from cogs.osa_musify import _on_now

    guild, _member, _player = await _paused_with_next(9811)
    try:
        guild.voice_client = None
        assert _on_now(guild) is True
        card = MusicPlayer(guild)
        assert "Slow Down" in card.song.content
        assert "Nothing is playing" not in card.heading.content
        # A card opened while the bot is out stays up. It never saw a room.
        assert card._left() is False
        # A card that did see the bot in voice goes once the bot is out.
        seen = MusicPlayer(guild)
        guild.voice_client = _player
        assert seen._left() is False
        guild.voice_client = None
        assert seen._left() is True
    finally:
        guild.voice_client = None
        await stop_track(guild)


async def test_a_pause_that_leaves_keeps_the_song_even_when_the_disconnect_fails():
    import music.playback as playback
    from music.playback import start

    guild, _member, player = await _paused_with_next(9812)
    try:
        async def disconnect():
            # Discord took the bot out, then Lavalink had nothing to answer.
            guild.voice_client = None
            raise RuntimeError("no session")

        player.disconnect.side_effect = disconnect
        start._pause_ready.add(9812)
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


async def test_a_disconnect_that_fails_while_still_in_voice_keeps_the_player():
    from music.playback import start

    guild, _member, player = await _paused_with_next(9813)
    try:
        player.disconnect.side_effect = RuntimeError("busy")
        start._pause_ready.add(9813)
        assert await leave_if_quiet(guild) is False
        assert guild.voice_client is player
        assert _stored(guild) is not None
    finally:
        player.disconnect.side_effect = None
        await stop_track(guild)


async def test_a_song_that_is_still_joining_is_not_called_saved():
    """While a join is in flight the line is left alone."""
    import music.lavalink as lavalink

    guild = MagicMock()
    guild.id = 9814
    guild.voice_client = None
    await stop_track(guild)
    import music.playback as playback
    from music.playback.remember import _Line, _Song

    song = _Song(_named("Old"), 9, "youtube")
    playback._lines[9814] = _Line(current=song, resting=False)
    lavalink._joins[9814] = (asyncio.ensure_future(asyncio.sleep(5)), MagicMock())
    try:
        now_playing(guild)
        assert playback._lines[9814].resting is False
    finally:
        pending = lavalink._joins.pop(9814)[0]
        pending.cancel()
        playback._lines.pop(9814, None)
