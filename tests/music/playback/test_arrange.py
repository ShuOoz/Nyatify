"""The queue, the songs already played, and a saved list starting. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from music.archive import SavedSong
from music.playback import (
    HISTORY_SONGS,
    QUEUE_SONGS,
    clear_queued,
    drop_queued,
    earlier_at,
    load_line,
    next_track,
    play_now,
    now_playing,
    played,
    finish_saved,
    play_saved,
    queue_opened,
    queue_tracks,
    queued,
    skipped_songs,
    stop_track,
    _stored,
)

from tests.music.support import _Player, _card_text, _named, _room, _waiting
from tests.music.playlist.support import _line_up

async def test_the_live_line_drops_songs():
    """The live line can drop a song or start another. Removing the last waiting song ends the playlist."""
    await case_the_live_line_can_drop_a_song_or_start_another()
    await case_removing_the_last_waiting_song_ends_the_playlist()

async def case_the_live_line_can_drop_a_song_or_start_another():
    guild, member, player, tracks, note = await _line_up(9100, ["Alpha", "Beta"])
    try:
        assert note == "Playing Alpha."
        rows = queued(guild)
        assert [(row.title, row.playing, row.url) for row in rows] == [
            ("Alpha", True, "https://example.com/alpha"),
            ("Beta", False, "https://example.com/beta"),
        ]
        assert await drop_queued(guild, 1) is None
        assert [row.title for row in queued(guild)] == ["Alpha"]
        assert await drop_queued(guild, 0) is None
        assert queued(guild) == []
        player.disconnect.assert_not_awaited()
        await stop_track(guild)

        guild, member, player, tracks, _note = await _line_up(9101, ["Alpha", "Beta"])
        charlie, delta = _named("Charlie"), _named("Delta")
        assert await load_line(member, guild, [(charlie, "spotify"), (delta, "youtube")]) == (
            "Playing Charlie."
        )
        assert [row.title for row in queued(guild)] == ["Charlie", "Delta"]
        assert queued(guild)[1].platform == "youtube"
        assert [song.track.title for song in _stored(guild).earlier] == ["Alpha"]

        echo = _named("Echo")
        assert await play_now(member, guild, echo, "soundcloud") == "Playing Echo."
        assert [row.title for row in queued(guild)] == ["Echo", "Delta"]
        assert [song.track.title for song in _stored(guild).earlier] == ["Alpha", "Charlie"]

        outsider = MagicMock()
        outsider.voice.channel = MagicMock()
        outsider.voice.channel.id = 1
        played = player.play.await_count
        assert await play_now(outsider, guild, _named("Nope"), "youtube") == (
            "Join the voice channel first."
        )
        assert player.play.await_count == played
        assert [row.title for row in queued(guild)] == ["Echo", "Delta"]
    finally:
        await stop_track(guild)

async def case_removing_the_last_waiting_song_ends_the_playlist():
    guild, member, player = _room(86)
    await stop_track(guild)
    tracks = [(_named(str(index)), "youtube") for index in range(3)]
    try:
        assert await queue_opened(member, guild, tracks, whole=True) == "Playing 0."
        guild.voice_client = player
        assert _stored(guild).whole is True
        assert await drop_queued(guild, 2) is None
        assert _stored(guild).whole is True
        assert await drop_queued(guild, 1) is None
        assert _stored(guild).whole is False
        assert [row.title for row in queued(guild)] == ["0"]
    finally:
        await stop_track(guild)

async def test_history_remembers_the_last_fifty_played_songs():
    guild = MagicMock()
    guild.id = 78
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
    tracks = [_named(str(index)) for index in range(HISTORY_SONGS + 1)]
    try:
        assert await queue_tracks(member, guild, [tracks[0]]) == "Playing 0."
        guild.voice_client = player
        cursor = 1
        finished = 0
        while finished < HISTORY_SONGS + 1:
            state = now_playing(guild)
            playing = state is not None and state.has_track
            waiting = _waiting(guild) if playing else []
            room = QUEUE_SONGS - len(waiting)
            if cursor < len(tracks) and room > 0 and playing:
                batch = tracks[cursor : cursor + room]
                note = await queue_tracks(member, guild, batch)
                assert note.startswith("Added ")
                cursor += len(batch)
                continue
            assert playing
            assert await next_track(guild) is None
            finished += 1

        heard = played(guild)
        assert [row.title for row in heard] == [str(index) for index in range(HISTORY_SONGS, 0, -1)]
        assert earlier_at(guild, 0)[0].title == str(HISTORY_SONGS)
        assert earlier_at(guild, HISTORY_SONGS) is None

        await stop_track(guild)
        guild.voice_client = None
        assert now_playing(guild) is None
        assert [row.title for row in played(guild)] == [row.title for row in heard]
        assert earlier_at(guild, 0)[0].title == str(HISTORY_SONGS)
    finally:
        await stop_track(guild)

async def test_a_song_on_the_line_leaves_history():
    """Playing a song again, or waiting on the same recording, takes it out of history."""
    await case_a_song_leaves_history_while_it_plays_again()
    await case_a_shared_link_leaves_history_while_that_recording_waits()

async def case_a_song_leaves_history_while_it_plays_again():
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
    three, one, two = _named("Three", "Bea"), _named("One", "Mac"), _named("Two", "Ada")
    three.uri = "https://example.com/three"
    one.uri = "https://example.com/one"
    two.uri = "https://example.com/two"
    again = _named("One Remastered", "Mac")
    again.uri = "https://example.com/one"
    try:
        assert await queue_tracks(member, guild, [three, one, two]) == "Playing Three."
        guild.voice_client = player
        assert await next_track(guild) is None
        assert await next_track(guild) is None
        assert now_playing(guild).title == "Two"
        assert [row.title for row in played(guild)] == ["One", "Three"]

        assert await queue_tracks(member, guild, [again]) == "Added One Remastered."
        assert [row.title for row in played(guild)] == ["Three"]

        assert await next_track(guild) is None
        assert now_playing(guild).title == "One Remastered"
        assert [row.title for row in played(guild)] == ["Two", "Three"]

        await stop_track(guild)
        guild.voice_client = None
        assert [row.title for row in played(guild)] == ["One Remastered", "Two", "Three"]
    finally:
        await stop_track(guild)

async def case_a_shared_link_leaves_history_while_that_recording_waits():
    """youtu.be and a youtube.com share are one recording, so history hides it."""
    guild = MagicMock()
    guild.id = 821
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
    first = _named("One", "Mac")
    first.uri = "https://youtu.be/abc"
    later = _named("Two", "Ada")
    later.uri = "https://example.com/two"
    again = _named("One Remastered", "Mac")
    again.uri = "https://music.youtube.com/watch?v=abc&si=track"
    try:
        assert await queue_tracks(member, guild, [first, later]) == "Playing One."
        guild.voice_client = player
        assert await next_track(guild) is None
        assert now_playing(guild).title == "Two"
        assert [row.title for row in played(guild)] == ["One"]

        assert await queue_tracks(member, guild, [again]) == "Added One Remastered."
        assert played(guild) == []
    finally:
        await stop_track(guild)


async def test_an_unplayed_queue_song_returns_to_its_history_spot():
    import music.playback as playback

    guild = MagicMock()
    guild.id = 83
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

    def track(title: str, author: str = "") -> MagicMock:
        song = _named(title, author)
        song.uri = f"https://example.com/{title.casefold()}"
        return song

    aye, bee, sea, dee, eff = (track(name) for name in ("A", "B", "C", "D", "F"))
    bee.author = "Mac"
    again = track("B Live", "Mac")
    again.uri = bee.uri
    fresh = track("G")
    try:
        assert await queue_tracks(member, guild, [aye, bee, sea, dee]) == "Playing A."
        guild.voice_client = player
        assert await next_track(guild) is None
        assert await next_track(guild) is None
        assert await next_track(guild) is None
        assert now_playing(guild).title == "D"
        assert [row.title for row in played(guild)] == ["C", "B", "A"]

        assert await queue_tracks(member, guild, [eff, again, fresh]) == "Added 3 songs."
        assert [row.title for row in played(guild)] == ["C", "A"]
        assert await next_track(guild) is None
        assert now_playing(guild).title == "F"
        assert [row.title for row in played(guild)] == ["D", "C", "A"]

        assert await drop_queued(guild, 1) is None
        assert [row.title for row in queued(guild)] == ["F", "G"]
        assert [row.title for row in played(guild)] == ["D", "C", "B", "A"]
        assert played(guild)[2].author == "Mac"
        assert earlier_at(guild, 2)[0].title == "B"

        assert await drop_queued(guild, 1) is None
        assert [row.title for row in queued(guild)] == ["F"]
        assert [row.title for row in played(guild)] == ["D", "C", "B", "A"]

        assert await queue_tracks(member, guild, [again, again]) == "Added 2 songs."
        assert [row.title for row in played(guild)] == ["D", "C", "A"]
        assert await drop_queued(guild, 1) is None
        assert [row.title for row in played(guild)] == ["D", "C", "A"]
        assert await drop_queued(guild, 1) is None
        assert [row.title for row in played(guild)] == ["D", "C", "B", "A"]

        assert await queue_tracks(member, guild, [again]) == "Added B Live."
        assert [row.title for row in played(guild)] == ["D", "C", "A"]
        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        assert [row.title for row in played(guild)] == ["D", "C", "A"]
        assert [row.title for row in queued(guild)] == ["F", "B Live"]
        assert await drop_queued(guild, 1) is None
        assert [row.title for row in played(guild)] == ["D", "C", "B", "A"]
    finally:
        await stop_track(guild)

async def test_a_playlist_starts_and_keeps_every_song(monkeypatch):
    """A long playlist starts before the rest open, and every song stays."""
    with monkeypatch.context() as patch:
        await case_a_long_playlist_starts_before_the_rest_open(patch)
    with monkeypatch.context() as patch:
        await case_playing_a_playlist_keeps_every_song(patch)

async def case_a_long_playlist_starts_before_the_rest_open(monkeypatch):
    guild, member, player = _room(80)
    await stop_track(guild)
    release = asyncio.Event()
    current = 0
    peak = 0

    async def open_one(_guild, song):
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        try:
            if song.title in ("Two", "Three"):
                await release.wait()
            else:
                await asyncio.sleep(0)
        finally:
            current -= 1
        if song.title == "Skip":
            return None, "Skip", "Nothing on YouTube matched that."
        return (_named(song.title), song.platform), song.title, ""

    monkeypatch.setattr("music.playback.open_one", open_one)
    songs = [
        SavedSong("Skip", "youtube", "https://example.com/skip"),
        SavedSong("One", "youtube", "https://example.com/one"),
        SavedSong("Two", "youtube", "https://example.com/two"),
        SavedSong("Three", "apple", "https://example.com/three"),
    ]
    try:
        note = await asyncio.wait_for(
            play_saved(member, guild, songs, replace=False),
            timeout=2,
        )
        assert note == "Playing One. Skipped Skip."
        assert peak > 1
        guild.voice_client = player
        assert now_playing(guild).title == "One"
        assert _waiting(guild) == []
        release.set()
        await finish_saved(guild)
        assert _waiting(guild) == ["Two", "Three"]
        assert _stored(guild).later[-1].platform == "apple"
    finally:
        release.set()
        await finish_saved(guild)
        await stop_track(guild)

async def case_playing_a_playlist_keeps_every_song(monkeypatch):
    guild, member, player = _room(81)
    await stop_track(guild)

    async def open_one(_guild, song):
        return (_named(song.title), song.platform), song.title, ""

    monkeypatch.setattr("music.playback.open_one", open_one)
    count = QUEUE_SONGS + 3
    songs = [
        SavedSong(str(index), "youtube", f"https://example.com/{index}")
        for index in range(count)
    ]
    songs[-1] = SavedSong(str(count - 1), "apple", f"https://example.com/{count - 1}")
    try:
        note = await play_saved(member, guild, songs, replace=True)
        assert note == "Playing 0."
        guild.voice_client = player
        await finish_saved(guild)
        assert now_playing(guild).title == "0"
        assert _waiting(guild) == [str(index) for index in range(1, QUEUE_SONGS + 1)]
        assert skipped_songs(guild) is None
        assert [song.track.title for song in _stored(guild).held] == [
            str(index) for index in range(QUEUE_SONGS + 1, count)
        ]
        assert await next_track(guild) is None
        assert skipped_songs(guild) is None
        assert _waiting(guild)[-1] == str(QUEUE_SONGS + 1)
        assert await queue_tracks(member, guild, [_named("Extra")]) == "The queue is full."
    finally:
        await finish_saved(guild)
        await stop_track(guild)

async def test_a_saved_playlist_replaces_radio(monkeypatch):
    """A playlist takes the station's place. Adding a song while radio is on still waits."""
    guild, member, player = _room(8803)
    await stop_track(guild)

    async def open_one(_guild, song):
        return (_named(song.title), song.platform), song.title, ""

    monkeypatch.setattr("music.playback.open_one", open_one)
    songs = [
        SavedSong("One", "youtube", "https://example.com/one"),
        SavedSong("Two", "youtube", "https://example.com/two"),
    ]
    try:
        assert await queue_tracks(member, guild, [_named("Stay")]) == "Playing Stay."
        guild.voice_client = player
        assert await queue_tracks(member, guild, [_named("Queued")]) == "Added Queued."
        held = _stored(guild)
        assert held is not None
        held.radio = True

        assert await play_now(member, guild, _named("Echo"), "youtube") == "Added Echo."
        assert now_playing(guild).title == "Stay"
        assert held.radio is True

        note = await play_saved(member, guild, songs, replace=True)
        assert note == "Playing One."
        await finish_saved(guild)
        assert now_playing(guild).title == "One"
        assert _stored(guild).radio is False
        assert _waiting(guild) == ["Two"]
    finally:
        await finish_saved(guild)
        await stop_track(guild)


