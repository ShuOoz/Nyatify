"""Pause, skip, a similar song, and a station that ends. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import wavelink

from music.lavalink import Found
from music.playback import (
    advance_from_end,
    next_track,
    now_playing,
    play_now,
    queue_tracks,
    release,
    repeat_track,
    shuffle_queue,
    stop_track,
    toggle_radio,
    tune_track,
    _stored,
    toggle_pause,
)

from tests.music.support import _Player, _named, _playing

async def test_a_failed_pause_stays_playing():
    guild, player, _track = await _playing()
    try:
        player.pause.side_effect = wavelink.WavelinkException("no")
        assert await toggle_pause(guild) == "I could not pause that."
        assert player.paused is False
        player.pause.assert_awaited_once_with(True)
        assert not _pause_leave_pending()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


def _pause_leave_pending() -> bool:
    return any(task.get_name() == "music-pause-leave" and not task.done() for task in asyncio.all_tasks())


async def test_resuming_or_the_next_song_keeps_the_pause_from_leaving(monkeypatch):
    guild, player, _track = await _playing()
    try:
        async def hold(_seconds):
            await asyncio.Event().wait()

        monkeypatch.setattr("music.playback.start._wait", hold)
        player.client = MagicMock()
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("Later")]) == "Added Later."
        assert await toggle_pause(guild) is None
        assert _pause_leave_pending()
        assert await toggle_pause(guild) is None
        await asyncio.sleep(0)
        assert player.paused is False
        assert not _pause_leave_pending()
        player.client.dispatch.assert_not_called()
        player.disconnect.assert_not_awaited()

        assert await toggle_pause(guild) is None
        assert await next_track(guild) is None
        await asyncio.sleep(0)
        assert player.paused is False
        assert not _pause_leave_pending()
        player.client.dispatch.assert_not_called()
        player.disconnect.assert_not_awaited()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_pause_needs_a_song():
    assert await toggle_pause(None) == "Use this command in a server."
    guild = MagicMock()
    guild.voice_client = None
    assert await toggle_pause(guild) == "Nothing is playing."
    assert now_playing(guild) is None
    assert now_playing(None) is None


async def test_a_paused_song_resumes_for_someone_still_in_voice():
    """The bot has left. Someone in a voice channel can continue the paused song."""
    from music.playback import member_in_voice
    from music.playback.remember import _Line, _Song, _lines

    guild = MagicMock()
    guild.id = 9106
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    channel.id = 19
    player = _Player(channel)

    async def connect(**_kwargs):
        guild.voice_client = player
        return player

    async def play(track):
        player._track = track

    channel.connect = AsyncMock(side_effect=connect)
    player.play.side_effect = play
    track = _named("Paused")
    _lines[9106] = _Line(current=_Song(track, 9, "youtube"), paused=True, resting=True)
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel
    away = MagicMock()
    away.voice = None
    try:
        assert member_in_voice(member, guild) is True
        assert member_in_voice(away, guild) is False
        assert await toggle_pause(guild, member) is None
        player.play.assert_awaited_once_with(track)
        assert now_playing(guild).title == "Paused"
        assert now_playing(guild).paused is False
    finally:
        await stop_track(guild)


async def test_a_paused_song_comes_back_where_it_stopped():
    """Leaving voice and pressing play again starts at the paused place."""
    from music.playback.remember import _Line, _Song, _lines

    guild = MagicMock()
    guild.id = 9108
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    channel.id = 19
    player = _Player(channel)

    async def connect(**_kwargs):
        guild.voice_client = player
        return player

    async def play(track, **kwargs):
        player._track = track
        player._position = kwargs.get("start", 0)

    channel.connect = AsyncMock(side_effect=connect)
    player.play.side_effect = play
    track = _named("Paused")
    track.length = 180_000
    _lines[9108] = _Line(
        current=_Song(track, 9, "youtube"), paused=True, resting=True, spot=65_000,
    )
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel
    try:
        assert await toggle_pause(guild, member) is None
        player.play.assert_awaited_once_with(track, start=65_000)
        assert now_playing(guild).paused is False
        assert now_playing(guild).position == 65_000
        assert player.position == 65_000
    finally:
        await stop_track(guild)


async def test_a_paused_song_from_disk_is_opened_again(monkeypatch):
    """A reload gives the saved song a new class. It still has to be searched and played."""
    from music.playback.remember import _Line, _Song, _heard, _lines

    @dataclass(frozen=True)
    class _Kept:
        title: str
        author: str
        uri: str | None
        artwork: str | None
        length: int
        is_stream: bool

    guild = MagicMock()
    guild.id = 9107
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    channel.id = 19
    player = _Player(channel)

    async def connect(**_kwargs):
        guild.voice_client = player
        return player

    async def play(track):
        player._track = track

    channel.connect = AsyncMock(side_effect=connect)
    player.play.side_effect = play
    live = _named("Paused")
    saved = _Kept("Paused", "Band", "https://www.youtube.com/watch?v=abc", None, 1_000, False)
    _lines[9107] = _Line(current=_Song(saved, 9, "youtube"), paused=True, resting=True)
    _heard[9107] = []

    async def search(_guild, query, _platform):
        assert query == "https://www.youtube.com/watch?v=abc"
        return Found(query, "youtube", [live], True)

    monkeypatch.setattr("music.playback.find_songs", search)
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel
    try:
        assert await toggle_pause(guild, member) is None
        player.play.assert_awaited_once_with(live)
        guild.voice_client = player
        player._track = None
        _lines[9107] = _Line(current=_Song(saved, 9, "youtube"), paused=True, resting=True)
        assert await toggle_pause(guild, member) is None
        assert player.play.await_count == 2
        player.play.assert_awaited_with(live)
    finally:
        await stop_track(guild)

async def test_repeat_and_shuffle_change_mode_when_nothing_is_on(monkeypatch):
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("One"), _named("Two")]) == "Added 2 songs."
        player._track = None

        assert await repeat_track(guild) is None
        assert _stored(guild).repeat is True
        assert [item.track.title for item in _stored(guild).later] == ["One", "Two"]

        def reverse(songs):
            songs.reverse()

        monkeypatch.setattr("music.playback.control.random.shuffle", reverse)
        assert await shuffle_queue(guild) is None
        assert _stored(guild).shuffle is True
        assert _stored(guild).repeat is False
        assert [item.track.title for item in _stored(guild).later] == ["Two", "One"]

        assert await shuffle_queue(guild) is None
        assert _stored(guild).shuffle is False
        assert [item.track.title for item in _stored(guild).later] == ["One", "Two"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_a_failed_skip_leaves_the_song_playing():
    guild, player, _track = await _playing()
    try:
        player.stop.side_effect = wavelink.WavelinkException("no")
        assert await next_track(guild) == "Something went wrong while changing the song."
        assert now_playing(guild).title == "I Ain't Worried"
        assert _stored(guild).current.track.title == "I Ain't Worried"
        assert _stored(guild).repeat is False
    finally:
        player.stop.side_effect = None
        if guild.voice_client is not None:
            await stop_track(guild)

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
        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        monkeypatch.setattr("music.playback.control.similar_songs", AsyncMock(return_value=[]))
        preview = MagicMock(title="Clip", length=30_000, is_preview=False, identifier="clip")
        full = MagicMock(title="Full", length=180_000, is_preview=False, identifier="full")
        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[preview, full]))
        assert await toggle_radio(guild) is None
        assert _stored(guild).radio is True
        assert now_playing(guild).title == "I Ain't Worried"
        assert [item.track.title for item in _stored(guild).later] == ["Queued"]

        player._guild = guild
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "Full"
        assert [item.track.title for item in _stored(guild).later] == ["Queued"]

        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[preview]))
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "Full"
        assert _stored(guild).radio is True
        assert [item.track.title for item in _stored(guild).later] == ["Queued"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_adding_during_radio_keeps_the_station(monkeypatch):
    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("Queued")]) == "Added Queued."
        track.identifier = "now"
        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        peace = MagicMock(title="Peace Piece", identifier="peace", length=180_000, is_preview=False)
        monkeypatch.setattr(
            "music.playback.control.similar_songs",
            AsyncMock(return_value=[("Bill Evans", "Peace Piece")]),
        )
        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[peace]))
        assert await toggle_radio(guild) is None
        played = player.play.await_count

        assert await queue_tracks(member, guild, [_named("Extra")]) == "Added Extra."
        assert await play_now(member, guild, _named("Echo"), "youtube") == "Added Echo."
        monkeypatch.setattr("music.playback.start._replaced", lambda _player: True)
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."

        assert player.play.await_count == played
        assert now_playing(guild).title == "I Ain't Worried"
        assert _stored(guild).radio is True
        assert [item.track.title for item in _stored(guild).later] == [
            "Queued",
            "Extra",
            "Echo",
            "After",
        ]

        player._guild = guild
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "Peace Piece"
        assert _stored(guild).radio is True
        assert [item.track.title for item in _stored(guild).later] == [
            "Queued",
            "Extra",
            "Echo",
            "After",
        ]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_a_missed_platform_keeps_the_song(monkeypatch):
    guild, player, _track = await _playing()
    try:
        monkeypatch.setattr(
            "music.playback.find_songs",
            AsyncMock(return_value="Nothing on Spotify matched that."),
        )
        note = await tune_track(guild, 40, "spotify", 5_000)
        assert note == "Nothing on Spotify matched that."
        assert now_playing(guild).volume == 40
        assert now_playing(guild).title == "I Ain't Worried"
        assert _stored(guild).current.platform == "youtube"
        player.seek.assert_not_awaited()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_a_platform_switch_keeps_the_queue(monkeypatch):
    guild, player, track = await _playing()
    try:
        track.identifier = "old-id"
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After"), _named("Later")]) == "Added 2 songs."
        other = _named("I Ain't Worried", "OneRepublic")
        other.identifier = "new-id"
        other.uri = "https://example.com/spotify"
        other.length = 180_000

        async def search(*_args, **_kwargs):
            return Found("I Ain't Worried OneRepublic", "spotify", [other], False)

        async def play(new):
            # A missing session leaves voice. That used to drop every waiting song.
            release(guild)
            player._track = new

        player.play.side_effect = play
        monkeypatch.setattr("music.playback.find_songs", search)
        assert await tune_track(guild, 100, "spotify", None) is None
        assert _stored(guild).current.platform == "spotify"
        assert [song.track.title for song in _stored(guild).later] == ["After", "Later"]

        player._guild = guild
        ended = MagicMock(identifier="old-id", uri="https://example.com/song")
        await advance_from_end(type("End", (), {
            "reason": "finished",
            "player": player,
            "track": ended,
        })())
        assert now_playing(guild).title == "I Ain't Worried"
        assert [song.track.title for song in _stored(guild).later] == ["After", "Later"]

        nxt = MagicMock(identifier="new-id", uri="https://example.com/spotify")
        await advance_from_end(type("End", (), {
            "reason": "finished",
            "player": player,
            "track": nxt,
        })())
        assert now_playing(guild).title == "After"
        assert [song.track.title for song in _stored(guild).later] == ["Later"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_a_time_past_the_end_stays_put():
    guild, player, _track = await _playing()
    try:
        assert await tune_track(guild, 100, None, 200_000) == "That is past the end of the song."
        player.seek.assert_not_awaited()
        assert now_playing(guild).position == 0
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_a_live_song_stays_where_it_is():
    guild, player, track = await _playing()
    try:
        track.length = 0
        assert await tune_track(guild, 100, None, 1_000) == "That song cannot move."
        player.seek.assert_not_awaited()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_radio_asks_for_a_key_when_lastfm_is_unset():
    guild, _player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = _player.channel
        assert await toggle_radio(guild) == "You need a Last.fm key to do this."
        assert _stored(guild).radio is False
        assert _stored(guild).later == []
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_radio_queues_the_lastfm_song(monkeypatch):
    guild, _player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = _player.channel
        track.identifier = "now"
        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        peace = MagicMock(title="Peace Piece", identifier="peace", length=180_000, is_preview=False)
        asked = []

        async def search(query, platform):
            asked.append((query, platform))
            return [peace]

        monkeypatch.setattr(
            "music.playback.control.similar_songs",
            AsyncMock(return_value=[("Bill Evans", "Peace Piece")]),
        )
        monkeypatch.setattr("music.source.search_track", search)
        assert await toggle_radio(guild) is None
        assert asked == []
        assert now_playing(guild).title == "I Ain't Worried"
        _player._guild = guild
        _player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": _player})())
        assert asked == [("Peace Piece Bill Evans", "youtube")]
        assert now_playing(guild).title == "Peace Piece"
        assert _stored(guild).later == []
        assert now_playing(guild).radio is True
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_radio_falls_back_when_the_similar_name_is_only_this_song(monkeypatch):
    guild, _player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = _player.channel
        track.identifier = "now"
        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        same = MagicMock(title="I Ain't Worried", identifier="now", length=148_000)
        other = MagicMock(title="Counting Stars", identifier="other", length=180_000)
        asked = []

        async def search(query, _platform):
            asked.append(query)
            if query == "Missing Nobody":
                return [same]
            return [same, other]

        monkeypatch.setattr(
            "music.playback.control.similar_songs",
            AsyncMock(return_value=[("Nobody", "Missing")]),
        )
        monkeypatch.setattr("music.source.search_track", search)
        assert await toggle_radio(guild) is None
        assert asked == []
        assert now_playing(guild).title == "I Ain't Worried"
        _player._guild = guild
        _player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": _player})())
        assert asked == ["Missing Nobody", "I Ain't Worried OneRepublic"]
        assert now_playing(guild).title == "Counting Stars"
        assert _stored(guild).later == []
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_a_finished_station_plays_the_queue(monkeypatch):
    guild, player, _track = await _playing()
    try:
        song = _stored(guild).current
        song.videos = (
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
        assert _stored(guild).current.videos == ()
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
        song.videos = links
        song.video_at = 0
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
        assert _stored(guild).current.video_at == 0
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
        assert _stored(guild).current.video_at == 1
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
        song.videos = five
        song.video_at = 4
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
        assert _stored(guild).current.video_at == 0
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
