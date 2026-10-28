"""Songs already played. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.osa_musify import OsaMusify
from guild import GuildConfig
from music.panel import LABEL_PLAYER, MARK_ADD, MARK_BACK, MARK_CLOSE, MARK_NEXT
from music.player.view import MusicPlayer
from music.playlist.card import (
    LiveLine,
    HISTORY,
    LABEL_EMPTY,
    LABEL_YOUR,
    MARK_SAVE,
    PICK,
    PLAYLISTS,
)
from music.archive import create_playlist, find_playlist
from music.playback import next_track, played, queue_tracks, queued, stop_track

from tests.music.support import _ask, _defer_marks_done, _named
from tests.music.playlist.support import _answer_empty, _button, _line_up, _press, _text

async def test_replay_uses_the_song_the_button_showed(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(
        9311, ["Alpha", "Beta", "Gamma", "Delta"],
    )
    store = GuildConfig(tmp_path / "guild.json")
    view = LiveLine(guild, store)
    try:
        assert note == "Playing Alpha."
        assert await next_track(guild) is None
        assert await next_track(guild) is None
        assert [row.title for row in played(guild)] == ["Beta", "Alpha"]
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        await _button(view, HISTORY).callback(_press(member))
        alpha = _button(view, MARK_ADD, 1)
        assert await next_track(guild) is None
        assert [row.title for row in played(guild)] == ["Gamma", "Beta", "Alpha"]
        press = _press(member)
        press.guild = guild
        await alpha.callback(press)
        assert [row.title for row in queued(guild)] == ["Delta", "Alpha"]
    finally:
        view.stop()
        await stop_track(guild)

async def test_an_empty_queue_opens_the_player_when_a_song_starts(monkeypatch):
    guild, member, player, _tracks, note = await _line_up(9500, ["Alpha", "Beta"])
    view = None
    panel = None
    try:
        assert note == "Playing Alpha."
        await next_track(guild)
        await stop_track(guild)
        guild.voice_client = None
        assert [row.title for row in played(guild)] == ["Beta", "Alpha"]

        cog = OsaMusify(MagicMock())
        monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)
        interaction = _ask()
        interaction.guild = guild
        interaction.user = member
        interaction.response.send_message = AsyncMock()
        message = MagicMock()
        message.edit = AsyncMock()
        interaction.original_response = AsyncMock(return_value=message)
        await cog.playlist.callback(cog, interaction)
        view = interaction.response.send_message.await_args.kwargs["view"]
        await _button(view, HISTORY).callback(_press(member))

        press = _press(member)
        press.guild = guild
        press.channel.send = AsyncMock(return_value=MagicMock())
        _defer_marks_done(press)
        await _button(view, MARK_ADD, 0).callback(press)
        panel = press.channel.send.await_args.kwargs["view"]
        assert isinstance(panel, MusicPlayer)
        assert view.is_finished() is False
        assert press.send.await_count == 0

        guild.voice_client = player
        added = _press(member)
        added.guild = guild
        added.channel.send = AsyncMock()
        _defer_marks_done(added)
        await _button(view, MARK_ADD, 0).callback(added)
        added.channel.send.assert_not_awaited()
        assert [row.title for row in queued(guild)] == ["Beta", "Alpha"]
        assert "Added Alpha" in added.send.await_args.args[0]
        assert "Alpha" not in _text(view)
    finally:
        if panel is not None:
            panel.shutdown()
        if view is not None:
            view.stop()
        await stop_track(guild)

async def test_mine_shows_only_that_persons_songs(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(9302, ["Alpha", "Beta"])
    store = GuildConfig(tmp_path / "guild.json")
    try:
        assert note == "Playing Alpha."
        assert await next_track(guild) is None
        other = MagicMock()
        other.id = 9305
        other.voice.channel = member.voice.channel
        gamma = _named("Gamma")
        gamma.uri = "https://example.com/gamma"
        assert await queue_tracks(other, guild, [gamma]) == "Added Gamma."
        assert await next_track(guild) is None
        assert await next_track(guild) is None
        assert [row.title for row in played(guild)] == ["Gamma", "Beta", "Alpha"]

        view = LiveLine(guild, store)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        await _button(view, HISTORY).callback(_press(member))
        assert "Gamma" in _text(view)
        assert _button(view, LABEL_YOUR).disabled is False
        assert _button(view, LABEL_YOUR).style == discord.ButtonStyle.secondary

        press = _press(member)
        await _button(view, LABEL_YOUR).callback(press)
        assert _button(view, LABEL_YOUR).style == discord.ButtonStyle.primary
        text = _text(view)
        assert "Beta" in text
        assert "Alpha" in text
        assert "Gamma" not in text
        assert _button(view, MARK_ADD, 1).label == MARK_ADD

        await _button(view, LABEL_YOUR).callback(press)
        assert _button(view, LABEL_YOUR).style == discord.ButtonStyle.secondary
        assert "Gamma" in _text(view)

        await _button(view, LABEL_YOUR).callback(_press(other))
        assert "Gamma" in _text(view)
        assert "Alpha" not in _text(view)
        assert _button(view, LABEL_YOUR).style == discord.ButtonStyle.primary

        await _button(view, LABEL_YOUR).callback(_press(member))
        text = _text(view)
        assert "Beta" in text
        assert "Alpha" in text
        assert "Gamma" not in text

        lonely = MagicMock()
        lonely.id = 1
        await _button(view, LABEL_YOUR).callback(_press(lonely))
        assert "-# Nothing of yours yet" in _text(view)
        assert "Alpha" not in _text(view)
        assert _button(view, LABEL_YOUR).style == discord.ButtonStyle.primary

        await _answer_empty(view, _press(member), "yes")
        assert played(guild) == []
        assert "-# Nothing has played yet" in _text(view)
        assert "Nothing of yours yet" not in _text(view)
        assert _button(view, LABEL_YOUR).disabled is True
        assert _button(view, LABEL_YOUR).style == discord.ButtonStyle.secondary
    finally:
        await stop_track(guild)

async def test_sweep_drops_every_played_song(tmp_path):
    guild, _member, player, _tracks, note = await _line_up(9301, ["Alpha", "Beta", "Gamma"])
    store = GuildConfig(tmp_path / "guild.json")
    try:
        assert note == "Playing Alpha."
        assert await next_track(guild) is None
        assert [row.title for row in played(guild)] == ["Alpha"]
        assert [row.title for row in queued(guild)] == ["Beta", "Gamma"]

        view = LiveLine(guild, store)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        opened = _press()
        await _button(view, HISTORY).callback(opened)
        rows = [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]
        assert [child.label for child in rows[-1].children] == [
            MARK_BACK, MARK_NEXT, LABEL_YOUR, LABEL_EMPTY, MARK_CLOSE,
        ]
        assert LABEL_YOUR.split("@Your") == LABEL_PLAYER.split("Player")
        assert _button(view, LABEL_EMPTY).disabled is False

        guild.voice_client = None
        outsider = MagicMock()
        outsider.voice = None
        press = _press(outsider)
        await _answer_empty(view, press, "yes")
        assert played(guild) == []
        assert [row.title for row in queued(guild)] == ["Beta", "Gamma"]
        assert all(row.playing is False for row in queued(guild))
        player.stop.assert_not_awaited()
        text = _text(view)
        assert "-# Nothing has played yet" in text
        assert "Alpha" not in text
        assert _button(view, LABEL_EMPTY).disabled is True
    finally:
        await stop_track(guild)

async def test_the_star_saves_a_played_song(tmp_path):
    guild, member, _player, _tracks, note = await _line_up(9600, ["Alpha", "Beta"])
    store = GuildConfig(tmp_path / "guild.json")
    view = None
    try:
        assert note == "Playing Alpha."
        assert await next_track(guild) is None
        assert [row.title for row in played(guild)] == ["Alpha"]
        view = LiveLine(guild, store)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        await _button(view, HISTORY).callback(_press(member))
        assert _button(view, MARK_SAVE).disabled is False
        assert _button(view, MARK_SAVE).style == discord.ButtonStyle.secondary
        assert _button(view, MARK_ADD, 0).method == "on_again"

        await _button(view, MARK_SAVE).callback(_press(member))
        assert _button(view, MARK_SAVE).style == discord.ButtonStyle.primary
        assert _button(view, MARK_ADD, 0).label == MARK_ADD
        assert _button(view, MARK_ADD, 0).method == "on_keep"

        press = _press(member)
        press.guild = guild
        await _button(view, MARK_ADD, 0).callback(press)
        assert "There is no playlist yet" in press.send.await_args.args[0]
        assert [row.title for row in queued(guild)] == ["Beta"]

        create_playlist(store, guild.id, "Evening", member.id)
        create_playlist(store, guild.id, "Morning", member.id)
        press = _press(member)
        press.guild = guild
        await _button(view, MARK_ADD, 0).callback(press)
        modal = press.response.send_modal.await_args.args[0]
        assert modal.title == "Save"
        assert modal.menu.placeholder == PICK
        assert [option.label for option in modal.menu.options] == ["Evening", "Morning"]
        modal.menu._values = []
        blank = _press(member)
        await modal.on_submit(blank)
        assert "Pick a playlist" in blank.send.await_args.args[0]

        modal.menu._values = ["Evening"]
        submit = _press(member)
        await modal.on_submit(submit)
        assert "Saved to Evening" in submit.send.await_args.args[0]
        saved = find_playlist(store, guild.id, "Evening")
        assert [(song.title, song.url) for song in saved.songs] == [
            ("Alpha", "https://example.com/alpha"),
        ]
        assert [row.title for row in queued(guild)] == ["Beta"]

        modal.menu._values = ["Evening"]
        again = _press(member)
        await modal.on_submit(again)
        assert "already in the playlist" in again.send.await_args.args[0]
        assert len(find_playlist(store, guild.id, "Evening").songs) == 1

        modal.menu._values = ["Missing"]
        missing = _press(member)
        await modal.on_submit(missing)
        assert "That playlist is gone" in missing.send.await_args.args[0]

        await _button(view, MARK_SAVE).callback(_press(member))
        assert _button(view, MARK_SAVE).style == discord.ButtonStyle.secondary
        assert _button(view, MARK_ADD, 0).method == "on_again"

        await _button(view, MARK_SAVE).callback(_press(member))
        press = _press(member)
        await _button(view, MARK_ADD, 0).callback(press)
        modal = press.response.send_modal.await_args.args[0]
        view.panel = PLAYLISTS
        view.opened = find_playlist(store, guild.id, "Morning")
        modal.menu._values = ["Morning"]
        shown = _press(member)
        await modal.on_submit(shown)
        assert "Saved to Morning" in shown.send.await_args.args[0]
        assert [song.title for song in view.opened.songs] == ["Alpha"]
        assert "Alpha" in _text(view)
        view.message.edit.assert_awaited()
    finally:
        if view is not None:
            view.stop()
        await stop_track(guild)
