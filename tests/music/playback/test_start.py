"""Play a search result, queue songs, and stop. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import wavelink

from music.lavalink import IDLE_SECONDS, Found, GuildPlayer, find_songs, node_is_ready, offline_message
from music.playback import (
    QUEUE_SONGS,
    advance_from_end,
    change_volume,
    load_line,
    next_track,
    play_now,
    played,
    now_playing,
    previous_track,
    queue_opened,
    queue_tracks,
    leave_if_idle,
    leave_if_quiet,
    queued,
    save_and_clear,
    shuffle_queue,
    warm_voice,
    stop_track,
    toggle_pause,
    _stored,
)

from tests.music.support import _Player, _named, _playing, _room, _waiting, _scene

async def test_play_and_stop(monkeypatch):
    """Play joins or refuses, stop leaves, and a blank title is Unknown. A song that does not start keeps the rest or leaves no queue."""
    with monkeypatch.context() as patch:
        await case_play_refuses_a_missing_node_voice_or_other_site(patch)
    with monkeypatch.context() as patch:
        await case_play_joins_and_starts_the_first_result(patch)
    await case_a_failed_join_stays_in_the_channel()
    await case_stop_leaves_when_a_player_is_connected()
    await case_stop_says_when_nothing_is_playing()
    await case_a_blank_title_is_unknown()
    await _scene(case_a_song_that_does_not_start)

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

async def test_the_line_holds_the_songs():
    """A playlist lines up every song. The waiting cap and previous stay inside it."""
    await _scene(case_a_playlist_lines_up_every_song)
    await _scene(case_the_queue_cap)
    await _scene(case_previous_and_the_waiting_cap)

async def case_a_playlist_lines_up_every_song():
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

async def case_the_queue_cap():
    """Fifteen songs wait. A playlist keeps the rest, and stays in order while shuffle is on."""
    await case_the_queue_holds_fifteen_waiting_songs()
    await case_a_playlist_queues_every_song_past_the_cap()
    await case_a_playlist_stays_in_order_while_shuffle_is_on()

async def case_the_queue_holds_fifteen_waiting_songs():
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

async def case_a_playlist_queues_every_song_past_the_cap():
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
        assert _waiting(guild) == [str(index) for index in range(1, QUEUE_SONGS + 1)]
        assert [song.track.title for song in _stored(guild).held] == [
            str(index) for index in range(QUEUE_SONGS + 1, count)
        ]
        assert await queue_tracks(member, guild, [_named("Extra")]) == "The queue is full."
        assert "Extra" not in _waiting(guild)
        assert await next_track(guild) is None
        assert _waiting(guild)[-1] == str(QUEUE_SONGS + 1)
        assert [song.track.title for song in _stored(guild).held] == [str(count - 1)]
    finally:
        await stop_track(guild)

async def case_a_playlist_stays_in_order_while_shuffle_is_on():
    guild, member, player = _room(86)
    await stop_track(guild)
    try:
        assert await queue_tracks(
            member, guild, [_named("Now"), _named("A"), _named("B"), _named("C")],
        ) == "Playing Now."
        guild.voice_client = player
        assert await shuffle_queue(guild) is None
        assert _stored(guild).shuffle is True
        note = await queue_opened(
            member, guild,
            [(_named("D"), "youtube"), (_named("E"), "spotify")],
            whole=True,
        )
        assert note == "Added 2 songs."
        assert _waiting(guild)[-2:] == ["D", "E"]
    finally:
        await stop_track(guild)

async def case_previous_and_the_waiting_cap():
    """Previous keeps a playlist's last song, stays inside that cap, and parks a song past the normal cap."""
    await case_previous_keeps_the_last_song_of_a_full_playlist()
    await case_previous_stays_inside_the_cap_after_a_playlist()
    await case_previous_drops_the_song_past_the_cap()

async def case_previous_keeps_the_last_song_of_a_full_playlist():
    guild, member, player = _room(85)
    await stop_track(guild)
    count = QUEUE_SONGS + 1
    tracks = [(_named(str(index)), "youtube") for index in range(count)]
    try:
        note = await queue_opened(member, guild, tracks, whole=True)
        assert note == "Playing 0."
        guild.voice_client = player
        assert len(_waiting(guild)) == QUEUE_SONGS
        assert _waiting(guild)[-1] == str(count - 1)
        assert await next_track(guild) is None
        assert now_playing(guild).title == "1"
        assert len(_waiting(guild)) == QUEUE_SONGS - 1
        assert _waiting(guild)[-1] == str(count - 1)

        player._position = 0
        assert await previous_track(guild) is None
        assert now_playing(guild).title == "0"
        assert _waiting(guild)[0] == "1"
        assert _waiting(guild)[-1] == str(count - 1)
        assert len(_waiting(guild)) == QUEUE_SONGS
    finally:
        await stop_track(guild)

