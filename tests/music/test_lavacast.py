"""Voice join and playback through Lavalink. No Discord login."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import wavelink

from music.lavacast import (
    GuildPlayer,
    _join,
    _play_track,
    _replaced,
    _use_node,
)
from music.playback import now_playing, queue_tracks, stop_track, warm_voice

from tests.music.support import _Player, _linked_player, _named, _ready_node

async def test_a_closed_voice_socket_asks_discord_for_another():
    import music.lavacast as cast

    cast._voice_renew.clear()
    player = MagicMock()
    player.guild.id = 7
    player.guild.change_voice_state = AsyncMock()
    player.channel = MagicMock()
    player._sent_voice = {"endpoint": "old"}
    await cast.renew_voice(player)
    assert player._sent_voice is None
    player.guild.change_voice_state.assert_awaited_once_with(
        channel=player.channel, self_mute=False, self_deaf=True,
    )
    player.guild.change_voice_state.reset_mock()
    player._sent_voice = {"endpoint": "old"}
    await cast.renew_voice(player)
    player.guild.change_voice_state.assert_not_awaited()
    assert player._sent_voice == {"endpoint": "old"}
    cast._voice_renew.clear()

async def test_the_voice_link():
    """The link goes out once, including when the session arrives last. A rejected link stays in the channel."""
    await case_the_voice_link_is_sent_when_the_session_arrives_last()
    await case_one_voice_link_goes_out_once()
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

    # The same room again is not a second link. A second link plays the song fast.
    node._update_player.reset_mock()
    await player.on_voice_state_update({"channel_id": "19", "session_id": "session"})
    node._update_player.assert_not_awaited()

async def case_one_voice_link_goes_out_once():
    """The state update and the server update can overlap. Lavalink still gets one link."""
    node = _ready_node()
    player = _linked_player(node)
    player._voice_state = {
        "voice": {
            "session_id": "session",
            "token": "voice-token",
            "endpoint": "wss://example.discord.media",
        },
        "channel_id": "19",
    }
    started = asyncio.Event()
    release = asyncio.Event()
    sent = 0

    async def update(*_args, **_kwargs):
        nonlocal sent
        sent += 1
        started.set()
        await release.wait()

    node._update_player = update
    first = asyncio.create_task(player._dispatch_voice_update())
    await started.wait()
    second = asyncio.create_task(player._dispatch_voice_update())
    await asyncio.sleep(0)
    release.set()
    await first
    await second
    assert sent == 1
    assert player._connection_event.is_set() is True

    player._voice_state["channel_id"] = "20"
    node._update_player = AsyncMock()
    await player._dispatch_voice_update()
    node._update_player.assert_awaited_once()
    assert node._update_player.await_args.kwargs["data"]["voice"]["channelId"] == "20"

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

async def test_a_resumed_song_starts_at_the_saved_spot():
    """A mid-song return is one play at that spot."""
    node = _ready_node()
    player = _linked_player(node)
    track = MagicMock(title="Paused")
    player.play = AsyncMock()
    assert await _play_track(player, track, start=12_000) is player
    player.play.assert_awaited_once_with(track, start=12_000, paused=False)

async def case_a_dead_session_rejoins_from_the_saved_channel_id():
    from music.lavacast import _play_track

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

def test_either_live_node_keeps_the_player(monkeypatch):
    """A reload's leftover node is replaced. Lavalink and NodeLink are not."""

    class Node:
        def __eq__(self, other):
            return self is other

        __hash__ = None

    java, nodelink, stale = Node(), Node(), Node()
    monkeypatch.setattr("music.lavalink._java_node", lambda: java)
    monkeypatch.setattr("music.lavalink._named_node", lambda _name: nodelink)

    def player(node):
        made = GuildPlayer.__new__(GuildPlayer)
        made._node = node
        return made

    assert _replaced(player(java)) is False
    assert _replaced(player(nodelink)) is False
    assert _replaced(player(stale)) is True

async def test_a_new_session_gets_the_voice_link():
    """The same room is one link. A replaced Lavalink session is a new player, so the link is sent again."""
    node = _ready_node()
    player = _linked_player(node)
    player._voice_state = {
        "voice": {"session_id": "session", "token": "voice-token", "endpoint": "example.discord.media"},
        "channel_id": "19",
    }
    await player._dispatch_voice_update()
    assert player._sent_session == "lavalink-session"
    node._update_player.reset_mock()
    await player._dispatch_voice_update()
    node._update_player.assert_not_awaited()
    node.session_id = "fresh"
    await player._dispatch_voice_update()
    node._update_player.assert_awaited_once()
    assert player._sent_session == "fresh"

