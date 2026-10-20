"""Play, stop, and the player card. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import wavelink

from announce import ACCENT_COLOR
from cogs.osa_musify import OsaMusify
from music.commands.playlist import LiveLine
from music.commands.player import (
    PANEL_SECONDS,
    RADIO_MARK,
    REFRESH_SECONDS,
    MusicPlayer,
    format_clock,
    progress_bar,
    progress_line,
    progress_text,
    song_line,
)
from music.commands.play import (
    MARK_ADD,
    MARK_ANY,
    MARK_BACK,
    LABEL_PLAYER,
    LABEL_PLAYLIST,
    MARK_CLOSE,
    MARK_SEARCH,
    MARK_NEXT,
    PAGE,
    EMPTY_SECONDS,
    GenreList,
    MatchList,
    page_line,
    song_row,
)
from music.lavalink import Found, GuildPlayer, find_songs, node_is_ready, offline_message
from music.playback import (
    Queued,
    advance_from_end,
    change_volume,
    lineup,
    next_track,
    now_playing,
    nudge,
    previous_track,
    queue_tracks,
    stop_track,
    suggest_next,
    _stored,
    toggle_pause,
)
from music.source import (
    GENRES,
    RADIOS,
    radio_lives,
    MATCHES,
    NAME_PLATFORM,
    normalize_platform,
    request_mode,
    search_track,
    take_tracks,
    track_query,
)


def test_track_query_keeps_names_and_supported_links():
    assert track_query("  ocean   drive ") == "ocean drive"
    assert track_query("") == ""
    assert track_query("   ") == ""
    link = "https://www.youtube.com/watch?v=abc"
    assert track_query(link) == link
    assert track_query("https://youtu.be/abc") == "https://youtu.be/abc"
    assert track_query("https://music.youtube.com/watch?v=abc") == "https://music.youtube.com/watch?v=abc"
    spotify = "https://open.spotify.com/track/abc"
    assert track_query(spotify) == spotify
    apple = "https://music.apple.com/us/song/never-gonna-give-you-up/1559885421"
    assert track_query(apple) == apple
    soundcloud = "https://soundcloud.com/rick-astley-official/never-gonna-give-you-up"
    assert track_query(soundcloud) == soundcloud
    mobile = "https://m.soundcloud.com/rick-astley-official/never-gonna-give-you-up"
    assert track_query(mobile) == "https://soundcloud.com/rick-astley-official/never-gonna-give-you-up"
    assert track_query("https://on.soundcloud.com/abc") == "https://on.soundcloud.com/abc"
    assert track_query("https://geo.music.apple.com/us/album/x/1") == "https://music.apple.com/us/album/x/1"
    assert track_query("https://y.music.163.com/m/song?id=1") == "https://music.163.com/m/song?id=1"
    assert track_query("open.spotify.com/track/abc") == "https://open.spotify.com/track/abc"
    netease = "https://music.163.com/song?id=186016"
    assert track_query(netease) == netease
    assert track_query("https://music.163.com/#/song?id=186016") == "https://music.163.com/#/song?id=186016"


def test_platform_names():
    assert normalize_platform(None) == "youtube"
    assert normalize_platform("Spotify") == "spotify"
    assert normalize_platform("apple") == "apple"
    assert normalize_platform("SoundCloud") == "soundcloud"
    assert normalize_platform("NetEase") == "netease"
    assert normalize_platform("deezer") == "youtube"


def test_a_link_keeps_its_shape():
    assert request_mode("one wish") == "name"
    assert request_mode("https://www.youtube.com/watch?v=abc") == "track"
    assert request_mode("https://www.youtube.com/watch?v=abc&list=PL1") == "track"
    assert request_mode("https://youtu.be/abc") == "track"
    assert request_mode("https://www.youtube.com/playlist?list=PL1") == "playlist"
    assert request_mode("https://open.spotify.com/playlist/abc") == "playlist"
    assert request_mode("https://music.apple.com/us/playlist/evening/pl.1") == "playlist"
    assert request_mode("https://soundcloud.com/user/sets/evening") == "playlist"
    assert request_mode("https://music.163.com/playlist?id=1") == "playlist"
    assert request_mode("https://music.163.com/#/playlist?id=1") == "playlist"
    assert request_mode("https://music.163.com/song?id=1") == "track"
    assert request_mode("https://open.spotify.com/album/abc") == "playlist"
    assert request_mode("https://music.apple.com/us/album/name/1") == "playlist"
    assert request_mode("https://music.163.com/album?id=1") == "playlist"
    assert request_mode("https://open.spotify.com/artist/abc") == "artist"
    assert request_mode("https://music.apple.com/us/artist/name/1") == "artist"
    assert ("Jazz", "jazz piano") in GENRES
    assert ("Rock", "classic rock") in GENRES
    assert all(len(phrase.split()) >= 2 for _, phrase in GENRES)
    assert all(phrase != label.casefold() for label, phrase in GENRES)


async def test_soundcloud_search_drops_previews(monkeypatch):
    preview = MagicMock(length=30_000, is_preview=False, title="Billie Jean")
    marked = MagicMock(length=248_000, is_preview=True, title="Snippet")
    full = MagicMock(length=248_000, is_preview=False, title="Beat It")
    found = AsyncMock(return_value=[preview, marked, full])
    monkeypatch.setattr("music.lavalink.node_is_ready", lambda: True)
    monkeypatch.setattr("music.source.search_track", found)
    guild = MagicMock()

    songs = await find_songs(guild, "michael jackson", "soundcloud")
    assert isinstance(songs, Found)
    assert songs.tracks == [full]

    youtube = await find_songs(guild, "michael jackson", "youtube")
    assert isinstance(youtube, Found)
    assert youtube.tracks == [preview, marked, full]

    found.return_value = [preview]
    direct = await find_songs(guild, "https://soundcloud.com/mj/billie-jean", "youtube")
    assert isinstance(direct, Found)
    assert direct.tracks == [preview]

    found.return_value = [marked]
    assert await find_songs(guild, "https://soundcloud.com/mj/snippet", "youtube") == (
        "Nothing on SoundCloud matched that."
    )


def test_take_tracks_follows_the_link_shape():
    songs = ["one", "two", "three", "four", "five", "six"]
    assert take_tracks(songs, "name") == songs
    assert take_tracks(["x"] * (MATCHES + 3), "name") == ["x"] * MATCHES
    assert take_tracks(songs, "track") == ["one"]
    assert take_tracks(["x"] * (MATCHES + 3), "artist") == ["x"] * MATCHES
    assert take_tracks(songs, "playlist") == songs
    playlist = type("Playlist", (), {"tracks": songs})()
    assert take_tracks(playlist, "playlist") == songs
    assert take_tracks(playlist, "name") == songs
    assert take_tracks(None, "name") == []


def test_commands_are_play_and_stop():
    names = {command.name for command in OsaMusify.music.walk_commands()}
    assert names == {"play", "player", "playlist", "stop"}
    assert OsaMusify.music.name == "music"
    shown = next(command for command in OsaMusify.music.walk_commands() if command.name == "player")
    assert shown.description == "Show the music player"
    listed = next(command for command in OsaMusify.music.walk_commands() if command.name == "playlist")
    assert listed.description == "Show the live line, or a saved playlist"
    listed_name = next(parameter for parameter in listed.parameters if parameter.name == "name")
    assert listed_name.required is False
    assert listed_name.description == "Leave this empty to see the live line"
    play = next(command for command in OsaMusify.music.walk_commands() if command.name == "play")
    assert play.description == "Play a song or a link. Leave it empty to pick a random song"
    assert [parameter.name for parameter in play.parameters] == ["search"]
    search = play.parameters[0]
    assert search.required is False
    assert search.description == "A song name or a link. Leave this empty to pick a random song"


async def test_search_uses_the_chosen_platform(monkeypatch):
    called = {}

    async def search(query, *, source=None):
        called["query"] = query
        called["source"] = source
        return []

    monkeypatch.setattr(wavelink.Playable, "search", search)
    assert await search_track("ocean drive") == []
    assert called == {"query": "ocean drive", "source": wavelink.TrackSource.YouTube}

    called.clear()
    assert await search_track("ocean drive", "spotify") == []
    assert called == {"query": "ocean drive", "source": "spsearch"}

    called.clear()
    assert await search_track("ocean drive", "apple") == []
    assert called == {"query": "ocean drive", "source": "amsearch"}

    called.clear()
    assert await search_track("ocean drive", "soundcloud") == []
    assert called == {"query": "ocean drive", "source": "scsearch"}

    called.clear()
    assert await search_track("晴天", "netease") == []
    assert called == {"query": "晴天", "source": "ntsearch"}

    called.clear()
    link = "https://open.spotify.com/track/abc"
    assert await search_track(link, "youtube") == []
    assert called == {"query": link, "source": None}


async def test_play_and_stop(monkeypatch):
    await case_play_refuses_a_missing_node_voice_or_other_site(monkeypatch)
    await case_play_joins_and_starts_the_first_result(monkeypatch)
    await case_a_failed_join_stays_in_the_channel()
    await case_stop_leaves_when_a_player_is_connected()
    await case_stop_says_when_nothing_is_playing()


def _ready_node() -> MagicMock:
    node = MagicMock()
    node.players = []
    node._inactive_channel_tokens = None
    node._inactive_player_timeout = None
    node.client = None
    node.session_id = "lavalink-session"
    node.status = type("Status", (), {"name": "CONNECTED"})()
    node._update_player = AsyncMock()
    return node


def _linked_player(node: MagicMock) -> GuildPlayer:
    player = GuildPlayer(MagicMock(), MagicMock(), nodes=[node])
    guild = MagicMock()
    guild.id = 31
    player._guild = guild
    player.disconnect = AsyncMock()
    return player


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
    await player._dispatch_voice_update()
    voice = node._update_player.await_args.kwargs["data"]["voice"]
    assert voice["endpoint"] == "example.discord.media"
    player.disconnect.assert_not_awaited()
    assert player._connection_event.is_set() is False


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


async def test_a_cleared_channel_before_the_link_does_not_drop_the_room():
    node = _ready_node()
    node._players = {}
    player = _linked_player(node)
    node._players[31] = player
    await player.on_voice_state_update({"channel_id": None, "session_id": "session"})
    player.disconnect.assert_not_awaited()
    assert node._players[31] is player
    assert player._connection_event.is_set() is False


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


class _Player(wavelink.Player):
    """A Player subclass that does not open a voice connection."""

    def __init__(self, channel) -> None:
        self.channel = channel
        self.play = AsyncMock()
        self.move_to = AsyncMock()
        self.pause = AsyncMock()
        self.stop = AsyncMock()
        self.disconnect = AsyncMock()
        self.seek = AsyncMock()
        self.set_volume = AsyncMock()
        self._track = None
        self._position = 0
        self._paused = False
        self._guild = None

    @property
    def guild(self):
        return self._guild

    @property
    def current(self):
        return self._track

    @property
    def position(self):
        return self._position

    @property
    def paused(self):
        return self._paused


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


def test_clock_and_song_line():
    assert REFRESH_SECONDS == 5
    assert format_clock(0) == "0:00"
    assert format_clock(148_000) == "2:28"
    assert format_clock(3_600_000) == "1:00:00"
    assert format_clock(-20) == "0:00"
    assert progress_text(0, 148_000) == "0:00 / 2:28"
    assert progress_text(0, 0) == "live"
    assert progress_line(0, 148_000) == f"0:00  {progress_bar(0, 148_000)}  2:28"
    assert progress_line(0, 0).startswith("live  ")
    assert progress_bar(0, 0) == progress_bar(50_000, 0)
    assert "━" not in progress_bar(0, 0)
    assert progress_bar(0, 148_000).startswith("●")
    assert progress_bar(148_000, 148_000).endswith("●")
    assert progress_bar(0, 148_000) != progress_bar(74_000, 148_000)
    assert song_line("I Ain't Worried", "OneRepublic", "https://example.com/song") == (
        "**[I Ain't Worried](https://example.com/song)**\nOneRepublic"
    )
    assert song_line("I Ain't Worried - OneRepublic", "OneRepublic", None) == (
        "**I Ain't Worried - OneRepublic**"
    )


def _card_text(view: MusicPlayer) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


async def _playing():
    track = MagicMock()
    track.title = "I Ain't Worried"
    track.author = "OneRepublic"
    track.uri = "https://example.com/song"
    track.artwork = "https://example.com/art.jpg"
    track.length = 148_000
    channel = MagicMock()
    channel.id = 19
    player = _Player(channel)
    channel.connect = AsyncMock(return_value=player)
    member = MagicMock()
    member.id = 17
    member.voice.channel = channel
    guild = MagicMock()
    guild.id = 31
    guild.voice_client = None
    await stop_track(guild)
    assert await queue_tracks(member, guild, [track]) == "Playing I Ain't Worried."
    guild.voice_client = player
    player._track = track
    player._position = 0
    player._paused = False
    return guild, player, track


async def test_pause_needs_a_song():
    assert await toggle_pause(None) == "Use this command in a server."
    guild = MagicMock()
    guild.voice_client = None
    assert await toggle_pause(guild) == "Nothing is playing."
    assert now_playing(guild) is None
    assert now_playing(None) is None


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

        track.artwork = None
        plain = MusicPlayer(guild)
        assert not any(isinstance(item, discord.ui.Thumbnail) for item in plain.walk_children())

        order = []

        async def defer():
            order.append("defer")

        async def flip(value):
            order.append("pause")
            player._paused = value

        player._position = 12_000
        player.pause.side_effect = flip
        interaction = _press(player)
        interaction.response.defer.side_effect = defer
        await view.pause.callback(interaction)
        assert order == ["defer", "pause"]
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


def _press(player):
    interaction = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.edit_message = AsyncMock()
    interaction.user.voice.channel = player.channel
    return interaction


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
        assert f"1. **I Ain't Worried - OneRepublic**\n{RADIO_MARK}" in line.body.content
        assert "2. After" in line.body.content
        assert line.body.content.count(RADIO_MARK) == 1

        assert await next_track(guild) is None
        assert now_playing(guild).title == "After"
        assert now_playing(guild).stream is False
        song = MusicPlayer(guild)
        assert RADIO_MARK not in _card_text(song)
        assert song.rewind.disabled is False
        assert song.skip.disabled is False

        track.is_stream = False
        track.length = 8 * 60 * 60 * 1000
        _stored(guild).current.lives = ("https://www.youtube.com/watch?v=long",)
        held = MusicPlayer(guild)
        held_text = _card_text(held)
        assert RADIO_MARK in held_text
        assert "━" not in held_text
        assert held.rewind.disabled is True
        assert held.skip.disabled is False
        held_line = LiveLine(guild, MagicMock())
        assert RADIO_MARK in held_line.body.content
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
        assert "1. Midnight City - M83" in lineup(guild)

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
        assert lineup(guild) == "Nothing is after this song."
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


def _ask():
    interaction = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.delete_original_response = AsyncMock()
    interaction.original_response = AsyncMock()
    interaction.send = AsyncMock()
    interaction.user = MagicMock()
    interaction.guild = MagicMock()
    interaction.guild.id = 31
    return interaction


def _action_rows(view):
    return [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]


def _button(view, face):
    for row in _action_rows(view):
        for child in row.children:
            if getattr(child, "label", None) == face or getattr(child, "emoji", None) == face:
                return child
    for item in view.walk_children():
        accessory = getattr(item, "accessory", None)
        if getattr(accessory, "label", None) == face or getattr(accessory, "emoji", None) == face:
            return accessory
    raise AssertionError(face)


def _sections(view):
    return [
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.Section) and getattr(item.accessory, "label", None) == MARK_ADD
    ]


def _page_text(view) -> str:
    for item in view.walk_children():
        if isinstance(item, discord.ui.TextDisplay) and item.content.startswith("Page "):
            return item.content
    return ""


def _defer_marks_done(interaction) -> None:
    async def defer():
        interaction.response.is_done.return_value = True

    interaction.response.defer.side_effect = defer


def _named(title: str, author: str = "") -> MagicMock:
    track = MagicMock()
    track.title = title
    track.author = author
    return track


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

    found.return_value = songs
    album = "https://geo.music.apple.com/us/album/evening/1"
    looked = await find_songs(guild, album, "youtube")
    assert isinstance(looked, Found)
    assert looked.link is True
    assert looked.platform == "apple"
    assert looked.tracks == songs
    found.assert_awaited_with("https://music.apple.com/us/album/evening/1", "apple")

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
    assert looked.tracks == songs

    async def closed(_url):
        return None

    monkeypatch.setattr("music.source.follow_redirect", closed)
    assert await find_songs(guild, "https://on.soundcloud.com/abc", "apple") == "That link did not open."


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
        text = lineup(guild)
        assert "1. Two" in text
        assert "2. Three" in text

        four, five = _named("Four"), _named("Five")
        assert await queue_tracks(member, guild, [four, five]) == "Added 2 songs."
        player.play.assert_awaited_once()
        assert "3. Four" in lineup(guild)
        assert "4. Five" in lineup(guild)
    finally:
        await stop_track(guild)


def test_song_row_names_the_artist_and_the_length():
    assert song_row(1, "sunny days", "wave to earth", 248_000) == (
        "**1\\.** sunny days\n-# \u2007\u2008\u00a0wave to earth • 4:08"
    )
    assert song_row(4, "Back To Me", "The Rose", 224_000) == (
        "**4\\.** Back To Me\n-# \u2007\u2008\u00a0The Rose • 3:44"
    )
    assert song_row(10, "Later", "Band", 0) == "**10\\.** Later\n-# \u2007\u2007\u2008\u00a0Band"
    assert song_row(3, "  ", "", 0) == "**3\\.** Unknown"
    assert song_row(2, "x" * 90, "A", 0).split("\n", 1)[0].endswith("…")
    assert song_row(3, "cherry flavoured love inside your heart", "wave to earth", 268_000).split("\n", 1)[0] == (
        "**3\\.** cherry flavoured love inside yo…"
    )
    wide = page_line(1, 2, 6)
    assert wide == "Page 1/2  |  6 Results"
    single = page_line(1, 1, 1)
    assert single == "Page 1/1  |  1 Result"
    assert "Initiated" not in wide and "Initiated" not in single


async def test_a_name_lists_songs_until_one_is_added(monkeypatch):
    first = _named("One Wish", "Ray J")
    first.length = 248_000
    second = _named("x" * 90, "Band")
    second.length = 65_000
    started = AsyncMock()
    view = MatchList("one wish", "apple", [first, second], started, 9)
    view.to_components()
    rows = _action_rows(view)
    title = view.children[0].children[0]
    assert isinstance(title, discord.ui.Section)
    assert title.children[0].content == "## Osaka Play"
    assert title.accessory.label == MARK_SEARCH
    rule = view.children[0].children[1]
    assert isinstance(rule, discord.ui.Separator)
    assert rule.visible is True
    assert rows[0].children[0] is view.platforms
    gap = view.children[0].children[3]
    assert isinstance(gap, discord.ui.Separator)
    assert gap.visible is False
    assert gap.spacing is discord.SeparatorSpacing.small
    assert view.platforms.placeholder == "Apple Music"
    assert [option.label for option in view.platforms.options] == [
        "Apple Music", "YouTube", "Spotify", "SoundCloud", "NetEase",
    ]
    assert [option.value for option in view.platforms.options] == [
        "apple", "youtube", "spotify", "soundcloud", "netease",
    ]
    assert [option.value for option in view.platforms.options if option.default] == ["apple"]
    sections = _sections(view)
    assert sections[0].children[0].content == song_row(1, "One Wish", "Ray J", 248_000)
    assert sections[0].accessory.label == MARK_ADD
    assert sections[1].children[0].content.split("\n", 1)[0].endswith("…")
    assert sections[1].children[0].content.endswith("Band • 1:05")
    assert _page_text(view) == page_line(1, 1, 2)
    assert [button.label for button in rows[-1].children] == [
        MARK_BACK, MARK_NEXT, LABEL_PLAYER, LABEL_PLAYLIST, MARK_CLOSE,
    ]
    assert [_button(view, MARK_BACK).disabled, _button(view, MARK_NEXT).disabled] == [True, True]

    calls = []

    async def search(guild, song, platform="youtube", mode=None):
        calls.append((song, platform, mode))
        if platform == "spotify":
            return "Nothing on Spotify matched that."
        return Found(song, platform, [_named("Other", "Band")], False)

    monkeypatch.setattr("music.commands.play.find_songs", search)
    queued = AsyncMock(return_value="Playing Other.")
    monkeypatch.setattr("music.commands.play.queue_tracks", queued)
    monkeypatch.setattr("music.commands.play.now_playing", lambda guild: None)

    press = _ask()
    _defer_marks_done(press)
    view.platforms._values = ["spotify"]
    await view.on_platform(press)
    assert calls == [("one wish", "spotify", None)]
    assert not _sections(view)
    assert any(
        isinstance(item, discord.ui.TextDisplay)
        and item.content == "Nothing on Spotify matched that."
        for item in view.walk_children()
    )
    press.edit_original_response.assert_awaited()

    press = _ask()
    _defer_marks_done(press)
    view.platforms._values = ["youtube"]
    await view.on_platform(press)
    assert _sections(view)[0].children[0].content == song_row(1, "Other", "Band", 0)
    chosen = next(option for option in view.platforms.options if option.default)
    assert chosen.label == "YouTube"

    async def opened(_interaction):
        assert view.is_finished() is False

    started.side_effect = opened
    press = _ask()
    await _sections(view)[0].accessory.callback(press)
    queued.assert_awaited_once()
    assert queued.await_args.args[2] == [view.tracks[0]]
    assert queued.await_args.args[3] == "youtube"
    started.assert_not_awaited()
    assert view.is_finished() is False
    assert "Playing Other" in press.send.await_args.args[0]

    press = _ask()
    await _button(view, LABEL_PLAYER).callback(press)
    started.assert_not_awaited()
    assert "Nothing is playing" in press.send.await_args.args[0]
    assert view.is_finished() is False

    monkeypatch.setattr("music.commands.play.now_playing", lambda guild: object())
    press = _ask()
    await _button(view, LABEL_PLAYER).callback(press)
    started.assert_awaited_once()
    assert view.is_finished() is False
    press.delete_original_response.assert_not_awaited()
    press.edit_original_response.assert_not_awaited()

    listed = AsyncMock()
    view.open_playlist = listed
    press = _ask()
    await _button(view, LABEL_PLAYLIST).callback(press)
    listed.assert_awaited_once()
    assert view.is_finished() is False
    press.delete_original_response.assert_not_awaited()

    fresh = MatchList("one wish", "apple", [first], AsyncMock(), 9)
    queued.return_value = "Join a voice channel first."
    press = _ask()
    await _sections(fresh)[0].accessory.callback(press)
    fresh.on_started.assert_not_awaited()
    assert "Join a voice channel first" in press.send.await_args.args[0]

    fresh._working = True
    press = _ask()
    await _sections(fresh)[0].accessory.callback(press)
    assert "Already working" in press.send.await_args.args[0]

    press = _ask()
    close = _button(fresh, MARK_CLOSE)
    await close.callback(press)
    assert fresh.is_finished()
    press.delete_original_response.assert_awaited_once()


async def test_adding_a_song_says_so_and_can_open_a_player(monkeypatch):
    queued = AsyncMock(return_value="Playing Ocean Drive.")
    monkeypatch.setattr("music.commands.play.queue_tracks", queued)
    opened = AsyncMock()
    view = MatchList("ocean", "apple", [_named("Ocean Drive")], AsyncMock(), 9, on_playing=opened)
    press = _ask()
    _defer_marks_done(press)
    await _sections(view)[0].accessory.callback(press)
    opened.assert_awaited_once()
    assert view.is_finished() is False
    assert "Playing Ocean Drive" in press.send.await_args.args[0]
    press.edit_original_response.assert_not_awaited()

    opened.reset_mock()
    queued.return_value = "Added Ocean Drive."
    press = _ask()
    _defer_marks_done(press)
    await _sections(view)[0].accessory.callback(press)
    opened.assert_not_awaited()
    assert "Added Ocean Drive" in press.send.await_args.args[0]

    cog = OsaMusify(MagicMock())
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)
    sent = MagicMock()
    press = _ask()
    press.response.is_done.return_value = True
    press.followup.send = AsyncMock(return_value=sent)
    await cog._open_beside(press)
    assert isinstance(press.followup.send.await_args.kwargs["view"], MusicPlayer)
    press.edit_original_response.assert_not_awaited()
    assert cog._panels

    press = _ask()
    press.response.is_done.return_value = True
    press.followup.send = AsyncMock()
    await cog._open_beside(press)
    press.followup.send.assert_not_awaited()


async def test_a_quick_second_platform_replaces_the_first(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def search(guild, song, platform="youtube", mode=None):
        calls.append(platform)
        if platform == "spotify":
            started.set()
            await release.wait()
            return "Nothing on Spotify matched that."
        return Found(song, platform, [_named("Other", "Band")], False)

    monkeypatch.setattr("music.commands.play.find_songs", search)
    view = MatchList("one wish", "apple", [_named("One Wish")], AsyncMock(), 9)
    first = _ask()
    _defer_marks_done(first)
    view.platforms._values = ["spotify"]
    pending = asyncio.create_task(view.on_platform(first))
    await started.wait()

    second = _ask()
    _defer_marks_done(second)
    view.platforms._values = ["youtube"]
    await view.on_platform(second)
    release.set()
    await pending

    assert calls == ["spotify", "youtube"]
    assert view.platform == "youtube"
    assert _sections(view)[0].children[0].content == song_row(1, "Other", "Band", 0)
    first.edit_original_response.assert_not_awaited()
    second.edit_original_response.assert_awaited()
    assert first.send.await_count == 0
    assert second.send.await_count == 0


async def test_search_pages_five_songs_at_a_time():
    tracks = [_named(f"Song {index}", "Artist") for index in range(6)]
    for track in tracks:
        track.length = 60_000
    view = MatchList("sunny", NAME_PLATFORM, tracks, AsyncMock(), 4)
    view.to_components()
    assert len(_sections(view)) == PAGE
    assert _page_text(view) == page_line(1, 2, 6)
    assert _sections(view)[0].children[0].content == song_row(1, "Song 0", "Artist", 60_000)

    press = _ask()
    await _button(view, MARK_NEXT).callback(press)
    assert len(_sections(view)) == 1
    assert _sections(view)[0].children[0].content == song_row(6, "Song 5", "Artist", 60_000)
    assert _page_text(view) == page_line(2, 2, 6)
    assert [_button(view, MARK_BACK).disabled, _button(view, MARK_NEXT).disabled] == [False, True]

    press = _ask()
    await _button(view, MARK_BACK).callback(press)
    assert _page_text(view).startswith("Page 1/2")

    press = _ask()
    await _button(view, MARK_NEXT).callback(press)
    assert _page_text(view).startswith("Page 2/2")


async def test_playlist_and_player_open_beside_the_search(monkeypatch):
    cog = OsaMusify(MagicMock())
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)
    monkeypatch.setattr("music.commands.play.now_playing", lambda guild: object())
    monkeypatch.setattr(
        "cogs.osa_musify.queued",
        lambda guild: [Queued("Alpha", "apple", None, True, False)],
    )
    view = MatchList(
        "one wish",
        "apple",
        [_named("One Wish")],
        cog._show_player,
        9,
        on_playlist=cog._show_line,
    )
    view.to_components()

    press = _ask()
    press.response.send_message = AsyncMock()
    press.original_response = AsyncMock()
    await _button(view, LABEL_PLAYER).callback(press)
    sent = press.response.send_message.await_args.kwargs["view"]
    assert isinstance(sent, MusicPlayer)
    press.edit_original_response.assert_not_awaited()
    press.delete_original_response.assert_not_awaited()
    assert view.is_finished() is False

    press = _ask()
    press.guild.voice_client = None
    press.response.send_message = AsyncMock()
    await _button(view, LABEL_PLAYLIST).callback(press)
    listed = press.response.send_message.await_args.kwargs["view"]
    assert isinstance(listed, LiveLine)
    press.edit_original_response.assert_not_awaited()
    press.delete_original_response.assert_not_awaited()
    assert view.is_finished() is False


async def test_a_genre_plays_one_random_match(monkeypatch):
    tracks = [_named("First"), _named("Second")]

    async def search(guild, song, platform="youtube", mode=None):
        assert song == "jazz piano"
        assert platform == "spotify"
        assert mode == "draw"
        return Found(song, "spotify", tracks, False)

    monkeypatch.setattr("music.commands.play.find_songs", search)
    queued = AsyncMock(return_value="Playing Second.")
    monkeypatch.setattr("music.commands.play.queue_tracks", queued)
    monkeypatch.setattr("music.commands.play.random.choice", lambda options: options[-1])
    started = AsyncMock()
    view = GenreList("Spotify", started)
    view.to_components()
    assert view.timeout == EMPTY_SECONDS
    assert view.rows.content == "**Pick a random song**"
    assert view.any.label == "꩜"
    assert [button.label for button in view.buttons] == [
        "Pop", "Jazz", "Rock", "R&B",
        "Lo-fi", "Classical", "Hip hop", "Electronic", "Ambient",
    ]
    assert {button.label: button.phrase for button in view.buttons} == dict(GENRES)
    parts = view.children[0].children
    assert isinstance(parts[0], discord.ui.Section)
    assert parts[0].children[0].content == "## Osaka Play"
    assert parts[0].accessory.label == MARK_SEARCH
    assert isinstance(parts[1], discord.ui.Separator)
    assert parts[1].visible is True
    assert parts[1].spacing is discord.SeparatorSpacing.small
    assert isinstance(parts[5], discord.ui.Separator)
    assert parts[5].visible is False
    assert parts[5].spacing is discord.SeparatorSpacing.small
    assert isinstance(parts[6], discord.ui.Separator)
    assert parts[6].visible is True
    assert parts[6].spacing is discord.SeparatorSpacing.small
    assert parts[7].content == "**Pick a radio station**"
    assert view.stations.placeholder == "Choose"
    assert [option.label for option in view.stations.options] == [label for label, _ in RADIOS]
    rows = [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]
    assert [len(row.children) for row in rows] == [5, 5, 1]
    assert [child.label for child in rows[0].children] == [MARK_ANY, "Pop", "Jazz", "Rock", "R&B"]
    assert [child.label for child in rows[1].children] == [
        "Lo-fi", "Classical", "Hip hop", "Electronic", "Ambient",
    ]

    message = MagicMock()
    message.delete = AsyncMock()
    view.message = message
    press = _ask()
    button = next(item for item in view.buttons if item.phrase == "jazz piano")
    await button.callback(press)
    message.delete.assert_not_awaited()
    queued.assert_awaited_once()
    assert queued.await_args.args[2] == [tracks[1]]
    started.assert_awaited_once()

    missed = GenreList("Spotify", started)
    missed.message = message
    message.delete.reset_mock()
    queued.reset_mock()
    started.reset_mock()
    queued.return_value = "Join a voice channel first."
    press = _ask()
    button = next(item for item in missed.buttons if item.phrase == "jazz piano")
    await button.callback(press)
    message.delete.assert_not_awaited()
    assert missed.is_finished() is False
    started.assert_not_awaited()
    assert "Join a voice channel first" in press.send.await_args.args[0]


async def test_the_any_button_picks_a_random_genre(monkeypatch):
    tracks = [_named("Anywhere")]

    async def search(guild, song, platform="youtube", mode=None):
        assert song == "classic rock"
        assert platform == "spotify"
        assert mode == "draw"
        return Found(song, "spotify", tracks, False)

    def choose(options):
        if options and isinstance(options[0], tuple):
            return ("Rock", "classic rock")
        return options[0]

    monkeypatch.setattr("music.commands.play.find_songs", search)
    monkeypatch.setattr("music.commands.play.random.choice", choose)
    queued = AsyncMock(return_value="Playing Anywhere.")
    monkeypatch.setattr("music.commands.play.queue_tracks", queued)
    started = AsyncMock()
    view = GenreList("spotify", started)
    await view.any.callback(_ask())
    queued.assert_awaited_once()
    assert queued.await_args.args[2] == [tracks[0]]
    started.assert_awaited_once()


async def test_a_radio_station_plays_a_long_video(monkeypatch):
    video = _named("Sweet Dreams")
    video.is_stream = False
    video.length = 8 * 60 * 60 * 1000
    links = radio_lives("lofi")
    assert [len(radio_lives(key)) for _, key in RADIOS] == [5, 5, 5, 5, 5]
    assert all("/watch?v=" in link for link in links)
    calls = []

    async def search(guild, song, platform="youtube", mode=None):
        calls.append(song)
        assert platform == "youtube"
        if song == links[0]:
            return "Nothing on YouTube matched that."
        return Found(song, platform, [video], True)

    monkeypatch.setattr("music.commands.play.find_songs", search)
    drawn = iter((0, 3, 4))
    monkeypatch.setattr("music.commands.play.random.randrange", lambda count: next(drawn))
    queued = AsyncMock(return_value="Playing Sweet Dreams.")
    monkeypatch.setattr("music.commands.play.queue_tracks", queued)
    started = AsyncMock()
    view = GenreList("youtube", started)
    view.to_components()
    view.stations._values = ["lofi"]
    press = _ask()
    await view.stations.callback(press)
    assert calls == [links[0], links[1]]
    queued.assert_awaited_once()
    assert queued.await_args.args[2] == [video]
    assert queued.await_args.args[3] == "youtube"
    assert queued.await_args.kwargs["lives"] == links
    assert queued.await_args.kwargs["live_at"] == 1
    assert queued.await_args.kwargs["station"] == "lofi"
    started.assert_awaited_once()

    calls.clear()
    queued.reset_mock()
    started.reset_mock()
    press = _ask()
    await view.stations.callback(press)
    assert calls == [links[3]]
    assert queued.await_args.kwargs["live_at"] == 3

    calls.clear()

    async def quiet(guild, song, platform="youtube", mode=None):
        calls.append(song)
        return "Nothing on YouTube matched that."

    monkeypatch.setattr("music.commands.play.find_songs", quiet)
    queued.reset_mock()
    started.reset_mock()
    message = MagicMock()
    message.delete = AsyncMock()
    view.message = message
    press = _ask()
    await view.stations.callback(press)
    assert calls == [links[4], *links[:4]]
    queued.assert_not_awaited()
    started.assert_not_awaited()
    message.delete.assert_not_awaited()
    assert "did not start" in press.send.await_args.args[0]


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


async def test_a_failed_refresh_keeps_the_buttons(monkeypatch):
    guild, _player, _track = await _playing()
    try:
        async def no_wait(_seconds):
            return None

        monkeypatch.setattr("music.commands.player.asyncio.sleep", no_wait)
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


async def test_a_kick_forgets_the_line_and_the_card(monkeypatch):
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("Queued")]) == "Added Queued."
        guild.voice_client = None
        home = MagicMock()
        home.id = 20
        fresh = _Player(home)

        async def play(song):
            fresh._track = song

        fresh.play.side_effect = play
        home.connect = AsyncMock(return_value=fresh)
        member.voice.channel = home
        assert await queue_tracks(member, guild, [_named("Fresh"), _named("Second")]) == "Playing Fresh."
        assert _stored(guild).current.track.title == "Fresh"
        assert [song.track.title for song in _stored(guild).later] == ["Second"]

        cog = OsaMusify(MagicMock())
        cog.bot.user.id = 50
        card = MusicPlayer(guild)
        message = MagicMock()
        message.delete = AsyncMock()
        card.message = message
        cog._panels.append(card)
        leaver = MagicMock()
        leaver.id = 50
        leaver.guild = guild
        before = MagicMock()
        before.channel = home
        after = MagicMock()
        after.channel = None
        await cog.on_voice_state_update(leaver, before, after)
        assert _stored(guild) is None
        message.delete.assert_awaited_once()
        assert card._closed is True

        guild.voice_client = None
        gone = MusicPlayer(guild)
        press = _press(player)
        press.user.voice = None
        press.response.defer = AsyncMock()
        press.delete_original_response = AsyncMock()
        await gone.on_close(press)
        press.delete_original_response.assert_awaited_once()
        player.stop.assert_not_awaited()
        assert gone._closed is True

        async def no_wait(_seconds):
            return None

        monkeypatch.setattr("music.commands.player.asyncio.sleep", no_wait)
        guild.voice_client = player
        lingering = MusicPlayer(guild)
        lingering.message = message
        message.delete.reset_mock()
        guild.voice_client = None
        lingering.follow(message)
        await lingering._task
        message.delete.assert_awaited()
        assert lingering._closed is True
    finally:
        guild.voice_client = None


async def test_a_finished_station_plays_the_queue(monkeypatch):
    guild, player, _track = await _playing()
    try:
        song = _stored(guild).current
        song.lives = (
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
        assert _stored(guild).current.lives == ()
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
        song.lives = links
        song.live_at = 0
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
        assert _stored(guild).current.live_at == 0
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
        assert _stored(guild).current.live_at == 1
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
        song.lives = five
        song.live_at = 4
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
        assert _stored(guild).current.live_at == 0
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


async def test_an_idle_music_card_is_deleted():
    message = MagicMock()
    message.delete = AsyncMock()

    genre = GenreList("spotify", AsyncMock())
    assert genre.timeout == EMPTY_SECONDS
    genre.message = message
    await genre.on_timeout()
    message.delete.assert_awaited_once()

    quiet = MagicMock()
    quiet.voice_client = None
    line = LiveLine(quiet, MagicMock())
    assert line.timeout == PANEL_SECONDS
    message.delete.reset_mock()
    line.message = message
    await line.on_timeout()
    message.delete.assert_awaited_once()

    playing, _player, _track = await _playing()
    try:
        card = MusicPlayer(playing)
        assert card.timeout is None
        card.message = message
        message.delete.reset_mock()
        await card.dismiss()
        message.delete.assert_awaited_once()
        assert card._closed is True
    finally:
        await stop_track(playing)

    kept = GenreList("spotify", AsyncMock())
    kept.message = message
    message.delete.reset_mock()
    kept.stop()
    message.delete.assert_not_awaited()


async def test_opening_search_removes_the_empty_card_and_the_reverse():
    cog = OsaMusify(MagicMock())
    genre_message = MagicMock()
    genre_message.delete = AsyncMock()
    search_message = MagicMock()
    search_message.delete = AsyncMock()

    opening = _person(9)
    opening.response.send_message = AsyncMock()
    opening.response.is_done.return_value = False
    opening.original_response = AsyncMock(return_value=genre_message)
    await cog.play.callback(cog, opening, None)
    genre = opening.response.send_message.await_args.kwargs["view"]
    assert isinstance(genre, GenreList)
    assert genre.timeout == EMPTY_SECONDS

    other = _person(10)
    other.response.is_done.return_value = True
    other.original_response = AsyncMock(return_value=search_message)
    await cog._show_matches(other, "one", "apple", [])
    genre_message.delete.assert_not_awaited()

    searching = _person(9)
    searching.response.is_done.return_value = True
    searching.original_response = AsyncMock(return_value=search_message)
    await cog._show_matches(searching, "one", "apple", [])
    genre_message.delete.assert_awaited_once()
    assert genre.is_finished() is True
    listed = searching.edit_original_response.await_args.kwargs["view"]
    assert isinstance(listed, MatchList)
    assert listed.timeout == PANEL_SECONDS
    assert listed.message is search_message

    again = _person(9)
    again.response.send_message = AsyncMock()
    again.response.is_done.return_value = False
    again.original_response = AsyncMock(return_value=genre_message)
    genre_message.delete.reset_mock()
    await cog.play.callback(cog, again, None)
    search_message.delete.assert_awaited_once()
    assert listed.is_finished() is True


def _person(user_id: int):
    interaction = _ask()
    interaction.user.id = user_id
    interaction.guild.id = 31
    return interaction


async def test_a_sent_card_keeps_its_message():
    cog = OsaMusify(MagicMock())
    interaction = _ask()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done.return_value = False
    await cog.play.callback(cog, interaction, None)
    view = interaction.response.send_message.await_args.kwargs["view"]
    assert isinstance(view, GenreList)
    assert view.message is interaction.original_response.return_value

    interaction = _ask()
    interaction.response.is_done.return_value = True
    await cog._show_matches(interaction, "one", "apple", [])
    listed = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(listed, MatchList)
    assert listed.message is interaction.original_response.return_value

    interaction = _ask()
    interaction.guild.voice_client = None
    interaction.response.send_message = AsyncMock()
    shown = LiveLine(interaction.guild, MagicMock())
    await cog._show_list(interaction, shown)
    assert shown.message is interaction.original_response.return_value


async def test_play_opens_genres_a_list_or_the_song(monkeypatch):
    cog = OsaMusify(MagicMock())
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)

    async def blank(song):
        interaction = _ask()
        interaction.response.send_message = AsyncMock()
        interaction.response.is_done.return_value = False
        await cog.play.callback(cog, interaction, song)
        view = interaction.response.send_message.await_args.kwargs["view"]
        assert isinstance(view, GenreList)
        assert view.platform == "spotify"

    search = AsyncMock()
    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    await blank(None)
    await blank("   ")
    search.assert_not_awaited()

    interaction = _ask()
    interaction.user.id = 9
    interaction.response.is_done.return_value = True
    interaction.edit_original_response = AsyncMock()
    track = _named("One Wish", "Ray J")
    search.return_value = Found("one wish", "apple", [track], False)
    queued = AsyncMock()
    monkeypatch.setattr("cogs.osa_musify.queue_tracks", queued)
    await cog.play.callback(cog, interaction, "one wish")
    search.assert_awaited_once_with(interaction.guild, "one wish", NAME_PLATFORM)
    view = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(view, MatchList)
    assert view.platform == "apple"
    assert view.requester_id == 9
    assert view.tracks == [track]
    queued.assert_not_awaited()

    interaction = _ask()
    interaction.response.is_done.return_value = True
    interaction.edit_original_response = AsyncMock()
    interaction.original_response = AsyncMock()
    link = _named("Ocean Drive")
    search.return_value = Found("https://open.spotify.com/track/abc", "spotify", [link], True)
    queued.return_value = "Playing Ocean Drive."
    await cog.play.callback(cog, interaction, "https://open.spotify.com/track/abc")
    queued.assert_awaited_once()
    assert queued.await_args.args[2] == [link]
    assert queued.await_args.args[3] == "spotify"
    shown = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(shown, MusicPlayer)

    interaction = _ask()
    interaction.response.is_done.return_value = True
    interaction.edit_original_response = AsyncMock()
    search.return_value = "Nothing on Apple Music matched that."
    await cog.play.callback(cog, interaction, "missing")
    empty = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(empty, MatchList)
    assert empty.tracks == []
    assert empty.platform == "apple"

    interaction = _ask()
    link = "https://music.apple.com/us/song/never-gonna-give-you-up/1"
    search.return_value = "Nothing on Apple Music matched that."
    await cog.play.callback(cog, interaction, link)
    assert "Nothing on Apple Music" in interaction.edit_original_response.await_args.kwargs["content"]

    interaction = _ask()
    search.return_value = "Apple Music did not answer."
    await cog.play.callback(cog, interaction, "missing")
    missed = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(missed, MatchList)
    assert missed.tracks == []
    assert missed.note == "Apple Music did not answer."

    interaction = _ask()
    interaction.response.is_done.return_value = True
    interaction.edit_original_response = AsyncMock()
    search.return_value = "Lavalink is not connected."
    await cog.play.callback(cog, interaction, "missing")
    assert "Lavalink is not connected" in interaction.edit_original_response.await_args.kwargs["content"]


async def test_the_empty_card_search_opens_the_song_list(monkeypatch):
    cog = OsaMusify(MagicMock())
    opening = _person(9)
    opening.response.send_message = AsyncMock()
    opening.response.is_done.return_value = False
    message = MagicMock()
    message.delete = AsyncMock()
    opening.original_response = AsyncMock(return_value=message)
    await cog.play.callback(cog, opening, None)
    genre = opening.response.send_message.await_args.kwargs["view"]
    assert isinstance(genre, GenreList)
    heading = genre.children[0].children[0]
    assert isinstance(heading, discord.ui.Section)
    assert heading.children[0].content == "## Osaka Play"
    assert heading.accessory.label == MARK_SEARCH

    busy = _ask()
    busy.response.send_modal = AsyncMock()
    genre._working = True
    await heading.accessory.callback(busy)
    assert "Already working" in busy.send.await_args.args[0]
    busy.response.send_modal.assert_not_awaited()
    genre._working = False

    press = _ask()
    press.response.send_modal = AsyncMock()
    await heading.accessory.callback(press)
    modal = press.response.send_modal.await_args.args[0]
    assert modal.title == "Search"
    assert modal.query.component.default is None

    track = _named("One Wish", "Ray J")
    found = AsyncMock(return_value=Found("one wish", "apple", [track], False))
    monkeypatch.setattr("cogs.osa_musify.find_songs", found)
    submit = _person(9)
    submit.response.is_done.return_value = True
    submit.original_response = AsyncMock(return_value=message)
    modal.query.component._value = "one wish"
    await modal.on_submit(submit)
    found.assert_awaited_once_with(submit.guild, "one wish", "apple")
    assert genre.is_finished() is True
    message.delete.assert_not_awaited()
    listed = submit.edit_original_response.await_args.kwargs["view"]
    assert isinstance(listed, MatchList)
    assert listed.query == "one wish"
    assert listed.platform == "apple"
    assert listed.tracks == [track]
    assert listed.requester_id == 9
    assert listed.message is message
    assert cog._browse[(31, 9)] is listed
    assert listed.children[0].children[0].accessory.label == MARK_SEARCH


async def test_search_again_keeps_the_platform(monkeypatch):
    calls = []

    async def search(guild, song, platform="youtube", mode=None):
        calls.append((song, platform, mode))
        return Found(song, platform, [_named("Ocean Drive")], False)

    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    cog = OsaMusify(MagicMock())
    message = MagicMock()
    message.delete = AsyncMock()
    view = MatchList(
        "one wish",
        "spotify",
        [_named("One Wish")],
        AsyncMock(),
        9,
        on_search=cog._search,
    )
    view.message = message
    cog._browse[(31, 9)] = view

    press = _ask()
    press.response.send_modal = AsyncMock()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    assert modal.title == "Search again"
    assert modal.query.component.default == "one wish"

    blank = _ask()
    modal.query.component._value = "   "
    await modal.on_submit(blank)
    assert view.is_finished() is False
    assert "Name an artist or a song" in blank.send.await_args.args[0]
    assert calls == []

    submit = _person(9)
    submit.response.is_done.return_value = True
    submit.original_response = AsyncMock(return_value=message)
    modal.query.component._value = "ocean drive"
    await modal.on_submit(submit)
    assert calls == [("ocean drive", "spotify", None)]
    assert view.is_finished() is True
    assert view._closed is True
    message.delete.assert_not_awaited()
    listed = submit.edit_original_response.await_args.kwargs["view"]
    assert isinstance(listed, MatchList)
    assert listed is not view
    assert listed.platform == "spotify"
    assert listed.query == "ocean drive"
    assert cog._browse[(31, 9)] is listed


async def test_a_failed_search_keeps_the_card(monkeypatch):
    async def search(guild, song, platform="youtube", mode=None):
        return "Nothing on YouTube matched that."

    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    cog = OsaMusify(MagicMock())
    message = MagicMock()
    message.id = 77
    message.delete = AsyncMock()
    view = MatchList(
        "one wish",
        "spotify",
        [_named("One Wish")],
        AsyncMock(),
        9,
        on_search=cog._search,
    )
    view.message = message
    cog._browse[(31, 9)] = view
    press = _ask()
    press.response.send_modal = AsyncMock()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]

    submit = _person(9)
    submit.response.is_done.return_value = True
    submit.original_response = AsyncMock(return_value=message)
    modal.query.component._value = "https://www.youtube.com/watch?v=abc"
    await modal.on_submit(submit)
    assert view.is_finished() is False
    assert view._closed is False
    assert view.tracks[0].title == "One Wish"
    message.delete.assert_not_awaited()
    assert "Nothing on YouTube" in submit.send.await_args.args[0]
    assert submit.edit_original_response.await_args.kwargs["view"] is view
    assert cog._browse[(31, 9)] is view


async def test_a_missed_name_still_opens_the_list(monkeypatch):
    async def search(guild, song, platform="youtube", mode=None):
        return "Nothing on Spotify matched that."

    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    cog = OsaMusify(MagicMock())
    message = MagicMock()
    message.id = 78
    message.delete = AsyncMock()
    view = MatchList(
        "one wish",
        "spotify",
        [_named("One Wish")],
        AsyncMock(),
        9,
        on_search=cog._search,
    )
    view.message = message
    cog._browse[(31, 9)] = view
    press = _ask()
    press.response.send_modal = AsyncMock()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    submit = _person(9)
    submit.response.is_done.return_value = True
    submit.original_response = AsyncMock(return_value=message)
    modal.query.component._value = "ocean"
    await modal.on_submit(submit)
    assert view.is_finished() is True
    message.delete.assert_not_awaited()
    listed = submit.edit_original_response.await_args.kwargs["view"]
    assert isinstance(listed, MatchList)
    assert listed.tracks == []
    assert listed.platform == "spotify"
    assert listed.note == "Nothing on Spotify matched that."
    assert cog._browse[(31, 9)] is listed


async def test_a_link_that_does_not_start_keeps_the_card(monkeypatch):
    track = _named("Ocean Drive")
    monkeypatch.setattr(
        "cogs.osa_musify.find_songs",
        AsyncMock(return_value=Found(
            "https://www.youtube.com/watch?v=abc", "youtube", [track], True,
        )),
    )
    monkeypatch.setattr(
        "cogs.osa_musify.queue_tracks",
        AsyncMock(return_value="Join a voice channel first."),
    )
    cog = OsaMusify(MagicMock())
    message = MagicMock()
    message.delete = AsyncMock()
    view = MatchList(
        "one wish",
        "apple",
        [_named("One Wish")],
        AsyncMock(),
        9,
        on_search=cog._search,
    )
    view.message = message
    press = _ask()
    press.response.send_modal = AsyncMock()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    submit = _person(9)
    submit.response.is_done.return_value = True
    submit.original_response = AsyncMock(return_value=message)
    modal.query.component._value = "https://www.youtube.com/watch?v=abc"
    await modal.on_submit(submit)
    assert view.is_finished() is False
    assert "Join a voice channel first" in submit.send.await_args.args[0]
    assert submit.edit_original_response.await_args.kwargs["view"] is view
    message.delete.assert_not_awaited()


async def test_a_closed_card_does_not_search_again(monkeypatch):
    search = AsyncMock()
    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    cog = OsaMusify(MagicMock())
    view = MatchList(
        "one wish",
        "apple",
        [_named("One Wish")],
        AsyncMock(),
        9,
        on_search=cog._search,
    )
    press = _ask()
    press.response.send_modal = AsyncMock()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    view.stop()
    submit = _ask()
    modal.query.component._value = "ocean"
    await modal.on_submit(submit)
    search.assert_not_awaited()
    assert view.is_finished() is True
    assert "That card has closed" in submit.send.await_args.args[0]
    submit.edit_original_response.assert_not_awaited()


async def test_a_long_search_still_opens_the_form():
    view = MatchList("y" * 5000, "apple", [_named("One")], AsyncMock(), 9)
    press = _ask()
    press.response.send_modal = AsyncMock()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    assert len(modal.query.component.default) == 4000
    assert modal.query.component.max_length == 4000


async def test_the_page_stays_while_a_search_is_in_flight():
    tracks = [_named(f"Song {index}") for index in range(6)]
    view = MatchList("sunny", "apple", tracks, AsyncMock(), 4)
    view._closed = True
    press = _ask()
    await _button(view, MARK_NEXT).callback(press)
    press.response.defer.assert_awaited()
    assert view.page == 0
