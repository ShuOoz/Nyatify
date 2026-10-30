"""The Playlist tab. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord

from guild import GuildConfig
from music.panel import LABEL_PLAYER, MARK_ADD, MARK_BACK, MARK_NEXT, MARK_SEARCH
from music.playlist.card import (
    PlaylistCard,
    LABEL_EMPTY,
    LABEL_NEW_LIST,
    LABEL_REMOVE,
    MARK_DELETE,
    MARK_MINUS,
    PICK,
    PLAYLISTS,
    QUEUE,
    empty_text,
    pick_text,
)
from music.lavalink import Found, Opened
from music.playback import stop_track
from music.archive import (
    SavedSong,
    create_playlist,
    SavedList,
    SONG_LIMIT,
    delete_playlist,
    drop_song,
    find_playlist,
    list_playlists,
    save_song,
)
from music.source import platform_key
from music.playlist.popups import remove_hint
from music.playlist.lists import song_matches

from tests.music.support import _named, _scene
from tests.music.playlist.support import (
    _after_menu,
    _button,
    _gap_under_menu,
    _line_up,
    _menu,
    _press,
    _sections,
    _text,
)

def test_a_rough_name_still_finds_the_song():
    slow = SavedSong("Slow Down", "youtube", "https://example.com/slow", "Wave to Earth", 1)
    night = SavedSong("Long Night", "youtube", "https://example.com/night", "Mac DeMarco", 1)
    cafe = SavedSong("Café", "youtube", "https://example.com/cafe", "Bebel Gilberto", 1)
    one = SavedSong("One", "youtube", "https://example.com/one")
    assert song_matches("slow", slow)
    assert song_matches("EARTH", slow)
    assert song_matches("slwo down", slow)
    assert song_matches("wav earth", slow)
    assert song_matches("slowdown", slow)
    assert song_matches("earth wave", slow)
    assert song_matches("cafe", cafe)
    assert song_matches("bebel", cafe)
    assert song_matches("mac", night)
    assert song_matches("demarco", night)
    assert song_matches("long", night)
    assert song_matches("song", night) is False
    assert song_matches("zzzz", slow) is False
    assert song_matches("a", slow) is False
    assert song_matches("on", one) is False
    assert song_matches("one", one)
    assert song_matches(
        "beetles",
        SavedSong("Beatles", "youtube", "https://example.com/b", "The Beatles"),
    )
    assert song_matches("love", SavedSong("Clover", "youtube", "https://example.com/c")) is False
    assert song_matches("atal", SavedSong("Catalog", "youtube", "https://example.com/c")) is False
    assert song_matches("eatl", SavedSong("Beatles", "youtube", "https://example.com/b")) is False
    assert song_matches("catalog", SavedSong("Cat Nap", "youtube", "https://example.com/c")) is False
    assert song_matches("beatles", SavedSong("Beat It", "youtube", "https://example.com/b")) is False
    assert song_matches("info", SavedSong("Information", "youtube", "https://example.com/i"))
    assert song_matches("slowdown", SavedSong("The Slow Down", "youtube", "https://example.com/s"))
    assert song_matches("earth's", SavedSong("Wave to Earth", "youtube", "https://example.com/e"))
    assert song_matches("REM", SavedSong("R.E.M.", "youtube", "https://example.com/r"))
    assert song_matches("rem", SavedSong("R.E.M. Singer", "youtube", "https://example.com/r"))
    assert song_matches("xy", SavedSong("Xylophone", "youtube", "https://example.com/x")) is False
    night_drive = SavedSong("夜に駆ける", "netease", "https://example.com/y", "YOASOBI")
    jasmine = SavedSong("七里香", "netease", "https://example.com/q", "周杰伦")
    assert song_matches("夜に", night_drive)
    assert song_matches("駆ける", night_drive)
    assert song_matches("里香", jasmine)
    assert song_matches("杰伦", jasmine)
    assert song_matches("周", jasmine) is False

async def test_new_playlist_asks_for_a_name_and_lists_it(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    view = PlaylistCard(guild, store)
    person = _press()
    person.user.id = 4
    await _button(view, PLAYLISTS).callback(person)
    assert pick_text() == (
        '-# Use the "Create" button for a new playlist\n'
        "-# A server keeps 5 playlists, and each keeps 100 songs"
    )
    assert LABEL_NEW_LIST.split("Create") == LABEL_PLAYER.split("Player")
    assert LABEL_REMOVE.split("Delete") == LABEL_EMPTY.split("Empty")
    assert "." not in pick_text()
    assert pick_text() in _text(view)
    assert isinstance(_after_menu(view), discord.ui.TextDisplay)
    assert _button(view, LABEL_NEW_LIST).label == LABEL_NEW_LIST
    assert _button(view, LABEL_REMOVE).disabled is True
    menu = _menu(view)
    assert menu.placeholder == PICK
    assert menu.disabled is True
    assert menu.options[0].label != PICK
    assert menu.options[0].default is False
    assert "0 playlists  |  0:00" in _text(view)
    assert "Page " not in _text(view)

    await _button(view, LABEL_NEW_LIST).callback(person)
    modal = person.response.send_modal.await_args.args[0]
    assert modal.title == "Create"
    modal.query.component._value = "   "
    blank = _press()
    await modal.on_submit(blank)
    assert "Name the playlist" in blank.send.await_args.args[0]
    assert list_playlists(store, guild.id) == []

    modal.query.component._value = "  Evening  "
    submit = _press()
    submit.user.id = 4
    await modal.on_submit(submit)
    assert find_playlist(store, guild.id, "Evening").saver_id == 4
    menu = _menu(view)
    assert menu.placeholder == PICK
    assert menu.disabled is False
    assert [(option.label, option.description, option.default) for option in menu.options] == [
        ("Evening", "0 songs", False),
    ]
    assert "1 playlist  |  0:00" in _text(view)
    assert "Page " not in _text(view)
    submit.response.edit_message.assert_awaited()

    menu._values = ["Evening"]
    await menu.callback(submit)
    assert empty_text("Evening") == "-# Evening has no songs"
    assert empty_text("Evening") in _text(view)
    assert not empty_text("Evening").endswith(".")
    assert "**Evening**" not in _text(view)
    assert "Page 1/1  |  0 songs  |  0:00  |  <@4>" in _text(view)
    assert pick_text() not in _text(view)
    open_menu = _menu(view)
    assert open_menu.placeholder == PICK
    assert [(option.label, option.default) for option in open_menu.options] == [
        (PICK, False),
        ("Evening", True),
    ]
    assert _button(view, MARK_DELETE).label == MARK_DELETE
    assert _button(view, LABEL_REMOVE).disabled is True

    back = next(option.value for option in open_menu.options if option.label == PICK)
    open_menu._values = [back]
    await open_menu.callback(submit)
    assert pick_text() in _text(view)
    assert "1 playlist  |  0:00" in _text(view)
    assert [(option.label, option.default) for option in _menu(view).options] == [
        ("Evening", False),
    ]

    await _button(view, PLAYLISTS).callback(submit)
    await _button(view, LABEL_NEW_LIST).callback(submit)
    again = submit.response.send_modal.await_args.args[0]
    again.query.component._value = "evening"
    duplicate = _press()
    await again.on_submit(duplicate)
    assert "already a playlist" in duplicate.send.await_args.args[0]
    assert [item.name for item in list_playlists(store, guild.id)] == ["Evening"]

    again.query.component._value = "Night"
    named = _press()
    named.user.id = 4
    await again.on_submit(named)
    menu = _menu(view)
    assert [option.label for option in menu.options] == ["Evening", "Night"]
    assert "2 playlists  |  0:00" in _text(view)
    menu._values = ["Night"]
    await menu.callback(named)
    assert empty_text("Night") in _text(view)
    assert empty_text("Evening") not in _text(view)
    assert "Page 1/1  |  0 songs  |  0:00  |  <@4>" in _text(view)
    open_menu = _menu(view)
    assert open_menu.placeholder == PICK
    assert [(option.label, option.default) for option in open_menu.options] == [
        (PICK, False),
        ("Evening", False),
        ("Night", True),
    ]
    open_menu._values = ["Evening"]
    await open_menu.callback(named)
    assert empty_text("Evening") in _text(view)
    assert empty_text("Night") not in _text(view)
    assert [(option.label, option.default) for option in _menu(view).options] == [
        (PICK, False),
        ("Evening", True),
        ("Night", False),
    ]

    await _button(view, PLAYLISTS).callback(named)
    for name in ("Three", "Four", "Five"):
        assert isinstance(create_playlist(store, guild.id, name, 4), SavedList)
    blocked = _press()
    await _button(view, LABEL_NEW_LIST).callback(blocked)
    assert "5 playlists" in blocked.send.await_args.args[0]
    blocked.response.send_modal.assert_not_awaited()

    await _button(view, QUEUE).callback(blocked)
    assert _button(view, MARK_SEARCH).label == MARK_SEARCH

async def test_an_open_list(tmp_path):
    """The pick follows the open list. It can drop a song and then itself. Removing stays on that list and asks for the voice channel."""
    for index, case in enumerate((
        case_the_pick_follows_the_open_list,
        case_an_open_list_drops_a_song_and_then_itself,
        case_removing_stays_on_the_list_the_box_opened,
        case_removing_a_playlist_asks_for_the_voice_channel,
    )):
        spot = tmp_path / f"open{index}"
        spot.mkdir()
        await _scene(case, spot)

async def case_the_pick_follows_the_open_list(tmp_path):
    """The pick follows the name when a list is removed, and a list named pick still opens."""
    spot = tmp_path / "part0"
    spot.mkdir()
    await case_a_pick_follows_the_name_when_a_list_is_removed(spot)
    spot = tmp_path / "part1"
    spot.mkdir()
    await case_a_list_named_pick_opens(spot)

async def case_a_pick_follows_the_name_when_a_list_is_removed(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    create_playlist(store, guild.id, "Evening", 4)
    create_playlist(store, guild.id, "Night", 4)
    assert isinstance(delete_playlist(store, guild.id, "Evening"), SavedList)
    view = PlaylistCard(guild, store)
    press = _press()
    await _button(view, PLAYLISTS).callback(press)
    menu = _menu(view)
    assert [option.value for option in menu.options] == ["Night"]
    menu._values = ["0"]
    missed = _press()
    await menu.callback(missed)
    assert view.opened is None
    assert "not here" in missed.send.await_args.args[0]
    menu._values = ["Night"]
    await menu.callback(press)
    assert view.opened is not None
    assert view.opened.name == "Night"

async def case_a_list_named_pick_opens(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    assert isinstance(create_playlist(store, guild.id, "pick", 4), SavedList)
    assert isinstance(create_playlist(store, guild.id, "Night", 4), SavedList)
    view = PlaylistCard(guild, store)
    press = _press()
    await _button(view, PLAYLISTS).callback(press)
    menu = _menu(view)
    assert [option.value for option in menu.options] == ["pick", "Night"]
    menu._values = ["pick"]
    await menu.callback(press)
    assert view.opened is not None
    assert view.opened.name == "pick"
    open_menu = _menu(view)
    assert [option.label for option in open_menu.options] == [PICK, "pick", "Night"]
    assert len({option.value for option in open_menu.options}) == 3
    back = next(option.value for option in open_menu.options if option.label == PICK)
    assert back != "pick"
    open_menu._values = [back]
    await open_menu.callback(press)
    assert view.opened is None
    assert "2 playlists" in _text(view)

async def case_an_open_list_drops_a_song_and_then_itself(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    created = create_playlist(
        store,
        guild.id,
        "Evening",
        4,
        (
            SavedSong("One", "youtube", "https://example.com/one"),
            SavedSong("Two", "spotify", "https://example.com/two"),
        ),
    )
    assert isinstance(created, SavedList)
    view = PlaylistCard(guild, store)
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    press = _press()
    press.user.id = 4
    await _button(view, PLAYLISTS).callback(press)
    assert _button(view, MARK_SEARCH).label == MARK_SEARCH
    menu = _menu(view)
    menu._values = ["Evening"]
    await menu.callback(press)
    _gap_under_menu(view)
    assert _button(view, MARK_DELETE).label == MARK_DELETE
    assert _button(view, LABEL_REMOVE).disabled is False
    assert _button(view, LABEL_REMOVE).style == discord.ButtonStyle.secondary
    assert _button(view, MARK_ADD, 0).label == MARK_ADD

    await _button(view, LABEL_REMOVE).callback(press)
    assert _button(view, LABEL_REMOVE).style == discord.ButtonStyle.primary
    await _button(view, MARK_MINUS, 0).callback(press)
    assert [item.title for item in find_playlist(store, guild.id, "Evening").songs] == ["Two"]
    assert "One" not in _text(view)
    assert "Two" in _text(view)
    assert _menu(view).placeholder == PICK
    assert [(option.label, option.description, option.default) for option in _menu(view).options] == [
        (PICK, None, False),
        ("Evening", "1 song", True),
    ]

    await _button(view, MARK_DELETE).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    assert modal.title == "Remove"
    assert modal.query.description == remove_hint()
    modal.query.component._value = "yes remove it"
    refused = _press()
    await modal.on_submit(refused)
    assert len(modal.query.description) <= 100
    assert [item.title for item in find_playlist(store, guild.id, "Evening").songs] == ["Two"]
    modal.query.component._value = "yes, remove it"
    await modal.on_submit(press)
    assert list_playlists(store, guild.id) == []
    assert _menu(view).placeholder == PICK
    assert _button(view, MARK_SEARCH).label == MARK_SEARCH
    assert _button(view, MARK_DELETE).disabled is True
    assert _button(view, LABEL_REMOVE).disabled is True

async def case_removing_stays_on_the_list_the_box_opened(tmp_path):
    """Confirm removes the playlist the box was opened on. A stale delete reloads the open list. A song saved at the top is not what remove deletes."""
    for name, case in (
        ("confirm", case_confirm_removes_the_playlist_the_box_was_opened_on),
        ("stale", case_a_stale_delete_reloads_the_open_list),
        ("top", case_a_song_saved_at_the_top_is_not_what_remove_deletes),
    ):
        spot = tmp_path / name
        spot.mkdir()
        await case(spot)

async def case_confirm_removes_the_playlist_the_box_was_opened_on(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 16
    assert isinstance(create_playlist(store, guild.id, "Evening", 4), SavedList)
    assert isinstance(create_playlist(store, guild.id, "Night", 4), SavedList)
    view = PlaylistCard(guild, store)
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    try:
        press = _press()
        await _button(view, PLAYLISTS).callback(press)
        menu = _menu(view)
        menu._values = ["Evening"]
        await menu.callback(press)
        await _button(view, MARK_DELETE).callback(press)
        modal = press.response.send_modal.await_args.args[0]
        menu = _menu(view)
        menu._values = ["Night"]
        await menu.callback(press)
        modal.query.component._value = "yes, remove it"
        await modal.on_submit(press)
        assert [item.name for item in list_playlists(store, guild.id)] == ["Night"]
        assert view.opened is not None
        assert view.opened.name == "Night"
        assert "Night" in _text(view)
    finally:
        view.stop()

async def case_a_stale_delete_reloads_the_open_list(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 16
    created = create_playlist(
        store, guild.id, "Evening", 4,
        (
            SavedSong("One", "youtube", "https://example.com/one"),
            SavedSong("Two", "spotify", "https://example.com/two"),
        ),
    )
    assert isinstance(created, SavedList)
    view = PlaylistCard(guild, store, opened=created)
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    try:
        view.to_components()
        await _button(view, LABEL_REMOVE).callback(_press())
        assert isinstance(drop_song(store, guild.id, "Evening", 0), SavedList)
        assert "One" in _text(view)
        gone = _press()
        await _button(view, MARK_MINUS, 1).callback(gone)
        assert "That song is gone" in gone.send.await_args.args[0]
        assert [song.title for song in view.opened.songs] == ["Two"]
        assert "One" not in _text(view)
        assert "Two" in _text(view)

        assert isinstance(delete_playlist(store, guild.id, "Evening"), SavedList)
        removed = _press()
        await _button(view, MARK_MINUS, 0).callback(removed)
        assert "That playlist is gone" in removed.send.await_args.args[0]
        assert view.opened is None
        assert "Two" not in _text(view)
    finally:
        view.stop()

async def case_a_song_saved_at_the_top_is_not_what_remove_deletes(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 16
    created = create_playlist(
        store, guild.id, "Evening", 4,
        (SavedSong("One", "youtube", "https://example.com/one"),),
    )
    assert isinstance(created, SavedList)
    view = PlaylistCard(guild, store, opened=created)
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    try:
        view.to_components()
        await _button(view, LABEL_REMOVE).callback(_press())
        save_song(
            store, guild.id, "Evening",
            SavedSong("Extra", "youtube", "https://example.com/extra"),
            4,
        )
        press = _press()
        await _button(view, MARK_MINUS, 0).callback(press)
        assert "That song is gone" in press.send.await_args.args[0]
        assert [song.title for song in find_playlist(store, guild.id, "Evening").songs] == [
            "Extra", "One",
        ]
        assert "Extra" in _text(view)
        assert "One" in _text(view)
    finally:
        view.stop()

async def test_importing_a_playlist(monkeypatch, tmp_path):
    """A link, an album, a watch list, a short link, duplicates, a missing artist, and an import that cannot finish."""
    for index, case in enumerate((
        case_a_playlist_link_imports_the_name_and_the_first_songs,
        case_create_imports_an_album_a_watch_list_and_a_short_link,
        case_import_skips_duplicate_links_names_a_blank_list_and_counts_the_rest,
        case_an_imported_list_reads_a_missing_artist,
        case_an_import_that_cannot_finish,
    )):
        spot = tmp_path / f"import{index}"
        spot.mkdir()
        await _scene(case, spot, monkeypatch=monkeypatch)

async def case_a_playlist_link_imports_the_name_and_the_first_songs(monkeypatch, tmp_path):
    calls = []

    async def found(_guild, query, platform="youtube", mode=None):
        calls.append(query)
        key = platform_key(query)
        tracks = []
        for index in range(SONG_LIMIT + 5):
            track = MagicMock()
            track.title = f"{key} {index}"
            track.uri = f"https://example.com/{key}/{index}"
            track.author = "Chet Baker"
            track.length = 182_000
            tracks.append(track)
        blank = MagicMock()
        blank.title = "No link"
        blank.uri = None
        return Found(query, key, [blank, *tracks], True, f"  {key} nights ")

    monkeypatch.setattr("music.playlist.lists.find_songs", found)
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    view = PlaylistCard(guild, store)
    press = _press()
    press.user.id = 4
    await _button(view, PLAYLISTS).callback(press)

    await _button(view, LABEL_NEW_LIST).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    modal.query.component._value = "https://www.youtube.com/watch?v=abc"
    song = _press()
    await modal.on_submit(song)
    assert "playlist link" in song.send.await_args.args[0]
    assert calls == []

    modal.query.component._value = "https://example.com/playlist/1"
    other = _press()
    await modal.on_submit(other)
    assert "Apple Music" in other.send.await_args.args[0]
    assert calls == []

    links = [
        "https://www.youtube.com/playlist?list=PL1",
        "https://open.spotify.com/playlist/abc",
        "https://music.apple.com/us/playlist/evening/pl.1",
        "https://soundcloud.com/user/sets/evening",
        "https://music.163.com/#/playlist?id=1",
    ]
    for link in links:
        await _button(view, LABEL_NEW_LIST).callback(press)
        modal = press.response.send_modal.await_args.args[0]
        modal.query.component._value = link
        submit = _press()
        submit.user.id = 4
        await modal.on_submit(submit)
    saved = list_playlists(store, guild.id)
    assert [item.name for item in saved] == [
        "youtube nights",
        "spotify nights",
        "apple nights",
        "soundcloud nights",
        "netease nights",
    ]
    assert all(len(item.songs) == SONG_LIMIT for item in saved)
    assert saved[0].songs[0].title == "youtube 0"
    assert saved[0].songs[-1].title == f"youtube {SONG_LIMIT - 1}"
    assert saved[1].songs[0].platform == "spotify"
    assert all(
        song.author == "Chet Baker" and song.length == 182_000
        for item in saved
        for song in item.songs
    )
    assert "netease 0" in _text(view)
    assert "Chet Baker · 3:02" in _text(view)
    assert "NetEase" not in _text(view)
    assert "Page 1/20  |  100 songs  |  5:03:20  |  <@4>" in _text(view)
    assert calls == [
        *links[:-1],
        "https://music.163.com/playlist?id=1",
    ]

async def case_create_imports_an_album_a_watch_list_and_a_short_link(monkeypatch, tmp_path):
    calls = []

    async def found(_guild, query, platform="youtube", mode=None):
        calls.append(query)
        track = MagicMock()
        track.title = "Kept"
        track.uri = "https://example.com/kept"
        track.author = "Chet Baker"
        track.length = 1_000
        if "album" in query:
            name = "Album"
        elif "list=" in query:
            name = "Listed"
        else:
            name = "Shared"
        return Found(query, "youtube", [track], True, name)

    async def follow(url):
        assert url == "https://spotify.link/abc"
        return "https://open.spotify.com/playlist/from-share"

    monkeypatch.setattr("music.playlist.lists.find_songs", found)
    monkeypatch.setattr("music.playlist.lists.follow_redirect", follow)
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    view = PlaylistCard(guild, store)
    press = _press()
    press.user.id = 4
    await _button(view, PLAYLISTS).callback(press)
    links = [
        "https://open.spotify.com/album/abc",
        "https://www.youtube.com/watch?v=abc&list=PLxxxx",
        "https://spotify.link/abc",
    ]
    for link in links:
        await _button(view, LABEL_NEW_LIST).callback(press)
        modal = press.response.send_modal.await_args.args[0]
        modal.query.component._value = link
        submit = _press()
        submit.user.id = 4
        await modal.on_submit(submit)
    assert calls == [
        "https://open.spotify.com/album/abc",
        "https://www.youtube.com/playlist?list=PLxxxx",
        "https://open.spotify.com/playlist/from-share",
    ]
    assert [item.name for item in list_playlists(store, guild.id)] == [
        "Album",
        "Listed",
        "Shared",
    ]

async def case_import_skips_duplicate_links_names_a_blank_list_and_counts_the_rest(monkeypatch, tmp_path):
    async def found(_guild, query, platform="youtube", mode=None):
        if "list=dupes" in query:
            tracks = []
            for _ in range(SONG_LIMIT):
                track = MagicMock()
                track.title = "Same"
                track.uri = "https://example.com/same"
                track.author = "A"
                track.length = 1_000
                tracks.append(track)
            tail = MagicMock()
            tail.title = "Tail"
            tail.uri = "https://example.com/tail"
            tail.author = "A"
            tail.length = 1_000
            tracks.append(tail)
            return Found(query, "youtube", tracks, True, "Dupes")
        if "list=long" in query:
            tracks = []
            for index in range(SONG_LIMIT + 2):
                track = MagicMock()
                track.title = f"Song {index}"
                track.uri = f"https://example.com/{index}"
                track.author = "A"
                track.length = 1_000
                tracks.append(track)
            return Found(query, "youtube", tracks, True, "Long")
        track = MagicMock()
        track.title = "Kept"
        track.uri = "https://example.com/kept"
        track.author = "A"
        track.length = 1_000
        return Found(query, "youtube", [track], True, "   ")

    monkeypatch.setattr("music.playlist.lists.find_songs", found)
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    view = PlaylistCard(guild, store)
    press = _press()
    press.user.id = 4
    await _button(view, PLAYLISTS).callback(press)

    async def submit(link: str):
        await _button(view, LABEL_NEW_LIST).callback(press)
        modal = press.response.send_modal.await_args.args[0]
        modal.query.component._value = link
        sent = _press()
        sent.user.id = 4
        await modal.on_submit(sent)
        return sent

    dupes = await submit("https://www.youtube.com/playlist?list=dupes")
    assert dupes.send.await_count == 0
    saved = find_playlist(store, guild.id, "Dupes")
    assert [song.title for song in saved.songs] == ["Same", "Tail"]

    long = await submit("https://www.youtube.com/playlist?list=long")
    assert "Saved 100 songs. 2 did not fit" in long.send.await_args.args[0]
    saved = find_playlist(store, guild.id, "Long")
    assert len(saved.songs) == SONG_LIMIT
    assert saved.songs[0].title == "Song 0"
    assert saved.songs[-1].title == f"Song {SONG_LIMIT - 1}"

    await submit("https://open.spotify.com/playlist/blank")
    await submit("https://music.apple.com/us/playlist/blank/pl.2")
    assert [item.name for item in list_playlists(store, guild.id)] == [
        "Dupes",
        "Long",
        "Playlist",
        "Playlist 2",
    ]

async def case_an_imported_list_reads_a_missing_artist(monkeypatch, tmp_path):
    async def found(_guild, query, platform="youtube", mode=None):
        if mode == "track":
            track = MagicMock()
            track.author = "Chet Baker"
            track.length = 182_000
            return Found(query, "youtube", [track], True)
        track = MagicMock()
        track.title = "Kept"
        track.uri = "https://example.com/kept"
        track.author = ""
        track.length = 0
        return Found(query, "youtube", [track], True, "Evening")

    monkeypatch.setattr("music.playlist.lists.find_songs", found)
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    view = PlaylistCard(guild, store)
    press = _press()
    press.user.id = 4
    await _button(view, PLAYLISTS).callback(press)
    await _button(view, LABEL_NEW_LIST).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    modal.query.component._value = "https://www.youtube.com/playlist?list=PL1"
    submit = _press()
    submit.user.id = 4
    await modal.on_submit(submit)
    saved = find_playlist(store, guild.id, "Evening")
    assert saved is not None
    assert [(song.title, song.author, song.length) for song in saved.songs] == [
        ("Kept", "Chet Baker", 182_000),
    ]
    assert "Chet Baker · 3:02" in _text(view)
    assert "Page 1/1  |  1 song  |  3:02  |  <@4>" in _text(view)

async def case_removing_a_playlist_asks_for_the_voice_channel(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(9422, ["Alpha"])
    store = GuildConfig(tmp_path / "guild.json")
    created = create_playlist(
        store, guild.id, "Evening", member.id,
        (
            SavedSong("One", "youtube", "https://example.com/one"),
            SavedSong("Two", "youtube", "https://example.com/two"),
        ),
    )
    assert isinstance(created, SavedList)
    view = PlaylistCard(guild, store, opened=created)
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    try:
        assert note == "Playing Alpha."
        view.to_components()
        outsider = MagicMock()
        outsider.voice.channel = MagicMock()
        outsider.voice.channel.id = 1
        refused = _press(outsider)
        await _button(view, MARK_DELETE).callback(refused)
        assert "Join the voice channel first" in refused.send.await_args.args[0]
        refused.response.send_modal.assert_not_awaited()

        await _button(view, LABEL_REMOVE).callback(_press(member))
        song = _press(outsider)
        await _button(view, MARK_MINUS, 0).callback(song)
        assert "Join the voice channel first" in song.send.await_args.args[0]
        assert [item.title for item in find_playlist(store, guild.id, "Evening").songs] == [
            "One", "Two",
        ]

        await _button(view, MARK_MINUS, 0).callback(_press(member))
        assert [item.title for item in find_playlist(store, guild.id, "Evening").songs] == ["Two"]
    finally:
        view.stop()
        await stop_track(guild)

async def case_an_import_that_cannot_finish(monkeypatch, tmp_path):
    """A taken name keeps the songs. A failed import says the playlist was not created."""
    spot = tmp_path / "part0"
    spot.mkdir()
    with monkeypatch.context() as patch:
        await case_an_import_keeps_its_songs_when_the_name_is_taken(patch, spot)
    spot = tmp_path / "part1"
    spot.mkdir()
    with monkeypatch.context() as patch:
        await case_a_failed_import_says_the_playlist_was_not_created(patch, spot)

async def case_an_import_keeps_its_songs_when_the_name_is_taken(monkeypatch, tmp_path):
    async def found(_guild, query, platform="youtube", mode=None):
        track = MagicMock()
        track.title = "Kept"
        track.uri = "https://example.com/kept"
        track.author = "A"
        track.length = 1_000
        return Found(query, "youtube", [track], True, "Evening")

    monkeypatch.setattr("music.playlist.lists.find_songs", found)
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    assert isinstance(create_playlist(store, guild.id, "Evening", 4), SavedList)
    assert isinstance(create_playlist(store, guild.id, "Evening 2", 4), SavedList)
    view = PlaylistCard(guild, store)
    press = _press()
    press.user.id = 4
    await _button(view, PLAYLISTS).callback(press)
    await _button(view, LABEL_NEW_LIST).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    modal.query.component._value = "https://www.youtube.com/playlist?list=PL1"
    submit = _press()
    submit.user.id = 4
    await modal.on_submit(submit)
    saved = find_playlist(store, guild.id, "Evening 3")
    assert saved is not None
    assert [song.title for song in saved.songs] == ["Kept"]
    assert [song.title for song in find_playlist(store, guild.id, "Evening").songs] == []

async def case_a_failed_import_says_the_playlist_was_not_created(monkeypatch, tmp_path):
    async def found(_guild, query, platform="youtube", mode=None):
        raise RuntimeError("down")

    monkeypatch.setattr("music.playlist.lists.find_songs", found)
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    view = PlaylistCard(guild, store)
    press = _press()
    press.user.id = 4
    await _button(view, PLAYLISTS).callback(press)
    await _button(view, LABEL_NEW_LIST).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    modal.query.component._value = "https://www.youtube.com/playlist?list=PL1"
    submit = _press()
    submit.user.id = 4
    await modal.on_submit(submit)
    note = submit.send.await_args.args[0]
    assert "creating the playlist" in note
    assert "starting the song" not in note
    assert list_playlists(store, guild.id) == []

async def test_artists_are_read_once(monkeypatch, tmp_path):
    """An open list reads each artist once. A bare song is filled once. A list that changes during the read stays as it was."""
    for index, case in enumerate((
        case_an_open_list_reads_the_artist_once,
        case_a_bare_song_is_filled_once,
        case_a_list_changing_while_artists_are_read,
    )):
        spot = tmp_path / f"read{index}"
        spot.mkdir()
        await _scene(case, spot, monkeypatch=monkeypatch)

async def case_an_open_list_reads_the_artist_once(monkeypatch, tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    created = create_playlist(
        store,
        guild.id,
        "Chet Baker",
        4,
        (
            SavedSong("Song 0", "apple", "https://example.com/0", "Kept", 60_000),
            *(
                SavedSong(f"Song {index}", "apple", f"https://example.com/{index}")
                for index in range(1, 6)
            ),
        ),
    )
    assert isinstance(created, SavedList)
    calls = []
    current = 0
    peak = 0

    async def found(_guild, query, platform="youtube", mode=None):
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        try:
            await asyncio.sleep(0)
            calls.append(query)
            track = MagicMock()
            track.author = "Chet Baker"
            track.length = 182_000
            return Found(query, "apple", [track], True)
        finally:
            current -= 1

    monkeypatch.setattr("music.playlist.lists.find_songs", found)
    view = PlaylistCard(guild, store)
    press = _press()
    await _button(view, PLAYLISTS).callback(press)
    menu = _menu(view)
    menu._values = ["Chet Baker"]
    await menu.callback(press)
    text = _text(view)
    assert "Kept · 1:00" in text
    assert "Chet Baker · 3:02" in text
    assert "Apple Music" not in text
    assert "Page 1/2  |  6 songs  |  16:10  |  <@4>" in text
    assert "13:08" not in text
    faces = [item.label for item in view.walk_children() if isinstance(item, discord.ui.Button)]
    assert "Play" not in faces
    assert "Shuffle" not in faces
    assert peak == 4
    assert sorted(calls) == [f"https://example.com/{index}" for index in range(1, 6)]
    stored = find_playlist(store, guild.id, "Chet Baker")
    assert stored is not None
    assert [song.author for song in stored.songs] == ["Kept", *["Chet Baker"] * 5]
    assert [song.length for song in stored.songs] == [60_000, *([182_000] * 5)]

    await _button(view, MARK_NEXT).callback(press)
    assert sorted(calls) == [f"https://example.com/{index}" for index in range(1, 6)]
    assert "Chet Baker · 3:02" in _text(view)
    assert "Apple Music" not in _text(view)
    assert "**6\\.** Song 5" in _text(view)
    assert "Page 2/2  |  6 songs  |  16:10  |  <@4>" in _text(view)
    await _button(view, PLAYLISTS).callback(press)
    assert "1 playlist  |  16:10" in _text(view)

async def case_a_bare_song_is_filled_once(monkeypatch, tmp_path):
    """A page turn does not reread a bare song. A missing artist or length is filled, including on the next list."""
    spot = tmp_path / "part0"
    spot.mkdir()
    with monkeypatch.context() as patch:
        await case_a_page_turn_does_not_reread_a_song_that_stayed_bare(patch, spot)
    spot = tmp_path / "part1"
    spot.mkdir()
    with monkeypatch.context() as patch:
        await case_a_song_missing_only_the_artist_or_the_length_is_filled(patch, spot)
    spot = tmp_path / "part2"
    spot.mkdir()
    with monkeypatch.context() as patch:
        await case_the_same_bare_song_is_filled_on_the_next_list(patch, spot)

async def case_a_page_turn_does_not_reread_a_song_that_stayed_bare(monkeypatch, tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    created = create_playlist(
        store,
        guild.id,
        "Live",
        4,
        tuple(
            SavedSong(f"Song {index}", "youtube", f"https://example.com/{index}", "", 0)
            for index in range(6)
        ),
    )
    assert isinstance(created, SavedList)
    calls = []

    async def found(_guild, query, platform="youtube", mode=None):
        calls.append(query)
        if query.endswith("/0"):
            raise RuntimeError("lookup")
        track = MagicMock()
        track.author = ""
        track.length = 0
        return Found(query, "youtube", [track], True)

    monkeypatch.setattr("music.playlist.lists.find_songs", found)
    view = PlaylistCard(guild, store)
    press = _press()
    await _button(view, PLAYLISTS).callback(press)
    menu = _menu(view)
    menu._values = ["Live"]
    await menu.callback(press)
    assert sorted(calls) == [f"https://example.com/{index}" for index in range(6)]
    await _button(view, MARK_NEXT).callback(press)
    await _button(view, MARK_BACK).callback(press)
    assert sorted(calls) == [f"https://example.com/{index}" for index in range(6)]

    again = PlaylistCard(guild, store)
    fresh = _press()
    await _button(again, PLAYLISTS).callback(fresh)
    menu = _menu(again)
    menu._values = ["Live"]
    await menu.callback(fresh)
    assert len(calls) == 12

async def case_a_song_missing_only_the_artist_or_the_length_is_filled(monkeypatch, tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    created = create_playlist(
        store,
        guild.id,
        "Evening",
        4,
        (
            SavedSong("Named", "apple", "https://example.com/named", "Kept", 0),
            SavedSong("Timed", "apple", "https://example.com/timed", "", 60_000),
        ),
    )
    assert isinstance(created, SavedList)
    calls = []

    async def found(_guild, query, platform="youtube", mode=None):
        calls.append(query)
        track = MagicMock()
        track.author = "" if query.endswith("named") else "Chet Baker"
        track.length = 182_000 if query.endswith("named") else 0
        return Found(query, "apple", [track], True)

    monkeypatch.setattr("music.playlist.lists.find_songs", found)
    view = PlaylistCard(guild, store)
    press = _press()
    await _button(view, PLAYLISTS).callback(press)
    menu = _menu(view)
    menu._values = ["Evening"]
    await menu.callback(press)
    stored = find_playlist(store, guild.id, "Evening")
    assert sorted(calls) == ["https://example.com/named", "https://example.com/timed"]
    assert [(song.author, song.length) for song in stored.songs] == [
        ("Kept", 182_000),
        ("Chet Baker", 60_000),
    ]
    assert "Kept · 3:02" in _text(view)
    assert "Chet Baker · 1:00" in _text(view)
    assert "Page 1/1  |  2 songs  |  4:02  |  <@4>" in _text(view)

async def case_the_same_bare_song_is_filled_on_the_next_list(monkeypatch, tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    song = SavedSong("Kept", "youtube", "https://example.com/kept")
    assert isinstance(create_playlist(store, guild.id, "Evening", 4, (song,)), SavedList)
    assert isinstance(create_playlist(store, guild.id, "Morning", 4, (song,)), SavedList)
    calls = []

    async def found(_guild, query, platform="youtube", mode=None):
        calls.append(query)
        track = MagicMock()
        track.author = "Chet Baker"
        track.length = 182_000
        return Found(query, "youtube", [track], True)

    monkeypatch.setattr("music.playlist.lists.find_songs", found)
    view = PlaylistCard(guild, store)
    press = _press()
    await _button(view, PLAYLISTS).callback(press)
    menu = _menu(view)
    menu._values = ["Evening"]
    await menu.callback(press)
    assert calls == ["https://example.com/kept"]
    menu = _menu(view)
    back = next(option.value for option in menu.options if option.label == PICK)
    menu._values = [back]
    await menu.callback(press)
    menu = _menu(view)
    menu._values = ["Morning"]
    await menu.callback(press)
    assert calls == ["https://example.com/kept", "https://example.com/kept"]
    morning = find_playlist(store, guild.id, "Morning")
    assert [(item.author, item.length) for item in morning.songs] == [("Chet Baker", 182_000)]
    assert "Chet Baker · 3:02" in _text(view)

async def case_a_list_changing_while_artists_are_read(monkeypatch, tmp_path):
    """A song saved while artists are read stays. A list removed while they are read stays gone."""
    spot = tmp_path / "part0"
    spot.mkdir()
    with monkeypatch.context() as patch:
        await case_a_song_saved_while_artists_are_read_stays(patch, spot)
    spot = tmp_path / "part1"
    spot.mkdir()
    with monkeypatch.context() as patch:
        await case_a_list_removed_while_artists_are_read_stays_gone(patch, spot)

async def case_a_song_saved_while_artists_are_read_stays(monkeypatch, tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    created = create_playlist(
        store,
        guild.id,
        "Evening",
        4,
        (SavedSong("Kept", "youtube", "https://example.com/kept"),),
    )
    assert isinstance(created, SavedList)

    async def found(_guild, query, platform="youtube", mode=None):
        save_song(
            store,
            guild.id,
            "Evening",
            SavedSong("Extra", "youtube", "https://example.com/extra", "New", 2_000),
            4,
        )
        track = MagicMock()
        track.author = "Chet Baker"
        track.length = 182_000
        return Found(query, "youtube", [track], True)

    monkeypatch.setattr("music.playlist.lists.find_songs", found)
    view = PlaylistCard(guild, store)
    press = _press()
    await _button(view, PLAYLISTS).callback(press)
    menu = _menu(view)
    menu._values = ["Evening"]
    await menu.callback(press)
    saved = find_playlist(store, guild.id, "Evening")
    assert [(song.title, song.author, song.length) for song in saved.songs] == [
        ("Extra", "New", 2_000),
        ("Kept", "Chet Baker", 182_000),
    ]
    assert "Extra" in _text(view)
    assert "Chet Baker · 3:02" in _text(view)

async def case_a_list_removed_while_artists_are_read_stays_gone(monkeypatch, tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    created = create_playlist(
        store,
        guild.id,
        "Evening",
        4,
        (SavedSong("Kept", "youtube", "https://example.com/kept"),),
    )
    assert isinstance(created, SavedList)

    async def found(_guild, query, platform="youtube", mode=None):
        assert isinstance(delete_playlist(store, guild.id, "Evening"), SavedList)
        track = MagicMock()
        track.author = "Chet Baker"
        track.length = 182_000
        return Found(query, "youtube", [track], True)

    monkeypatch.setattr("music.playlist.lists.find_songs", found)
    view = PlaylistCard(guild, store)
    press = _press()
    await _button(view, PLAYLISTS).callback(press)
    menu = _menu(view)
    menu._values = ["Evening"]
    await menu.callback(press)
    assert list_playlists(store, guild.id) == []
    assert view.opened is None
    assert pick_text() in _text(view)

async def test_search_in_a_playlist_narrows_the_songs_or_leaves_them(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    created = create_playlist(
        store,
        guild.id,
        "Evening",
        4,
        (
            SavedSong("Slow Down", "youtube", "https://example.com/slow", "Wave to Earth", 180_000),
            SavedSong("Long Night", "spotify", "https://example.com/long", "Mac DeMarco", 120_000),
        ),
    )
    assert isinstance(created, SavedList)
    view = PlaylistCard(guild, store, opened=created)
    view.to_components()
    assert "Slow Down" in _text(view)
    assert "Long Night" in _text(view)
    assert _sections(view)[0].accessory.style == discord.ButtonStyle.secondary

    press = _press()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    assert modal.title == "Search"
    modal.query.component._value = "   "
    blank = _press()
    await modal.on_submit(blank)
    assert "Name a song or an artist" in blank.send.await_args.args[0]

    modal.query.component._value = "zzzz"
    miss = _press()
    await modal.on_submit(miss)
    assert "No song in Evening matches that" in miss.send.await_args.args[0]
    assert "Slow Down" in _text(view)
    assert "Long Night" in _text(view)
    assert _sections(view)[0].accessory.style == discord.ButtonStyle.secondary
    miss.response.edit_message.assert_not_awaited()

    modal.query.component._value = "slwo"
    hit = _press()
    await modal.on_submit(hit)
    assert "Slow Down" in _text(view)
    assert "Long Night" not in _text(view)
    assert "**1\\.** Slow Down" in _text(view)
    assert "Page 1/1  |  1 song" in _text(view)
    assert _sections(view)[0].accessory.style == discord.ButtonStyle.primary

    modal.query.component._value = "mac"
    artist = _press()
    await modal.on_submit(artist)
    assert "Long Night" in _text(view)
    assert "Slow Down" not in _text(view)
    assert "**2\\.** Long Night" in _text(view)

    await _button(view, MARK_SEARCH).callback(artist)
    assert "Slow Down" in _text(view)
    assert "Long Night" in _text(view)
    assert _sections(view)[0].accessory.style == discord.ButtonStyle.secondary

async def test_a_saved_song_plays_on_its_own(monkeypatch):
    saved = SavedList(
        "Evening",
        17,
        (
            SavedSong("One", "youtube", "https://example.com/one", "A", 182_000),
            SavedSong("Two", "spotify", "https://example.com/two", "B", 120_000),
        ),
    )
    started = AsyncMock()

    async def open_all(_guild, songs):
        return Opened([(_named(song.title), song.platform) for song in songs], [])

    monkeypatch.setattr("music.playlist.lists.open_recordings", open_all)
    view = PlaylistCard(MagicMock(), MagicMock(), opened=saved, on_playing=started)
    view.to_components()
    text = _text(view)
    assert "**1\\.** One" in text
    assert "**2\\.** Two" in text
    assert "A · 3:02 · YouTube" in text
    assert "B · 2:00 · Spotify" in text
    assert "Page 1/1  |  2 songs  |  5:02  |  <@17>" in text
    faces = [item.label for item in view.walk_children() if isinstance(item, discord.ui.Button)]
    assert "Play" not in faces
    assert "Shuffle" not in faces

    heard = []

    async def queue(_member, _guild, tracks, platform="youtube", **_kwargs):
        track = tracks[0]
        if track.title == "One":
            return "Join a voice channel first."
        heard.append((track.title, platform))
        return f"Added {track.title}."

    monkeypatch.setattr("music.playlist.lists.queue_tracks", queue)
    missed = _press()
    await _button(view, MARK_ADD, 0).callback(missed)
    missed.delete_original_response.assert_not_awaited()
    assert "voice channel" in missed.send.await_args.args[0]
    assert view.is_finished() is False
    started.assert_not_awaited()

    press = _press()
    await _button(view, MARK_ADD, 1).callback(press)
    assert heard == [("Two", "spotify")]
    press.delete_original_response.assert_not_awaited()
    started.assert_not_awaited()
    assert "Two" in _text(view)
    assert view.is_finished() is False

async def test_a_paused_song_does_not_pull_a_saved_song_into_voice(monkeypatch):
    """Plus on a saved song stays out of voice while a paused song is still current."""
    saved = SavedList(
        "Evening",
        17,
        (SavedSong("After", "youtube", "https://example.com/after", "A", 180_000),),
    )

    async def open_all(_guild, songs):
        return Opened([(_named(song.title), song.platform) for song in songs], [])

    warmed = MagicMock()
    left = AsyncMock()
    monkeypatch.setattr("music.playlist.lists.open_recordings", open_all)
    monkeypatch.setattr("music.playlist.lists.paused_away", lambda guild: True)
    monkeypatch.setattr("music.playlist.lists.warm_voice", warmed)
    monkeypatch.setattr("music.playlist.lists.leave_if_idle", left)
    monkeypatch.setattr(
        "music.playlist.lists.queue_tracks",
        AsyncMock(return_value="Added After. I Ain't Worried is still paused."),
    )
    view = PlaylistCard(MagicMock(), MagicMock(), opened=saved)
    press = _press()
    press.guild.voice_client = None
    await _button(view, MARK_ADD, 0).callback(press)
    warmed.assert_not_called()
    left.assert_not_called()

    warmed.reset_mock()
    left.reset_mock()
    monkeypatch.setattr("music.playlist.lists.paused_away", lambda guild: False)
    monkeypatch.setattr("music.playlist.lists.queue_tracks", AsyncMock(return_value="Playing After."))
    fresh = _press()
    fresh.guild.voice_client = None
    await _button(view, MARK_ADD, 0).callback(fresh)
    warmed.assert_called_once()
    left.assert_called_once()
