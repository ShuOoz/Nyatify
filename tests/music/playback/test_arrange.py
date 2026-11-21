"""The queue and the songs already played, and the session file. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music.playback import (
    HISTORY_SONGS,
    QUEUE_SONGS,
    drop_queued,
    earlier_at,
    load_line,
    next_track,
    play_now,
    now_playing,
    played,
    queue_opened,
    queue_tracks,
    queued,
    stop_track,
    _stored,
)

from tests.music.support import _Player, _named, _room, _waiting
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

async def test_history_remembers_the_last_fifty_played_songs(monkeypatch):
    """Fifty songs are kept. A short history proves the oldest one drops."""
    assert HISTORY_SONGS == 50
    cap = 3
    monkeypatch.setattr("music.playback.arrange.HISTORY_SONGS", cap)
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
    tracks = [_named(str(index)) for index in range(cap + 1)]
    try:
        assert await queue_tracks(member, guild, [tracks[0]]) == "Playing 0."
        guild.voice_client = player
        cursor = 1
        finished = 0
        while finished < cap + 1:
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
        assert [row.title for row in heard] == [str(index) for index in range(cap, 0, -1)]
        assert earlier_at(guild, 0)[0].title == str(cap)
        assert earlier_at(guild, cap) is None

        await stop_track(guild)
        guild.voice_client = None
        assert now_playing(guild) is None
        assert [row.title for row in played(guild)] == [row.title for row in heard]
        assert earlier_at(guild, 0)[0].title == str(cap)
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

def test_a_bad_file_is_left_alone(tmp_path):
    """A bad session file is left alone, and an old line file moves once."""
    case_a_bad_session_file_is_left_alone()
    spot = tmp_path / "part1"
    spot.mkdir()
    case_an_old_music_line_file_moves_once(spot)

def case_a_bad_session_file_is_left_alone():
    import music.playback.remember as remember
    import music.playback.arrange as resume

    remember.PATH.write_text("{not json", encoding="utf-8")
    remember._book = None
    remember._cache_path = None
    remember._failed_path = None
    assert resume.read_guild(5) is None
    assert resume.read_all() == {}
    resume.write_guild(5, {"current": {"title": "Kept"}})
    assert remember.PATH.read_text(encoding="utf-8") == "{not json"

    remember.PATH.write_text("[]\n", encoding="utf-8")
    remember._book = None
    remember._cache_path = None
    remember._failed_path = None
    resume.write_guild(5, {"current": {"title": "Kept"}})
    assert remember.PATH.read_text(encoding="utf-8") == "[]\n"

def case_an_old_music_line_file_moves_once(tmp_path):
    from music.playback import remember

    legacy = tmp_path / "music_line.json"
    current = tmp_path / "music_session.json"
    legacy.write_text("{}\n", encoding="utf-8")
    remember._adopt_previous(current, "music_line.json")
    assert not legacy.exists()
    assert current.read_text(encoding="utf-8") == "{}\n"

    legacy.write_text('{"9": {}}\n', encoding="utf-8")
    remember._adopt_previous(current, "music_line.json")
    assert legacy.exists()
    assert current.read_text(encoding="utf-8") == "{}\n"
