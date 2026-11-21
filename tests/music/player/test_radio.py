"""Last.fm names a similar song, and the key box turns radio on. No Discord login and no network."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import discord

from music.playback import (
    _stored,
    clear_queued,
    now_playing,
    queue_tracks,
    stop_track,
)
from music.player.card import MusicPlayer
from types import SimpleNamespace

from music.player.radio import (
    _chosen,
    another_track,
    channel_author,
    radio_seed,
    read_artists,
    read_similar,
    read_top,
    same_song,
    same_title,
    similar_songs,
    weighted_order,
)

from tests.music.support import _named, _playing, _press


async def test_radio_names_a_similar_song(monkeypatch):
    """The seed keeps a real artist. A bad answer, or no key, queues nothing."""
    assert radio_seed("It's Always You", "Chet Baker") == ("Chet Baker", "It's Always You")
    assert radio_seed("It's Always You", "Chet Baker - Topic") == ("Chet Baker", "It's Always You")
    assert radio_seed(
        "Chet Baker - It's Always You (Official Audio)",
        "ChetBakerVEVO",
    ) == ("Chet Baker", "It's Always You")
    assert radio_seed("It's Always You (Live)", "Chet Baker") == ("Chet Baker", "It's Always You (Live)")
    assert radio_seed("It's Always You", "") is None
    assert radio_seed("Air on the G String", "Air") == ("Air", "Air on the G String")
    assert radio_seed("Wait", "AI") == ("AI", "Wait")
    assert radio_seed("Chet Baker - It's Always You", "Chet Baker") == ("Chet Baker", "It's Always You")

    payload = {
        "similartracks": {
            "track": [
                {"name": "Peace Piece", "artist": {"name": "Bill Evans"}},
                {"name": "", "artist": {"name": "Nobody"}},
            ]
        }
    }
    assert read_similar(payload) == [("Bill Evans", "Peace Piece")]
    assert read_similar({"similartracks": {"track": {"name": "So What", "artist": {"name": "Miles Davis"}}}}) == [
        ("Miles Davis", "So What"),
    ]
    assert read_similar({"error": 6, "message": "missing"}) == []
    assert read_similar([]) == []
    assert read_top({"toptracks": {"track": [{"name": "Counting Stars", "artist": {"name": "OneRepublic"}}]}}) == [
        ("OneRepublic", "Counting Stars"),
    ]
    assert read_top({"error": 6}) == []

    monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", None)

    async def unused(_url: str) -> str:
        raise AssertionError("no key")

    assert await similar_songs("Chet Baker", "It's Always You", getter=unused) == []

    monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
    seen: list[str] = []

    async def getter(url: str) -> str:
        seen.append(url)
        return json.dumps({
            "similartracks": {"track": [{"name": "Peace Piece", "artist": {"name": "Bill Evans"}}]}
        })

    assert await similar_songs("Chet Baker", "It's Always You", getter=getter) == [
        ("Bill Evans", "Peace Piece"),
    ]
    assert "method=track.getsimilar" in seen[0]
    assert "artist=Chet+Baker" in seen[0]
    assert "autocorrect=0" in seen[0]
    assert "limit=30" in seen[0]
    assert "api_key=fm-key" in seen[0]

    async def broken(_url: str) -> str:
        raise TimeoutError("slow")

    assert await similar_songs("Chet Baker", "It's Always You", getter=broken) == []

    async def error(_url: str) -> str:
        return json.dumps({"error": 10, "message": "Invalid API key"})

    assert await similar_songs("Chet Baker", "It's Always You", getter=error) == []

    # The closest Last.fm name is first. A draw can play a later song, and a
    # nearer title in the search is not that song.
    song = MagicMock()
    song.platform = "youtube"
    song.track = MagicMock(title="Wonderwall", author="Oasis", identifier="now")
    closest = MagicMock(title="Don't Look Back in Anger", author="Oasis", identifier="closest")
    drawn = MagicMock(title="Song 2", author="Blur", identifier="drawn")
    lookalike = MagicMock(title="Song Two", author="Blur", identifier="like")
    asked: list[str] = []

    async def search(query: str, _platform: str):
        asked.append(query)
        if query == "Song 2 Blur":
            return [lookalike, drawn]
        return [closest]

    monkeypatch.setattr(
        "music.player.radio.load_similar",
        AsyncMock(return_value=[("Oasis", "Don't Look Back in Anger"), ("Blur", "Song 2")]),
    )
    monkeypatch.setattr(
        "music.player.radio.weighted_order",
        lambda weights, rolls: list(reversed(range(len(weights)))),
    )
    monkeypatch.setattr("music.source.search_track", search)
    assert await another_track(song) is drawn
    assert asked == ["Song 2 Blur"]


async def test_radio_searches_lavalink(monkeypatch):
    """A NetEase song is looked up on Apple Music, and the search stays on Lavalink."""
    case_a_match_is_this_song()
    song = MagicMock()
    song.platform = "netease"
    song.track = MagicMock(title="Wonderwall", author="Oasis", identifier="now")
    home = object()
    seen = {}

    async def search(query, platform, node=None):
        seen["query"] = query
        seen["platform"] = platform
        seen["node"] = node
        return [MagicMock(title="Don't Look Back in Anger", author="Oasis", identifier="next")]

    monkeypatch.setattr("music.lavalink._named_node", lambda name: home if name == "lavalink" else object())
    monkeypatch.setattr("music.source.search_track", search)
    monkeypatch.setattr(
        "music.player.radio.load_similar",
        AsyncMock(return_value=[("Oasis", "Don't Look Back in Anger")]),
    )
    found = await another_track(song)
    assert found.identifier == "next"
    assert seen == {
        "query": "Don't Look Back in Anger Oasis",
        "platform": "apple",
        "node": home,
    }


def _station_song(title: str = "Wonderwall", author: str = "Oasis"):
    song = MagicMock()
    song.platform = "youtube"
    song.track = MagicMock(title=title, author=author, identifier="now")
    return song


def _hit(query: str):
    title, artist = query.split(" ", 1)
    return MagicMock(title=title, author=artist, identifier=title)


async def test_a_similar_song_does_not_open_the_artist_list(monkeypatch):
    """Other songs and nearby artists wait until this song's similar list cannot be played."""
    song = _station_song()
    tops = AsyncMock(return_value=[("Oasis", "Live")])
    artists = AsyncMock(return_value=["Blur"])

    async def search(query, _platform):
        return [_hit(query)]

    monkeypatch.setattr(
        "music.player.radio.load_similar",
        AsyncMock(return_value=[("Blur", "Song", 1.0)]),
    )
    monkeypatch.setattr("music.player.radio.top_tracks", tops)
    monkeypatch.setattr("music.player.radio.similar_artists", artists)
    monkeypatch.setattr("music.source.search_track", search)
    assert (await another_track(song)).title == "Song"
    tops.assert_not_awaited()
    artists.assert_not_awaited()


