"""The queue tab. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from guild import GuildConfig
from music.panel import MARK_BACK, MARK_NEXT
from music.playlist.card import (
    LiveLine,
    HISTORY,
    LABEL_ADJUST,
    LABEL_EMPTY,
    MARK_DELETE,
    MARK_LINE,
    MARK_MOVE,
    QUEUE,
)
from music.archive import SavedSong, create_playlist, SavedList
from music.playback import clear_queued, next_track, queued, release, stop_track, _stored
from music.playlist.popups import empty_hint

from tests.music.playlist.support import (
    _answer_empty,
    _button,
    _line_up,
    _open_empty,
    _press,
    _sections,
    _text,
)

async def test_the_queue_button_lines_up_a_playlist_in_order(monkeypatch, tmp_path):
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
            SavedSong("Slow Down", "youtube", "https://example.com/slow", "Wave to Earth"),
            SavedSong("Long Night", "spotify", "https://example.com/long", "Mac DeMarco"),
            SavedSong("Four", "apple", "https://example.com/four"),
        ),
    )
    assert isinstance(created, SavedList)
    empty = create_playlist(store, guild.id, "Morning", 4)
    assert isinstance(empty, SavedList)
    view = LiveLine(guild, store)
    assert _button(view, MARK_LINE).disabled is False
    press = _press()
    await _button(view, MARK_LINE).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    assert modal.title == "Queue"
    assert [option.label for option in modal.menu.options] == ["Evening", "Morning"]
    modal.menu._values = []
    blank = _press()
    await modal.on_submit(blank)
    assert "Pick a playlist" in blank.send.await_args.args[0]

    queued = []

    async def play(_member, _guild, songs, *, replace):
        assert replace is False
        queued.append([(song.title, song.platform) for song in songs])
        return f"Added {len(songs)} songs."

    monkeypatch.setattr("music.playlist.play_saved", play)

    modal.menu._values = ["Evening"]
    submit = _press()
    await modal.on_submit(submit)
    assert queued == [[
        ("One", "youtube"),
        ("Slow Down", "youtube"),
        ("Long Night", "spotify"),
        ("Four", "apple"),
    ]]
    submit.send.assert_not_awaited()

    modal.menu._values = ["Morning"]
    bare = _press()
    await modal.on_submit(bare)
    assert "has no songs" in bare.send.await_args.args[0]
    assert len(queued) == 1

    modal.menu._values = ["Missing"]
    missing = _press()
    await modal.on_submit(missing)
    assert "not here" in missing.send.await_args.args[0]
    assert len(queued) == 1

async def test_delete_removes_the_song_the_button_showed(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9413, ["Song 1", "Song 2", "Song 3", "Song 4"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    view = LiveLine(guild, store)
    try:
        assert note == "Playing Song 1."
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        press = _press(member)
        song_3 = _button(view, MARK_DELETE, 2)
        assert await next_track(guild) is None
        await song_3.callback(press)
        assert [row.title for row in queued(guild)] == ["Song 2", "Song 4"]
        assert queued(guild)[0].playing is True
    finally:
        view.stop()
        await stop_track(guild)

async def test_delete_skips_a_song_that_started_after_the_button_was_drawn(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9414, ["Song 1", "Song 2", "Song 3"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    view = LiveLine(guild, store)
    try:
        assert note == "Playing Song 1."
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        press = _press(member)
        song_2 = _button(view, MARK_DELETE, 1)
        assert await next_track(guild) is None
        await song_2.callback(press)
        assert [row.title for row in queued(guild)] == ["Song 3"]
        assert queued(guild)[0].playing is True
    finally:
        view.stop()
        await stop_track(guild)

async def test_order_swaps_waiting_songs_across_pages(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9400, ["Song 1", "Song 2", "Song 3", "Song 4", "Song 5", "Song 6", "Song 7"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    try:
        assert note == "Playing Song 1."
        view = LiveLine(guild, store)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        view.to_components()

        short, _member, _player, _tracks, _note = await _line_up(9401, ["Only", "Next"])
        quiet = LiveLine(short, store)
        quiet.to_components()
        assert _button(quiet, LABEL_ADJUST).disabled is True
        await stop_track(short)

        press = _press(member)
        await _button(view, LABEL_ADJUST).callback(press)
        assert _button(view, LABEL_ADJUST).style == discord.ButtonStyle.primary
        songs = _sections(view)[1:]
        assert [item.accessory.label for item in songs] == [MARK_MOVE] * 5

        await _button(view, MARK_MOVE, 2).callback(press)
        assert _sections(view)[2].accessory.style == discord.ButtonStyle.primary
        await _button(view, MARK_MOVE, 2).callback(press)
        assert _sections(view)[2].accessory.style == discord.ButtonStyle.secondary
        assert _button(view, LABEL_ADJUST).style == discord.ButtonStyle.primary

        await _button(view, MARK_NEXT).callback(press)
        await _button(view, MARK_MOVE, 6).callback(press)
        assert _sections(view)[1].accessory.style == discord.ButtonStyle.primary
        assert "-# Moving **6\\.** Song 7" in _text(view)
        await _button(view, MARK_BACK).callback(press)
        assert all(
            item.accessory.style == discord.ButtonStyle.secondary for item in _sections(view)[1:]
        )
        assert "-# Moving **6\\.** Song 7" in _text(view)

        outsider = MagicMock()
        outsider.voice.channel = MagicMock()
        outsider.voice.channel.id = 1
        refused = _press(outsider)
        await _button(view, MARK_MOVE, 1).callback(refused)
        assert "Join the voice channel first" in refused.send.await_args.args[0]
        assert [row.title for row in queued(guild)] == [
            "Song 1", "Song 2", "Song 3", "Song 4", "Song 5", "Song 6", "Song 7",
        ]

        await _button(view, MARK_MOVE, 1).callback(press)
        assert [row.title for row in queued(guild)] == [
            "Song 1", "Song 7", "Song 3", "Song 4", "Song 5", "Song 6", "Song 2",
        ]
        assert "Moving" not in _text(view)
        assert queued(guild)[0].playing is True
        assert [item.accessory.label for item in _sections(view)[1:]] == [MARK_MOVE] * 5
        assert all(
            item.accessory.style == discord.ButtonStyle.secondary for item in _sections(view)[1:]
        )

        await _button(view, MARK_MOVE, 2).callback(press)
        await _button(view, MARK_NEXT).callback(press)
        await _button(view, MARK_MOVE, 6).callback(press)
        assert [row.title for row in queued(guild)] == [
            "Song 1", "Song 7", "Song 2", "Song 4", "Song 5", "Song 6", "Song 3",
        ]

        await _button(view, MARK_BACK).callback(press)
        await _button(view, MARK_MOVE, 2).callback(press)
        assert "-# Moving **2\\.** Song 2" in _text(view)
        await next_track(guild)
        view.paint()
        assert "-# Moving **1\\.** Song 2" in _text(view)
        assert _sections(view)[1].accessory.index == 1
        assert _sections(view)[1].accessory.style == discord.ButtonStyle.primary

        await _button(view, HISTORY).callback(press)
        await _button(view, QUEUE).callback(press)
        assert [item.accessory.label for item in _sections(view)[1:]] == [MARK_DELETE] * 5
        assert _button(view, LABEL_ADJUST).style == discord.ButtonStyle.secondary
    finally:
        await stop_track(guild)

async def test_adjust_follows_the_armed_song_when_the_current_one_ends(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9410, ["Song 1", "Song 2", "Song 3", "Song 4"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    view = LiveLine(guild, store)
    try:
        assert note == "Playing Song 1."
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        press = _press(member)
        await _button(view, LABEL_ADJUST).callback(press)
        await _button(view, MARK_MOVE, 2).callback(press)
        assert "-# Moving **2\\.** Song 3" in _text(view)

        assert await next_track(guild) is None
        assert [row.title for row in queued(guild)] == ["Song 2", "Song 3", "Song 4"]
        view.paint()
        assert "-# Moving **1\\.** Song 3" in _text(view)
        assert _button(view, MARK_MOVE, 1).style == discord.ButtonStyle.primary
        await _button(view, MARK_MOVE, 2).callback(press)
        assert [row.title for row in queued(guild)] == ["Song 2", "Song 4", "Song 3"]
        assert queued(guild)[0].playing is True
    finally:
        view.stop()
        await stop_track(guild)

async def test_a_second_tap_still_swaps_the_armed_song_after_a_skip(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9411, ["Song 1", "Song 2", "Song 3", "Song 4"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    view = LiveLine(guild, store)
    try:
        assert note == "Playing Song 1."
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        press = _press(member)
        await _button(view, LABEL_ADJUST).callback(press)
        await _button(view, MARK_MOVE, 2).callback(press)
        song_4 = _button(view, MARK_MOVE, 3)

        assert await next_track(guild) is None
        await song_4.callback(press)
        assert [row.title for row in queued(guild)] == ["Song 2", "Song 4", "Song 3"]
        assert queued(guild)[0].playing is True
        assert "That song is gone" not in (press.send.await_args.args[0] if press.send.await_args else "")
    finally:
        view.stop()
        await stop_track(guild)

async def test_adjust_arms_the_song_the_button_showed(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9412, ["Song 1", "Song 2", "Song 3", "Song 4"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    view = LiveLine(guild, store)
    try:
        assert note == "Playing Song 1."
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        press = _press(member)
        await _button(view, LABEL_ADJUST).callback(press)
        song_3 = _button(view, MARK_MOVE, 2)
        assert await next_track(guild) is None
        await song_3.callback(press)
        assert "-# Moving **1\\.** Song 3" in _text(view)
        assert _button(view, MARK_MOVE, 1).style == discord.ButtonStyle.primary
    finally:
        view.stop()
        await stop_track(guild)

async def test_sweep_drops_every_waiting_song(tmp_path):
    guild, member, player, _tracks, note = await _line_up(9300, ["Alpha", "Beta", "Gamma"])
    store = GuildConfig(tmp_path / "guild.json")
    try:
        assert note == "Playing Alpha."
        assert await next_track(guild) is None
        assert [row.title for row in queued(guild)] == ["Beta", "Gamma"]
        assert [song.track.title for song in _stored(guild).earlier] == ["Alpha"]

        view = LiveLine(guild, store)
        view.message = MagicMock()
        view.message.edit = AsyncMock()

        outsider = MagicMock()
        outsider.voice.channel = MagicMock()
        outsider.voice.channel.id = 1
        refused = _press(outsider)
        await _answer_empty(view, refused, "YES")
        assert "Join the voice channel first" in refused.send.await_args.args[0]
        assert [row.title for row in queued(guild)] == ["Beta", "Gamma"]

        held = _press(member)
        opened = await _open_empty(view, held)
        assert opened.title == "Empty"
        assert opened.query.text == "This action cannot be undone."
        assert opened.query.description == empty_hint(QUEUE)
        assert 'Type "yes" to confirm' in opened.query.description
        assert opened.query.component.placeholder == "yes"
        opened.query.component._value = "no"
        declined = _press(member)
        await opened.on_submit(declined)
        declined.send.assert_not_awaited()
        assert [row.title for row in queued(guild)] == ["Beta", "Gamma"]

        press = _press(member)
        await _answer_empty(view, press, "Yes")
        assert [row.title for row in queued(guild)] == ["Beta"]
        assert queued(guild)[0].playing is True
        assert [song.track.title for song in _stored(guild).earlier] == ["Alpha"]
        player.stop.assert_not_awaited()
        player.disconnect.assert_not_awaited()
        text = _text(view)
        assert "Beta" in text
        assert "Gamma" not in text
        assert "0 songs in queue" in text
        assert _button(view, LABEL_EMPTY).disabled is True
        assert _button(view, LABEL_ADJUST).disabled is True

        await clear_queued(guild)
        assert [row.title for row in queued(guild)] == ["Beta"]
    finally:
        await stop_track(guild)

async def test_a_parked_queue_can_be_emptied_deleted_and_reordered(tmp_path):
    import music.playback as playback

    guild, _member, _player, _tracks, note = await _line_up(9700, ["Alpha", "Beta", "Gamma"])
    store = GuildConfig(tmp_path / "guild.json")
    try:
        assert note == "Playing Alpha."
        release(guild)
        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        assert [row.title for row in queued(guild)] == ["Alpha", "Beta", "Gamma"]
        assert [row.playing for row in queued(guild)] == [False, False, False]

        view = LiveLine(guild, store)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        press = _press()
        await _button(view, MARK_DELETE, 2).callback(press)
        assert [row.title for row in queued(guild)] == ["Alpha", "Beta"]

        await _button(view, LABEL_ADJUST).callback(press)
        await _button(view, MARK_MOVE, 0).callback(press)
        await _button(view, MARK_MOVE, 1).callback(press)
        assert [row.title for row in queued(guild)] == ["Beta", "Alpha"]

        await _answer_empty(view, press, "yes")
        assert queued(guild) is None
    finally:
        await stop_track(guild)
