"""Isolated tests for the admin function. No Discord login."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from announce import COMMAND_REPLY_SECONDS, notice_text, quote_text
from cogs.admin import HIDDEN_OWNER_COMMANDS, Admin
from guild import GuildConfig, register_announce_feature
from nyatify import Nyatify
from tests.helpers import fake_bot, fake_context, fake_guild, reply_text

NEWS = "news"


@pytest.fixture
def store(tmp_path):
    return GuildConfig(tmp_path / "guild.json")


@pytest.fixture(autouse=True)
def news_feature():
    register_announce_feature(NEWS)
    return NEWS


@pytest.fixture
def cog(store):
    return Admin(fake_bot(), guild_config=store)


async def test_admin_commands_and_errors(cog):
    await case_cog_commands_load_reload_and_guards(cog)
    await case_admin_errors_are_clear(cog)


async def case_cog_commands_load_reload_and_guards(cog):
    ctx = fake_context()
    await cog.ping.callback(cog, ctx)
    ctx.send.assert_awaited_once_with(
        quote_text(notice_text("pong", starter=False)),
        delete_after=COMMAND_REPLY_SECONDS,
    )
    await cog.list_cogs.callback(cog, ctx)
    assert ctx.send.await_args.kwargs["embed"].title == "Cogs"

    await cog.load_cog.callback(cog, ctx, "voice")
    cog.bot.load_extension.assert_awaited_with("cogs.voice_notify")
    assert "Loaded" in reply_text(ctx)
    cog.bot.intents = discord.Intents.none()
    await cog.load_cog.callback(cog, ctx, "voice")
    assert "voice_states" in reply_text(ctx) and "Need" in reply_text(ctx)

    await cog.reload_cog.callback(cog, ctx, "voice")
    cog.bot.reload_extension.assert_awaited_once_with("cogs.voice_notify")
    assert "Reloaded" in reply_text(ctx)
    await cog.reload_cog.callback(cog, ctx, "announce")
    assert "shared" in reply_text(ctx)
    await cog.reload_cog.callback(cog, ctx, "notice")
    cog.bot.reload_extension.assert_awaited_once()
    assert "shared" in reply_text(ctx)

    await cog.unload_cog.callback(cog, ctx, "admin")
    cog.bot.unload_extension.assert_not_awaited()
    assert "cannot be unloaded" in reply_text(ctx)


async def case_admin_errors_are_clear(cog):
    cases = (
        (commands.NotOwner(), "Only the bot owner can manage cogs."),
        (commands.NoPrivateMessage(), "Use this command in a server."),
        (
            commands.MissingPermissions(["manage_guild"]),
            "Need Manage Server to set announce channels.",
        ),
    )
    for error, expected in cases:
        ctx = fake_context(
            None if isinstance(error, commands.NoPrivateMessage) else fake_guild()
        )
        if isinstance(error, commands.NoPrivateMessage):
            with pytest.raises(commands.NoPrivateMessage):
                await cog.cog_check(ctx)
        await cog.cog_command_error(ctx, error)
        assert reply_text(ctx) == quote_text(notice_text(expected))


async def test_leaving_a_guild_drops_its_settings(cog, store):
    guild = fake_guild(7)
    store.set_announce_channel_id(7, 10)
    store.set_feature_announce_channel_id(7, NEWS, 55)
    await cog.on_guild_remove(guild)
    assert store.get_default_announce_channel_id(7) is None
    assert store.get_feature_override_id(7, NEWS) is None


def _news_group() -> app_commands.Group:
    news = app_commands.Group(name="news", description="Follow news accounts")

    @news.command(name="cookie", description="Write a cookie")
    async def news_cookie(_interaction: discord.Interaction) -> None:
        return None

    @news.command(name="check", description="Preview a follow")
    async def news_check(_interaction: discord.Interaction) -> None:
        return None

    @news.command(name="show", description="Show follows")
    async def news_show(_interaction: discord.Interaction) -> None:
        return None

    return news


async def _no_guilds(**_kwargs):
    if False:
        yield None


@pytest.fixture
async def gated_admin(monkeypatch):
    monkeypatch.setattr("config.settings.admin_password", "gate-pass-9")
    monkeypatch.setattr("config.settings.sync_guild_id", 111)
    bot = Nyatify(enabled_cogs=[])
    bot.tree.sync = AsyncMock(return_value=[])
    bot.tree.fetch_commands = AsyncMock(return_value=[])
    bot.fetch_guilds = _no_guilds
    cog = Admin(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(_news_group())
    await bot._sync_slash_commands()
    try:
        yield bot, cog
    finally:
        await bot.close()


def _names(bot, *, guild: discord.Object | None = None) -> set[str]:
    return {command.name for command in bot.tree.get_commands(guild=guild)}


async def test_admin_password(monkeypatch, gated_admin, caplog):
    await case_admin_password_shows_and_hides_owner_commands(gated_admin, caplog)
    with monkeypatch.context() as mp:
        await case_admin_password_missing_or_sync_failure(mp)


async def case_admin_password_shows_and_hides_owner_commands(gated_admin, caplog):
    caplog.clear()
    bot, cog = gated_admin
    guild = discord.Object(id=111)
    caplog.set_level(logging.INFO)
    assert HIDDEN_OWNER_COMMANDS
    assert _names(bot).isdisjoint(HIDDEN_OWNER_COMMANDS)
    admin = bot.tree.get_command("admin")
    assert admin is not None
    password = next(param for param in admin.parameters if param.name == "password")
    assert password.required is False
    assert bot.tree.get_command("ping") is not None
    news = bot.tree.get_command("news")
    assert news is not None and news.get_command("cookie") is None
    assert news.get_command("check") is None
    assert news.get_command("show") is not None

    ctx = fake_context(fake_guild(111))
    await cog.admin_commands.callback(cog, ctx, "nope")
    assert reply_text(ctx) == notice_text("Wrong password.")
    assert bot.admin_commands_visible is False
    assert "gate-pass-9" not in reply_text(ctx)

    await cog.admin_commands.callback(cog, ctx, "gate-pass-9")
    assert bot.admin_commands_visible is True
    assert reply_text(ctx) == notice_text("Admin commands shown.")
    assert _names(bot) >= set(HIDDEN_OWNER_COMMANDS)
    assert _names(bot, guild=guild) >= set(HIDDEN_OWNER_COMMANDS)
    assert bot.tree.get_command("news").get_command("cookie") is not None
    assert bot.tree.get_command("news").get_command("check") is not None
    assert bot.tree.get_command("news", guild=guild).get_command("cookie") is not None
    assert bot.tree.get_command("news", guild=guild).get_command("check") is not None
    assert "gate-pass-9" not in caplog.text
    assert "gate-pass-9" not in reply_text(ctx)

    await cog.admin_commands.callback(cog, ctx, "gate-pass-9")
    assert bot.admin_commands_visible is False
    assert reply_text(ctx) == notice_text("Admin commands hidden.")
    assert _names(bot).isdisjoint(HIDDEN_OWNER_COMMANDS)
    assert _names(bot, guild=guild).isdisjoint(HIDDEN_OWNER_COMMANDS)
    assert bot.tree.get_command("news").get_command("cookie") is None
    assert bot.tree.get_command("news").get_command("check") is None
    assert bot.tree.get_command("news", guild=guild).get_command("show") is not None
    assert bot.tree.sync.await_args.kwargs["guild"].id == 111

    await cog.admin_commands.callback(cog, ctx, None)
    assert bot.admin_commands_visible is False
    assert reply_text(ctx) == notice_text("Enter the password.")
    assert bot.tree.get_command("cogs") is None

    await cog.admin_commands.callback(cog, ctx, "gate-pass-9")
    assert bot.admin_commands_visible is True
    await cog.admin_commands.callback(cog, ctx, "   ")
    assert bot.admin_commands_visible is False
    assert reply_text(ctx) == notice_text("Admin commands hidden.")
    assert _names(bot).isdisjoint(HIDDEN_OWNER_COMMANDS)
    assert bot.tree.get_command("news").get_command("check") is None


async def case_admin_password_missing_or_sync_failure(monkeypatch):
    monkeypatch.setattr("config.settings.admin_password", None)
    monkeypatch.setattr("config.settings.sync_guild_id", None)
    bot = Nyatify(enabled_cogs=[])
    cog = Admin(bot)
    await bot.add_cog(cog)
    ctx = fake_context(fake_guild(1))
    await cog.admin_commands.callback(cog, ctx, "gate-pass-9")
    assert reply_text(ctx) == notice_text("Set ADMIN_PASSWORD in config.txt.")
    assert bot.admin_commands_visible is False

    monkeypatch.setattr("config.settings.admin_password", "gate-pass-9")
    bot.tree.sync = AsyncMock(return_value=[])
    bot.tree.fetch_commands = AsyncMock(return_value=[])
    bot.fetch_guilds = _no_guilds
    try:
        await cog.admin_commands.callback(cog, ctx, "gate-pass-9")
        assert reply_text(ctx) == notice_text("Admin commands shown.", "May take an hour")
        assert bot.admin_commands_visible is True

        bot.tree.sync = AsyncMock(side_effect=RuntimeError("down"))
        await cog.admin_commands.callback(cog, ctx, "gate-pass-9")
        assert bot.admin_commands_visible is True
        assert reply_text(ctx) == notice_text("Could not update commands.")
        assert bot.tree.get_command("cogs") is not None
        assert "gate-pass-9" not in reply_text(ctx)
    finally:
        await bot.close()