async def case_previous_stays_inside_the_cap_after_a_playlist():
    """A playlist that fit used to turn the cap off. Previous keeps it."""
    guild, member, player = _room(851)
    await stop_track(guild)
    tracks = [(_named(str(index)), "youtube") for index in range(3)]
    try:
        assert await queue_opened(member, guild, tracks, whole=True) == "Playing 0."
        guild.voice_client = player
        assert _stored(guild).whole is True
        assert await next_track(guild) is None
        filler = [_named(f"S{index}") for index in range(QUEUE_SONGS - 1)]
        assert await queue_tracks(member, guild, filler) == f"Added {QUEUE_SONGS - 1} songs."
        assert len(_stored(guild).later) == QUEUE_SONGS
        assert _stored(guild).whole is False

        player._position = 0
        assert await previous_track(guild) is None
        assert len(_stored(guild).later) == QUEUE_SONGS
    finally:
        await stop_track(guild)


async def test_play_stays_in_the_right_room():
    """The open room, Discord's own voice state, and a state left by the last process."""
    await case_play_stays_in_the_open_room()
    await case_the_room_is_where_discord_has_the_bot()
    await case_a_restart_does_not_keep_the_old_room()

async def case_play_stays_in_the_open_room():
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

async def case_the_room_is_where_discord_has_the_bot():
    """A stale player channel is not the room. The bot's own voice state is."""
    from music.playback import member_in_voice

    home = MagicMock()
    home.id = 19
    away = MagicMock()
    away.id = 8
    player = _Player(away)
    guild = MagicMock()
    guild.id = 9102
    guild.voice_client = None
    await stop_track(guild)
    me = MagicMock()
    me.id = 4
    me.voice.channel = home
    guild.me = me
    guild.voice_client = player
    member = MagicMock()
    member.id = 9
    member.voice.channel = home
    try:
        # The player object still names the old room. Discord has the bot in this one.
        assert member_in_voice(member, guild) is True
        assert await queue_tracks(member, guild, [_named("Next")]) == "Playing Next."

        # The channel object was cleared. The last voice update still names the room.
        player.channel = None
        me.voice = None
        player._voice_state = {"channel_id": "19"}
        assert member_in_voice(member, guild) is True
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."

        # Discord really has the bot somewhere else. That room stays closed.
        me.voice = MagicMock()
        me.voice.channel = away
        assert await queue_tracks(member, guild, [_named("Nope")]) == "Join the voice channel first."

        # Discord dropped the channel object. The link is still up in this room.
        me.voice = None
        player.channel = home
        player._connected = True
        player._voice_state = {}
        member.voice.channel = home
        assert member_in_voice(member, guild) is True
        assert await queue_tracks(member, guild, [_named("Still")]) == "Added Still."
    finally:
        await stop_track(guild)

async def case_a_restart_does_not_keep_the_old_room():
    """A voice state with no player was left by the last process. It is not a room."""
    home = MagicMock()
    home.id = 19
    away = MagicMock()
    away.id = 8
    guild = MagicMock()
    guild.id = 9103
    guild.voice_client = None
    await stop_track(guild)
    me = MagicMock()
    me.id = 4
    me.voice.channel = away
    guild.me = me
    player = _Player(home)
    home.connect = AsyncMock(return_value=player)

    async def play(track):
        player._track = track

    player.play.side_effect = play
    member = MagicMock()
    member.id = 9
    member.voice = None
    voice = MagicMock()
    voice.channel = home
    guild._voice_states = {9: voice}
    try:
        assert await queue_tracks(member, guild, [_named("Next")]) == "Playing Next."
    finally:
        await stop_track(guild)

async def test_play_now_keeps_the_line(monkeypatch):
    """Adding while this song is on lines up behind it. Play now keeps a parked queue, its volume, and the mode."""
    await _scene(case_adding_while_this_song_is_still_on)
    await _scene(case_play_now_keeps_a_parked_queue_and_its_volume, monkeypatch=monkeypatch)
    await _scene(case_play_now_keeps_the_mode, monkeypatch=monkeypatch)

async def case_adding_while_this_song_is_still_on():
    """A reloaded player, and the gap before the next song, both line the new song up."""
    await case_adding_while_a_reloaded_player_is_still_on_the_song()
    await case_a_play_during_the_gap_lines_up()