async def test_a_channel_upload_counts_as_the_song(monkeypatch):
    """A VEVO author is the channel. The title is still the song."""
    song = _station_song()
    upload = MagicMock(title="Don't Look Back in Anger", author="OasisVEVO", identifier="vevo")

    async def search(_query, _platform):
        return [upload]

    monkeypatch.setattr(
        "music.player.radio.load_similar",
        AsyncMock(return_value=[("Oasis", "Don't Look Back in Anger")]),
    )
    monkeypatch.setattr("music.source.search_track", search)
    assert await another_track(song) is upload


async def test_each_song_is_drawn_from_the_one_playing(monkeypatch):
    """The next song follows the one on now. A closer match comes up first when the rolls tie."""
    fetched: list[str] = []

    async def similar(_artist, title, **_kwargs):
        fetched.append(title)
        return [("Oasis", "One", 0.2), ("Blur", "Two", 0.9)]

    async def search(query, _platform):
        return [_hit(query)]

    monkeypatch.setattr("music.player.radio.load_similar", similar)
    monkeypatch.setattr("music.player.radio.random.random", lambda: 0.5)
    monkeypatch.setattr("music.source.search_track", search)
    song = _station_song()
    first = await another_track(song)
    second = await another_track(song, heard={("blur", "two")})
    assert first.title == "Two"
    assert second.title == "One"
    assert fetched == ["Wonderwall", "Wonderwall"]


