"""Pause, skip, a similar song, and a station that ends. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import wavelink

from music.lavalink import Found
from music.playback.control import (
    back_keeps,
    pause_position,
    previous_step,
    repeat_after_replacement,
    station_ended,
    waiting_kept,
)
from music.playback import (
    advance_from_end,
    drop_queued,
    next_track,
    now_playing,
    play_now,
    previous_track,
    queue_tracks,
    repeat_track,
    shuffle_queue,
    stop_track,
    toggle_radio,
    _stored,
    toggle_pause,
)

from tests.music.support import _Player, _named, _playing, _scene

def case_a_replaced_line_keeps_the_song_the_station_started_on():
    """play() can build a new line. The station still belongs to the first song."""
    from types import SimpleNamespace

    from music.playback.control import _snapshot
    from music.playback.remember import _Line, _Song

    origin = _Song(SimpleNamespace(title="Origin"), 1, "youtube")
    carried = _snapshot(_Line(current=origin, radio=True, radio_from=origin))
    fresh = _Line(current=_Song(SimpleNamespace(title="Suggestion"), 1, "youtube"))
    for name, value in carried.items():
        setattr(fresh, name, value)
    assert fresh.radio is True
    assert fresh.radio_from is origin


async def test_a_paused_song(monkeypatch):
    """Pause stays put. Someone still in voice resumes at the saved place. A song opened from disk plays again."""
    await _scene(case_pause_stays_put, monkeypatch=monkeypatch)
    await _scene(case_a_paused_song_resumes_for_someone_still_in_voice)
    await _scene(case_a_paused_song_comes_back_where_it_stopped)
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

        monkeypatch.setattr("music.playback.idle._wait", hold)
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
        player.seek.assert_not_awaited()
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
    """Leaving voice and pressing play again starts once at the paused place."""
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
        player.play.assert_awaited_once_with(track, start=65_000, paused=False)
        player.pause.assert_not_awaited()
        assert now_playing(guild).paused is False
        assert now_playing(guild).position == 65_000
        assert player.position == 65_000
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
    """Shuffle puts the line back. Skip plays the queue or stays. Repeat stays through the line."""
    case_a_step_picks_one_move()
    await _scene(case_shuffle_puts_the_line_back, monkeypatch=monkeypatch)
    await _scene(case_skip_plays_the_queue_or_stays, monkeypatch=monkeypatch)
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
    """A failed skip leaves the song. Skip on repeat plays a waiting song. Removing it plays the queue."""
    await case_a_failed_skip_leaves_the_song_playing()
    with monkeypatch.context() as patch:
        await case_skip_with_repeat_and_radio_plays_the_queue(patch)
    with monkeypatch.context() as patch:
        await case_skip_while_repeating_names_the_song_now_on(patch)

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

async def case_radio_on_a_saved_song_does_not_need_a_player():
    """The bot has left. Radio changes the saved song, and a station still cannot start it."""
    from music.playback.remember import _Line, _Song, _lines

    guild = MagicMock()
    guild.id = 9109
    guild.voice_client = None
    await stop_track(guild)
    track = _named("Saved")
    _lines[9109] = _Line(current=_Song(track, 9, "youtube"), paused=True, resting=True)
    away = MagicMock()
    away.id = 4
    away.voice = None
    try:
        assert await toggle_radio(guild, away) == "You need a Last.fm key to do this."
        assert _lines[9109].radio is False
        track.is_stream = True
        assert await toggle_radio(guild, away) == "A station cannot start radio."
        assert _lines[9109].radio is False
    finally:
        await stop_track(guild)

async def test_radio_plays_a_similar_song(monkeypatch):
    """A similar song skips a preview and waits. Adding keeps the station. Radio queues a match, draws another song, and a finished station plays the queue."""
    await case_radio_on_a_saved_song_does_not_need_a_player()
    case_a_replaced_line_keeps_the_song_the_station_started_on()
    await _scene(case_a_similar_song_skips_a_preview_and_waits, monkeypatch=monkeypatch)
    await _scene(case_adding_during_radio_keeps_the_station, monkeypatch=monkeypatch)
    await _scene(case_radio_queues_a_similar_song, monkeypatch=monkeypatch)
    await _scene(case_radio_draws_another_song_when_the_similar_name_is_only_this_song, monkeypatch=monkeypatch)
    await _scene(case_a_finished_station_plays_the_queue, monkeypatch=monkeypatch)
    await _scene(case_back_during_radio_keeps_the_song_on_the_station, monkeypatch=monkeypatch)
    await _scene(case_radio_leaves_netease_for_apple, monkeypatch=monkeypatch)
    await _scene(case_radio_plays_a_title_the_site_spells_differently, monkeypatch=monkeypatch)
    await _scene(case_skip_on_radio_plays_the_suggestion_while_repeat_is_on, monkeypatch=monkeypatch)
    await _scene(case_radio_finds_the_song_on_youtube_when_spotify_misses, monkeypatch=monkeypatch)
    await _scene(case_radio_stays_about_the_song_that_started_it, monkeypatch=monkeypatch)

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
        monkeypatch.setattr("music.player.radio.load_similar", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            "music.player.radio.top_tracks",
            AsyncMock(return_value=[("Someone", "Clip")]),
        )
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
        assert now_playing(guild).title == "Queued"
        assert _stored(guild).radio is False
        assert [item.track.title for item in _stored(guild).later] == []
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_skip_with_repeat_and_radio_plays_the_queue(monkeypatch):
    """Skip plays the queue. Radio does not take the song, and repeat stays on."""
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
            "music.player.radio.load_similar",
            AsyncMock(return_value=[("Bill Evans", "Peace Piece")]),
        )
        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[peace]))

        player._position = 20_000
        assert await next_track(guild) is None
        assert now_playing(guild).title == "After"
        assert line.repeat is True
        assert line.radio is True
        assert [song.track.title for song in line.later] == ["Later"]
        assert now_playing(guild).title != "Peace Piece"

        assert await drop_queued(guild, 0, member) is None
        assert now_playing(guild).title == "Later"
        assert line.repeat is True
        assert line.later == []
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_skip_while_repeating_names_the_song_now_on(monkeypatch):
    """Skip plays the song that is waiting. Ending that song does not ask again."""
    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        later = _named("After", "Oasis")
        later.length = 120_000
        later.identifier = "after"
        assert await queue_tracks(member, guild, [later]) == "Added After."
        track.identifier = "now"
        asked: list[str] = []

        async def similar(artist, title, **_kwargs):
            asked.append(title)
            if title == "After":
                return [("Blur", "Song 2")]
            return [("Oasis", "Wonderwall")]

        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        monkeypatch.setattr("music.player.radio.load_similar", similar)
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        assert await repeat_track(guild) is None
        assert await toggle_radio(guild) is None
        line = _stored(guild)
        assert line.radio_next == ("Oasis", "Wonderwall")
        assert now_playing(guild).upcoming == "After - Oasis"

        asked_before = len(asked)
        assert await next_track(guild) is None
        assert now_playing(guild).title == "After"
        assert line.repeat is True
        assert line.radio is True
        assert line.later == []
        assert line.radio_next == ("Blur", "Song 2")
        assert now_playing(guild).radio_next == "Song 2"
        assert len(asked) > asked_before

        seen = len(asked)
        player._guild = guild
        player._last_position = 120_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "After"
        assert line.radio_next == ("Blur", "Song 2")
        assert len(asked) == seen

        monkeypatch.setattr("music.player.radio.load_similar", AsyncMock(return_value=[]))
        again = _named("Later", "Blur")
        again.length = 90_000
        again.identifier = "later"
        assert await queue_tracks(member, guild, [again]) == "Added Later."
        assert await next_track(guild) is None
        assert now_playing(guild).title == "Later"
        assert line.later == []
        assert line.radio is True
        assert line.repeat is True
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
            "music.player.radio.load_similar",
            AsyncMock(return_value=[("Bill Evans", "Peace Piece")]),
        )
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
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

async def test_a_skip_does_not_take_the_song_it_just_started():
    """The end reported as a skip lands is the song it replaced, not the one now on."""
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        nxt = _named("Midnight City")
        nxt.identifier = "midnight"
        nxt.uri = "https://example.com/midnight"
        nxt.length = 180_000
        after = _named("After")
        after.length = 180_000
        assert await queue_tracks(member, guild, [nxt, after]) == "Added 2 songs."
        assert await next_track(guild) is None
        assert now_playing(guild).title == "Midnight City"
        player._guild = guild
        player._last_position = 0
        started = MagicMock(identifier="midnight", uri="https://example.com/midnight", title="Midnight City")
        await advance_from_end(type("End", (), {"reason": "finished", "player": player, "track": started})())
        assert now_playing(guild).title == "Midnight City"
        assert [song.track.title for song in _stored(guild).later] == ["After"]
        player._started_at = 0
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player, "track": started})())
        assert now_playing(guild).title == "After"
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_a_cleanup_keeps_the_song_that_started():
    """Switching nodes reports cleanup. That is not the end of the song on the line."""
    guild, player, track = await _playing()
    try:
        player._guild = guild
        player._track = None
        await advance_from_end(type("End", (), {"reason": "cleanup", "player": player})())
        assert player.current is track
        assert now_playing(guild).title == "I Ain't Worried"
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_radio_leaves_netease_for_apple(monkeypatch):
    """A NetEase song continues on Apple Music, which Lavalink can search."""
    guild, player, track = await _playing()
    try:
        _stored(guild).current.platform = "netease"
        _stored(guild).radio = True
        track.identifier = "now"
        peace = MagicMock(
            title="Peace Piece", author="Bill Evans", identifier="peace",
            length=180_000, is_preview=False,
        )
        asked = []

        async def search(_query, platform, node=None):
            asked.append(platform)
            return [peace]

        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        monkeypatch.setattr(
            "music.player.radio.load_similar",
            AsyncMock(return_value=[("Bill Evans", "Peace Piece")]),
        )
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.source.search_track", search)
        player._guild = guild
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert asked == ["apple"]
        assert _stored(guild).current.platform == "apple"
        assert now_playing(guild).title == "Peace Piece"
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
            "music.player.radio.load_similar",
            AsyncMock(return_value=[("Bill Evans", "Peace Piece")]),
        )
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.source.search_track", search)
        assert await toggle_radio(guild) is None
        assert _stored(guild).radio_next == ("Bill Evans", "Peace Piece")
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

async def case_skip_on_radio_plays_the_suggestion_while_repeat_is_on(monkeypatch):
    """Repeat is on and nothing is queued. Skip plays the radio song instead of rewinding."""
    guild, player, track = await _playing()
    try:
        track.identifier = "now"
        line = _stored(guild)
        line.repeat = True
        line.radio = True
        line.radio_next = ("Knx.", "Kanaloap(Layte)")
        line.radio_plan = [("Knx.", "Kanaloap(Layte)")]
        found = MagicMock(
            title="Kanaloap(Layte)", author="Knx.", identifier="next",
            length=120_000, is_preview=False,
        )
        monkeypatch.setattr("music.player.radio.load_similar", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[found]))
        player._position = 20_000
        assert await next_track(guild) is None
        assert now_playing(guild).title == "Kanaloap(Layte)"
        assert line.repeat is True
        assert line.radio is True
        assert line.later == []
        player.seek.assert_not_awaited()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_radio_finds_the_song_on_youtube_when_spotify_misses(monkeypatch):
    """Spotify has no recording of this name. YouTube does, and that is what plays."""
    guild, player, track = await _playing()
    try:
        _stored(guild).current.platform = "spotify"
        track.identifier = "now"
        jive = MagicMock(
            title="Knxwledge. - Jive (SKR\u2206WBERRiES)", author="GodsConnect2", identifier="jive",
            length=180_000, is_preview=False,
        )
        asked = []

        async def search(_query, platform, node=None):
            asked.append(platform)
            if platform == "spotify":
                return [MagicMock(title="Something Else", author="Whoever", identifier="else")]
            return [jive]

        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        monkeypatch.setattr(
            "music.player.radio.load_similar",
            AsyncMock(return_value=[("Knx.", "Jive")]),
        )
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.source.search_track", search)
        assert await toggle_radio(guild) is None
        player._guild = guild
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert asked == ["spotify", "youtube"]
        assert now_playing(guild).title == "Knxwledge. - Jive (SKR\u2206WBERRiES)"
        assert _stored(guild).current.platform == "youtube"
        assert _stored(guild).radio is True
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def _finish_radio():
    for _turn in range(6):
        pending = [
            task for task in asyncio.all_tasks()
            if task.get_name() == "music-radio" and not task.done()
        ]
        if not pending:
            return
        await asyncio.gather(*pending)


async def case_radio_stays_about_the_song_that_started_it(monkeypatch):
    """A suggestion plays. The next name is still about the song radio started on."""
    guild, player, track = await _playing()
    try:
        track.identifier = "now"
        asked = []

        async def similar(artist, title):
            asked.append((artist, title))
            if title == "I Ain't Worried":
                return [("Near", "Closer"), ("Near", "Beside")]
            return [("Far", "Drifted")]

        async def search(query, _platform, node=None):
            title = query.split(" ", 1)[0]
            return [MagicMock(
                title=title, author="Near", identifier=title, length=180_000, is_preview=False,
            )]

        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        monkeypatch.setattr("music.player.radio.load_similar", similar)
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.source.search_track", search)
        assert await toggle_radio(guild) is None
        await _finish_radio()
        player._guild = guild
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        await _finish_radio()
        assert asked
        assert {title for _artist, title in asked} == {"I Ain't Worried"}
        assert _stored(guild).radio_from.track.title == "I Ain't Worried"
        assert now_playing(guild).title in {"Closer", "Beside"}
        assert _stored(guild).radio is True
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_radio_plays_a_title_the_site_spells_differently(monkeypatch):
    """Last.fm writes Don't. The recording writes Don’t. That is still the song, and the next draw leaves it."""
    guild, player, track = await _playing()
    try:
        track.identifier = "now"
        found = MagicMock(
            title="Don\u2019t Look Back in Anger", author="Oasis", identifier="anger",
            length=180_000, is_preview=False,
        )
        later = MagicMock(
            title="Live Forever", author="Oasis", identifier="forever",
            length=180_000, is_preview=False,
        )

        async def search(query, _platform, node=None):
            if "Live Forever" in query:
                return [later]
            return [found]

        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        monkeypatch.setattr(
            "music.player.radio.load_similar",
            AsyncMock(return_value=[("Oasis", "Don't Look Back in Anger"), ("Oasis", "Live Forever")]),
        )
        monkeypatch.setattr(
            "music.player.radio.weighted_order",
            lambda weights, rolls: list(range(len(weights))),
        )
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.source.search_track", search)
        assert await toggle_radio(guild) is None
        await _finish_radio()
        player._guild = guild
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "Don\u2019t Look Back in Anger"
        assert _stored(guild).radio is True
        assert _stored(guild).later == []
        await _finish_radio()
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "Live Forever"
        await _finish_radio()
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        stayed = _stored(guild)
        assert stayed is None or stayed.current is None or stayed.current.track is not found
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_radio_draws_another_song_when_the_similar_name_is_only_this_song(monkeypatch):
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
            "music.player.radio.load_similar",
            AsyncMock(return_value=[("Nobody", "Missing")]),
        )
        monkeypatch.setattr(
            "music.player.radio.top_tracks",
            AsyncMock(return_value=[("OneRepublic", "Counting Stars")]),
        )
        monkeypatch.setattr("music.source.search_track", search)
        assert await toggle_radio(guild) is None
        assert asked == []
        assert now_playing(guild).title == "I Ain't Worried"
        _player._guild = guild
        _player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": _player})())
        assert asked == ["Missing Nobody", "Counting Stars OneRepublic"]
        assert now_playing(guild).title == "Counting Stars"
        assert _stored(guild).later == []
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_back_during_radio_keeps_the_song_on_the_station(monkeypatch):
    """Back during radio leaves the queue alone and returns to the song just left."""
    guild, player, track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        track.identifier = "now"
        peace = MagicMock(
            title="Peace Piece", author="Bill Evans", identifier="peace",
            length=180_000, is_preview=False,
        )
        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        monkeypatch.setattr(
            "music.player.radio.load_similar",
            AsyncMock(return_value=[("Bill Evans", "Peace Piece")]),
        )
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[peace]))
        assert await toggle_radio(guild) is None
        player._guild = guild
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "Peace Piece"
        player._position = 20_000
        assert await previous_track(guild) is None
        assert now_playing(guild).title == "I Ain't Worried"
        assert [item.track.title for item in _stored(guild).later] == ["After"]
        assert _stored(guild).radio_next == ("Bill Evans", "Peace Piece")
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "Peace Piece"
        assert [item.track.title for item in _stored(guild).later] == ["After"]
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
    await case_skip_still_moves_when_this_song_cannot_repeat()

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
    """Skip plays a song that is waiting. An empty queue starts this song again. Previous leaves repeat on."""
    guild, player, _track = await _playing()
    try:
        line = _stored(guild)
        line.repeat = True
        player._position = 20_000
        assert await previous_track(guild) is None
        assert player.seek.await_args.args[0] == 0
        assert now_playing(guild).title == "I Ain't Worried"
        assert line.repeat is True

        player._position = 20_000
        player.seek.reset_mock()
        assert await next_track(guild) is None
        assert now_playing(guild).title == "I Ain't Worried"
        assert now_playing(guild).position == 0
        assert line.repeat is True

        later = _named("After")
        later.length = 120_000
        assert await queue_tracks(_member(player, 18), guild, [later]) == "Added After."
        assert now_playing(guild).upcoming == "After"
        player._position = 20_000
        player.seek.reset_mock()
        assert await next_track(guild) is None
        assert now_playing(guild).title == "After"
        assert [song.track.title for song in line.later] == []
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


