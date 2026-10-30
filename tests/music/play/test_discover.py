"""The genre card. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.osa_musify import OsaMusify
from guild import GuildConfig
from music.panel import MARK_ANY, MARK_SEARCH, PANEL_SECONDS
from music.play.discover import EMPTY_SECONDS, GenreList, _kept
from music.lavalink import Found
from music.archive import SavedSong, create_playlist
from music.source import GENRES, RADIOS, station_videos

from tests.music.support import _ask, _defer_marks_done, _named, _person

async def test_a_discover_search_that_fails_leaves_the_card(monkeypatch):
    monkeypatch.setattr(
        "cogs.osa_musify.find_songs",
        AsyncMock(return_value="Lavalink is not connected."),
    )
    cog = OsaMusify(MagicMock())
    message = MagicMock()
    message.delete = AsyncMock()
    view = GenreList("spotify", AsyncMock(), cog._search)
    view.message = message
    press = _ask()
    press.response.send_modal = AsyncMock()
    await view.on_search(press)
    assert view.timeout == PANEL_SECONDS
    modal = press.response.send_modal.await_args.args[0]
    submit = _person(9)
    submit.response.is_done.return_value = True
    modal.query.component._value = "ocean"
    await modal.on_submit(submit)
    assert view.is_finished() is False
    assert view.timeout == EMPTY_SECONDS
    assert "Lavalink is not connected" in submit.send.await_args.args[0]
    message.delete.assert_not_awaited()

async def test_the_empty_card_plays_a_saved_playlist(monkeypatch, tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    create_playlist(
        store,
        guild.id,
        "Evening",
        4,
        (SavedSong("One", "youtube", "https://example.com/one"),),
    )
    create_playlist(store, guild.id, "Night", 4)
    started = AsyncMock()
    view = GenreList("spotify", started, guild=guild, store=store)
    view.to_components()
    assert view.lists.disabled is False
    assert view.lists.placeholder == "Pick a playlist"
    assert [(option.label, option.description, option.default) for option in view.lists.options] == [
        ("Evening", "1 song", False),
        ("Night", "0 songs", False),
    ]

    played = []

    async def play(_member, _guild, songs, *, replace):
        played.append(([song.title for song in songs], replace))
        return "Playing One."

    monkeypatch.setattr("music.play.play_saved", play)

    press = _ask()
    press.guild = guild
    view.lists._values = ["Night"]
    await view.lists.callback(press)
    assert "Night has no songs" in press.send.await_args.args[0]
    started.assert_not_awaited()
    assert played == []

    press = _ask()
    press.guild = guild
    view.lists._values = ["0"]
    await view.lists.callback(press)
    assert played == []
    started.assert_not_awaited()

    press = _ask()
    _defer_marks_done(press)
    press.guild = guild
    view.lists._values = ["Evening"]
    await view.lists.callback(press)
    assert played == [(["One"], True)]
    started.assert_awaited_once()
    assert press.edit_original_response.await_args.kwargs["view"] is view

    # The menu edit is what lets Discord accept the same choice again.
    view.lists._values = ["Evening"]
    again = _ask()
    _defer_marks_done(again)
    again.guild = guild
    await view.lists.callback(again)
    assert played == [(["One"], True), (["One"], True)]
    assert again.edit_original_response.await_args.kwargs["view"] is view

async def test_a_radio_station_plays_a_long_video(monkeypatch):
    video = _named("Sweet Dreams")
    video.is_stream = False
    video.length = 8 * 60 * 60 * 1000
    links = station_videos("lofi")
    assert [len(station_videos(key)) for _, key in RADIOS] == [5, 5, 5, 5, 5]
    assert all("/watch?v=" in link for link in links)
    calls = []

    async def search(guild, song, platform="youtube", mode=None):
        calls.append(song)
        assert view.timeout == PANEL_SECONDS
        assert platform == "youtube"
        if song == links[0]:
            return "Nothing on YouTube matched that."
        return Found(song, platform, [video], True)

    monkeypatch.setattr("music.play.find_songs", search)
    drawn = iter((0, 3, 4))
    monkeypatch.setattr("music.play.random.randrange", lambda count: next(drawn))
    queued = AsyncMock(return_value="Playing Sweet Dreams.")
    monkeypatch.setattr("music.play.queue_tracks", queued)
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
    assert queued.await_args.kwargs["videos"] == links
    assert queued.await_args.kwargs["video_at"] == 1
    assert queued.await_args.kwargs["station"] == "lofi"
    started.assert_awaited_once()
    assert view.timeout == EMPTY_SECONDS

    calls.clear()
    queued.reset_mock()
    started.reset_mock()
    press = _ask()
    await view.stations.callback(press)
    assert calls == [links[3]]
    assert queued.await_args.kwargs["video_at"] == 3

    calls.clear()

    async def quiet(guild, song, platform="youtube", mode=None):
        calls.append(song)
        return "Nothing on YouTube matched that."

    monkeypatch.setattr("music.play.find_songs", quiet)
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

async def test_a_radio_station_stops_when_one_video_settles(monkeypatch):
    """A queued station or a room error is final. A dead address can try the next."""
    links = station_videos("lofi")
    video = _named("Sweet Dreams")

    async def search(_guild, song, platform="youtube", mode=None):
        calls.append(song)
        return Found(song, platform, [video], True)

    calls: list[str] = []
    monkeypatch.setattr("music.play.find_songs", search)
    monkeypatch.setattr("music.play.random.randrange", lambda count: 0)
    queued = AsyncMock(return_value="Added Sweet Dreams.")
    monkeypatch.setattr("music.play.queue_tracks", queued)
    started = AsyncMock()
    view = GenreList("youtube", started)
    view.stations._values = ["lofi"]
    press = _ask()
    await view.stations.callback(press)
    assert calls == [links[0]]
    queued.assert_awaited_once()
    assert queued.await_args.kwargs["station"] == "lofi"
    press.send.assert_not_awaited()
    started.assert_not_awaited()

    calls.clear()
    queued.reset_mock()
    queued.return_value = "Join the voice channel first."
    press = _ask()
    await view.stations.callback(press)
    queued.assert_awaited_once()
    assert calls == [links[0]]
    assert press.send.await_count == 1
    assert "voice channel" in press.send.await_args.args[0]
    started.assert_not_awaited()

    calls.clear()
    queued.reset_mock()
    queued.side_effect = ["YouTube did not start that song.", "Playing Sweet Dreams."]
    press = _ask()
    await view.stations.callback(press)
    assert calls == [links[0], links[1]]
    assert queued.await_count == 2
    started.assert_awaited_once()
    press.send.assert_not_awaited()
    assert press.response.edit_message.await_args.kwargs["view"] is view

async def test_the_any_button_picks_a_song_from_the_weekly_lists(monkeypatch):
    tracks = {url: [_named(label)] for label, url in GENRES}
    opened = []
    _kept.clear()

    async def search(guild, song, platform="youtube", mode=None):
        opened.append(song)
        assert mode is None
        return Found(song, "spotify", tracks[song], True)

    monkeypatch.setattr("music.play.find_songs", search)
    monkeypatch.setattr("music.play.random.choice", lambda options: options[-1])
    queued = AsyncMock(return_value="Playing Anywhere.")
    monkeypatch.setattr("music.play.queue_tracks", queued)
    started = AsyncMock()
    view = GenreList("spotify", started)
    await view.any.callback(_ask())
    assert set(opened) == {url for _label, url in GENRES}
    queued.assert_awaited_once()
    assert queued.await_args.args[2] == tracks[GENRES[-1][1]]
    started.assert_awaited_once()

async def test_a_genre_plays_one_random_match(monkeypatch):
    tracks = [_named("First"), _named("Second")]
    jazz = dict(GENRES)["Jazz"]
    _kept.clear()

    async def search(guild, song, platform="youtube", mode=None):
        assert song == jazz
        assert mode is None
        return Found(song, "spotify", tracks, True)

    monkeypatch.setattr("music.play.find_songs", search)
    queued = AsyncMock(return_value="Playing Second.")
    monkeypatch.setattr("music.play.queue_tracks", queued)
    monkeypatch.setattr("music.play.random.choice", lambda options: options[-1])
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
    assert isinstance(parts[9], discord.ui.Separator)
    assert parts[9].visible is True
    assert parts[10].content == "**Pick a playlist**"
    assert view.lists.placeholder == "Pick a playlist"
    assert view.lists.disabled is True
    rows = [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]
    assert [len(row.children) for row in rows] == [5, 5, 1, 1]
    assert [child.label for child in rows[0].children] == [MARK_ANY, "Pop", "Jazz", "Rock", "R&B"]
    assert [child.label for child in rows[1].children] == [
        "Lo-fi", "Classical", "Hip hop", "Electronic", "Ambient",
    ]

    message = MagicMock()
    message.delete = AsyncMock()
    view.message = message
    press = _ask()
    button = next(item for item in view.buttons if item.phrase == jazz)
    await button.callback(press)
    message.delete.assert_not_awaited()
    queued.assert_awaited_once()
    assert queued.await_args.args[2] == [tracks[1]]
    started.assert_awaited_once()
    assert view.timeout == EMPTY_SECONDS

    queued.reset_mock()
    started.reset_mock()
    queued.return_value = "Added Second."
    waiting = GenreList("Spotify", started)
    waiting.message = message
    press = _ask()
    again = next(item for item in waiting.buttons if item.phrase == jazz)
    await again.callback(press)
    message.delete.assert_not_awaited()
    started.assert_not_awaited()
    assert waiting.is_finished() is False
    press.send.assert_not_awaited()

    missed = GenreList("Spotify", started)
    missed.message = message
    message.delete.reset_mock()
    queued.reset_mock()
    started.reset_mock()
    queued.return_value = "Join a voice channel first."
    press = _ask()
    button = next(item for item in missed.buttons if item.phrase == jazz)
    await button.callback(press)
    message.delete.assert_not_awaited()
    assert missed.is_finished() is False
    started.assert_not_awaited()
    assert "Join a voice channel first" in press.send.await_args.args[0]

async def test_a_genre_start_holds_the_card_open(monkeypatch):
    seen = []
    _kept.clear()

    async def search(guild, song, platform="youtube", mode=None):
        seen.append(view.timeout)
        return Found(song, "spotify", [_named("Anywhere")], False)

    monkeypatch.setattr("music.play.find_songs", search)
    monkeypatch.setattr("music.play.queue_tracks", AsyncMock(return_value="Playing Anywhere."))
    view = GenreList("spotify", AsyncMock())
    assert view.timeout == EMPTY_SECONDS
    await view.any.callback(_ask())
    assert seen == [PANEL_SECONDS] * len(GENRES)
    assert view.timeout == EMPTY_SECONDS
