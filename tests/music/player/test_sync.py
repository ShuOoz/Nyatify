"""The player refresh. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music.playback import stop_track
from music.player.card import MusicPlayer
from music.player.sync import (
    _replace_mode,
    _replace_next,
    after_quiet,
    card_stays,
    shows_mode,
    spend_mode,
)

from tests.music.support import _playing, _scene


async def test_a_card_follows_the_server_not_one_guild_copy():
    """The card watches every copy. One opened after the bot has left stays up."""
    await case_a_card_on_a_blind_guild_goes_when_the_server_loses_the_bot()
    await case_a_card_opened_after_the_bot_left_stays()


async def case_a_card_on_a_blind_guild_goes_when_the_server_loses_the_bot():
    guild, player, _track = await _playing()
    try:
        blind = MagicMock()
        blind.id = guild.id
        blind.voice_client = None

        def client(guild_id):
            if guild_id == guild.id and guild.voice_client is not None:
                return player
            return None

        blind._state._get_voice_client = client
        card = MusicPlayer(blind)
        assert card._left() is False
        assert card._voiced is True
        guild.voice_client = None
        assert card._left() is True
    finally:
        guild.voice_client = None
        await stop_track(guild)


async def case_a_card_opened_after_the_bot_left_stays():
    from music.playback.remember import bot_was_in, note_bot_in

    guild, _player, _track = await _playing()
    try:
        note_bot_in(guild)
        guild.voice_client = None
        card = MusicPlayer(guild)
        assert bot_was_in(guild) is True
        assert card._left() is False
        assert card._voiced is False
    finally:
        guild.voice_client = None
        await stop_track(guild)


async def test_the_refresh_stays_with_the_open_card(monkeypatch):
    """A dropped voice closes the card without deleting it, finds the guild again, and a mode press keeps the card until the song is gone."""
    case_a_quiet_press_keeps_or_drops_the_card()
    await _scene(case_a_dropped_voice_closes_without_deleting_the_card, monkeypatch=monkeypatch)
    await _scene(case_a_dropped_voice_finds_the_guild_again, monkeypatch=monkeypatch)
    await _scene(case_a_mode_press_keeps_the_card_until_the_song_is_gone)


async def case_a_dropped_voice_closes_without_deleting_the_card(monkeypatch):
    guild, player, _track = await _playing()
    view = None
    try:
        stale = MagicMock()
        stale.id = guild.id
        stale.voice_client = player
        view = MusicPlayer(stale)
        message = MagicMock()
        message.delete = AsyncMock()
        message.edit = AsyncMock()
        message.guild = guild
        view.message = message
        stale.voice_client = None
        passes = {"n": 0}

        async def no_wait(_seconds):
            passes["n"] += 1
            if passes["n"] == 2:
                view.shutdown()

        monkeypatch.setattr("music.player.sync.asyncio.sleep", no_wait)
        view.follow(message)
        await view._task
        assert passes["n"] == 2
        assert view.guild is guild
        assert view._closed is True
        message.delete.assert_not_awaited()
    finally:
        if view is not None and view._task is not None:
            view.shutdown()
        guild.voice_client = None
        await stop_track(guild)


async def case_a_dropped_voice_finds_the_guild_again(monkeypatch):
    guild, player, _track = await _playing()
    view = None
    try:
        stale = MagicMock()
        stale.id = guild.id
        stale.voice_client = player
        view = MusicPlayer(stale)
        message = MagicMock()
        message.delete = AsyncMock()
        message.edit = AsyncMock()
        message.guild = stale
        view.message = message
        stale.voice_client = None
        stale._state.get_guild = lambda guild_id: guild if guild_id == stale.id else None
        passes = {"n": 0}

        async def no_wait(_seconds):
            passes["n"] += 1
            if passes["n"] == 2:
                view.shutdown()

        monkeypatch.setattr("music.player.sync.asyncio.sleep", no_wait)
        view.follow(message)
        await view._task
        assert passes["n"] == 2
        assert view.guild is guild
        message.delete.assert_not_awaited()
    finally:
        if view is not None and view._task is not None:
            view.shutdown()
        guild.voice_client = None
        await stop_track(guild)


async def case_a_mode_press_keeps_the_card_until_the_song_is_gone():
    guild, player, _track = await _playing()
    view = None
    try:
        view = MusicPlayer(guild)
        player._track = None
        message = MagicMock()
        message.delete = AsyncMock()
        message.edit = AsyncMock()
        view.message = message
        view._mode_press = True
        await view.redraw()
        await view.redraw()
        assert view._closed is False
        message.delete.assert_not_awaited()
        await view.redraw()
        assert view._closed is True
        message.delete.assert_awaited()
    finally:
        if view is not None and not view._closed:
            view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)

def case_a_quiet_press_keeps_or_drops_the_card():
    """The mode line changes on its own. A press that is not a mode lets the card go."""
    assert card_stays(False, False) is False
    assert card_stays(True, False) is True
    assert card_stays(False, True) is True
    assert _replace_mode("-# On Queue\n-# Up next: …", False, False, True) == "-# On Radio\n-# Up next: …"
    assert _replace_mode("-# On Radio\n-# Up next: Wonderwall", False, False, False) == (
        "-# On Queue\n-# Up next: Wonderwall"
    )
    assert _replace_mode("-# On Repeat + Radio\n-# Up next: After", False, False, False) == (
        "-# On Queue\n-# Up next: After"
    )
    assert _replace_mode("-# On Repeat\n-# Up next: After", True, False, True) == (
        "-# On Repeat + Radio\n-# Up next: After"
    )
    assert _replace_next("-# On Repeat\n-# Up next: After", "-# Up next: I Ain't Worried") == (
        "-# On Repeat\n-# Up next: I Ain't Worried"
    )
    assert _replace_next("**Nothing is playing**", "-# Up next: After") == "**Nothing is playing**"
    assert spend_mode(True, False) == (True, True)
    assert spend_mode(False, True) == (True, False)
    assert spend_mode(False, False) == (False, False)
    assert after_quiet("repeat", failed=False, showing=True, holding=False) == "stay"
    assert after_quiet("shuffle", failed=True, showing=True, holding=True) == "restore"
    assert after_quiet("repeat", failed=False, showing=False, holding=True) == "stay"
    assert after_quiet(None, failed=False, showing=False, holding=False) == "dismiss"
    assert after_quiet("radio", failed=False, showing=True, holding=False) == "dismiss"
    assert shows_mode("radio") is True
    assert shows_mode("pause") is False