async def test_starting_a_song_does_not_send_a_seek(monkeypatch):
    node = _ready_node()
    sent = {}

    async def update(guild_id, *, data, replace=False):
        sent["data"] = dict(data)
        sent["replace"] = replace

    node._update_player = update
    player = _linked_player(node)
    track = MagicMock()
    track.encoded = "abc"
    track.extras = {}
    track.source = "netease"
    track.uri = "https://music.163.com/song?id=1"

    async def play(song, **kwargs):
        await player.node._update_player(
            31,
            data={
                "track": {"encoded": song.encoded},
                "position": kwargs.get("start", 0),
                "endTime": None,
            },
            replace=True,
        )

    player.play = play
    monkeypatch.setattr("music.lavalink._use_node", AsyncMock())
    await _play_track(player, track)
    assert "endTime" not in sent["data"]
    assert "position" not in sent["data"]
    assert sent["replace"] is True
    await _play_track(player, track, start=12_000)
    assert sent["data"]["position"] == 12_000
    assert "endTime" not in sent["data"]
    assert node._update_player is update

async def test_a_netease_play_lifts_the_volume_once(monkeypatch):
    """The play update and the loudness lift stack. One hundred on the card plays at one hundred and fifty."""
    from music.lavacast import _play_track
    from music.lavalink import _lift_netease, _netease_level
    from wavelink.payloads import StatsEventCPU

    assert _netease_level(100) == 150
    assert _netease_level(40) == 60
    assert _netease_level(0) == 0
    assert _netease_level(1000) == 1000
    assert _netease_level(None) is None
    nodelink = StatsEventCPU({"cores": 1, "systemLoad": 0.2, "nodelinkLoad": 0.1})
    java = StatsEventCPU({"cores": 2, "systemLoad": 0.5, "lavalinkLoad": 0.3})
    assert nodelink.lavalink_load == 0.1
    assert java.lavalink_load == 0.3

    node = _ready_node()
    sent = {}

    async def update(guild_id, *, data, replace=False):
        sent["data"] = dict(data)
        sent["replace"] = replace

    node._update_player = update
    _lift_netease(node)
    lifted = node._update_player
    player = _linked_player(node)
    track = MagicMock()
    track.encoded = "abc"
    track.extras = {}
    track.source = "netease"
    track.uri = "https://music.163.com/song?id=1"

    async def play(song, **kwargs):
        await player.node._update_player(
            31,
            data={
                "track": {"encoded": song.encoded},
                "position": kwargs.get("start", 0),
                "endTime": None,
                "volume": 100,
            },
            replace=True,
        )

    player.play = play
    monkeypatch.setattr("music.lavalink._use_node", AsyncMock())
    await _play_track(player, track)
    assert sent["data"]["volume"] == 150
    assert "endTime" not in sent["data"]
    assert "position" not in sent["data"]
    assert node._update_player is lifted
    await lifted(31, data={"volume": 40, "paused": False}, replace=False)
    assert sent["data"] == {"volume": 60, "paused": False}
    assert sent["replace"] is False

async def test_a_failed_switch_opens_nodelink_again(monkeypatch):
    java = MagicMock()
    java.identifier = "lavalink"
    nodelink = MagicMock()
    nodelink.identifier = "nodelink"
    nodelink.session_id = "nl"
    nodelink.status = type("Status", (), {"name": "CONNECTED"})()
    monkeypatch.setattr("music.lavalink._java_node", lambda: java)
    monkeypatch.setattr("music.lavalink._named_node", lambda name: nodelink if name == "nodelink" else java)
    monkeypatch.setattr("music.lavalink.settings.nodelink_uri", "http://nodelink")
    monkeypatch.setattr("music.lavalink.settings.nodelink_password", "secret")
    monkeypatch.setattr("music.lavalink._ensure_node", AsyncMock(return_value=True))
    revived = []

    async def revive(node, client):
        revived.append(node)
        return True

    monkeypatch.setattr("music.lavalink._revive_node", revive)
    player = MagicMock()
    player.node = java
    player.client = MagicMock()
    player.connected = False
    player._dispatch_voice_update = AsyncMock()

    async def switch(wanted):
        player.node = wanted
        raise RuntimeError("Failed to switch voice_state.")

    player.switch_node = switch
    track = MagicMock()
    track.source = "netease"
    track.uri = "https://music.163.com/song?id=1"
    await _use_node(player, track)
    assert revived == [nodelink]
    player._dispatch_voice_update.assert_awaited_once()
