"""Play a search result, queue songs, and stop. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import wavelink

from music.lavalink import Found, GuildPlayer, find_songs, node_is_ready, offline_message
from music.playback import (
    QUEUE_SONGS,
    advance_from_end,
    change_volume,
    load_line,
    next_track,
    play_now,
    now_playing,
    previous_track,
    queue_opened,
    queue_tracks,
    leave_if_idle,
    queued,
    release,
    warm_voice,
    stop_track,
    _stored,
)

from tests.music.support import _Player, _named, _playing, _room, _waiting

async def test_play_and_stop(monkeypatch):
    await case_play_refuses_a_missing_node_voice_or_other_site(monkeypatch)
    await case_play_joins_and_starts_the_first_result(monkeypatch)
    await case_a_failed_join_stays_in_the_channel()
    await case_stop_leaves_when_a_player_is_connected()
    await case_stop_says_when_nothing_is_playing()

async def case_play_refuses_a_missing_node_voice_or_other_site(monkeypatch):
    member = MagicMock()
    member.voice = None
    guild = MagicMock()
    guild.voice_client = None
    monkeypatch.setattr("music.lavalink.settings.lavalink_uri", None)
    monkeypatch.setattr("music.lavalink.settings.lavalink_password", None)
    assert node_is_ready() is False
    assert "LAVALINK_URI" in offline_message()
    assert await find_songs(guild, "ocean drive") == offline_message()
    assert await find_songs(None, "ocean drive") == "Use this command in a server."
    assert await find_songs(guild, "https://open.spotify.com/track/abc") == offline_message()
    assert await find_songs(guild, "https://on.soundcloud.com/abc") == "That link did not open."
    assert await find_songs(guild, "  ") == (
        "Say a song name, or paste an Apple Music, YouTube, Spotify, "
        "SoundCloud, or NetEase link."
    )

    monkeypatch.setattr("music.lavalink.node_is_ready", lambda: True)
    assert await queue_tracks(member, guild, [MagicMock(title="Ocean Drive")]) == (
        "Join a voice channel first."
    )

async def case_play_joins_and_starts_the_first_result(monkeypatch):
    track = MagicMock()
    track.title = "Ocean Drive"
    found = AsyncMock(return_value=[track, MagicMock()])
    monkeypatch.setattr("music.lavalink.node_is_ready", lambda: True)
    monkeypatch.setattr("music.source.search_track", found)

    channel = MagicMock()
    player = _Player(channel)
    channel.connect = AsyncMock(return_value=player)
    member = MagicMock()
    member.voice.channel = channel
    guild = MagicMock()
    guild.voice_client = None

    looked = await find_songs(guild, "ocean drive")
    assert isinstance(looked, Found)
    assert looked.link is False
    assert looked.tracks == found.return_value
    channel.connect.assert_not_awaited()
    assert await queue_tracks(member, guild, [track]) == "Playing Ocean Drive."
    found.assert_awaited_with("ocean drive", "youtube")
    channel.connect.assert_awaited_once_with(cls=GuildPlayer, self_deaf=True)
    player.play.assert_awaited_once_with(track)

    guild.voice_client = player
    other = MagicMock()
    other.id = 8
    channel.id = 19
    member.voice.channel = other
    player.move_to = AsyncMock()
    assert await queue_tracks(member, guild, [track], "spotify") == "Join the voice channel first."
    player.move_to.assert_not_awaited()
    player.play.assert_awaited_once()

    found.return_value = []
    link = "https://music.apple.com/us/song/never-gonna-give-you-up/1559885421"
    assert await find_songs(guild, link, "youtube") == "Nothing on Apple Music matched that."
    found.assert_awaited_with(link, "apple")

    found.return_value = []
    soundcloud = "https://soundcloud.com/rick-astley-official/never-gonna-give-you-up"
    assert await find_songs(guild, soundcloud, "youtube") == "Nothing on SoundCloud matched that."
    found.assert_awaited_with(soundcloud, "soundcloud")

    found.return_value = []
    netease = "https://music.163.com/song?id=186016"
    assert await find_songs(guild, netease, "youtube") == "Nothing on NetEase matched that."
    found.assert_awaited_with(netease, "netease")

async def case_a_failed_join_stays_in_the_channel():
    channel = MagicMock()
    leftover = MagicMock()
    leftover.disconnect = AsyncMock()
    guild = MagicMock()
    guild.voice_client = None

    async def connect(**kwargs):
        guild.voice_client = leftover
        raise wavelink.ChannelTimeoutException("timed out")

    channel.connect = connect
    member = MagicMock()
    member.voice.channel = channel
    track = MagicMock(title="Ocean Drive")
    assert await queue_tracks(member, guild, [track]) == "I could not join that voice channel."
    leftover.disconnect.assert_not_awaited()

    sitting = _Player(channel)
    channel.id = 19
    elsewhere = MagicMock()
    elsewhere.id = 8
    sitting.move_to = AsyncMock(side_effect=wavelink.ChannelTimeoutException("move"))
    guild.voice_client = sitting
    member.voice.channel = elsewhere
    assert await queue_tracks(member, guild, [track]) == "Join the voice channel first."
    sitting.move_to.assert_not_awaited()
    sitting.disconnect.assert_not_awaited()

async def case_stop_leaves_when_a_player_is_connected():
    player = MagicMock()
    player.stop = AsyncMock()
    player.disconnect = AsyncMock()
    guild = MagicMock()
    guild.voice_client = player
    assert await stop_track(guild) == "Stopped."
    player.stop.assert_awaited_once()
    player.disconnect.assert_awaited_once()

async def case_stop_says_when_nothing_is_playing():
    guild = MagicMock()
    guild.voice_client = None
    assert await stop_track(guild) == "Nothing is playing."
    assert await stop_track(None) == "Use this command in a server."

async def test_a_playlist_lines_up_every_song():
    guild = MagicMock()
    guild.id = 44
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
    try:
        assert await queue_tracks(member, guild, [one, two, three]) == "Playing One."
        player.play.assert_awaited_once_with(one)
        guild.voice_client = player
        assert now_playing(guild).title == "One"
        assert [row.title for row in queued(guild) if not row.playing] == ["Two", "Three"]

        four, five = _named("Four"), _named("Five")
        assert await queue_tracks(member, guild, [four, five]) == "Added 2 songs."
        player.play.assert_awaited_once()
        assert [row.title for row in queued(guild) if not row.playing] == [
            "Two", "Three", "Four", "Five",
        ]
    finally:
        await stop_track(guild)

async def test_the_queue_holds_fifteen_waiting_songs():
    guild = MagicMock()
    guild.id = 77
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
    tracks = [_named(str(index)) for index in range(QUEUE_SONGS + 3)]
    try:
        note = await queue_tracks(member, guild, tracks)
        assert note == "Playing 0. The queue is full."
        guild.voice_client = player
        assert _waiting(guild) == [str(index) for index in range(1, QUEUE_SONGS + 1)]
        assert await queue_tracks(member, guild, [_named("Extra")]) == "The queue is full."
        assert _waiting(guild) == [str(index) for index in range(1, QUEUE_SONGS + 1)]

        assert await next_track(guild) is None
        assert await queue_tracks(member, guild, [_named("A"), _named("B")]) == (
            "Added A. The queue is full."
        )
        assert _waiting(guild)[-1] == "A"
        assert "B" not in _waiting(guild)
        assert len(_waiting(guild)) == QUEUE_SONGS

        player._position = 0
        assert await previous_track(guild) is None
        assert now_playing(guild).title == "0"
        assert len(_waiting(guild)) == QUEUE_SONGS
        assert "A" not in _waiting(guild)

        fresh = [(_named(f"L{index}"), "youtube") for index in range(QUEUE_SONGS + 2)]
        assert await load_line(member, guild, fresh) == "Playing L0. The queue is full."
        assert now_playing(guild).title == "L0"
        assert _waiting(guild) == [f"L{index}" for index in range(1, QUEUE_SONGS + 1)]
    finally:
        await stop_track(guild)

async def test_a_playlist_queues_every_song_past_the_cap():
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
    count = QUEUE_SONGS + 3
    tracks = [(_named(str(index)), "youtube") for index in range(count)]
    try:
        note = await queue_opened(member, guild, tracks, whole=True)
        assert note == "Playing 0."
        guild.voice_client = player
        assert _waiting(guild) == [str(index) for index in range(1, count)]
        assert await queue_tracks(member, guild, [_named("Extra")]) == "The queue is full."
        assert _waiting(guild)[-1] == str(count - 1)
        more = [(_named("Later"), "spotify"), (_named("After"), "apple")]
        assert await queue_opened(member, guild, more, whole=True) == "Added 2 songs."
        assert _waiting(guild)[-2:] == ["Later", "After"]
    finally:
        await stop_track(guild)

async def test_previous_keeps_the_last_song_of_a_full_playlist():
    guild, member, player = _room(85)
    await stop_track(guild)
    count = QUEUE_SONGS + 2
    tracks = [(_named(str(index)), "youtube") for index in range(count)]
    try:
        note = await queue_opened(member, guild, tracks, whole=True)
        assert note == "Playing 0."
        guild.voice_client = player
        assert len(_waiting(guild)) == QUEUE_SONGS + 1
        assert await next_track(guild) is None
        assert now_playing(guild).title == "1"
        assert len(_waiting(guild)) == QUEUE_SONGS
        assert _waiting(guild)[-1] == str(count - 1)

        player._position = 0
        assert await previous_track(guild) is None
        assert now_playing(guild).title == "0"
        assert _waiting(guild)[0] == "1"
        assert _waiting(guild)[-1] == str(count - 1)
        assert len(_waiting(guild)) == QUEUE_SONGS + 1
    finally:
        await stop_track(guild)

async def test_play_stays_in_the_open_room():
    home = MagicMock()
    home.id = 19
    away = MagicMock()
    away.id = 8
    player = _Player(home)
    guild = MagicMock()
    guild.id = 31
    guild.voice_client = None
    await stop_track(guild)
    guild.voice_client = player
    player._track = _named("Already")
    member = MagicMock()
    member.id = 9
    member.voice.channel = home
    assert await queue_tracks(member, guild, [_named("Next")]) == "Added Next."
    player.move_to.assert_not_called()
    assert [song.track.title for song in _stored(guild).later] == ["Next"]

    member.voice.channel = away
    played = player.play.await_count
    assert await queue_tracks(member, guild, [_named("Nope")]) == "Join the voice channel first."
    assert player.play.await_count == played
    player.move_to.assert_not_called()
    assert [song.track.title for song in _stored(guild).later] == ["Next"]

    player._track = None
    assert await queue_tracks(member, guild, [_named("Nope")]) == "Join the voice channel first."
    player.move_to.assert_not_called()
    await stop_track(guild)

async def test_a_play_during_the_gap_lines_up():
    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        waiting = _named("Waiting")
        assert await queue_tracks(member, guild, [waiting]) == "Added Waiting."

        # Lavalink has cleared the track. The line still has the song that just ended.
        player._track = None
        played = player.play.await_count
        incoming = _named("Incoming")
        assert await queue_tracks(member, guild, [incoming]) == "Added Incoming."
        assert player.play.await_count == played
        assert _stored(guild).current.track is track
        assert [song.track.title for song in _stored(guild).later] == ["Waiting", "Incoming"]

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_play(song):
            started.set()
            await release.wait()
            player._track = song

        player.play.side_effect = slow_play
        player._guild = guild
        handoff = asyncio.create_task(
            advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        )
        await started.wait()
        later = asyncio.create_task(queue_tracks(member, guild, [_named("After")]))
        await asyncio.sleep(0)
        assert later.done() is False
        release.set()
        await handoff
        assert await later == "Added After."
        assert now_playing(guild).title == "Waiting"
        assert [song.track.title for song in _stored(guild).later] == ["Incoming", "After"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_play_now_keeps_a_parked_queue_and_its_volume(monkeypatch):
    import music.playback as playback

    guild = MagicMock()
    guild.id = 82
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
    try:
        assert await queue_tracks(member, guild, [named("One"), named("Two")]) == "Playing One."
        guild.voice_client = player
        assert await change_volume(guild, -60) is None
        release(guild)
        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        player.set_volume.reset_mock()
        assert await play_now(member, guild, named("Three"), "youtube") == "Playing Three."
        guild.voice_client = player
        line = _stored(guild)
        assert line is not None and line.current is not None
        assert line.current.track.title == "Three"
        assert [song.track.title for song in line.later] == ["One", "Two"]
        assert line.volume == 40
        player.set_volume.assert_awaited_with(40)
        assert now_playing(guild).volume == 40
        assert now_playing(guild).upcoming == "One"
    finally:
        await stop_track(guild)

async def test_a_replaced_song_turns_repeat_off():
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        _stored(guild).repeat = True

        assert await play_now(member, guild, _named("Echo"), "youtube") == "Playing Echo."
        assert _stored(guild).repeat is False
        assert [item.track.title for item in _stored(guild).later] == ["After"]

        _stored(guild).repeat = True
        one, two = _named("One"), _named("Two")
        assert await load_line(member, guild, [(one, "spotify"), (two, "youtube")]) == "Playing One."
        assert _stored(guild).repeat is False
        assert [row.title for row in queued(guild)] == ["One", "Two"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_an_early_join_leaves_only_when_nothing_started():
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