async def test_a_heard_list_opens_the_next_pool(monkeypatch):
    """Similar songs already heard are not drawn again. The artist's other songs are."""
    assert read_artists({"similarartists": {"artist": [{"name": "Blur"}, {"name": ""}]}}) == ["Blur"]
    assert read_artists({"error": 6}) == []
    song = _station_song()
    heard = {("oasis", "one"), ("blur", "two")}
    asked: list[str] = []

    async def search(query, _platform):
        asked.append(query)
        return [_hit(query)]

    monkeypatch.setattr(
        "music.player.radio.load_similar",
        AsyncMock(return_value=[("Oasis", "One"), ("Blur", "Two")]),
    )
    monkeypatch.setattr(
        "music.player.radio.top_tracks",
        AsyncMock(return_value=[("Oasis", "Live")]),
    )
    monkeypatch.setattr("music.source.search_track", search)
    found = await another_track(song, heard=heard)
    assert found.title == "Live"
    assert asked == ["Live Oasis"]


async def test_a_finished_artist_draws_from_a_nearby_one(monkeypatch):
    """No similar songs and no other songs by this artist opens a nearby artist's list."""
    song = _station_song()
    asked: list[str] = []

    async def tops(artist, **_kwargs):
        if artist == "Blur":
            return [("Blur", "Song")]
        return []

    async def search(query, _platform):
        asked.append(query)
        return [_hit(query)]

    monkeypatch.setattr("music.player.radio.load_similar", AsyncMock(return_value=[]))
    monkeypatch.setattr("music.player.radio.top_tracks", tops)
    monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=["Oasis", "Blur"]))
    monkeypatch.setattr("music.player.radio.random.shuffle", lambda tracks: tracks.reverse())
    monkeypatch.setattr("music.source.search_track", search)
    found = await another_track(song)
    assert found.title == "Song"
    assert asked == ["Song Blur"]

async def test_repeat_keeps_a_song_when_the_queue_is_empty(monkeypatch):
    """On Repeat leaves Up next on the queue. An empty queue stays Nothing.

    Radio keeps that line. The song that is on is not named there.
    """
    guild, player, _track = await _playing()
    try:
        view = MusicPlayer(guild)
        press = _press(player)
        await view.repeat.callback(press)
        assert _stored(guild).repeat is True
        assert view.meta.content == "-# On Repeat\n-# Up next: Nothing"

        monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
        monkeypatch.setattr(
            "music.player.radio.load_similar",
            AsyncMock(return_value=[("Oasis", "Wonderwall")]),
        )
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        await view.radio.callback(press)
        assert _stored(guild).radio is True
        assert _stored(guild).repeat is True
        assert _stored(guild).radio_next == ("Oasis", "Wonderwall")
        assert view.radio.style == discord.ButtonStyle.primary
        assert view.meta.content == "-# On Repeat + Radio\n-# Up next: Nothing"

        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        assert view.paint() is True
        assert view.meta.content == "-# On Repeat + Radio\n-# Up next: After"
        await clear_queued(guild)
        assert view.paint() is True
        assert view.meta.content == "-# On Repeat + Radio\n-# Up next: Nothing"

        await view.repeat.callback(press)
        assert _stored(guild).repeat is False
        assert _stored(guild).radio is True
        assert view.meta.content == "-# On Radio\n-# Up next: Nothing"
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_radio_without_a_key_opens_the_key_box(monkeypatch):
    guild, player, _track = await _playing()
    try:
        from config import settings

        view = MusicPlayer(guild)
        outsider = MagicMock()
        outsider.send = AsyncMock()
        outsider.user.voice = None
        outsider.response.send_modal = AsyncMock()
        await view.radio.callback(outsider)
        outsider.response.send_modal.assert_not_awaited()
        assert "Join the voice channel first" in outsider.send.await_args.args[0]

        press = _press(player)
        press.response.send_modal = AsyncMock()
        await view.radio.callback(press)
        modal = press.response.send_modal.await_args.args[0]
        assert modal.title == "Last.fm"

        empty = _press(player)
        empty.send = AsyncMock()
        modal.key.component._value = "short"
        await modal.on_submit(empty)
        assert "Paste a Last.fm API key" in empty.send.await_args.args[0]
        assert settings.lastfm_api_key is None

        written = {}
        monkeypatch.setattr(
            "music.player.radio.upsert_config_values",
            lambda _path, values: written.update(values),
        )
        monkeypatch.setattr("music.player.radio.load_similar", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.top_tracks", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.player.radio.similar_artists", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[]))
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        submit = _press(player)
        modal.key.component._value = "abc123abc123abc1"
        await modal.on_submit(submit)
        assert written == {"LASTFM_API_KEY": "abc123abc123abc1"}
        assert settings.lastfm_api_key == "abc123abc123abc1"
        assert _stored(guild).radio is True
        assert view.radio.style == discord.ButtonStyle.primary
        assert now_playing(guild).title == "I Ain't Worried"
        assert view.meta.content == "-# On Radio\n-# Up next: After"
        _stored(guild).radio_next = ("Oasis", "Wonderwall")
        assert view.paint() is False
        assert view.meta.content == "-# On Radio\n-# Up next: After"
        assert view.shuffle.disabled is True
        assert view.repeat.disabled is False
        assert [item.track.title for item in _stored(guild).later] == ["After"]

        await view.repeat.callback(submit)
        assert _stored(guild).repeat is True
        assert _stored(guild).radio is True
        assert view.repeat.style == discord.ButtonStyle.primary
        assert view.radio.style == discord.ButtonStyle.primary
        assert view.meta.content == "-# On Repeat + Radio\n-# Up next: After"

        await view.shuffle.callback(submit)
        assert _stored(guild).shuffle is False

        again = _press(player)
        again.response.send_modal = AsyncMock()
        await view.radio.callback(again)
        again.response.send_modal.assert_not_awaited()
        assert _stored(guild).radio is False
        assert _stored(guild).repeat is True
        assert view.radio.style == discord.ButtonStyle.secondary
        assert view.shuffle.disabled is False
        assert view.meta.content == "-# On Repeat\n-# Up next: After"
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

