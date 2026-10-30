"""The Lavalink node, search, and the voice connection. No Discord login."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import wavelink

from music.lavalink import (
    IDLE_SECONDS,
    Found,
    GuildPlayer,
    Opened,
    _join,
    _play_track,
    connect_node,
    find_songs,
    node_is_ready,
    open_recordings,
)
from music.archive import SavedSong
from music.playback import now_playing, queue_tracks, warm_voice, stop_track
from music.source import MATCHES

from tests.music.support import _Player, _linked_player, _named, _ready_node

async def case_an_idle_room_is_five_minutes(monkeypatch):
    created = {}

    class Node:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self._retries = 0
            self.session_id = "session"
            self.status = type("Status", (), {"name": "CONNECTED"})()

    async def connect(**kwargs):
        return None

    monkeypatch.setattr("music.lavalink.wavelink.Node", Node)
    monkeypatch.setattr("music.lavalink.wavelink.Pool.connect", connect)
    monkeypatch.setattr("music.lavalink.settings.lavalink_uri", "http://lavalink")
    monkeypatch.setattr("music.lavalink.settings.lavalink_password", "secret")
    await connect_node(MagicMock())
    assert created["inactive_player_timeout"] == IDLE_SECONDS == 300
    assert created["inactive_channel_tokens"] is None
    assert created["retries"] == 0


async def test_the_session_comes_back(monkeypatch):
    """An idle room is five minutes. No session is offline. A slow one still reconnects, and a missing one is opened again."""
    with monkeypatch.context() as patch:
        await case_an_idle_room_is_five_minutes(patch)
    with monkeypatch.context() as patch:
        case_a_node_without_a_session_is_offline(patch)
    with monkeypatch.context() as patch:
        await case_a_slow_session_still_reconnects(patch)
    await case_a_missing_lavalink_session_is_opened_again()

def case_a_node_without_a_session_is_offline(monkeypatch):
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


async def case_a_slow_session_still_reconnects(monkeypatch):
    created = {}

    class Node:
        def __init__(self, **kwargs):
            created["node"] = self
            self._retries = kwargs["retries"]
            self.session_id = ""
            self.status = type("Status", (), {"name": "CONNECTING"})()

    async def connect(**kwargs):
        return None

    monkeypatch.setattr("music.lavalink.wavelink.Node", Node)
    monkeypatch.setattr("music.lavalink.wavelink.Pool.connect", connect)
    monkeypatch.setattr("music.lavalink.settings.lavalink_uri", "http://lavalink")
    monkeypatch.setattr("music.lavalink.settings.lavalink_password", "secret")
    monkeypatch.setattr("music.lavalink._SESSION_TRIES", 1)
    await connect_node(MagicMock())
    assert created["node"]._retries is None

    node = _ready_node()
    node._retries = 0
    player = _linked_player(node)
    assert await player._ensure_session() is True
    assert node._retries is None

async def case_a_missing_lavalink_session_is_opened_again():
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

async def test_a_broken_session_stays_this_one():
    """A failed reconnect keeps the players. A closed socket does not open another session."""
    await case_a_failed_reconnect_keeps_the_players()
    await case_a_closed_socket_does_not_open_another_session()

async def case_a_failed_reconnect_keeps_the_players():
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

async def case_a_closed_socket_does_not_open_another_session():
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

    looked = await find_songs(guild, "https://music.163.com/#/song?id=186016", "youtube")
    assert isinstance(looked, Found)
    assert looked.platform == "netease"
    assert looked.tracks == [songs[0]]
    found.assert_awaited_with("https://music.163.com/song?id=186016", "netease")

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
    from music.source import remembered_share
    assert remembered_share(songs[0]) == (
        "https://music.apple.com/us/album/animals/1533388849?i=1533388859"
    )

    many = [_named(str(index)) for index in range(MATCHES + 3)]
    found.return_value = many
    artist = "https://open.spotify.com/artist/abc"
    looked = await find_songs(guild, artist, "apple")
    assert isinstance(looked, Found)
    assert looked.link is False
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
    # A link already opened plays from that track. The miss is looked up again.
    before = len(calls)
    again = await open_recordings(guild, songs)
    assert isinstance(again, Opened)
    assert [(track.title, platform) for track, platform in again.tracks] == [
        ("Kept", "spotify"),
        ("Kept", "apple"),
    ]
    assert calls[before:] == [("https://missing.example/b", "youtube", "track")]
    search_fail = AsyncMock(return_value="Nothing on YouTube matched that.")
    monkeypatch.setattr("music.lavalink.find_songs", search_fail)
    missed = [
        SavedSong("A", "spotify", "https://open.spotify.com/fresh-a"),
        SavedSong("B", "youtube", "https://missing.example/fresh-b"),
        SavedSong("C", "apple", "https://music.apple.com/fresh-c"),
    ]
    assert await open_recordings(guild, missed) == "Nothing on YouTube matched that."

async def test_the_voice_link():
    """The link goes out when the session arrives last. A rejected link stays in the channel."""
    await case_the_voice_link_is_sent_when_the_session_arrives_last()
    await case_a_rejected_voice_link_stays_in_the_channel()

async def case_the_voice_link_is_sent_when_the_session_arrives_last():
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

async def case_a_rejected_voice_link_stays_in_the_channel():
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

async def test_the_room_stays_when_the_lookup_fails(monkeypatch):
    """A missing lookup, a cleared channel, and a player without a voice link all stay in the room."""
    await case_a_missing_channel_lookup_keeps_the_room()
    await case_a_cleared_channel_before_the_link_does_not_drop_the_room()
    with monkeypatch.context() as patch:
        await case_a_player_without_a_voice_link_connects_again(patch)

async def case_a_missing_channel_lookup_keeps_the_room():
    node = _ready_node()
    player = _linked_player(node)
    home = MagicMock()
    home.id = 19
    player.channel = home
    player.client.get_channel = MagicMock(return_value=None)
    await player.on_voice_state_update({"channel_id": "19", "session_id": "session"})
    assert player.channel is home

async def case_a_cleared_channel_before_the_link_does_not_drop_the_room():
    node = _ready_node()
    node._players = {}
    player = _linked_player(node)
    node._players[31] = player
    await player.on_voice_state_update({"channel_id": None, "session_id": "session"})
    player.disconnect.assert_not_awaited()
    assert node._players[31] is player
    assert player._connection_event.is_set() is False

async def case_a_player_without_a_voice_link_connects_again(monkeypatch):
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

async def case_a_player_left_by_reload_rejoins_for_the_next_song():
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

async def test_a_dead_session_rejoins():
    """A player left by reload rejoins, and a dead session rejoins before the song and from the saved channel."""
    await case_a_player_left_by_reload_rejoins_for_the_next_song()
    await case_a_dead_session_rejoins_before_the_song_starts()
    await case_a_dead_session_rejoins_from_the_saved_channel_id()

async def case_a_dead_session_rejoins_before_the_song_starts():
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


async def test_a_resumed_song_stays_quiet_until_it_opens(monkeypatch):
    """A mid-song return opens paused. Sound waits until Lavalink has the track."""
    monkeypatch.setattr("music.lavalink._SEEK_HOLD", 0)
    node = _ready_node()
    player = _linked_player(node)
    track = MagicMock(title="Paused")

    async def play(song, **kwargs):
        assert kwargs == {"start": 12_000, "paused": True}
        assert isinstance(player._resume_open, asyncio.Event)
        assert player._resume_open.is_set() is False
        await player._track_start(MagicMock())
        assert player._resume_open.is_set() is True

    player.play = play
    assert await _play_track(player, track, start=12_000) is player
    assert player._resume_open is None


async def case_a_dead_session_rejoins_from_the_saved_channel_id():
    from music.lavalink import _play_track

    node = _ready_node()
    player = _linked_player(node)
    channel = MagicMock()
    channel.id = 19
    player.channel = None
    player._voice_state = {"channel_id": "19", "voice": {}}
    player.guild.get_channel = MagicMock(return_value=channel)
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
    track = MagicMock(title="Ocean Drive")
    assert await _play_track(player, track) is fresh
    player.guild.get_channel.assert_called_once_with(19)
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

async def test_a_join_stays_one_connection(monkeypatch):
    """A warm join, a join already in flight, and play waiting on the voice link stay one connection. Cancelling it does not open a second."""
    await case_a_warm_join_is_the_same_connection()
    await case_a_join_in_flight_stays_in_that_room()
    await case_play_waits_until_the_voice_link_is_up()
    with monkeypatch.context() as patch:
        await case_a_cancelled_voice_connect_is_not_tried_again(patch)
    await case_a_cancelled_join_does_not_open_a_second_one()

async def case_a_warm_join_is_the_same_connection():
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

async def case_a_join_in_flight_stays_in_that_room():
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

async def case_play_waits_until_the_voice_link_is_up():
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

async def case_a_cancelled_voice_connect_is_not_tried_again(monkeypatch):
    calls = 0

    async def connect(self, *, timeout, reconnect, self_deaf=False, self_mute=False):
        nonlocal calls
        calls += 1
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise wavelink.ChannelTimeoutException("Voice link was cancelled.")

    monkeypatch.setattr(wavelink.Player, "connect", connect)
    player = _linked_player(_ready_node())
    task = asyncio.create_task(player.connect(timeout=9, reconnect=True))
    await asyncio.sleep(0)
    task.cancel()
    cancelled = False
    try:
        await task
    except asyncio.CancelledError:
        cancelled = True
    assert cancelled is True
    assert calls == 1


async def case_a_cancelled_join_does_not_open_a_second_one():
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
