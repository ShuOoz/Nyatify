"""Songs prepared while one is playing, and a saved list opening. No Discord login."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import wavelink

from announce import notice_text, quote_text
from music.archive import SavedSong
from music.playback import (
    QUEUE_SONGS,
    advance_from_end,
    clear_queued,
    finish_saved,
    next_track,
    now_playing,
    play_now,
    play_saved,
    queue_tracks,
    stop_track,
    toggle_radio,
    _stored,
)

from tests.music.support import (
    _card_text,
    _named,
    _playing,
    _room,
    _scene,
    _waiting,
)

async def test_a_prepared_song_is_what_the_skip_plays(monkeypatch):
    """One searched song starts. A song that will not start is left out, and a queued song stays available."""
    await _scene(case_a_prepared_song_starts_before_the_others_are_ready, monkeypatch=monkeypatch)
    await _scene(case_a_failed_prepared_song_tries_the_next_one, monkeypatch=monkeypatch)
    await _scene(case_a_missed_start_stays_out_of_the_station, monkeypatch=monkeypatch)
    await _scene(case_a_queued_song_stays_available_to_the_station, monkeypatch=monkeypatch)


async def case_a_prepared_song_starts_before_the_others_are_ready(monkeypatch):
    """One searched song starts. The skip does not wait for the other two."""
    from music.playback.prepare import _Ahead
    from music.playback.remember import _Song

    guild, player, track = await _playing()
    started = asyncio.Event()
    try:
        track.identifier = "now"
        ready = MagicMock(
            title="Ready Song", author="Ada", identifier="ready",
            length=180_000, uri="https://example.com/ready",
        )
        line = _stored(guild)
        line.radio = True
        line.radio_ready = [_Ahead("Ada", "Ready Song", _Song(ready, 17, "youtube"))]
        line.radio_plan = [("Someone", "Later")]
        line.radio_next = ("Ada", "Ready Song")
        searched = []

        async def hang(*_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()
            return []

        async def refused(*_args, **_kwargs):
            searched.append("again")
            return None

        monkeypatch.setattr("music.lavalink._named_node", lambda _name: object())
        monkeypatch.setattr("music.source.search_track", hang)
        monkeypatch.setattr("music.playback.prepare.another_track", refused)
        monkeypatch.setattr("music.player.radio.load_similar", AsyncMock(return_value=[]))
        player._guild = guild
        player._last_position = 180_000
        await asyncio.wait_for(
            advance_from_end(type("End", (), {"reason": "finished", "player": player})()),
            timeout=2,
        )
        assert searched == []
        assert now_playing(guild).title == "Ready Song"
        assert _stored(guild).radio is True
        assert _stored(guild).later == []
        await asyncio.wait_for(started.wait(), timeout=2)
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_a_failed_prepared_song_tries_the_next_one(monkeypatch):
    """One prepared song that will not start leaves the station on the next one."""
    import wavelink

    from music.playback.prepare import _Ahead
    from music.playback.remember import _Song

    guild, player, track = await _playing()
    try:
        track.identifier = "now"
        bad = MagicMock(
            title="Bad Song", author="Ada", identifier="bad",
            length=180_000, uri="https://example.com/bad",
        )
        good = MagicMock(
            title="Good Song", author="Bea", identifier="good",
            length=180_000, uri="https://example.com/good",
        )
        line = _stored(guild)
        line.radio = True
        line.radio_ready = [
            _Ahead("Ada", "Bad Song", _Song(bad, 17, "youtube")),
            _Ahead("Bea", "Good Song", _Song(good, 17, "youtube")),
        ]
        line.radio_next = ("Ada", "Bad Song")

        async def play(song, **_kwargs):
            if getattr(song, "identifier", None) == "bad":
                raise wavelink.WavelinkException("that song did not start")

        monkeypatch.setattr(player, "play", play)
        monkeypatch.setattr("music.lavalink._named_node", lambda _name: object())
        monkeypatch.setattr("music.player.radio.load_similar", AsyncMock(return_value=[]))
        player._guild = guild
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert now_playing(guild).title == "Good Song"
        assert _stored(guild).radio is True
        assert _stored(guild).later == []
        assert ("Ada", "Bad Song") in _stored(guild).radio_miss
        # Lavalink reports the failed start after the next song is already on.
        await advance_from_end(type("End", (), {"reason": "loadFailed", "player": player, "track": bad})())
        assert now_playing(guild).title == "Good Song"
        assert _stored(guild).radio is True
        assert _stored(guild).later == []
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_a_missed_start_stays_out_of_the_station(monkeypatch):
    """A song that would not start is not searched again, and its failure does not skip the next one."""
    import wavelink

    from music.playback.prepare import _Ahead, _ahead_heard
    from music.playback.remember import _Song

    guild, player, track = await _playing()
    try:
        track.identifier = "now"
        bad = MagicMock(
            title="Bad Song", author="Ada", identifier="bad",
            length=180_000, uri="https://example.com/bad",
        )
        other = MagicMock(
            title="Other Song", author="Cy", identifier="other",
            length=180_000, uri="https://example.com/other",
        )
        line = _stored(guild)
        line.radio = True
        line.radio_ready = [_Ahead("Ada", "Bad Song", _Song(bad, 17, "youtube"))]
        line.radio_plan = [("Ada", "Bad Song")]
        line.radio_next = ("Ada", "Bad Song")
        seen = {}

        async def draw(_song, heard=None, preferred=None):
            seen["heard"] = set(heard or ())
            seen["preferred"] = preferred
            return other

        async def play(song, **_kwargs):
            if getattr(song, "identifier", None) == "bad":
                raise wavelink.WavelinkException("that song did not start")

        monkeypatch.setattr(player, "play", play)
        monkeypatch.setattr("music.playback.prepare.another_track", draw)
        monkeypatch.setattr("music.lavalink._named_node", lambda _name: None)
        player._guild = guild
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert seen["preferred"] is None
        assert ("ada", "bad song") in seen["heard"]
        assert ("ada", "bad song") in _ahead_heard(guild, _stored(guild))
        assert now_playing(guild).title == "Other Song"
        assert _stored(guild).radio is True
        await advance_from_end(type("End", (), {"reason": "loadFailed", "player": player, "track": bad})())
        assert now_playing(guild).title == "Other Song"
        assert _stored(guild).later == []
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_a_queued_song_stays_available_to_the_station(monkeypatch):
    """A song the user added is not treated as already heard."""
    from music.playback.remember import _Song, _heard

    guild, player, track = await _playing()
    prior: list = []
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("Peace Piece", "Bill Evans")]) == "Added Peace Piece."
        track.identifier = "now"
        prior = list(_heard.get(guild.id, ()))
        _heard.setdefault(guild.id, []).append(_Song(_named("Old Song", "Old Artist"), None, "youtube"))
        seen: dict[str, set] = {}

        async def draw(_song, guild_id=None, heard=None, preferred=None):
            seen["heard"] = set(heard or ())
            return MagicMock(title="Peace Piece", author="Bill Evans", identifier="peace", length=180_000)

        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        monkeypatch.setattr("music.player.radio.load_similar", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.playback.prepare.another_track", draw)
        assert await toggle_radio(guild) is None
        player._guild = guild
        player._last_position = 180_000
        await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
        assert ("bill evans", "peace piece") not in seen["heard"]
        assert ("onerepublic", "i ain't worried") in seen["heard"]
        assert ("old artist", "old song") in seen["heard"]
        assert [item.track.title for item in _stored(guild).later] == ["Peace Piece"]
    finally:
        if prior:
            _heard[guild.id] = prior
        else:
            _heard.pop(guild.id, None)
        if guild.voice_client is not None:
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
        # The rest are already queued. Their links open one at a time.
        assert peak == 1
        guild.voice_client = player
        assert now_playing(guild).title == "One"
        assert _waiting(guild) == ["Two", "Three"]
        release.set()
        await finish_saved(guild)
        assert peak == 1
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
        assert [song.track.title for song in _stored(guild).held] == [
            str(index) for index in range(QUEUE_SONGS + 1, count)
        ]
        assert await next_track(guild) is None
        assert _waiting(guild)[-1] == str(QUEUE_SONGS + 1)
        assert await queue_tracks(member, guild, [_named("Extra")]) == "The queue is full."
    finally:
        await finish_saved(guild)
        await stop_track(guild)

async def test_an_added_playlist_keeps_the_song_and_the_queue(monkeypatch):
    """Adding a list waits behind the song on now. The queue and the station stay."""
    guild, member, player = _room(8804)
    await stop_track(guild)

    async def open_one(_guild, song):
        if song.title == "Skip":
            return None, "Skip", "Nothing on YouTube matched that."
        return (_named(song.title), song.platform), song.title, ""

    monkeypatch.setattr("music.playback.open_one", open_one)
    songs = [
        SavedSong("Skip", "youtube", "https://example.com/skip"),
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
        held.repeat = True

        note = await play_saved(member, guild, songs, replace=False)
        assert note == "Added 2 songs. Skipped Skip."
        await finish_saved(guild)
        assert now_playing(guild).title == "Stay"
        assert _waiting(guild) == ["Queued", "One", "Two"]
        assert _stored(guild).radio is True
        assert _stored(guild).repeat is True
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
        channel = MagicMock()
        channel.send = AsyncMock()
        note = await asyncio.wait_for(
            play_saved(member, guild, songs, replace=False, channel=channel), timeout=2,
        )
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
        assert "Skipped" not in _card_text(view)
        channel.send.assert_awaited_once()
        sent = channel.send.await_args.kwargs
        assert sent["content"] == quote_text(notice_text("Skipped Two, Four."))
        assert sent["allowed_mentions"].everyone is False
        view.message.edit.assert_awaited()
    finally:
        release.set()
        await finish_saved(guild)
        if view is not None:
            view.stop()
        await stop_track(guild)