async def case_adding_while_a_reloaded_player_is_still_on_the_song():
    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 9
        member.voice.channel = player.channel

        class OldPlayer(wavelink.Player):
            def __init__(self) -> None:
                self.channel = player.channel
                self.disconnect = AsyncMock()
                self.play = AsyncMock()
                self._track = track

            @property
            def current(self):
                return self._track

        OldPlayer.__module__ = "music.lavalink"
        old = OldPlayer()
        guild.voice_client = old
        assert await queue_tracks(member, guild, [_named("Extra")]) == "Added Extra."
        old.play.assert_not_awaited()
        old.disconnect.assert_not_awaited()
        assert _stored(guild).current.track is track
        assert [song.track.title for song in _stored(guild).later] == ["Extra"]
    finally:
        guild.voice_client = player
        await stop_track(guild)

async def case_a_play_during_the_gap_lines_up():
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

async def case_play_now_keeps_a_parked_queue_and_its_volume(monkeypatch):
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
        save_and_clear(guild)
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

async def case_play_now_keeps_the_mode(monkeypatch):
    """Play now keeps shuffle on, and a replaced song turns repeat off."""
    with monkeypatch.context() as patch:
        await case_play_now_keeps_shuffle_on(patch)
    await case_a_replaced_song_turns_repeat_off()

async def case_play_now_keeps_shuffle_on(monkeypatch):
    """Starting one song now leaves the mixed queue mixed."""
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("A"), _named("B"), _named("C")]) == "Added 3 songs."
        monkeypatch.setattr("music.playback.control.random.shuffle", lambda songs: songs.reverse())
        assert await shuffle_queue(guild) is None
        assert await play_now(member, guild, _named("Echo"), "youtube") == "Playing Echo."
        line = _stored(guild)
        assert line.shuffle is True
        assert now_playing(guild).title == "Echo"
        assert [song.track.title for song in line.later] == ["C", "B", "A"]
        assert [song.track.title for song in line.order] == ["A", "B", "C"]

        assert await load_line(member, guild, [(_named("One"), "youtube")]) == "Playing One."
        assert _stored(guild).shuffle is False
        assert _stored(guild).order == []
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_a_replaced_song_turns_repeat_off():
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

async def test_the_bot_leaves_and_keeps_the_line(monkeypatch):
    """An early join leaves only when nothing started. Five quiet or paused minutes leave and keep the line. A failed resume stays put."""
    await _scene(case_an_early_join_leaves_only_when_nothing_started)
    await _scene(case_five_minutes_leave_and_keep_the_line, monkeypatch=monkeypatch)
    await _scene(case_a_paused_leave_keeps_this_song_when_another_is_added, monkeypatch=monkeypatch)
    await _scene(case_a_failed_resume_leaves_the_paused_song_where_it_was)

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

        monkeypatch.setattr("music.playback.start._wait", no_wait)
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


async def case_a_paused_leave_keeps_this_song_when_another_is_added(monkeypatch):
    """The bot left on a pause. A new song waits. Play continues this one."""
    import music.playback as playback

    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("Next")]) == "Added Next."
        player._position = 40_000
        assert await toggle_pause(guild) is None
        # The link is gone and the line is still the one pause wrote.
        guild.voice_client = None
        plays = player.play.await_count
        assert await queue_tracks(member, guild, [_named("Extra")]) == (
            "Added Extra. I Ain't Worried is still paused."
        )
        assert player.play.await_count == plays
        held = _stored(guild)
        assert held.current.track is track and held.paused is True and held.position == 40_000
        assert [song.track.title for song in held.later] == ["Next", "Extra"]

        save_and_clear(guild)
        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        live = _named("I Ain't Worried")
        live.uri = "https://example.com/song"
        live.length = 148_000

        async def search(_guild, query, _platform):
            assert query == "https://example.com/song"
            return Found(query, "youtube", [live], True)

        monkeypatch.setattr("music.playback.find_songs", search)
        assert await queue_tracks(member, guild, [_named("Later")]) == (
            "Added Later. I Ain't Worried is still paused."
        )
        assert player.play.await_count == plays
        state = now_playing(guild)
        assert state is not None and state.title == "I Ain't Worried"
        assert state.paused is True and state.position == 40_000
        assert [(row.title, row.playing) for row in queued(guild)] == [
            ("I Ain't Worried", True),
            ("Next", False),
            ("Extra", False),
            ("Later", False),
        ]

        async def connect(**_kwargs):
            guild.voice_client = player
            return player

        async def play(song, **kwargs):
            player._track = song
            player._position = kwargs.get("start", 0)

        channel = player.channel
        channel.connect = AsyncMock(side_effect=connect)
        player.play.side_effect = play
        assert await toggle_pause(guild, member) is None
        player.play.assert_awaited_with(live, start=40_000, paused=True)
        guild.voice_client = player
        assert now_playing(guild).paused is False
        assert now_playing(guild).position == 40_000
        assert [(row.title, row.playing) for row in queued(guild)] == [
            ("I Ain't Worried", True),
            ("Next", False),
            ("Extra", False),
            ("Later", False),
        ]
        assert [row.title for row in played(guild)] == []
    finally:
        guild.voice_client = None
        await stop_track(guild)


