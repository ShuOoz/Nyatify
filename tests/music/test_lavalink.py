"""The Lavalink node, search, and the voice connection. No Discord login."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import wavelink

from music.lavalink import (
    Found,
    GuildPlayer,
    Opened,
    _join,
    find_songs,
    node_is_ready,
    open_recordings,
)
from music.archive import SavedSong
from music.playback import now_playing, queue_tracks, warm_voice, stop_track
from music.source import MATCHES

from tests.music.support import _Player, _linked_player, _named, _ready_node

def test_a_node_without_a_session_is_offline(monkeypatch):
    node = MagicMock()
    node.session_id = ""
    node.status = type("Status", (), {"name": "DISCONNECTED"})()
    node._retries = 0
    monkeypatch.setattr("music.lavalink._current_node", lambda: node)
    assert node_is_ready() is False
    assert node._retries == 0

    node.session_id = "lavalink-session"
    node.status = type("Status", (), {"name": "CONNECTED"})()
    assert node_is_ready() is True
    assert node._retries is None

async def test_a_missing_lavalink_session_is_opened_again():
    node = _ready_node()
    missing = wavelink.LavalinkException(
        data={
            "timestamp": 0,
            "status": 404,
            "error": "Not Found",
            "path": "/v4/sessions/old/players/31",
        }
    )
    node._update_player = AsyncMock(side_effect=[missing, None])
    player = _linked_player(node)
    player._voice_state = {
        "voice": {
            "session_id": "session",
            "token": "voice-token",
            "endpoint": "example.discord.media",
        },
        "channel_id": "19",
    }
    player._open_fresh_session = AsyncMock(return_value=True)
    await player._dispatch_voice_update()
    player._open_fresh_session.assert_awaited_once()
    assert node._update_player.await_count == 2
    assert player._connection_event.is_set() is True
    player.disconnect.assert_not_awaited()

async def test_a_failed_reconnect_keeps_the_players():
    from music.lavalink import _connect_once

    node = MagicMock()
    player = object()
    node._players = {31: player}
    node._retries = None

    async def connect(*, client):
        node._players = {}

    node._connect = connect
    assert await _connect_once(node, MagicMock()) is True
    assert node._players == {31: player}
    assert node._retries is None

    async def fail(*, client):
        node._players = {}
        raise wavelink.LavalinkException(
            data={
                "timestamp": 0,
                "status": 500,
                "error": "down",
                "path": "/v4/websocket",
            }
        )

    node._players = {31: player}
    node._connect = fail
    assert await _connect_once(node, MagicMock()) is False
    assert node._players == {31: player}

async def test_a_closed_socket_does_not_open_another_session():
    from music.lavalink import _drop_socket

    opened = 0
    started = asyncio.Event()

    async def connect():
        nonlocal opened
        opened += 1

    async def keep_alive():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            asyncio.create_task(socket.connect())
            raise

    socket = MagicMock()
    socket.connect = connect
    socket.keep_alive_task = asyncio.create_task(keep_alive())
    socket.socket = None
    node = MagicMock()
    node._websocket = socket
    await started.wait()
    await _drop_socket(node)
    await asyncio.sleep(0)
    assert opened == 0
    assert socket.keep_alive_task.done() is True

async def test_a_name_keeps_the_matches_and_a_link_keeps_its_shape(monkeypatch):
    songs = [_named(f"Song {index}", "Artist") for index in range(6)]
    found = AsyncMock(return_value=songs)
    monkeypatch.setattr("music.lavalink.node_is_ready", lambda: True)
    monkeypatch.setattr("music.source.search_track", found)
    guild = MagicMock()
    guild.id = 44

    looked = await find_songs(guild, "one wish", "netease")
    assert isinstance(looked, Found)
    assert looked.link is False
    assert looked.platform == "netease"
    assert looked.tracks == songs
    found.assert_awaited_with("one wish", "netease")

    looked = await find_songs(guild, "jazz piano", mode="draw")
    assert isinstance(looked, Found)
    assert looked.link is False
    assert looked.tracks == songs

    link = "https://www.youtube.com/watch?v=abc&list=PL1"
    found.return_value = songs
    looked = await find_songs(guild, link, "spotify")
    assert isinstance(looked, Found)
    assert looked.link is True
    assert looked.platform == "youtube"
    assert looked.tracks == [songs[0]]

    playlist = type("Playlist", (), {"tracks": list(songs)})()
    found.return_value = playlist
    looked = await find_songs(guild, "https://open.spotify.com/playlist/abc", "youtube")
    assert isinstance(looked, Found)
    assert looked.link is True
    assert looked.platform == "spotify"
    assert looked.tracks == songs
    assert looked.name is None

    named = type("Playlist", (), {"tracks": list(songs), "name": "  Evening Jazz  "})()
    found.return_value = named
    looked = await find_songs(guild, "https://music.apple.com/us/playlist/evening/pl.1", "youtube")
    assert isinstance(looked, Found)
    assert looked.platform == "apple"
    assert looked.name == "Evening Jazz"
    assert looked.tracks == songs

    found.return_value = songs
    album = "https://geo.music.apple.com/us/album/evening/1"
    looked = await find_songs(guild, album, "youtube")
    assert isinstance(looked, Found)
    assert looked.link is True
    assert looked.platform == "apple"
    assert looked.tracks == songs
    found.assert_awaited_with("https://music.apple.com/us/album/evening/1", "apple")

    shared = "https://geo.music.apple.com/us/album/animals/1533388849?i=1533388859&uo=4"
    found.return_value = songs
    looked = await find_songs(guild, shared, "youtube")
    assert isinstance(looked, Found)
    assert looked.link is True
    assert looked.platform == "apple"
    assert looked.tracks == [songs[0]]
    assert looked.name is None
    found.assert_awaited_with("https://music.apple.com/us/song/1533388859", "apple")

    many = [_named(str(index)) for index in range(MATCHES + 3)]
    found.return_value = many
    artist = "https://open.spotify.com/artist/abc"
    looked = await find_songs(guild, artist, "apple")
    assert isinstance(looked, Found)
    assert looked.link is True
    assert looked.platform == "spotify"
    assert looked.tracks == many[:MATCHES]

    async def follow(url):
        assert url == "https://on.soundcloud.com/abc"
        return "https://soundcloud.com/artist/song"

    monkeypatch.setattr("music.source.follow_redirect", follow)
    found.return_value = songs
    looked = await find_songs(guild, "on.soundcloud.com/abc", "apple")
    assert isinstance(looked, Found)
    assert looked.platform == "soundcloud"
    assert looked.query == "https://soundcloud.com/artist/song"
    assert looked.link is True
    assert looked.tracks == [songs[0]]

    async def closed(_url):
        return None

    monkeypatch.setattr("music.source.follow_redirect", closed)
    assert await find_songs(guild, "https://on.soundcloud.com/abc", "apple") == "That link did not open."

async def test_a_saved_link_opens_one_recording(monkeypatch):
    calls = []

    async def search(guild, song, platform="youtube", mode=None):
        calls.append((song, platform, mode))
        if "missing" in song:
            return "Nothing on YouTube matched that."
        return Found(song, platform, [_named("Kept")], True)

    monkeypatch.setattr("music.lavalink.find_songs", search)
    guild = MagicMock()
    guild.id = 44
    songs = [
        SavedSong("A", "spotify", "https://open.spotify.com/a"),
        SavedSong("B", "youtube", "https://missing.example/b"),
        SavedSong("C", "apple", "https://music.apple.com/c"),
    ]
    opened = await open_recordings(guild, songs)
    assert isinstance(opened, Opened)
    assert [(track.title, platform) for track, platform in opened.tracks] == [
        ("Kept", "spotify"),
        ("Kept", "apple"),
    ]
    assert opened.skipped == ["B"]
    assert calls == [
        ("https://open.spotify.com/a", "spotify", "track"),
        ("https://missing.example/b", "youtube", "track"),
        ("https://music.apple.com/c", "apple", "track"),
    ]
    assert await open_recordings(guild, [SavedSong("A", "youtube", "not-a-link")]) == (
        "That song has no link to play."
    )
    search_fail = AsyncMock(return_value="Nothing on YouTube matched that.")
    monkeypatch.setattr("music.lavalink.find_songs", search_fail)
    assert await open_recordings(guild, songs) == "Nothing on YouTube matched that."

async def test_the_voice_link_is_sent_when_the_session_arrives_last():
    node = _ready_node()
    player = _linked_player(node)

    await player.on_voice_server_update({"token": "voice-token", "endpoint": "example.discord.media"})
    node._update_player.assert_not_awaited()
    assert player._connection_event.is_set() is False

    await player.on_voice_state_update({"channel_id": "19", "session_id": "session"})
    node._update_player.assert_awaited_once()
    voice = node._update_player.await_args.kwargs["data"]["voice"]
    assert node._update_player.await_args.args[0] == 31
    assert voice["sessionId"] == "session"
    assert voice["token"] == "voice-token"
    assert voice["endpoint"] == "example.discord.media"
    assert voice["channelId"] == "19"
    assert player._connection_event.is_set() is True
    player.disconnect.assert_not_awaited()

async def test_a_rejected_voice_link_stays_in_the_channel():
    node = _ready_node()
    node._update_player = AsyncMock(
        side_effect=wavelink.LavalinkException(
            data={
                "timestamp": 0,
                "status": 400,
                "error": "Session not found",
                "path": "/v4/sessions/x/players/31",
            }
        )
    )
    player = _linked_player(node)
    player._voice_state = {
        "voice": {
            "session_id": "session",
            "token": "voice-token",
            "endpoint": "wss://example.discord.media",
        },
        "channel_id": "19",
    }
    player._connected = True
    await player._dispatch_voice_update()
    assert player._connected is False
    voice = node._update_player.await_args.kwargs["data"]["voice"]
    assert voice["endpoint"] == "example.discord.media"
    player.disconnect.assert_not_awaited()
    assert player._connection_event.is_set() is False

async def test_a_cleared_channel_before_the_link_does_not_drop_the_room():
    node = _ready_node()
    node._players = {}
    player = _linked_player(node)
    node._players[31] = player
    await player.on_voice_state_update({"channel_id": None, "session_id": "session"})
    player.disconnect.assert_not_awaited()
    assert node._players[31] is player
    assert player._connection_event.is_set() is False

async def test_a_player_without_a_voice_link_connects_again(monkeypatch):
    node = _ready_node()
    player = _linked_player(node)
    channel = MagicMock()
    channel.id = 19
    player.channel = channel
    player._connected = True
    player._connection_event.clear()
    player.connect = AsyncMock()
    guild = MagicMock()
    guild.id = 31
    guild.voice_client = player
    monkeypatch.setattr("music.lavalink._current_node", lambda: node)
    assert await _join(guild, channel) is player
    player.connect.assert_awaited_once()
    assert player.connect.await_args.kwargs["timeout"] == 24
    assert player.connect.await_args.kwargs["self_deaf"] is True

async def test_a_player_left_by_reload_rejoins_for_the_next_song():
    class OldPlayer(wavelink.Player):
        def __init__(self) -> None:
            self.disconnect = AsyncMock()
            self._current = object()

    OldPlayer.__module__ = "music.lavalink"
    channel = MagicMock()
    old = OldPlayer()
    old.channel = channel
    fresh = _Player(channel)
    channel.connect = AsyncMock(return_value=fresh)
    member = MagicMock()
    member.id = 4
    member.voice.channel = channel
    guild = MagicMock()
    guild.id = 9
    guild.voice_client = old
    track = MagicMock(title="Ocean Drive")
    assert await queue_tracks(member, guild, [track]) == "Playing Ocean Drive."
    old.disconnect.assert_awaited_once()
    channel.connect.assert_awaited_once_with(cls=GuildPlayer, self_deaf=True)
    fresh.play.assert_awaited_once_with(track)

async def test_a_dead_session_rejoins_before_the_song_starts():
    node = _ready_node()
    player = _linked_player(node)
    channel = MagicMock()
    player.channel = channel
    player._connected = True
    player._connection_event.set()
    player.disconnect = AsyncMock()
    missing = wavelink.LavalinkException(
        data={
            "timestamp": 0,
            "status": 404,
            "error": "Not Found",
            "path": "/v4/sessions/old/players/31",
        }
    )
    player.play = AsyncMock(side_effect=missing)
    fresh = _Player(channel)
    channel.connect = AsyncMock(return_value=fresh)
    member = MagicMock()
    member.id = 4
    member.voice.channel = channel
    guild = MagicMock()
    guild.id = 31
    guild.voice_client = player
    track = MagicMock(title="Ocean Drive")
    assert await queue_tracks(member, guild, [track]) == "Playing Ocean Drive."
    player.disconnect.assert_awaited_once()
    fresh.play.assert_awaited_once_with(track)

async def test_a_dead_session_keeps_the_volume_on_the_player_that_starts():
    import music.playback as playback
    from music.playback.remember import _Line

    node = _ready_node()
    player = _linked_player(node)
    channel = MagicMock()
    player.channel = channel
    player._connected = True
    player._connection_event.set()
    player.disconnect = AsyncMock()
    missing = wavelink.LavalinkException(
        data={
            "timestamp": 0,
            "status": 404,
            "error": "Not Found",
            "path": "/v4/sessions/old/players/406",
        }
    )
    player.play = AsyncMock(side_effect=missing)
    fresh = _Player(channel)

    async def play(song):
        fresh._track = song

    fresh.play.side_effect = play
    member = MagicMock()
    member.id = 4
    member.voice.channel = channel
    guild = MagicMock()
    guild.id = 406
    guild.voice_client = player

    async def connect(**_kwargs):
        guild.voice_client = fresh
        return fresh

    channel.connect = connect
    playback._lines[406] = _Line(volume=40)
    track = MagicMock(title="Ocean Drive")
    try:
        assert await queue_tracks(member, guild, [track]) == "Playing Ocean Drive."
        fresh.set_volume.assert_awaited_once_with(40)
        fresh.pause.assert_not_awaited()
        assert now_playing(guild).volume == 40
        assert now_playing(guild).title == "Ocean Drive"
    finally:
        await stop_track(guild)

async def test_a_warm_join_is_the_same_connection():
    channel = MagicMock()
    player = _Player(channel)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def connect(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return player

    channel.connect = connect
    member = MagicMock()
    member.voice.channel = channel
    guild = MagicMock()
    guild.id = 77
    guild.voice_client = None
    warm_voice(member, guild)
    try:
        await started.wait()
        pending = asyncio.create_task(queue_tracks(member, guild, [_named("One")]))
        await asyncio.sleep(0)
        assert calls == [{"cls": GuildPlayer, "self_deaf": True}]
        assert pending.done() is False
        release.set()
        assert await pending == "Playing One."
        assert len(calls) == 1
    finally:
        release.set()
        guild.voice_client = player
        await stop_track(guild)

async def test_a_join_in_flight_stays_in_that_room():
    home = MagicMock()
    home.id = 4
    away = MagicMock()
    away.id = 9
    player = _Player(home)
    started = asyncio.Event()
    release = asyncio.Event()

    async def connect(**kwargs):
        started.set()
        await release.wait()
        guild.voice_client = player
        return player

    home.connect = connect
    member = MagicMock()
    member.voice.channel = home
    other = MagicMock()
    other.id = 8
    other.voice.channel = away
    guild = MagicMock()
    guild.id = 91
    guild.voice_client = None
    warm_voice(member, guild)
    try:
        await started.wait()
        pending = asyncio.create_task(_join(guild, away))
        await asyncio.sleep(0)
        assert pending.done() is False
        release.set()
        assert await pending == "Join the voice channel first."
        player.move_to.assert_not_awaited()
        assert await queue_tracks(member, guild, [_named("One")]) == "Playing One."
        assert player.play.await_count == 1
    finally:
        release.set()
        guild.voice_client = player
        await stop_track(guild)

async def test_play_waits_until_the_voice_link_is_up():
    channel = MagicMock()
    channel.id = 4
    player = _Player(channel)
    started = asyncio.Event()
    release = asyncio.Event()

    async def connect(**kwargs):
        guild.voice_client = player
        started.set()
        await release.wait()
        return player

    channel.connect = connect
    member = MagicMock()
    member.id = 3
    member.voice.channel = channel
    guild = MagicMock()
    guild.id = 94
    guild.voice_client = None
    warm_voice(member, guild)
    try:
        await started.wait()
        pending = asyncio.create_task(queue_tracks(member, guild, [_named("One")]))
        await asyncio.sleep(0)
        player.play.assert_not_awaited()
        release.set()
        assert await pending == "Playing One."
        player.play.assert_awaited_once()
    finally:
        release.set()
        guild.voice_client = player
        await stop_track(guild)

async def test_a_cancelled_join_does_not_open_a_second_one():
    channel = MagicMock()
    channel.id = 4
    player = _Player(channel)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def connect(**kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        guild.voice_client = player
        return player

    channel.connect = connect
    guild = MagicMock()
    guild.id = 95
    guild.voice_client = None
    first = asyncio.create_task(_join(guild, channel))
    try:
        await started.wait()
        first.cancel()
        cancelled = False
        try:
            await first
        except asyncio.CancelledError:
            cancelled = True
        assert cancelled is True
        assert calls == 1
        second = asyncio.create_task(_join(guild, channel))
        await asyncio.sleep(0)
        assert calls == 1
        assert second.done() is False
        release.set()
        assert await second is player
    finally:
        release.set()
        pending = [task for task in asyncio.all_tasks() if task.get_name() == "music-join" and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