async def case_skip_still_moves_when_this_song_cannot_repeat():
    """Repeat off, a stream, and a station still leave this song. The last song still stops."""
    guild, player, track = await _playing()
    try:
        member = _member(player, 18)
        later = _named("After")
        later.length = 120_000
        assert await queue_tracks(member, guild, [later]) == "Added After."
        assert await next_track(guild) is None
        assert now_playing(guild).title == "After"
        assert _stored(guild).repeat is False
        assert _stored(guild).later == []

        first = _stored(guild).current
        first.track.length = 0
        first.track.is_stream = True
        queued_song = _named("Next")
        queued_song.length = 90_000
        assert await queue_tracks(member, guild, [queued_song]) == "Added Next."
        _stored(guild).repeat = True
        assert await next_track(guild) is None
        assert now_playing(guild).title == "Next"
        assert _stored(guild).repeat is True

        station = _stored(guild).current
        station.videos = ("https://www.youtube.com/watch?v=live",)
        station.track.length = 180_000
        station.track.is_stream = False
        again = _named("Later")
        again.length = 90_000
        assert await queue_tracks(member, guild, [again]) == "Added Later."
        assert await next_track(guild) is None
        assert now_playing(guild).title == "Later"
        assert _stored(guild).repeat is True
        assert _stored(guild).later == []

        _stored(guild).repeat = False
        player._guild = guild
        assert await next_track(guild) is None
        assert now_playing(guild).has_track is False
        assert _stored(guild).repeat is False
    finally:
        track.is_stream = False
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


