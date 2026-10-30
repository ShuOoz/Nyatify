"""The queue tab. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from guild import GuildConfig
from music.panel import MARK_BACK, MARK_LIST, MARK_NEXT
from music.playlist.card import (
    PlaylistCard,
    HISTORY,
    LABEL_ADJUST,
    LABEL_EMPTY,
    MARK_ADD,
    MARK_MINUS,
    MARK_MOVE,
    QUEUE,
)
from music.archive import SavedSong, create_playlist, SavedList
from music.lavalink import Opened
from music.playback import clear_queued, next_track, queue_tracks, queued, save_and_clear, stop_track, _stored
from music.playback.remember import Queued
from music.playlist.popups import empty_hint

from tests.music.support import _cards_caught_up, _defer_marks_done, _named, _scene
from tests.music.playlist.support import (
    _answer_empty,
    _button,
    _line_up,
    _open_empty,
    _press,
    _sections,
    _text,
)

async def test_queueing_from_the_card_waits_behind_what_is_playing(monkeypatch, tmp_path):
    """The queue button lines a playlist up in order. Plus on a saved song waits behind the current one. Songs that join during the click show up after it."""
    spot = tmp_path / "order"
    spot.mkdir()
    with monkeypatch.context() as patch:
        await case_the_queue_button_lines_up_a_playlist_in_order(patch, spot)
    spot = tmp_path / "plus"
    spot.mkdir()
    with monkeypatch.context() as patch:
        await case_plus_on_a_saved_song_waits_behind_the_current_one(patch, spot)
    spot = tmp_path / "during"
    spot.mkdir()
    await _scene(case_songs_that_join_during_the_queue_click_show_up_after_it, spot, monkeypatch=monkeypatch)

async def case_the_queue_button_lines_up_a_playlist_in_order(monkeypatch, tmp_path):
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
    view = PlaylistCard(guild, store)
    assert _button(view, MARK_LIST).disabled is False
    press = _press()
    await _button(view, MARK_LIST).callback(press)
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

    monkeypatch.setattr("music.playlist.queue.play_saved", play)

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

async def case_songs_that_join_during_the_queue_click_show_up_after_it(monkeypatch, tmp_path):
    guild, member, _player, _tracks, note = await _line_up(9420, ["Alpha"])
    store = GuildConfig(tmp_path / "guild.json")
    created = create_playlist(
        store, guild.id, "Evening", member.id,
        (SavedSong("One", "youtube", "https://example.com/one"),),
    )
    assert isinstance(created, SavedList)
    view = PlaylistCard(guild, store)
    message = MagicMock()
    message.edit = AsyncMock()
    view.follow(message)
    try:
        assert note == "Playing Alpha."

        async def play(_member, _guild, songs, *, replace):
            assert replace is False
            return "Added One."

        monkeypatch.setattr("music.playlist.queue.play_saved", play)

        async def edit(*_args, **_kwargs):
            extra = _named("Beta")
            extra.uri = "https://example.com/beta"
            assert await queue_tracks(member, guild, [extra]) == "Added Beta."
            await _cards_caught_up()
            assert "Beta" not in _text(view)

        press = _press(member)
        press.guild = guild
        await _button(view, MARK_LIST).callback(press)
        modal = press.response.send_modal.await_args.args[0]
        modal.menu._values = ["Evening"]
        submit = _press(member)
        submit.guild = guild
        _defer_marks_done(submit)
        submit.edit_original_response = AsyncMock(side_effect=edit)
        await modal.on_submit(submit)
        assert "Beta" in _text(view)
        message.edit.assert_awaited()
    finally:
        view.stop()
        await stop_track(guild)

async def case_plus_on_a_saved_song_waits_behind_the_current_one(monkeypatch, tmp_path):
    guild, member, _player, _tracks, note = await _line_up(9421, ["Alpha", "Beta"])
    store = GuildConfig(tmp_path / "guild.json")
    created = create_playlist(
        store, guild.id, "Evening", member.id,
        (SavedSong("Gamma", "youtube", "https://example.com/gamma"),),
    )
    assert isinstance(created, SavedList)
    view = PlaylistCard(guild, store, opened=created)
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    try:
        assert note == "Playing Alpha."

        async def open_all(_guild, songs):
            track = _named(songs[0].title)
            track.uri = songs[0].url
            return Opened([(track, songs[0].platform)], [])

        monkeypatch.setattr("music.playlist.lists.open_recordings", open_all)
        press = _press(member)
        press.guild = guild
        _defer_marks_done(press)
        await _button(view, MARK_ADD, 0).callback(press)
        assert [row.title for row in queued(guild)] == ["Alpha", "Beta", "Gamma"]
        assert queued(guild)[0].playing is True
    finally:
        view.stop()
        await stop_track(guild)

async def test_the_waiting_songs(tmp_path):
    """Delete, reorder, and adjust use the song on the button. Sweep drops the waiting songs. A parked queue can still be changed."""
    for index, case in enumerate((
        case_delete_uses_the_song_on_the_button,
        case_order_swaps_waiting_songs_across_pages,
        case_adjust_follows_the_armed_song,
        case_sweep_drops_every_waiting_song,
        case_a_parked_queue,
    )):
        spot = tmp_path / f"edit{index}"
        spot.mkdir()
        await _scene(case, spot)

async def case_delete_uses_the_song_on_the_button(tmp_path):
    """Delete removes the song the button showed, and skips one that started after the button was drawn."""
    spot = tmp_path / "part0"
    spot.mkdir()
    await case_delete_removes_the_song_the_button_showed(spot)
    spot = tmp_path / "part1"
    spot.mkdir()
    await case_delete_skips_a_song_that_started_after_the_button_was_drawn(spot)

async def case_delete_removes_the_song_the_button_showed(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9413, ["Song 1", "Song 2", "Song 3", "Song 4"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    view = PlaylistCard(guild, store)
    try:
        assert note == "Playing Song 1."
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        press = _press(member)
        song_3 = _button(view, MARK_MINUS, 2)
        assert await next_track(guild) is None
        await song_3.callback(press)
        assert [row.title for row in queued(guild)] == ["Song 2", "Song 4"]
        assert queued(guild)[0].playing is True
    finally:
        view.stop()
        await stop_track(guild)

async def case_delete_skips_a_song_that_started_after_the_button_was_drawn(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9414, ["Song 1", "Song 2", "Song 3"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    view = PlaylistCard(guild, store)
    try:
        assert note == "Playing Song 1."
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        press = _press(member)
        song_2 = _button(view, MARK_MINUS, 1)
        assert await next_track(guild) is None
        await song_2.callback(press)
        assert [row.title for row in queued(guild)] == ["Song 3"]
        assert queued(guild)[0].playing is True
    finally:
        view.stop()
        await stop_track(guild)

async def case_order_swaps_waiting_songs_across_pages(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9400, ["Song 1", "Song 2", "Song 3", "Song 4", "Song 5", "Song 6", "Song 7"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    try:
        assert note == "Playing Song 1."
        view = PlaylistCard(guild, store)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        view.to_components()

        short, _member, _player, _tracks, _note = await _line_up(9401, ["Only", "Next"])
        quiet = PlaylistCard(short, store)
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
        assert [item.accessory.label for item in _sections(view)[1:]] == [MARK_MINUS] * 5
        assert _button(view, LABEL_ADJUST).style == discord.ButtonStyle.secondary
    finally:
        await stop_track(guild)

async def case_adjust_follows_the_armed_song(tmp_path):
    """Adjust arms the song on the button, follows it when the current one ends, and a second tap still swaps after a skip."""
    spot = tmp_path / "part0"
    spot.mkdir()
    await case_adjust_arms_the_song_the_button_showed(spot)
    spot = tmp_path / "part1"
    spot.mkdir()
    await case_adjust_follows_the_armed_song_when_the_current_one_ends(spot)
    spot = tmp_path / "part2"
    spot.mkdir()
    await case_a_second_tap_still_swaps_the_armed_song_after_a_skip(spot)

async def case_adjust_follows_the_armed_song_when_the_current_one_ends(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9410, ["Song 1", "Song 2", "Song 3", "Song 4"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    view = PlaylistCard(guild, store)
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

async def case_a_second_tap_still_swaps_the_armed_song_after_a_skip(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9411, ["Song 1", "Song 2", "Song 3", "Song 4"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    view = PlaylistCard(guild, store)
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

async def case_adjust_arms_the_song_the_button_showed(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9412, ["Song 1", "Song 2", "Song 3", "Song 4"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    view = PlaylistCard(guild, store)
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

async def case_sweep_drops_every_waiting_song(tmp_path):
    guild, member, player, _tracks, note = await _line_up(9300, ["Alpha", "Beta", "Gamma"])
    store = GuildConfig(tmp_path / "guild.json")
    try:
        assert note == "Playing Alpha."
        assert await next_track(guild) is None
        assert [row.title for row in queued(guild)] == ["Beta", "Gamma"]
        assert [song.track.title for song in _stored(guild).earlier] == ["Alpha"]

        view = PlaylistCard(guild, store)
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
        opened.query.component._value = "yes please"
        declined = _press(member)
        await opened.on_submit(declined)
        declined.send.assert_not_awaited()
        assert len(opened.query.description) <= 100
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

async def case_a_parked_queue(tmp_path):
    """A parked queue can be emptied, deleted, and reordered. A waiting song can be deleted after pause leaves voice."""
    spot = tmp_path / "part0"
    spot.mkdir()
    await case_a_parked_queue_can_be_emptied_deleted_and_reordered(spot)
    spot = tmp_path / "part1"
    spot.mkdir()
    await case_a_waiting_song_can_be_deleted_after_a_pause_leaves_the_bot_out_of_voice(spot)

async def case_a_parked_queue_can_be_emptied_deleted_and_reordered(tmp_path):
    import music.playback as playback

    guild, _member, _player, _tracks, note = await _line_up(9700, ["Alpha", "Beta", "Gamma"])
    store = GuildConfig(tmp_path / "guild.json")
    try:
        assert note == "Playing Alpha."
        save_and_clear(guild)
        playback._lines.clear()
        playback._heard.clear()
        playback._missing.clear()
        guild.voice_client = None
        assert [row.title for row in queued(guild)] == ["Alpha", "Beta", "Gamma"]
        assert [row.playing for row in queued(guild)] == [True, False, False]

        view = PlaylistCard(guild, store)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        shown = _text(view)
        assert "Now Playing" in shown
        assert "Nothing is playing" not in shown
        press = _press()
        await _button(view, LABEL_ADJUST).callback(press)
        await _button(view, MARK_MOVE, 1).callback(press)
        await _button(view, MARK_MOVE, 2).callback(press)
        assert [row.title for row in queued(guild)] == ["Alpha", "Gamma", "Beta"]

        await _button(view, LABEL_ADJUST).callback(press)
        await _button(view, MARK_MINUS, 2).callback(press)
        assert [row.title for row in queued(guild)] == ["Alpha", "Gamma"]

        await _answer_empty(view, press, "yes")
        assert [row.title for row in queued(guild)] == ["Alpha"]
        assert queued(guild)[0].playing is True
    finally:
        await stop_track(guild)

async def case_a_waiting_song_can_be_deleted_after_a_pause_leaves_the_bot_out_of_voice(tmp_path):
    """The bot left on a paused song. Someone outside voice removes a waiting one."""
    from music.playback import toggle_pause

    guild, _member, player, _tracks, note = await _line_up(9701, ["Alpha", "Beta", "Gamma"])
    store = GuildConfig(tmp_path / "guild.json")
    try:
        assert note == "Playing Alpha."
        player._track = _tracks[0]
        assert await toggle_pause(guild) is None
        # The link is gone and nothing released the line.
        guild.voice_client = None
        outsider = MagicMock()
        outsider.voice = None

        view = PlaylistCard(guild, store)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        shown = _text(view)
        assert "Now Playing" in shown
        assert "Nothing is playing" not in shown
        press = _press(outsider)
        await _button(view, MARK_MINUS, 1).callback(press)
        assert [(row.title, row.playing) for row in queued(guild)] == [
            ("Alpha", True),
            ("Gamma", False),
        ]
        assert _stored(guild).current.track.title == "Alpha"
    finally:
        guild.voice_client = None
        await stop_track(guild)

async def test_a_paused_song_does_not_pull_a_queued_playlist_into_voice(monkeypatch, tmp_path):
    """Queueing a playlist stays out of voice while a paused song is still current."""
    guild = MagicMock()
    guild.id = 9801
    guild.voice_client = None
    store = GuildConfig(tmp_path / "guild.json")
    created = create_playlist(
        store, guild.id, "Evening", 4,
        (SavedSong("After", "youtube", "https://example.com/after"),),
    )
    assert isinstance(created, SavedList)
    warmed = MagicMock()
    left = AsyncMock()
    monkeypatch.setattr("music.playlist.queue.paused_away", lambda _guild: True)
    monkeypatch.setattr("music.playlist.queue.warm_voice", warmed)
    monkeypatch.setattr("music.playlist.queue.leave_if_idle", left)
    monkeypatch.setattr(
        "music.playlist.queue.play_saved",
        AsyncMock(return_value="Added After. I Ain't Worried is still paused."),
    )
    view = PlaylistCard(guild, store)
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    press = _press()
    press.guild = guild
    await _button(view, MARK_LIST).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    modal.menu._values = ["Evening"]
    submit = _press()
    submit.guild = guild
    await modal.on_submit(submit)
    warmed.assert_not_called()
    left.assert_not_called()

    warmed.reset_mock()
    left.reset_mock()
    monkeypatch.setattr("music.playlist.queue.paused_away", lambda _guild: False)
    monkeypatch.setattr("music.playlist.queue.play_saved", AsyncMock(return_value="Playing After."))
    again = _press()
    again.guild = guild
    await modal.on_submit(again)
    warmed.assert_called_once()
    left.assert_called_once()

def test_two_copies_of_a_queued_song_keep_the_closer_row():
    """A stale button still finds a copy when the same song is queued twice."""
    view = PlaylistCard(MagicMock(), MagicMock())
    song = Queued("After", "youtube", "https://example.com/after", False, False, "A", 180_000, 9)
    other = Queued("Other", "youtube", "https://example.com/other", False, False, "B", 1, 9)
    rows = [song, other, song, other, song]
    key = (song.title, song.url, song.author, song.platform, song.requester_id)
    assert view._place_of(rows, key, 2) == 2
    assert view._place_of(rows, key, 0) == 0
    assert view._place_of([song], key, 5) == 0
    assert view._place_of(rows, key, None) is None
    assert view._place_of(rows, key, 1) == 0
    assert view._place_of(rows, key, 3) == 2
    assert view._place_of(rows, key, 5) == 4
