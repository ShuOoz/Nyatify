"""The playlist card frame. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from guild import GuildConfig
from music.panel import (
    LABEL_PLAYER,
    LABEL_QUEUE,
    MARK_ADD,
    MARK_BACK,
    MARK_CLOSE,
    MARK_LIST,
    MARK_NEXT,
    MARK_SEARCH,
    RADIO_MARK,
    song_row,
)
from music.playlist.card import (
    PlaylistCard,
    HISTORY,
    LABEL_ADJUST,
    LABEL_EMPTY,
    LABEL_NEW_LIST,
    LABEL_REMOVE,
    LABEL_YOUR,
    MARK_DELETE,
    MARK_MINUS,
    MARK_SAVE,
    PICK,
    PLAYLISTS,
    QUEUE,
    _song_span,
    _span,
    empty_text,
    now_text,
    pick_text,
    queue_row,
)
from music.archive import SavedSong, save_song
from music.lavalink import Found
from music.playback import next_track, played, queue_tracks, queued, stop_track, apply_playback, Queued

from tests.music.support import _cards_caught_up, _named, _scene
from tests.music.playlist.support import _button, _line_up, _menu, _press, _sections, _text

def test_the_queue_names_the_line():
    """The queue names who asked and what is waiting. A total hides until every song has a length."""
    case_the_queue_names_who_asked_and_what_is_waiting()
    case_a_total_hides_until_every_song_has_a_length()

def case_the_queue_names_who_asked_and_what_is_waiting():
    current = Queued("Heart", "apple", "https://example.com/a", True, False, "Mac", 182_000, 4)
    assert now_text(current) == (
        "### Now Playing\n**[Heart](https://example.com/a)**\nMac · 3:02 · Apple Music"
    )
    assert now_text(current, shuffling=True).startswith("### Now Playing - On shuffle\n")
    needed = Queued("Needed", "apple", "https://example.com/a", True, False, "Ed", 182_000, 4)
    assert now_text(needed) == (
        "### Now Playing\n**[Needed](https://example.com/a)**\nEd · 3:02 · Apple Music"
    )
    named = Queued("Needed, Ed", "apple", "https://example.com/a", True, False, "Ed", 182_000, 4)
    assert now_text(named) == (
        "### Now Playing\n**[Needed, Ed](https://example.com/a)**\n3:02 · Apple Music"
    )
    assert "<@" not in now_text(current)
    assert now_text(None) == "### Nothing is playing\n-# Use /music play to play a song"
    wide = Queued("雨" * 20, "apple", "https://example.com/a", True, False, "Mac", 182_000, 4)
    assert now_text(wide) == (
        f"### Now Playing\n**[{'雨' * 13}…](https://example.com/a)**\nMac · 3:02 · Apple Music"
    )
    radio = Queued("Live", "youtube", None, True, True, "", 0, None)
    assert now_text(radio).endswith(RADIO_MARK)
    assert "0:00" not in now_text(radio)
    assert "YouTube" not in now_text(radio)
    waiting = Queued("First", "apple", None, False, False, "Mac", 182_000, 4)
    assert queue_row(1, waiting) == song_row(
        1, "First", "Mac", 182_000, "Apple Music", sep=" · ",
    )
    linked = Queued("First", "apple", "https://example.com/a", False, False, "Mac", 182_000, 4)
    assert "https://example.com/a" not in queue_row(1, linked)
    assert "<@" not in queue_row(1, waiting)
    grey = Queued("Easy", "spotify", None, False, False, "Mac Ayres", 314_000, 4)
    assert queue_row(1, grey).split("\n", 1)[1] == "-# \u2007\u2008\u00a0Mac Ayres · 5:14 · Spotify"

def case_a_total_hides_until_every_song_has_a_length():
    known = SavedSong("A", "youtube", "https://example.com/a", "Mac", 60_000)
    missing = SavedSong("B", "youtube", "https://example.com/b", "Mac", 0)
    assert _song_span(()) == 0
    assert _song_span((known,)) == 60_000
    assert _song_span((known, missing)) is None
    radio = Queued("Live", "youtube", None, False, True, "", 0, None)
    song = Queued("A", "youtube", None, False, False, "", 60_000, None)
    assert _span([]) == 0
    assert _span([radio]) is None
    assert _span([radio, song]) == 60_000

async def test_the_star_waits_until_a_song_has_played(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    guild = MagicMock()
    guild.id = 15
    view = PlaylistCard(guild, store)
    one = MagicMock()
    one.id = 4
    press = _press(one)
    assert view.panel == QUEUE
    assert _button(view, MARK_LIST).disabled is True
    assert "Loved" not in _text(view)
    rows = [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]
    assert rows[0].children[-1].label == MARK_LIST
    assert rows[0].children[0].style == discord.ButtonStyle.primary

    await _button(view, HISTORY).callback(press)
    assert view.panel == HISTORY
    assert MARK_SAVE == "\u2726"
    assert MARK_LIST == "\u2261"
    assert _button(view, MARK_SAVE).label == MARK_SAVE
    assert _button(view, MARK_SAVE).disabled is True
    assert _button(view, MARK_SAVE).style == discord.ButtonStyle.secondary

    await _button(view, PLAYLISTS).callback(press)
    assert _button(view, MARK_DELETE).disabled is True
    save_song(
        store,
        guild.id,
        "Evening",
        SavedSong("Slow Down", "youtube", "https://example.com/slow", "Wave", 1_000),
        4,
    )
    menu = _menu(view)
    menu.options = [
        discord.SelectOption(label="Evening", value="Evening", description="1 song"),
    ]
    menu._values = ["Evening"]
    await menu.callback(press)
    assert _button(view, MARK_DELETE).disabled is False
    assert _sections(view)[0].accessory.label == MARK_SEARCH
    assert "Loved" not in _text(view)

async def test_the_queue_pages_and_removes(tmp_path):
    """The queue card turns a page, a song can be removed, a list and a played song open from it, and search asks for a name."""
    for name, case in (
        ("page", case_the_queue_card_turns_the_page),
        ("remove", case_removing_a_song_needs_the_voice_channel),
        ("saved", case_a_saved_list_opens_from_the_queue),
        ("played", case_a_played_song_lines_up_behind_the_queue),
        ("search", case_search_from_the_queue_asks_for_a_name),
    ):
        spot = tmp_path / name
        spot.mkdir()
        await _scene(case, spot)


def _queue_line():
    return [f"Song {index}" for index in range(1, 8)]


async def case_the_queue_card_turns_the_page(tmp_path):
    guild, member, _player, tracks, note = await _line_up(9200, _queue_line())
    store = GuildConfig(tmp_path / "guild.json")
    try:
        assert note == "Playing Song 1."
        tracks[0].author = "Mac"
        tracks[0].length = 182_000
        tracks[1].length = 120_000
        view = PlaylistCard(guild, store)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        view.to_components()
        text = _text(view)
        assert text.startswith("## Osaka Playlist")
        assert "### Now Playing" in text
        assert "**[Song 1](https://example.com/song-1)**" in text
        assert "https://example.com/song-2" not in text
        assert "Mac · 3:02 · YouTube" in text
        assert "<@" not in text
        assert "Page 1/2  |  6 songs in queue  |  2:00" in text
        rows = [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]
        labels = [child.label for child in rows[0].children]
        assert [getattr(child, "panel", None) for child in rows[0].children] == [
            QUEUE, HISTORY, PLAYLISTS, None,
        ]
        assert rows[0].children[-1].label == MARK_LIST
        assert len(set(labels)) == 4
        assert [label.split(name) for label, name in zip(labels, (QUEUE, HISTORY, PLAYLISTS))] == [
            ["\u3164\uffa0\uffa0\u00a0", "\u00a0\u3164\uffa0\uffa0"],
            ["\u3164\uffa0", "\u3164\uffa0"],
            ["\u3164", "\u3164"],
        ]
        assert rows[0].children[0].style == discord.ButtonStyle.primary
        assert [child.label for child in rows[-1].children] == [
            MARK_BACK, MARK_NEXT, LABEL_ADJUST, LABEL_EMPTY, MARK_CLOSE,
        ]
        assert _button(view, LABEL_ADJUST).disabled is False
        assert _button(view, LABEL_ADJUST).style == discord.ButtonStyle.secondary
        assert LABEL_ADJUST.split("Adjust") == LABEL_PLAYER.split("Player")
        assert LABEL_EMPTY.split("Empty") == LABEL_QUEUE.split("Queue")
        assert _button(view, LABEL_EMPTY).disabled is False
        sections = _sections(view)
        assert sections[0].accessory.label == MARK_SEARCH
        songs = sections[1:]
        assert len(songs[0].children) == 1
        assert len(songs) == 5
        assert all(isinstance(item.accessory, discord.ui.Button) for item in songs)
        assert [item.accessory.label for item in songs] == [MARK_MINUS] * 5

        press = _press(member)
        await _button(view, HISTORY).callback(press)
        assert "-# Nothing has played yet" in _text(view)
        assert _button(view, HISTORY).style == discord.ButtonStyle.primary
        history_rows = [
            item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)
        ]
        history_tabs = [child.label for child in history_rows[0].children]
        assert [label.split(name) for label, name in zip(history_tabs, (QUEUE, HISTORY, PLAYLISTS))] == [
            ["\u3164\uffa0", "\u3164\uffa0"],
            ["\u3164\uffa0\uffa0\u00a0", "\u00a0\u3164\uffa0\uffa0"],
            ["\u3164", "\u3164"],
        ]
        assert history_rows[0].children[-1].label == MARK_SAVE
        assert history_rows[0].children[-1].disabled is True
        assert [child.label for child in history_rows[-1].children] == [
            MARK_BACK, MARK_NEXT, LABEL_YOUR, LABEL_EMPTY, MARK_CLOSE,
        ]
        assert _button(view, LABEL_YOUR).disabled is True
        assert _button(view, LABEL_EMPTY).disabled is True
        await _button(view, QUEUE).callback(press)

        await _button(view, MARK_NEXT).callback(press)
        assert "Song 7" in _text(view)
        assert "Song 2" not in _text(view)
        assert len(_sections(view)) == 2
    finally:
        await stop_track(guild)


async def case_removing_a_song_needs_the_voice_channel(tmp_path):
    guild, member, _player, _tracks, _note = await _line_up(9201, _queue_line())
    store = GuildConfig(tmp_path / "guild.json")
    try:
        view = PlaylistCard(guild, store)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        press = _press(member)
        await _button(view, MARK_NEXT).callback(press)
        assert "Song 7" in _text(view)

        press = _press()
        await _button(view, MARK_MINUS, 6).callback(press)
        assert "Join the voice channel first" in press.send.await_args.args[0]
        assert any(row.title == "Song 7" for row in queued(guild))

        press = _press(member)
        await _button(view, MARK_MINUS, 6).callback(press)
        assert "Song 7" not in _text(view)
        assert [row.title for row in queued(guild)][-1] == "Song 6"
        view.message.edit.assert_awaited()
    finally:
        await stop_track(guild)


async def case_a_saved_list_opens_from_the_queue(tmp_path):
    guild, member, _player, _tracks, _note = await _line_up(9202, _queue_line())
    store = GuildConfig(tmp_path / "guild.json")
    try:
        save_song(
            store,
            guild.id,
            "Evening",
            SavedSong("Song 2", "youtube", "https://example.com/song-2"),
            member.id,
        )
        view = PlaylistCard(guild, store)
        press = _press(member)
        await _button(view, PLAYLISTS).callback(press)
        assert pick_text() in _text(view)
        assert _button(view, LABEL_NEW_LIST).label == LABEL_NEW_LIST
        assert _button(view, LABEL_REMOVE).disabled is True
        assert "<@" not in _text(view)
        assert _button(view, PLAYLISTS).style == discord.ButtonStyle.primary
        menu = _menu(view)
        assert menu.placeholder == PICK
        assert [(option.label, option.description, option.default) for option in menu.options] == [
            ("Evening", "1 song", False),
        ]
        menu._values = ["Evening"]
        await menu.callback(press)
        assert "Song 2" in _text(view)
        assert empty_text("Evening") not in _text(view)
        assert f"Page 1/1  |  1 song  |  <@{member.id}>" in _text(view)
        assert "0:00" not in _text(view)
    finally:
        await stop_track(guild)


async def case_a_played_song_lines_up_behind_the_queue(tmp_path):
    guild, member, _player, tracks, _note = await _line_up(9203, _queue_line())
    store = GuildConfig(tmp_path / "guild.json")
    try:
        tracks[0].length = 182_000
        view = PlaylistCard(guild, store)
        await next_track(guild)
        press = _press(member)
        await _button(view, HISTORY).callback(press)
        assert [row.title for row in played(guild)] == ["Song 1"]
        assert "Song 1" in _text(view)
        assert "-# Nothing has played yet" not in _text(view)
        assert "Page 1/1  |  1 song  |  3:02" in _text(view)
        waiting = [row.title for row in queued(guild)]
        press = _press(member)
        press.guild = guild
        await _button(view, MARK_ADD, 0).callback(press)
        press.send.assert_not_awaited()
        assert press.response.edit_message.await_count == 1
        assert view.is_finished() is False
        assert [row.title for row in played(guild)] == []
        assert "-# Nothing has played yet" in _text(view)
        assert "Song 1" not in _text(view)
        assert [row.title for row in queued(guild)] == [*waiting, "Song 1"]
        assert queued(guild)[0].title == waiting[0]
    finally:
        await stop_track(guild)


async def case_search_from_the_queue_asks_for_a_name(tmp_path):
    guild, member, _player, _tracks, _note = await _line_up(9204, ["Song 1", "Song 2"])
    store = GuildConfig(tmp_path / "guild.json")
    try:
        view = PlaylistCard(guild, store)
        found = []

        async def searched(_interaction, query, _platform=None, new_message=False):
            found.append((query, new_message))
            return True

        view.open_search = searched
        press = _press(member)
        await _button(view, MARK_SEARCH).callback(press)
        modal = press.response.send_modal.await_args.args[0]
        assert modal.title == "Search"
        modal.query.component._value = "   "
        blank = _press()
        await modal.on_submit(blank)
        assert found == []
        assert "Name an artist or a song" in blank.send.await_args.args[0]
        modal.query.component._value = "ocean drive"
        submit = _press(member)
        await modal.on_submit(submit)
        assert found == [("ocean drive", True)]
        assert view.is_finished() is False
    finally:
        await stop_track(guild)

async def test_an_open_queue_stays_current(monkeypatch):
    """An open queue shows a song just added and a new platform. A page and a tab keep the same buttons."""
    await case_an_open_queue_shows_a_song_just_added()
    with monkeypatch.context() as patch:
        await case_an_open_queue_shows_a_new_platform(patch)
    await case_a_page_and_a_tab_keep_the_same_buttons()

async def case_an_open_queue_shows_a_song_just_added():
    guild, member, _player, _tracks, note = await _line_up(9400, ["Alpha"])
    view = PlaylistCard(guild, MagicMock())
    try:
        assert note == "Playing Alpha."
        assert "Nothing is after this song." not in _text(view)
        message = MagicMock()
        message.edit = AsyncMock()
        view.follow(message)
        extra = _named("Beta")
        extra.uri = "https://example.com/beta"
        assert await queue_tracks(member, guild, [extra]) == "Added Beta."
        await _cards_caught_up()
        message.edit.assert_awaited()
        text = _text(view)
        assert "Alpha" in text
        assert "Beta" in text
        assert "Nothing is after this song." not in text

        message.edit.reset_mock()
        view.stop()
        later = _named("Later")
        assert await queue_tracks(member, guild, [later]) == "Added Later."
        message.edit.assert_not_awaited()
        assert "Later" not in _text(view)
    finally:
        view.stop()
        await stop_track(guild)


async def case_an_open_queue_shows_a_new_platform(monkeypatch):
    guild, _member, _player, tracks, note = await _line_up(9402, ["Alpha", "Beta"])
    view = PlaylistCard(guild, MagicMock())
    try:
        assert note == "Playing Alpha."
        tracks[0].author = "Mac"
        tracks[0].length = 182_000
        tracks[0].identifier = "alpha"
        message = MagicMock()
        message.edit = AsyncMock()
        view.follow(message)
        other = _named("Alpha", "Mac")
        other.identifier = "spot"
        other.uri = "https://example.com/spot"
        other.length = 182_000

        async def search(*_args, **_kwargs):
            return Found("Alpha Mac", "spotify", [other], False)

        monkeypatch.setattr("music.playback.find_songs", search)
        assert await apply_playback(guild, 100, "spotify", None) is None
        await _cards_caught_up()
        message.edit.assert_awaited()
        text = _text(view)
        assert "Mac · 3:02 · Spotify" in text
        assert "Beta" in text
        assert [row.title for row in queued(guild) if not row.playing] == ["Beta"]
    finally:
        view.stop()
        await stop_track(guild)


def _component_ids(view) -> list[int]:
    found: list[int] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("id"), int):
                found.append(node["id"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(view.to_components())
    return found


async def case_a_page_and_a_tab_keep_the_same_buttons():
    guild, _member, _player, _tracks, _note = await _line_up(9502, [f"Song {index}" for index in range(7)])
    view = PlaylistCard(guild, MagicMock())
    try:
        assert len(_component_ids(view)) == len(set(_component_ids(view)))
        next_button = _button(view, MARK_NEXT)
        queue_tab = _button(view, QUEUE)
        song = _button(view, MARK_MINUS, 1)
        kept = (next_button.custom_id, next_button.id, queue_tab.custom_id, song.custom_id, song.id)

        press = _press()
        press.guild = guild
        await _button(view, MARK_NEXT).callback(press)
        assert _button(view, MARK_MINUS).index == 6
        assert (_button(view, MARK_NEXT).custom_id, _button(view, MARK_NEXT).id) == kept[:2]
        assert _button(view, MARK_MINUS).custom_id == kept[3]
        assert _button(view, MARK_MINUS).id == kept[4]
        assert len(_component_ids(view)) == len(set(_component_ids(view)))

        await _button(view, HISTORY).callback(press)
        assert _button(view, QUEUE).custom_id == kept[2]
        assert _button(view, HISTORY).id == view._tabs[HISTORY].id
        await _button(view, QUEUE).callback(press)
        assert _button(view, QUEUE).custom_id == kept[2]
        assert _button(view, MARK_NEXT).custom_id == kept[0]
        assert len(_component_ids(view)) == len(set(_component_ids(view)))
    finally:
        view.stop()
        await stop_track(guild)