async def case_a_failed_resume_leaves_the_paused_song_where_it_was():
    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("Next")]) == "Added Next."
        player._position = 40_000
        assert await toggle_pause(guild) is None
        guild.voice_client = None
        player._track = None

        async def connect(**_kwargs):
            return player

        async def fail(_song, **_kwargs):
            raise wavelink.WavelinkException("no")

        player.channel.connect = AsyncMock(side_effect=connect)
        player.play.side_effect = fail
        assert await toggle_pause(guild, member) == "Something went wrong while changing the song."
        held = _stored(guild)
        assert held.current.track is track and held.paused is True and held.position == 40_000
        assert [song.track.title for song in held.later] == ["Next"]
        assert [(row.title, row.playing) for row in queued(guild)] == [
            ("I Ain't Worried", True),
            ("Next", False),
        ]
    finally:
        guild.voice_client = None
        await stop_track(guild)


async def case_a_song_that_does_not_start():
    """A dead first track keeps the rest. A link that never starts leaves no queue."""
    await case_a_dead_first_track_keeps_the_rest_of_the_link()
    await case_a_link_that_never_starts_leaves_no_queue()

async def case_a_dead_first_track_keeps_the_rest_of_the_link():
    """The first song of a link does not start. The next one does, and the rest wait."""
    guild = MagicMock()
    guild.id = 94
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    player = _Player(channel)
    channel.connect = AsyncMock(return_value=player)

    async def play(track):
        if track.title == "Dead":
            raise wavelink.WavelinkException("no")
        player._track = track

    player.play.side_effect = play
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel
    dead, live, after = _named("Dead"), _named("Live"), _named("After")
    try:
        assert await queue_tracks(member, guild, [dead, live, after]) == "Playing Live. Skipped Dead."
        guild.voice_client = player
        assert now_playing(guild).title == "Live"
        assert [song.track.title for song in _stored(guild).later] == ["After"]
    finally:
        await stop_track(guild)


async def case_a_link_that_never_starts_leaves_no_queue():
    guild = MagicMock()
    guild.id = 95
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    player = _Player(channel)
    channel.connect = AsyncMock(return_value=player)

    async def play(_track):
        raise wavelink.WavelinkException("no")

    player.play.side_effect = play
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel
    try:
        assert await queue_tracks(member, guild, [_named("Dead"), _named("Gone")]) == (
            "YouTube did not start that song."
        )
        assert _stored(guild) is None
    finally:
        await stop_track(guild)


async def case_previous_drops_the_song_past_the_cap():
    """Previous puts this song back on a full queue. The last waiting one plays when a slot opens."""
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        names = [str(index) for index in range(QUEUE_SONGS)]
        added = await queue_tracks(member, guild, [_named(name) for name in names])
        assert added == f"Added {QUEUE_SONGS} songs."
        assert await next_track(guild) is None
        assert await queue_tracks(member, guild, [_named("Tail")]) == "Added Tail."
        assert [song.track.title for song in _stored(guild).later][-1] == "Tail"
        assert len(_stored(guild).later) == QUEUE_SONGS

        player._position = 0
        assert await previous_track(guild) is None
        waiting = [song.track.title for song in _stored(guild).later]
        assert waiting[-1] != "Tail"
        assert len(waiting) == QUEUE_SONGS
        assert now_playing(guild).title == "I Ain't Worried"
        assert [song.track.title for song in _stored(guild).held] == ["Tail"]
        assert await next_track(guild) is None
        assert [song.track.title for song in _stored(guild).later][-1] == "Tail"
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_a_blank_title_is_unknown():
    """A song with no title is Unknown on the reply and on the player."""
    guild = MagicMock()
    guild.id = 96
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    player = _Player(channel)
    channel.connect = AsyncMock(return_value=player)
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel
    blank = _named("   ")
    try:
        assert await queue_tracks(member, guild, [blank]) == "Playing Unknown."
        guild.voice_client = player
        player._track = blank
        assert now_playing(guild).title == "Unknown"
    finally:
        await stop_track(guild)