async def test_a_playlist_still_opening_can_be_stopped(monkeypatch):
    """Stopping or emptying drops the songs that are still opening."""
    with monkeypatch.context() as patch:
        await case_stopping_drops_the_songs_still_opening(patch)
    with monkeypatch.context() as patch:
        await case_empty_cancels_a_playlist_still_opening(patch)

async def case_stopping_drops_the_songs_still_opening(monkeypatch):
    guild, member, player = _room(82)
    await stop_track(guild)
    release = asyncio.Event()

    async def open_one(_guild, song):
        if song.title != "One":
            await release.wait()
        return (_named(song.title), "youtube"), song.title, ""

    monkeypatch.setattr("music.playback.open_one", open_one)
    songs = [
        SavedSong("One", "youtube", "https://example.com/one"),
        SavedSong("Two", "youtube", "https://example.com/two"),
    ]
    try:
        note = await asyncio.wait_for(play_saved(member, guild, songs, replace=False), timeout=2)
        assert note == "Playing One."
        guild.voice_client = player
        await stop_track(guild)
        release.set()
        await finish_saved(guild)
        assert _waiting(guild) == []
    finally:
        release.set()
        await finish_saved(guild)
        await stop_track(guild)

async def case_empty_cancels_a_playlist_still_opening(monkeypatch):
    guild, member, player = _room(83)
    await stop_track(guild)
    release = asyncio.Event()

    async def open_one(_guild, song):
        if song.title != "One":
            await release.wait()
        return (_named(song.title), "youtube"), song.title, ""

    monkeypatch.setattr("music.playback.open_one", open_one)
    songs = [
        SavedSong("One", "youtube", "https://example.com/one"),
        SavedSong("Two", "youtube", "https://example.com/two"),
        SavedSong("Three", "youtube", "https://example.com/three"),
    ]
    try:
        note = await asyncio.wait_for(play_saved(member, guild, songs, replace=False), timeout=2)
        assert note == "Playing One."
        guild.voice_client = player
        await clear_queued(guild)
        release.set()
        await finish_saved(guild)
        assert now_playing(guild).title == "One"
        assert _waiting(guild) == []
        assert skipped_songs(guild) is None
    finally:
        release.set()
        await finish_saved(guild)
        await stop_track(guild)

