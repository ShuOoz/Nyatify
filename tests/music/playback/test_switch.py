"""Volume, another site's copy, and a time inside the song. No Discord login."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music.lavalink import Found
from music.playback import (
    advance_from_end,
    apply_playback,
    now_playing,
    queue_tracks,
    save_and_clear,
    stop_track,
    toggle_radio,
    _stored,
)
from music.player.radio import _heard_query

from tests.music.support import _named, _playing, _scene

async def test_a_typed_time_stays_inside_the_song():
    """A time past the end, or a live song, does not move."""
    await _scene(case_a_seek_stays_put)


async def test_volume_and_radio_stay_when_the_bot_has_left(monkeypatch):
    """Someone outside voice can retune the saved song. The bot does not join."""
    await _scene(case_volume_and_radio_stay_when_the_bot_has_left, monkeypatch=monkeypatch)

async def case_volume_and_radio_stay_when_the_bot_has_left(monkeypatch):
    from config import settings
    from music.playback.remember import _Line, _Song, _lines

    guild = MagicMock()
    guild.id = 9122
    guild.voice_client = None
    await stop_track(guild)
    track = _named("Paused", "OneRepublic")
    track.length = 180_000
    track.uri = "https://example.com/paused"
    _lines[9122] = _Line(
        current=_Song(track, 9, "youtube"), paused=True, resting=True, position=65_000,
    )
    away = MagicMock()
    away.voice = None
    away.id = 9
    try:
        assert await apply_playback(guild, 40, None, 20_000, away) is None
        line = _stored(guild)
        assert line is not None
        assert line.volume == 40
        assert line.position == 20_000
        assert line.current.platform == "youtube"
        assert guild.voice_client is None

        assert await apply_playback(guild, 30, None, 999_000, away) == "That is past the end of the song."
        assert _stored(guild).volume == 30
        assert _stored(guild).position == 20_000
        assert guild.voice_client is None

        settings.lastfm_api_key = "fm-key"
        monkeypatch.setattr("music.player.radio.load_similar", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        assert await toggle_radio(guild, away) is None
        assert _stored(guild).radio is True
        assert guild.voice_client is None

        other = _named("Paused", "OneRepublic")
        other.identifier = "new"
        other.uri = "https://example.com/spotify"
        other.length = 180_000

        async def search(*_args, **_kwargs):
            return Found("Paused OneRepublic", "spotify", [other], False)

        monkeypatch.setattr("music.playback.find_songs", search)
        assert await apply_playback(guild, 30, "spotify", None, away) is None
        assert _stored(guild).current.platform == "spotify"
        assert _stored(guild).current.track is other
        assert _stored(guild).position == 0
        assert _stored(guild).radio is True
        assert guild.voice_client is None
    finally:
        settings.lastfm_api_key = None
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
        # The replacement has played out. A finish at the start is the song it took off.
        player._started_at = 0
        player._last_position = 180_000
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
