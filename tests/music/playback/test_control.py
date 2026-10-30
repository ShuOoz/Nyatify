"""Pause, skip, a similar song, and a station that ends. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import wavelink

from music.lavalink import Found
from music.playback.control import _heard_query
from music.playback import (
    advance_from_end,
    drop_queued,
    next_track,
    now_playing,
    play_now,
    previous_track,
    queue_tracks,
    save_and_clear,
    repeat_track,
    shuffle_queue,
    stop_track,
    toggle_radio,
    apply_playback,
    _stored,
    toggle_pause,
)

from tests.music.support import _Player, _named, _playing, _scene

async def test_a_paused_song(monkeypatch):
    """Pause stays put. Someone still in voice resumes at the saved place. A failed resume and a song opened from disk stay paused there."""
    await _scene(case_pause_stays_put, monkeypatch=monkeypatch)
    await _scene(case_a_paused_song_resumes_for_someone_still_in_voice)
    await _scene(case_a_paused_song_comes_back_where_it_stopped)
    await _scene(case_a_failed_resume_stays_paused_where_it_was)
    await _scene(case_a_paused_song_from_disk_is_opened_again, monkeypatch=monkeypatch)

async def case_pause_stays_put(monkeypatch):
    """A failed pause, an empty room, and a forgotten spot leave the song where it was. Resuming or skipping cancels the leave."""
    await case_a_failed_pause_stays_playing()
    await case_pause_needs_a_song()
    await case_a_pause_keeps_the_place_the_player_forgets()
    with monkeypatch.context() as patch:
        await case_resuming_or_the_next_song_keeps_the_pause_from_leaving(patch)

async def case_a_failed_pause_stays_playing():
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


async def case_resuming_or_the_next_song_keeps_the_pause_from_leaving(monkeypatch):
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


async def case_pause_needs_a_song():
    assert await toggle_pause(None) == "Use this command in a server."
    guild = MagicMock()
    guild.voice_client = None
    assert await toggle_pause(guild) == "Nothing is playing."
    assert now_playing(guild) is None
    assert now_playing(None) is None


async def case_a_paused_song_resumes_for_someone_still_in_voice():
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


async def case_a_paused_song_comes_back_where_it_stopped():
    """Leaving voice and pressing play again opens quietly at the paused place, then resumes."""
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
        player._paused = kwargs.get("paused") is True

    channel.connect = AsyncMock(side_effect=connect)
    player.play.side_effect = play
    track = _named("Paused")
    track.length = 180_000
    _lines[9108] = _Line(
        current=_Song(track, 9, "youtube"), paused=True, resting=True, position=65_000,
    )
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel
    try:
        assert await toggle_pause(guild, member) is None
        player.play.assert_awaited_once_with(track, start=65_000, paused=True)
        player.pause.assert_awaited_once_with(False)
        assert now_playing(guild).paused is False
        assert now_playing(guild).position == 65_000
        assert player.position == 65_000
    finally:
        await stop_track(guild)


async def case_a_failed_resume_stays_paused_where_it_was():
    """Lavalink can refuse to unpause. The song stays paused at the saved place."""
    from music.playback.remember import _Line, _Song, _lines

    guild = MagicMock()
    guild.id = 9110
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
        player._paused = True

    async def refuse(_value):
        raise wavelink.WavelinkException("no")

    channel.connect = AsyncMock(side_effect=connect)
    player.play.side_effect = play
    player.pause.side_effect = refuse
    track = _named("Paused")
    track.length = 180_000
    _lines[9110] = _Line(
        current=_Song(track, 9, "youtube"), paused=True, resting=True, position=65_000,
    )
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel
    try:
        assert await toggle_pause(guild, member) is None
        state = now_playing(guild)
        assert state is not None
        assert state.title == "Paused"
        assert state.paused is True
        assert state.position == 65_000
        assert player.paused is True
        line = _stored(guild)
        assert line is not None and line.paused is True and line.position == 65_000
    finally:
        await stop_track(guild)


async def case_a_paused_song_from_disk_is_opened_again(monkeypatch):
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

async def test_the_line_moves(monkeypatch):
    """Shuffle puts the line back. Skip plays the queue or stays. A seek stays put. Repeat stays through the line."""
    await _scene(case_shuffle_puts_the_line_back, monkeypatch=monkeypatch)
    await _scene(case_skip_plays_the_queue_or_stays, monkeypatch=monkeypatch)
    await _scene(case_a_seek_stays_put)
    await _scene(case_repeat_stays_through_the_line)

async def case_shuffle_puts_the_line_back(monkeypatch):
    """Shuffle changes the mode while nothing is on, and previous puts a shuffled song back."""
    with monkeypatch.context() as patch:
        await case_repeat_and_shuffle_change_mode_when_nothing_is_on(patch)
    with monkeypatch.context() as patch:
        await case_previous_puts_a_shuffled_song_back_where_it_was(patch)

async def case_repeat_and_shuffle_change_mode_when_nothing_is_on(monkeypatch):
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


async def case_previous_puts_a_shuffled_song_back_where_it_was(monkeypatch):
    """Previous returns this song to its old place. Shuffle off does not play it first."""
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("A"), _named("B"), _named("C")]) == "Added 3 songs."
        monkeypatch.setattr("music.playback.control.random.shuffle", lambda songs: songs.reverse())
        assert await shuffle_queue(guild) is None
        assert [song.track.title for song in _stored(guild).later] == ["C", "B", "A"]
        assert await next_track(guild) is None
        assert now_playing(guild).title == "C"
        player._position = 0
        assert await previous_track(guild) is None
        assert now_playing(guild).title == "I Ain't Worried"
        assert await shuffle_queue(guild) is None
        assert _stored(guild).shuffle is False
        assert [song.track.title for song in _stored(guild).later] == ["A", "B", "C"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_skip_plays_the_queue_or_stays(monkeypatch):
    """A failed skip leaves the song. A skip with repeat and radio plays the queue."""
    await case_a_failed_skip_leaves_the_song_playing()
    with monkeypatch.context() as patch:
        await case_skip_with_repeat_and_radio_plays_the_queue(patch)

async def case_a_failed_skip_leaves_the_song_playing():
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

async def test_radio_plays_a_similar_song(monkeypatch):
    """A similar song skips a preview and waits. Adding keeps the station. Radio queues a match, falls back, and a finished station plays the queue."""
    await _scene(case_a_similar_song_skips_a_preview_and_waits, monkeypatch=monkeypatch)
    await _scene(case_adding_during_radio_keeps_the_station, monkeypatch=monkeypatch)
    await _scene(case_radio_queues_a_similar_song, monkeypatch=monkeypatch)
    await _scene(case_radio_falls_back_when_the_similar_name_is_only_this_song, monkeypatch=monkeypatch)
    await _scene(case_a_finished_station_plays_the_queue, monkeypatch=monkeypatch)

async def case_a_similar_song_skips_a_preview_and_waits(monkeypatch):
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
        monkeypatch.setattr("music.player.radio.similar_songs", AsyncMock(return_value=[]))
        marked = MagicMock(title="Snippet", length=30_000, is_preview=True, identifier="snippet")
        clip = MagicMock(title="Clip", length=30_000, is_preview=False, identifier="clip")
        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[marked, clip]))
        assert await toggle_radio(guild) is None
        assert _stored(guild).radio is True
        assert now_playing(guild).title == "I Ain't Worried"
        assert [item.track.title for item in _stored(guild).later] == ["Queued"]

        player._guild = guild
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "Clip"
        assert [item.track.title for item in _stored(guild).later] == ["Queued"]

        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[marked]))
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "Clip"
        assert _stored(guild).radio is True
        assert [item.track.title for item in _stored(guild).later] == ["Queued"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_skip_with_repeat_and_radio_plays_the_queue(monkeypatch):
    """Repeat stays on, and skip or removing the current song plays the queue."""
    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After"), _named("Later")]) == "Added 2 songs."
        track.identifier = "now"
        line = _stored(guild)
        line.repeat = True
        line.radio = True
        peace = MagicMock(title="Peace Piece", identifier="peace", length=180_000, is_preview=False)
        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        monkeypatch.setattr(
            "music.player.radio.similar_songs",
            AsyncMock(return_value=[("Bill Evans", "Peace Piece")]),
        )
        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[peace]))

        assert await next_track(guild) is None
        assert now_playing(guild).title == "After"
        assert line.repeat is True
        assert line.radio is True
        assert [song.track.title for song in line.later] == ["Later"]

        assert await drop_queued(guild, 0, member) is None
        assert now_playing(guild).title == "Later"
        assert line.repeat is True
        assert line.later == []
        assert now_playing(guild).title != "Peace Piece"
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_adding_during_radio_keeps_the_station(monkeypatch):
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
            "music.player.radio.similar_songs",
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

async def test_another_copy_keeps_this_recording(monkeypatch):
    """A bad copy or another site keeps this recording."""
    await _scene(case_a_bad_copy_keeps_this_recording, monkeypatch=monkeypatch)
    await _scene(case_a_platform_switch_keeps_this_recording, monkeypatch=monkeypatch)

async def case_a_bad_copy_keeps_this_recording(monkeypatch):
    """A missed site and a shorter copy both leave this song on."""
    with monkeypatch.context() as patch:
        await case_a_missed_platform_keeps_the_song(patch)
    with monkeypatch.context() as patch:
        await case_a_shorter_copy_keeps_the_song_and_repeat(patch)

async def case_a_missed_platform_keeps_the_song(monkeypatch):
    guild, player, _track = await _playing()
    try:
        monkeypatch.setattr(
            "music.playback.find_songs",
            AsyncMock(return_value="Nothing on Spotify matched that."),
        )
        note = await apply_playback(guild, 40, "spotify", 5_000)
        assert note == "Nothing on Spotify matched that."
        assert now_playing(guild).volume == 40
        assert now_playing(guild).title == "I Ain't Worried"
        assert _stored(guild).current.platform == "youtube"
        player.seek.assert_not_awaited()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_a_platform_switch_keeps_this_recording(monkeypatch):
    """Switching site keeps the queue and plays the recording with this title and artist."""
    with monkeypatch.context() as patch:
        await case_a_platform_switch_keeps_the_queue(patch)
    with monkeypatch.context() as patch:
        await case_a_site_change_plays_the_matching_recording(patch)

async def case_a_platform_switch_keeps_the_queue(monkeypatch):
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
            save_and_clear(guild)
            player._track = new

        player.play.side_effect = play
        monkeypatch.setattr("music.playback.find_songs", search)
        assert await apply_playback(guild, 100, "spotify", None) is None
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

async def case_a_site_change_plays_the_matching_recording(monkeypatch):
    """The first hit can be a different song. The one with this title and artist plays."""
    assert _heard_query("Air on the G String", "Air") == "Air on the G String"
    assert _heard_query("I Ain't Worried", "OneRepublic") == "I Ain't Worried OneRepublic"
    guild, player, track = await _playing()
    try:
        track.title = "Airplane"
        track.author = "Air"
        track.identifier = "old-id"
        _stored(guild).repeat = True
        wrong = _named("Airplane Mode", "Someone")
        wrong.identifier = "wrong"
        wrong.length = 180_000
        right = _named("Airplane", "Air")
        right.identifier = "right"
        right.length = 180_000
        right.uri = "https://example.com/airplane"
        asked = []

        async def search(_guild, query, platform):
            asked.append(query)
            return Found(query, platform, [wrong, right], False)

        monkeypatch.setattr("music.playback.find_songs", search)
        assert await apply_playback(guild, 100, "spotify", None) is None
        assert asked == ["Airplane Air"]
        assert _stored(guild).current.track is right
        assert _stored(guild).repeat is False
        assert player.play.await_args.args[0] is right

        async def nowhere(_guild, query, platform):
            return Found(query, platform, [wrong], False)

        monkeypatch.setattr("music.playback.find_songs", nowhere)
        assert await apply_playback(guild, 100, "youtube", None) == "Nothing on YouTube matched that."
        assert _stored(guild).current.track is right
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_a_shorter_copy_keeps_the_song_and_repeat(monkeypatch):
    """A time the other site cannot take does not replace this recording."""
    guild, player, track = await _playing()
    try:
        track.identifier = "old-id"
        line = _stored(guild)
        line.repeat = True
        short = _named("I Ain't Worried", "OneRepublic")
        short.identifier = "new-id"
        short.uri = "https://example.com/short"
        short.length = 4_000
        live = _named("I Ain't Worried", "OneRepublic")
        live.identifier = "live-id"
        live.length = 0

        async def search(*_args, **_kwargs):
            return Found("I Ain't Worried", "spotify", [short], False)

        monkeypatch.setattr("music.playback.find_songs", search)
        played = player.play.await_count
        note = await apply_playback(guild, 40, "spotify", 30_000)
        assert note == "That is past the end of the song."
        assert now_playing(guild).title == "I Ain't Worried"
        assert line.current.platform == "youtube"
        assert line.repeat is True
        assert player.play.await_count == played

        async def stream(*_args, **_kwargs):
            return Found("I Ain't Worried", "spotify", [live], False)

        monkeypatch.setattr("music.playback.find_songs", stream)
        note = await apply_playback(guild, 40, "spotify", 1_000)
        assert note == "That song cannot move."
        assert line.current.platform == "youtube"
        assert line.repeat is True
        assert player.play.await_count == played
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_a_seek_stays_put():
    """A time past the end, or a live song, does not move."""
    await case_a_time_past_the_end_stays_put()
    await case_a_live_song_stays_where_it_is()

async def case_a_time_past_the_end_stays_put():
    guild, player, track = await _playing()
    try:
        assert await apply_playback(guild, 100, None, 200_000) == "That is past the end of the song."
        player.seek.assert_not_awaited()
        assert now_playing(guild).position == 0
        assert await apply_playback(guild, 100, None, track.length - 1) is None
        player.seek.assert_awaited_once_with(track.length - 1)
        assert await apply_playback(guild, 100, None, track.length) is None
        assert not now_playing(guild).has_track
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_a_live_song_stays_where_it_is():
    guild, player, track = await _playing()
    try:
        track.length = 0
        assert await apply_playback(guild, 100, None, 1_000) == "That song cannot move."
        player.seek.assert_not_awaited()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_radio_queues_a_similar_song(monkeypatch):
    """Radio asks for a key, then queues the similar song."""
    await case_radio_asks_for_a_key_when_lastfm_is_unset()
    with monkeypatch.context() as patch:
        await case_radio_queues_the_lastfm_song(patch)

async def case_radio_asks_for_a_key_when_lastfm_is_unset():
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

async def case_radio_queues_the_lastfm_song(monkeypatch):
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
            "music.player.radio.similar_songs",
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

async def case_radio_falls_back_when_the_similar_name_is_only_this_song(monkeypatch):
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
            "music.player.radio.similar_songs",
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

async def case_a_finished_station_plays_the_queue(monkeypatch):
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

_STATION = (
    "https://www.youtube.com/live/one",
    "https://www.youtube.com/live/two",
    "https://www.youtube.com/live/three",
)


async def _on_air():
    guild, player, track = await _playing()
    track.is_stream = True
    song = _stored(guild).current
    song.videos = _STATION
    song.video_at = 0
    player._guild = guild
    return guild, player


def _ended(player, reason="finished"):
    return type("End", (), {"reason": reason, "player": player})()


async def test_a_radio_that_ends_plays_another_address(monkeypatch):
    """A short drop reloads this address. The next drop, a long play, and the last address each use a backup. A station that cannot load stops."""
    await _scene(case_a_short_drop_reloads_this_address, monkeypatch=monkeypatch)
    await _scene(case_the_next_short_drop_plays_another_address, monkeypatch=monkeypatch)
    await _scene(case_a_long_play_plays_the_next_address, monkeypatch=monkeypatch)
    await _scene(case_the_last_address_goes_back_to_the_first, monkeypatch=monkeypatch)
    await _scene(case_a_station_that_cannot_load_stops, monkeypatch=monkeypatch)


async def case_a_short_drop_reloads_this_address(monkeypatch):
    guild, player = await _on_air()
    try:
        again = _named("Lofi Girl")
        again.is_stream = True
        seen = []

        async def reload_same(_guild, song, platform="youtube", mode=None):
            seen.append(song)
            return Found(song, platform, [again], True)

        monkeypatch.setattr("music.playback.find_songs", reload_same)
        player._last_position = 4_000
        await advance_from_end(_ended(player))
        assert seen == [_STATION[0]]
        assert now_playing(guild).title == "Lofi Girl"
        assert _stored(guild).current.video_at == 0
        assert _stored(guild).current.drops == 1
        player.stop.assert_not_awaited()
    finally:
        await stop_track(guild)


async def case_the_next_short_drop_plays_another_address(monkeypatch):
    guild, player = await _on_air()
    try:
        _stored(guild).current.drops = 1
        nxt = _named("Chillhop")
        nxt.is_stream = True
        seen = []

        async def next_address(_guild, song, platform="youtube", mode=None):
            seen.append(song)
            if song == _STATION[1]:
                return Found(song, platform, [nxt], True)
            return "Nothing on YouTube matched that."

        monkeypatch.setattr("music.playback.find_songs", next_address)
        player._last_position = 4_000
        await advance_from_end(_ended(player))
        assert seen == [_STATION[1]]
        assert now_playing(guild).title == "Chillhop"
        assert _stored(guild).current.video_at == 1
        player.stop.assert_not_awaited()
    finally:
        await stop_track(guild)


async def case_a_long_play_plays_the_next_address(monkeypatch):
    guild, player = await _on_air()
    try:
        held = _named("Piano")
        held.is_stream = True
        seen = []

        async def after_a_long_play(_guild, song, platform="youtube", mode=None):
            seen.append(song)
            if song == _STATION[1]:
                return Found(song, platform, [held], True)
            return Found(song, platform, [_named("clip")], True)

        monkeypatch.setattr("music.playback.find_songs", after_a_long_play)
        player._last_position = 60_000
        await advance_from_end(_ended(player))
        assert seen == [_STATION[1]]
        assert now_playing(guild).title == "Piano"
        assert _stored(guild).current.drops == 0
        player.stop.assert_not_awaited()
    finally:
        await stop_track(guild)


async def case_the_last_address_goes_back_to_the_first(monkeypatch):
    guild, player = await _on_air()
    try:
        five = tuple(f"https://www.youtube.com/watch?v={name}" for name in "abcde")
        song = _stored(guild).current
        song.videos = five
        song.video_at = 4
        song.drops = 0
        wrapped = _named("First again")
        seen = []

        async def back_to_the_first(_guild, link, platform="youtube", mode=None):
            seen.append(link)
            if link == five[0]:
                return Found(link, platform, [wrapped], True)
            return "Nothing on YouTube matched that."

        monkeypatch.setattr("music.playback.find_songs", back_to_the_first)
        player._last_position = 3_600_000
        await advance_from_end(_ended(player))
        assert seen == [five[0]]
        assert now_playing(guild).title == "First again"
        assert _stored(guild).current.video_at == 0
        player.stop.assert_not_awaited()
    finally:
        await stop_track(guild)


async def case_a_station_that_cannot_load_stops(monkeypatch):
    guild, player = await _on_air()
    try:
        async def dead(_guild, _song, platform="youtube", mode=None):
            return "Nothing on YouTube matched that."

        monkeypatch.setattr("music.playback.find_songs", dead)
        player._last_position = 4_000
        await advance_from_end(_ended(player, "loadFailed"))
        player.stop.assert_awaited()
        assert now_playing(guild).has_track is False
    finally:
        await stop_track(guild)


def _member(player, user_id: int):
    member = MagicMock()
    member.id = user_id
    member.voice.channel = player.channel
    return member


async def case_repeat_stays_through_the_line():
    """Repeat stays on when the song ends, and when next or previous runs."""
    await case_repeat_and_radio_both_stay_when_the_song_ends()
    await case_next_and_previous_keep_repeat()

async def case_repeat_and_radio_both_stay_when_the_song_ends():
    """Repeat replays this song. Radio stays on, and the queue is not taken."""
    guild, player, _track = await _playing()
    try:
        line = _stored(guild)
        line.repeat = True
        line.radio = True
        assert await queue_tracks(_member(player, 18), guild, [_named("After")]) == "Added After."
        player._guild = guild
        player._last_position = 100_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "I Ain't Worried"
        assert line.repeat is True
        assert line.radio is True
        assert [song.track.title for song in line.later] == ["After"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_next_and_previous_keep_repeat():
    """Skip and previous leave repeat on. Only a replacement turns it off."""
    guild, player, _track = await _playing()
    try:
        line = _stored(guild)
        line.repeat = True
        player._position = 20_000
        assert await previous_track(guild) is None
        assert player.seek.await_args.args[0] == 0
        assert now_playing(guild).title == "I Ain't Worried"
        assert line.repeat is True

        later = _named("After")
        later.length = 120_000
        assert await queue_tracks(_member(player, 18), guild, [later]) == "Added After."
        assert await next_track(guild) is None
        assert now_playing(guild).title == "After"
        assert line.repeat is True

        player._position = 20_000
        player.seek.reset_mock()
        assert await previous_track(guild) is None
        assert now_playing(guild).title == "I Ain't Worried"
        player.seek.assert_not_awaited()
        assert line.repeat is True
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_a_pause_keeps_the_place_the_player_forgets():
    """The spot is read before the paused flag. A later report of the start does not win."""
    guild, player, _track = await _playing()
    try:
        player._position = 42_000
        assert await toggle_pause(guild) is None
        player._paused_mark = True
        player._last_position = 0
        assert player.position == 0
        assert _stored(guild).position == 42_000
        assert await toggle_pause(guild) is None
        assert player._position == 42_000
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)
