"""Isolated tests for the admin function. No Discord login."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from announce import COMMAND_REPLY_SECONDS, HINT_TTL_SECONDS, notice_text, quote_text
from cogs.admin import (
    ANNOUNCE_SHOW_SECONDS,
    HIDDEN_OWNER_COMMANDS,
    Admin,
    _announce_feature_autocomplete,
    ordered_announce_features,
)
from guild import GuildConfig, register_announce_feature, unregister_announce_feature
from news.follows import NEWS_FEATURES
from nyatify import Nyatify
from tests.helpers import (
    embed_fields,
    fake_bot,
    fake_context,
    fake_guild,
    fake_text_channel,
    reply_text,
)

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


async def test_announce_settings(cog, store):
    await case_announce_show_and_feature(cog, store)
    await case_guild_remove_and_deleted_feature_channel(cog, store)


async def case_announce_show_and_feature(cog, store):
    guild = fake_guild(7)
    default = fake_text_channel(10, guild=guild)
    override = fake_text_channel(55, guild=guild)
    extra = fake_text_channel(66, guild=guild)
    ctx = fake_context(guild)

    await cog.announce_show.callback(cog, ctx)
    fields = embed_fields(ctx.send.await_args.kwargs["embed"])
    assert fields["Default"] == "not set"
    assert fields["News"] == "using default"
    assert ctx.send.await_args.kwargs["delete_after"] == ANNOUNCE_SHOW_SECONDS

    await cog.announce_feature.callback(cog, ctx, default)
    assert store.get_default_announce_channel_id(7) == default.id
    assert "posts go to" in reply_text(ctx)
    guild.get_channel.return_value = default
    await cog.announce_show.callback(cog, ctx)
    assert embed_fields(ctx.send.await_args.kwargs["embed"])["Default"] == default.mention

    await cog.announce_feature.callback(cog, ctx, override, NEWS)
    assert store.get_default_announce_channel_id(7) == default.id
    assert store.get_feature_override_id(7, NEWS) == override.id
    assert override.mention in reply_text(ctx)
    guild.get_channel.side_effect = lambda channel_id: {
        default.id: default,
        override.id: override,
        extra.id: extra,
    }.get(channel_id)
    await cog.announce_show.callback(cog, ctx)
    shown = embed_fields(ctx.send.await_args.kwargs["embed"])
    assert shown["Default"] == default.mention
    assert shown["News"] == override.mention

    await cog.announce_feature.callback(cog, ctx, default, NEWS)
    assert store.get_feature_overrides(7) == {}
    assert "default" in reply_text(ctx)

    unregister_announce_feature(NEWS)
    register_announce_feature("voice")
    for name in NEWS_FEATURES:
        register_announce_feature(name)
    try:
        assert ordered_announce_features() == (
            "voice",
            "game",
            "ticket",
            "match",
            "other",
        )
        choices = await _announce_feature_autocomplete(MagicMock(), "")
        assert [choice.value for choice in choices] == list(ordered_announce_features())
        store.set_announce_channel_id(7, default.id)
        store.set_feature_announce_channel_id(7, "other", extra.id)
        await cog.announce_show.callback(cog, ctx)
        names = [field.name for field in ctx.send.await_args.kwargs["embed"].fields]
        by_name = embed_fields(ctx.send.await_args.kwargs["embed"])
        assert names[0] == "Default"
        assert names[1] == "Voice"
        assert names[-1] == "Other"
        assert by_name["Voice"] == "using default"
        assert by_name["Other"] == extra.mention
    finally:
        unregister_announce_feature("voice")
        for name in NEWS_FEATURES:
            unregister_announce_feature(name)
        register_announce_feature(NEWS)


async def case_guild_remove_and_deleted_feature_channel(cog, store):
    guild = fake_guild(7)
    default = fake_text_channel(10, guild=guild)
    override = fake_text_channel(55, guild=guild)
    override.guild = guild
    store.set_announce_channel_id(7, default.id)
    store.set_feature_announce_channel_id(7, NEWS, override.id)
    guild.get_channel.side_effect = lambda channel_id: {default.id: default}.get(channel_id)
    await cog.on_guild_channel_delete(override)
    assert store.get_feature_overrides(7) == {}
    assert store.get_feature_override_id(7, NEWS) is None
    assert store.get_default_announce_channel_id(7) == default.id
    assert NEWS in default.send.await_args.args[0]
    assert default.send.await_args.kwargs["delete_after"] == HINT_TTL_SECONDS
    await cog.on_guild_remove(guild)
    assert store.get_default_announce_channel_id(7) is None


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
