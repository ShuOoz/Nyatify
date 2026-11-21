"""The Lavalink node and a search that does not join. No Discord login."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import wavelink

from music.lavalink import (
    IDLE_SECONDS,
    Found,
    Opened,
    connect_node,
    find_songs,
    node_is_ready,
    open_recordings,
)
from music.archive import SavedSong
from music.source import MATCHES

from tests.music.support import _linked_player, _named, _ready_node

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
    monkeypatch.setattr("music.lavalink.settings.nodelink_uri", None)
    monkeypatch.setattr("music.lavalink.settings.nodelink_password", None)
    await connect_node(MagicMock())
    assert created["inactive_player_timeout"] == IDLE_SECONDS == 300
    assert created["inactive_channel_tokens"] is None
    assert created["retries"] == 0
    assert created["identifier"] == "lavalink"


async def test_netease_connects_a_second_node(monkeypatch):
    names = []

    class Node:
        def __init__(self, **kwargs):
            names.append(kwargs["identifier"])
            self._retries = 0
            self.session_id = "session"
            self.status = type("Status", (), {"name": "CONNECTED"})()

    async def connect(**kwargs):
        return None

    monkeypatch.setattr("music.lavalink.wavelink.Node", Node)
    monkeypatch.setattr("music.lavalink.wavelink.Pool.connect", connect)
    monkeypatch.setattr("music.lavalink.settings.lavalink_uri", "http://lavalink")
    monkeypatch.setattr("music.lavalink.settings.lavalink_password", "secret")
    monkeypatch.setattr("music.lavalink.settings.nodelink_uri", "http://nodelink")
    monkeypatch.setattr("music.lavalink.settings.nodelink_password", "netease")
    await connect_node(MagicMock())
    assert names == ["lavalink", "nodelink"]


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
    monkeypatch.setattr("music.lavalink.settings.nodelink_uri", None)
    monkeypatch.setattr("music.lavalink.settings.nodelink_password", None)
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

async def test_a_rate_limit_is_tried_again(monkeypatch):
    """A 429 is the node, not the song. Another try can open it."""
    songs = [_named("Kept", "Artist")]
    limited = wavelink.NodeException("slow", status=429)
    found = AsyncMock(side_effect=[limited, songs])
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("music.lavalink.node_is_ready", lambda: True)
    monkeypatch.setattr("music.source.search_track", found)
    monkeypatch.setattr("music.lavalink.asyncio.sleep", sleep)
    guild = MagicMock()
    guild.id = 44

    looked = await find_songs(guild, "one wish", "spotify")
    assert isinstance(looked, Found)
    assert looked.tracks == [songs[0]]
    assert found.await_count == 2
    assert slept == [3]

    found.side_effect = limited
    found.reset_mock()
    slept.clear()
    assert await find_songs(guild, "one wish", "spotify") == "Spotify did not answer."
    assert found.await_count == 3
    assert slept == [3, 3]

    found.side_effect = wavelink.NodeException("down", status=500)
    found.reset_mock()
    slept.clear()
    assert await find_songs(guild, "one wish", "spotify") == "Spotify did not answer."
    assert found.await_count == 1
    assert slept == []

    album = "https://open.spotify.com/album/0ETFjACtuP2ADo6LFhL6HN"
    assert await find_songs(guild, album) == "Spotify does not support playlist mode."
    playlist = "https://open.spotify.com/playlist/abc"
    assert await find_songs(guild, playlist) == "Spotify does not support playlist mode."
    apple = "https://music.apple.com/us/album/dreaming-out-loud/id"
    assert await find_songs(guild, apple) == "Apple Music does not support playlist mode."
