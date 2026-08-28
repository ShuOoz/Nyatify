from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from nyatify import Nyatify, bot_intents


def _bot_for_sync() -> Nyatify:
    bot = Nyatify(enabled_cogs=["admin"])
    bot.tree.copy_global_to = MagicMock()
    bot.tree.sync = AsyncMock(return_value=[object()])
    bot.tree.fetch_commands = AsyncMock(return_value=[])
    bot.tree.get_commands = MagicMock(return_value=[])
    bot.tree.clear_commands = MagicMock()
    bot.tree.add_command = MagicMock()
    return bot


async def _empty_guilds(**_kwargs):
    return
    yield  # pragma: no cover


async def _guilds(*guilds, **_kwargs):
    for guild in guilds:
        yield guild


def test_all_cogs_request_voice_and_message_content():
    intents = bot_intents(None)
    assert intents.voice_states is True
    assert intents.message_content is True
    assert intents.members is True


def test_admin_isolation_does_not_request_voice_states():
    intents = bot_intents(["admin"])
    assert intents.voice_states is False
    assert intents.message_content is True
    assert intents.members is True


def test_voice_isolation_does_not_request_message_content():
    intents = bot_intents(["voice"])
    assert intents.voice_states is True
    assert intents.message_content is False
    assert intents.members is True


async def test_guild_sync_publishes_to_that_server_and_clears_globals(monkeypatch):
    monkeypatch.setattr("nyatify.settings.sync_guild_id", 111)
    bot = _bot_for_sync()
    bot.fetch_guilds = _empty_guilds
    leftover = object()
    local = object()
    bot.tree.fetch_commands = AsyncMock(return_value=[leftover])
    bot.tree.get_commands = MagicMock(return_value=[local])

    await bot._sync_slash_commands()
    await bot.close()

    bot.tree.copy_global_to.assert_called_once()
    assert bot.tree.copy_global_to.call_args.kwargs["guild"].id == 111
    assert [call.kwargs.get("guild") for call in bot.tree.sync.await_args_list] == [
        bot.tree.copy_global_to.call_args.kwargs["guild"],
        None,
    ]
    bot.tree.clear_commands.assert_called_once_with(guild=None)
    bot.tree.add_command.assert_called_once_with(local, guild=None, override=True)


async def test_global_sync_clears_leftover_guild_commands(monkeypatch):
    monkeypatch.setattr("nyatify.settings.sync_guild_id", None)
    bot = _bot_for_sync()
    other = MagicMock()
    other.id = 222
    leftover = object()
    bot.fetch_guilds = lambda **_kwargs: _guilds(other)
    bot.tree.fetch_commands = AsyncMock(side_effect=lambda *, guild=None: [leftover] if guild is other else [])

    await bot._sync_slash_commands()
    await bot.close()

    bot.tree.copy_global_to.assert_not_called()
    synced_guilds = [call.kwargs.get("guild") for call in bot.tree.sync.await_args_list]
    assert synced_guilds == [None, other]
    bot.tree.clear_commands.assert_called_once_with(guild=other)


async def test_global_sync_skips_empty_guild_overwrite(monkeypatch):
    monkeypatch.setattr("nyatify.settings.sync_guild_id", None)
    bot = _bot_for_sync()
    other = MagicMock()
    other.id = 222
    bot.fetch_guilds = lambda **_kwargs: _guilds(other)

    await bot._sync_slash_commands()
    await bot.close()

    assert [call.kwargs.get("guild") for call in bot.tree.sync.await_args_list] == [None]
    bot.tree.clear_commands.assert_not_called()


async def test_guild_sync_does_not_clear_the_target_server(monkeypatch):
    monkeypatch.setattr("nyatify.settings.sync_guild_id", 111)
    bot = _bot_for_sync()
    keep = MagicMock()
    keep.id = 111
    other = MagicMock()
    other.id = 222
    leftover = object()
    bot.fetch_guilds = lambda **_kwargs: _guilds(keep, other)
    bot.tree.fetch_commands = AsyncMock(
        side_effect=lambda *, guild=None: [leftover] if guild is other else []
    )

    await bot._sync_slash_commands()
    await bot.close()

    synced_guilds = [call.kwargs.get("guild") for call in bot.tree.sync.await_args_list]
    assert synced_guilds[0].id == 111
    assert synced_guilds[1] is other
    bot.tree.clear_commands.assert_called_once_with(guild=other)
