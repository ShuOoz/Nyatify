"""The player refresh. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music.playback import stop_track
from music.player.card import MusicPlayer

from tests.music.support import _playing, _scene


async def test_the_refresh_stays_with_the_open_card(monkeypatch):
    """A dropped voice closes the card without deleting it, finds the guild again, and a mode press keeps the card until the song is gone."""
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