async def test_a_move_while_shuffled_stays_when_shuffle_turns_off(monkeypatch):
    """Moving two waiting songs replaces the order shuffle would restore."""
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("One"), _named("Two"), _named("Three")]) == (
            "Added 3 songs."
        )
        monkeypatch.setattr("music.playback.control.random.shuffle", lambda songs: songs.reverse())
        assert await shuffle_queue(guild) is None
        assert [item.track.title for item in _stored(guild).later] == ["Three", "Two", "One"]
        from music.playback import move_queued
        assert await move_queued(guild, 1, 2) is None
        assert [item.track.title for item in _stored(guild).later] == ["Two", "Three", "One"]
        assert await shuffle_queue(guild) is None
        assert _stored(guild).shuffle is False
        assert [item.track.title for item in _stored(guild).later] == ["Two", "Three", "One"]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

def case_a_step_picks_one_move():
    """Repeat, the station, and back each pick the one move that keeps the song."""
    assert waiting_kept(3, cap=15) == 3
    assert waiting_kept(16, cap=15) == 15
    assert repeat_after_replacement(True, replaced=True) is False
    assert repeat_after_replacement(True, replaced=False) is True
    assert station_ended(True) == "play"
    assert station_ended(False) == "release"
    assert back_keeps(True, False) == "radio"
    assert back_keeps(True, True) == "queue"
    assert back_keeps(False, False) == "queue"
    assert previous_step(movable=True, earlier=True, playing=False) == "back"
    assert previous_step(movable=True, earlier=False, playing=False) == "resume"
    assert previous_step(movable=False, earlier=False, playing=True) == "stay"
    assert previous_step(movable=True, earlier=False, playing=True) == "restart"
    assert pause_position(0, 5_000) == 5_000
    assert pause_position(8_000, 5_000) == 8_000
