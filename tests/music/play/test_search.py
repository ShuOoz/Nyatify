"""The search card. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.osa_musify import OsaMusify
from music.panel import (
    LABEL_PLAYER,
    LABEL_QUEUE,
    MARK_ADD,
    MARK_BACK,
    MARK_CLOSE,
    MARK_NEXT,
    MARK_SEARCH,
    PAGE,
    page_line,
    song_row,
)
from music.play.search import MatchList
from music.player.view import MusicPlayer
from music.playlist.card import LiveLine
from music.lavalink import Found
from music.source import NAME_PLATFORM

from tests.music.support import _ask, _defer_marks_done, _named
from tests.music.play.support import _action_rows, _button, _page_text, _sections

async def test_a_name_lists_songs_until_one_is_added(monkeypatch):
    first = _named("One Wish", "Ray J")
    first.length = 248_000
    second = _named("x" * 90, "Band")
    second.length = 65_000
    started = AsyncMock()
    view = MatchList("one wish", "apple", [first, second], started, 9)
    view.to_components()
    rows = _action_rows(view)
    title = view.children[0].children[0]
    assert isinstance(title, discord.ui.Section)
    assert title.children[0].content == "## Osaka Play"
    assert title.accessory.label == MARK_SEARCH
    rule = view.children[0].children[1]
    assert isinstance(rule, discord.ui.Separator)
    assert rule.visible is True
    assert rows[0].children[0] is view.platforms
    gap = view.children[0].children[3]
    assert isinstance(gap, discord.ui.Separator)
    assert gap.visible is False
    assert gap.spacing is discord.SeparatorSpacing.small
    assert view.platforms.placeholder == "Apple Music"
    assert [option.label for option in view.platforms.options] == [
        "Apple Music", "YouTube", "Spotify", "SoundCloud", "NetEase",
    ]
    assert [option.value for option in view.platforms.options] == [
        "apple", "youtube", "spotify", "soundcloud", "netease",
    ]
    assert [option.value for option in view.platforms.options if option.default] == ["apple"]
    sections = _sections(view)
    assert sections[0].children[0].content == song_row(1, "One Wish", "Ray J", 248_000)
    assert sections[0].accessory.label == MARK_ADD
    assert sections[1].children[0].content.split("\n", 1)[0].endswith("…")
    assert sections[1].children[0].content.endswith("Band • 1:05")
    assert _page_text(view).startswith(page_line(1, 1, 2))
    assert [button.label for button in rows[-1].children] == [
        MARK_BACK, MARK_NEXT, LABEL_PLAYER, LABEL_QUEUE, MARK_CLOSE,
    ]
    assert [_button(view, MARK_BACK).disabled, _button(view, MARK_NEXT).disabled] == [True, True]

    calls = []

    async def search(guild, song, platform="youtube", mode=None):
        calls.append((song, platform, mode))
        if platform == "spotify":
            return "Nothing on Spotify matched that."
        return Found(song, platform, [_named("Other", "Band")], False)

    monkeypatch.setattr("music.play.find_songs", search)
    queued = AsyncMock(return_value="Playing Other.")
    monkeypatch.setattr("music.play.queue_tracks", queued)
    monkeypatch.setattr("music.play.now_playing", lambda guild: None)

    press = _ask()
    _defer_marks_done(press)
    view.platforms._values = ["spotify"]
    await view.on_platform(press)
    assert calls == [("one wish", "spotify", None)]
    assert not _sections(view)
    assert any(
        isinstance(item, discord.ui.TextDisplay)
        and item.content == "Nothing on Spotify matched that."
        for item in view.walk_children()
    )
    press.edit_original_response.assert_awaited()

    press = _ask()
    _defer_marks_done(press)
    view.platforms._values = ["youtube"]
    await view.on_platform(press)
    assert _sections(view)[0].children[0].content == song_row(1, "Other", "Band", 0)
    chosen = next(option for option in view.platforms.options if option.default)
    assert chosen.label == "YouTube"

    async def opened(_interaction):
        assert view.is_finished() is False

    started.side_effect = opened
    press = _ask()
    await _sections(view)[0].accessory.callback(press)
    queued.assert_awaited_once()
    assert queued.await_args.args[2] == [view.tracks[0]]
    assert queued.await_args.args[3] == "youtube"
    started.assert_not_awaited()
    assert view.is_finished() is False
    assert "Playing Other" in press.send.await_args.args[0]

    press = _ask()
    await _button(view, LABEL_PLAYER).callback(press)
    started.assert_not_awaited()
    assert "Nothing is playing" in press.send.await_args.args[0]
    assert view.is_finished() is False

    monkeypatch.setattr("music.play.now_playing", lambda guild: object())
    press = _ask()
    await _button(view, LABEL_PLAYER).callback(press)
    started.assert_awaited_once()
    assert view.is_finished() is False
    press.delete_original_response.assert_not_awaited()
    press.edit_original_response.assert_not_awaited()

    listed = AsyncMock()
    view.open_queue = listed
    press = _ask()
    await _button(view, LABEL_QUEUE).callback(press)
    listed.assert_awaited_once()
    assert view.is_finished() is False
    press.delete_original_response.assert_not_awaited()

    fresh = MatchList("one wish", "apple", [first], AsyncMock(), 9)
    queued.return_value = "Join a voice channel first."
    press = _ask()
    await _sections(fresh)[0].accessory.callback(press)
    fresh.on_started.assert_not_awaited()
    assert "Join a voice channel first" in press.send.await_args.args[0]

    fresh._working = True
    press = _ask()
    await _sections(fresh)[0].accessory.callback(press)
    assert "Already working" in press.send.await_args.args[0]

    press = _ask()
    close = _button(fresh, MARK_CLOSE)
    await close.callback(press)
    assert fresh.is_finished()
    press.delete_original_response.assert_awaited_once()

async def test_search_pages_five_songs_at_a_time():
    tracks = [_named(f"Song {index}", "Artist") for index in range(6)]
    for track in tracks:
        track.length = 60_000
    view = MatchList("sunny", NAME_PLATFORM, tracks, AsyncMock(), 4)
    view.to_components()
    assert len(_sections(view)) == PAGE
    assert _page_text(view).startswith(page_line(1, 2, 6))
    assert _sections(view)[0].children[0].content == song_row(1, "Song 0", "Artist", 60_000)

    press = _ask()
    await _button(view, MARK_NEXT).callback(press)
    assert len(_sections(view)) == 1
    assert _sections(view)[0].children[0].content == song_row(6, "Song 5", "Artist", 60_000)
    assert _page_text(view).startswith(page_line(2, 2, 6))
    assert [_button(view, MARK_BACK).disabled, _button(view, MARK_NEXT).disabled] == [False, True]

    press = _ask()
    await _button(view, MARK_BACK).callback(press)
    assert _page_text(view).startswith("Page 1/2")

    press = _ask()
    await _button(view, MARK_NEXT).callback(press)
    assert _page_text(view).startswith("Page 2/2")

async def test_a_page_turn_waits_for_the_platform(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def search(guild, song, platform="youtube", mode=None):
        started.set()
        await release.wait()
        return Found(song, platform, [_named("Other")], False)

    monkeypatch.setattr("music.play.find_songs", search)
    tracks = [_named(f"Song {index}") for index in range(6)]
    view = MatchList("sunny", "apple", tracks, AsyncMock(), 4)
    first = _ask()
    _defer_marks_done(first)
    view.platforms._values = ["spotify"]
    pending = asyncio.create_task(view.on_platform(first))
    await started.wait()

    press = _ask()
    await _button(view, MARK_NEXT).callback(press)
    assert view.page == 0
    press.response.defer.assert_awaited()
    press.edit_original_response.assert_not_awaited()

    release.set()
    await pending
    assert view.platform == "spotify"
    assert view.page == 0
    assert _sections(view)[0].children[0].content == song_row(1, "Other", "", 0)

async def test_the_page_stays_while_a_search_is_in_flight():
    tracks = [_named(f"Song {index}") for index in range(6)]
    view = MatchList("sunny", "apple", tracks, AsyncMock(), 4)
    view._closed = True
    press = _ask()
    await _button(view, MARK_NEXT).callback(press)
    press.response.defer.assert_awaited()
    assert view.page == 0

async def test_adding_a_song_says_so_and_can_open_a_player(monkeypatch):
    queued = AsyncMock(return_value="Playing Ocean Drive.")
    monkeypatch.setattr("music.play.queue_tracks", queued)
    opened = AsyncMock()
    view = MatchList("ocean", "apple", [_named("Ocean Drive")], AsyncMock(), 9, on_playing=opened)
    press = _ask()
    _defer_marks_done(press)
    await _sections(view)[0].accessory.callback(press)
    opened.assert_awaited_once()
    assert view.is_finished() is False
    assert "Playing Ocean Drive" in press.send.await_args.args[0]
    press.edit_original_response.assert_not_awaited()

    opened.reset_mock()
    queued.return_value = "Added Ocean Drive."
    press = _ask()
    _defer_marks_done(press)
    await _sections(view)[0].accessory.callback(press)
    opened.assert_not_awaited()
    assert "Added Ocean Drive" in press.send.await_args.args[0]

    queued.return_value = "Added Ocean Drive. The queue is full."
    press = _ask()
    _defer_marks_done(press)
    await _sections(view)[0].accessory.callback(press)
    assert "queue is full" in press.send.await_args.args[0]

    cog = OsaMusify(MagicMock())
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)
    sent = MagicMock()
    press = _ask()
    press.response.is_done.return_value = True
    press.channel.send = AsyncMock(return_value=sent)
    await cog._open_beside(press)
    assert isinstance(press.channel.send.await_args.kwargs["view"], MusicPlayer)
    press.edit_original_response.assert_not_awaited()
    assert cog._panels

    press = _ask()
    press.response.is_done.return_value = True
    press.channel.send = AsyncMock()
    await cog._open_beside(press)
    press.channel.send.assert_not_awaited()

async def test_an_add_keeps_the_platform_from_the_click(monkeypatch):
    seen = []

    async def queued(member, guild, tracks, platform="youtube", **kwargs):
        seen.append(platform)
        return "Added Ocean Drive."

    monkeypatch.setattr("music.play.queue_tracks", queued)
    view = MatchList("ocean", "apple", [_named("Ocean Drive")], AsyncMock(), 9)
    press = _ask()

    async def defer():
        view.platform = "youtube"
        press.response.is_done.return_value = True

    press.response.defer.side_effect = defer
    await _sections(view)[0].accessory.callback(press)
    assert seen == ["apple"]

async def test_playlist_and_player_open_beside_the_search(monkeypatch):
    cog = OsaMusify(MagicMock())
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)
    monkeypatch.setattr("music.play.now_playing", lambda guild: object())
    view = MatchList(
        "one wish",
        "apple",
        [_named("One Wish")],
        cog._show_player,
        9,
        on_queue=cog._show_line,
    )
    view.to_components()

    press = _ask()
    press.response.send_message = AsyncMock()
    press.original_response = AsyncMock()
    await _button(view, LABEL_PLAYER).callback(press)
    sent = press.response.send_message.await_args.kwargs["view"]
    assert isinstance(sent, MusicPlayer)
    press.edit_original_response.assert_not_awaited()
    press.delete_original_response.assert_not_awaited()
    assert view.is_finished() is False

    press = _ask()
    press.guild.voice_client = None
    press.response.send_message = AsyncMock()
    await _button(view, LABEL_QUEUE).callback(press)
    listed = press.response.send_message.await_args.kwargs["view"]
    assert isinstance(listed, LiveLine)
    press.edit_original_response.assert_not_awaited()
    press.delete_original_response.assert_not_awaited()
    assert view.is_finished() is False
    listed.stop()

async def test_a_long_search_still_opens_the_form():
    view = MatchList("y" * 5000, "apple", [_named("One")], AsyncMock(), 9)
    press = _ask()
    press.response.send_modal = AsyncMock()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    assert len(modal.query.component.default) == 4000
    assert modal.query.component.max_length == 4000

async def test_a_quick_second_platform_replaces_the_first(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def search(guild, song, platform="youtube", mode=None):
        calls.append(platform)
        if platform == "spotify":
            started.set()
            await release.wait()
            return "Nothing on Spotify matched that."
        return Found(song, platform, [_named("Other", "Band")], False)

    monkeypatch.setattr("music.play.find_songs", search)
    view = MatchList("one wish", "apple", [_named("One Wish")], AsyncMock(), 9)
    first = _ask()
    _defer_marks_done(first)
    view.platforms._values = ["spotify"]
    pending = asyncio.create_task(view.on_platform(first))
    await started.wait()

    second = _ask()
    _defer_marks_done(second)
    view.platforms._values = ["youtube"]
    await view.on_platform(second)
    release.set()
    await pending

    assert calls == ["spotify", "youtube"]
    assert view.platform == "youtube"
    assert _sections(view)[0].children[0].content == song_row(1, "Other", "Band", 0)
    first.edit_original_response.assert_not_awaited()
    second.edit_original_response.assert_awaited()
    assert first.send.await_count == 0
    assert second.send.await_count == 0

async def test_a_failed_platform_keeps_the_songs_and_the_menu(monkeypatch):
    async def search(guild, song, platform="youtube", mode=None):
        return "Lavalink is not connected."

    monkeypatch.setattr("music.play.find_songs", search)
    track = _named("One Wish")
    view = MatchList("one wish", "apple", [track], AsyncMock(), 9)
    press = _ask()
    _defer_marks_done(press)
    view.platforms._values = ["spotify"]
    await view.on_platform(press)
    assert view.platform == "apple"
    assert view.tracks == [track]
    assert view.page == 0
    assert "Lavalink is not connected" in press.send.await_args.args[0]
    press.edit_original_response.assert_awaited()
    assert [option.value for option in view.platforms.options if option.default] == ["apple"]
    assert _sections(view)[0].children[0].content == song_row(1, "One Wish", "", 0)