async def test_a_finished_playlist_names_the_songs_it_skipped(monkeypatch, tmp_path):
    from guild import GuildConfig
    from music.playlist.card import PlaylistCard

    guild, member, player = _room(84)
    await stop_track(guild)
    release = asyncio.Event()

    async def open_one(_guild, song):
        if song.title in ("Two", "Four"):
            await release.wait()
            return None, song.title, "Nothing on YouTube matched that."
        if song.title == "Three":
            await release.wait()
        return (_named(song.title), "youtube"), song.title, ""

    monkeypatch.setattr("music.playback.open_one", open_one)
    songs = [
        SavedSong("One", "youtube", "https://example.com/one"),
        SavedSong("Two", "youtube", "https://example.com/two"),
        SavedSong("Three", "youtube", "https://example.com/three"),
        SavedSong("Four", "apple", "https://example.com/four"),
    ]
    view = None
    try:
        note = await asyncio.wait_for(play_saved(member, guild, songs, replace=False), timeout=2)
        assert note == "Playing One."
        guild.voice_client = player
        view = PlaylistCard(guild, GuildConfig(tmp_path / "guild.json"))
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        view.follow(view.message)
        assert "Skipped" not in _card_text(view)
        release.set()
        await finish_saved(guild)
        assert _waiting(guild) == ["Three"]
        assert skipped_songs(guild) == "Skipped Two, Four."
        assert "-# Skipped Two, Four." in _card_text(view)
        view.message.edit.assert_awaited()
    finally:
        release.set()
        await finish_saved(guild)
        if view is not None:
            view.stop()
        await stop_track(guild)