def case_a_match_is_this_song():
    """A topic channel and a curly apostrophe still count as the song that is on."""
    assert channel_author("OasisVEVO") is True
    assert channel_author("Oasis - Topic") is True
    assert channel_author("Oasis Official") is True
    assert channel_author("Oasis") is False
    assert same_song("Wonderwall", "Oasis", "Wonderwall", "Oasis") is True
    assert same_song("Wonderwall", "Oasis", "Wonderwall", "OasisVEVO", channel=True) is True
    assert same_song("Wonderwall", "Oasis", "Wonderwall", "OasisVEVO") is False
    assert same_song("Wonderwall", "Oasis", "Wonderwall", "Oasis - Topic", channel=True) is True
    assert same_song("Champagne Supernova", "Oasis", "Live Forever", "OasisVEVO", channel=True) is False
    assert same_song(
        "Don't Look Back in Anger", "Oasis", "Don\u2019t Look Back in Anger", "Oasis", channel=True,
    ) is True
    assert same_song("The Wolf", "Mumford & Sons", "The Wolf", "Mumford and Sons", channel=True) is True
    assert same_title("Jive", "Knxwledge. - Jive (SKR\u2206WBERRiES)") is True
    assert same_title("Air", "Airplane") is False

    def named(title, author, identifier):
        return SimpleNamespace(title=title, author=author, identifier=identifier)

    playing = named("hereiam_[allnght]", "Knx.", "now")
    other = named("Knx. - OvrMyne", "Tone Salon", "other")
    jive = named("Knxwledge. - Jive (SKR\u2206WBERRiES)", "GodsConnect2", "jive")
    assert _chosen([other, jive], "Jive", "Knx.", playing) is jive
    assert _chosen([named("Airplane", "Air", "plane")], "Air", "Bach", playing) is None
    assert weighted_order([0.2, 0.9], [0.5, 0.5]) == [1, 0]
    assert weighted_order([1.0, 1.0], [0.2, 0.8]) == [1, 0]
    assert weighted_order([], []) == []
