"""Isolated tests for the admin function. No Discord login."""

from __future__ import annotations

import pytest
from discord.ext import commands

from announce import HINT_TTL_SECONDS
from cogs.admin import (
    ANNOUNCE_SHOW_SECONDS,
    Admin,
    COMMAND_REPLY_SECONDS,
    _EXAMPLE_COG,
)
from guild_config import GuildConfig, register_announce_feature
from tests.helpers import (
    fake_bot,
    fake_context,
    fake_guild,
    fake_text_channel,
    fake_voice_channel,
)

NEWS = "news"


@pytest.fixture
def store(tmp_path):
    return GuildConfig(tmp_path / "guild_config.json")


@pytest.fixture(autouse=True)
def news_feature():
    register_announce_feature(NEWS)
    return NEWS


@pytest.fixture
def cog(store):
    return Admin(fake_bot(), guild_config=store)


@pytest.mark.asyncio
async def test_ping_replies_and_vanishes_after_10_seconds(cog):
    ctx = fake_context()
    await cog.ping.callback(cog, ctx)
    ctx.send.assert_awaited_once_with("pong", delete_after=COMMAND_REPLY_SECONDS)


@pytest.mark.asyncio
async def test_list_cogs_shows_loaded_and_available(cog):
    ctx = fake_context()
    await cog.list_cogs.callback(cog, ctx)
    embed = ctx.send.await_args.kwargs["embed"]
    assert embed.title == "Cogs"


@pytest.mark.asyncio
async def test_load_uses_cogs_package_prefix(cog):
    ctx = fake_context()
    await cog.load_cog.callback(cog, ctx, "voice")
    cog.bot.load_extension.assert_awaited_once_with("cogs.voice_notify")
    ctx.send.assert_awaited()
    assert "Loaded" in ctx.send.await_args.args[0]
    assert "voice" in ctx.send.await_args.args[0]


@pytest.mark.asyncio
async def test_admin_cannot_be_unloaded(cog):
    ctx = fake_context()
    await cog.unload_cog.callback(cog, ctx, "admin")
    cog.bot.unload_extension.assert_not_awaited()
    ctx.send.assert_awaited_once()
    assert "cannot be unloaded" in ctx.send.await_args.args[0]


@pytest.mark.asyncio
async def test_not_owner_gets_a_clear_message(cog):
    ctx = fake_context()
    await cog.cog_command_error(ctx, commands.NotOwner())
    ctx.send.assert_awaited_once()
    assert ctx.send.await_args.args[0] == "Only the bot owner can manage cogs."


@pytest.mark.asyncio
async def test_announce_channel_sets_all_functions(cog, store):
    guild = fake_guild(7)
    channel = fake_text_channel(55, guild=guild)
    guild.get_channel.return_value = channel
    ctx = fake_context(guild)

    await cog.announce.callback(cog, ctx, f"<#{channel.id}>")

    assert store.get_announce_channel_id(7) == 55
    ctx.send.assert_awaited()
    assert channel.mention in ctx.send.await_args.args[0]


@pytest.mark.asyncio
async def test_announce_rejects_a_voice_channel(cog, store):
    guild = fake_guild(7)
    voice = fake_voice_channel(channel_id=55, guild=guild)
    guild.get_channel.return_value = voice
    ctx = fake_context(guild)

    await cog.announce.callback(cog, ctx, f"<#{voice.id}>")

    assert store.get_announce_channel_id(7) is None
    ctx.send.assert_awaited()
    assert "not found" in ctx.send.await_args.args[0]


@pytest.mark.asyncio
async def test_announce_function_sets_only_that_function(cog, store):
    guild = fake_guild(7)
    default = fake_text_channel(10, guild=guild)
    override = fake_text_channel(55, guild=guild)
    store.set_announce_channel_id(7, default.id)

    def get_channel(channel_id):
        return {default.id: default, override.id: override}.get(channel_id)

    guild.get_channel.side_effect = get_channel
    ctx = fake_context(guild)

    await cog.announce.callback(cog, ctx, NEWS, f"<#{override.id}>")

    assert store.get_announce_channel_id(7) == 10
    assert store.get_announce_channel_id(7, NEWS) == 55
    ctx.send.assert_awaited()
    assert override.mention in ctx.send.await_args.args[0]


@pytest.mark.asyncio
async def test_slash_announce_feature_sets_only_that_function(cog, store):
    guild = fake_guild(7)
    default = fake_text_channel(10, guild=guild)
    override = fake_text_channel(55, guild=guild)
    store.set_announce_channel_id(7, default.id)
    ctx = fake_context(guild)

    await cog.announce_feature.callback(cog, ctx, override, NEWS)

    assert store.get_announce_channel_id(7) == 10
    assert store.get_announce_channel_id(7, NEWS) == 55
    ctx.send.assert_awaited()
    assert override.mention in ctx.send.await_args.args[0]


@pytest.mark.asyncio
async def test_announce_show(cog, store):
    guild = fake_guild(7)
    channel = fake_text_channel(55, guild=guild)
    guild.get_channel.return_value = channel
    store.set_announce_channel_id(7, 55)
    ctx = fake_context(guild)

    await cog.announce.callback(cog, ctx)
    ctx.send.assert_awaited()
    embed = ctx.send.await_args.kwargs["embed"]
    fields = {field.name: field.value for field in embed.fields}
    assert fields["All cogs (default)"] == channel.mention
    assert fields[NEWS] == "using default"


@pytest.mark.asyncio
async def test_missing_permissions_gets_a_clear_message(cog):
    ctx = fake_context()
    await cog.cog_command_error(ctx, commands.MissingPermissions(["manage_guild"]))
    ctx.send.assert_awaited_once()
    assert (
        ctx.send.await_args.args[0]
        == "You need Manage Server to change the announcement channel."
    )


@pytest.mark.asyncio
async def test_leaving_a_guild_drops_stored_settings(cog, store):
    store.set_announce_channel_id(7, 55)
    guild = fake_guild(7)
    await cog.on_guild_remove(guild)
    assert store.get_announce_channel_id(7) is None


@pytest.mark.asyncio
async def test_slash_announce_feature_without_function_sets_all(cog, store):
    guild = fake_guild(7)
    channel = fake_text_channel(55, guild=guild)
    ctx = fake_context(guild)

    await cog.announce_feature.callback(cog, ctx, channel)

    assert store.get_announce_channel_id(7) == 55
    ctx.send.assert_awaited()
    assert channel.mention in ctx.send.await_args.args[0]


@pytest.mark.asyncio
async def test_slash_announce_show_when_empty(cog):
    guild = fake_guild(7)
    ctx = fake_context(guild)

    await cog.announce_show.callback(cog, ctx)

    ctx.send.assert_awaited()
    embed = ctx.send.await_args.kwargs["embed"]
    assert embed.title == "Announcement channels"
    fields = {field.name: field.value for field in embed.fields}
    assert fields["All cogs (default)"] == "not set"
    assert fields[NEWS] == "not set"
    assert ctx.send.await_args.kwargs["delete_after"] == ANNOUNCE_SHOW_SECONDS


@pytest.mark.asyncio
async def test_announce_show_lists_a_function_override(cog, store):
    guild = fake_guild(7)
    default = fake_text_channel(10, guild=guild)
    override = fake_text_channel(55, guild=guild)

    def get_channel(channel_id):
        return {default.id: default, override.id: override}.get(channel_id)

    guild.get_channel.side_effect = get_channel
    store.set_announce_channel_id(7, default.id)
    store.set_feature_announce_channel_id(7, NEWS, override.id)
    ctx = fake_context(guild)

    await cog.announce.callback(cog, ctx)

    embed = ctx.send.await_args.kwargs["embed"]
    fields = {field.name: field.value for field in embed.fields}
    assert fields["All cogs (default)"] == default.mention
    assert fields[NEWS] == override.mention


@pytest.mark.asyncio
async def test_announce_function_to_default_clears_the_override(cog, store):
    guild = fake_guild(7)
    default = fake_text_channel(10, guild=guild)
    override = fake_text_channel(55, guild=guild)
    store.set_announce_channel_id(7, default.id)
    store.set_feature_announce_channel_id(7, NEWS, override.id)
    ctx = fake_context(guild)

    await cog.announce_feature.callback(cog, ctx, default, NEWS)

    assert store.get_feature_overrides(7) == {}
    assert store.get_announce_channel_id(7, NEWS) == default.id
    ctx.send.assert_awaited()
    assert "default" in ctx.send.await_args.args[0]


@pytest.mark.asyncio
async def test_deleted_function_channel_is_assigned_back_to_default(cog, store):
    guild = fake_guild(7)
    default = fake_text_channel(10, guild=guild)
    override = fake_text_channel(55, guild=guild)
    override.guild = guild
    store.set_announce_channel_id(7, default.id)
    store.set_feature_announce_channel_id(7, NEWS, override.id)
    guild.get_channel.side_effect = lambda channel_id: {
        default.id: default,
    }.get(channel_id)

    await cog.on_guild_channel_delete(override)

    assert store.get_feature_overrides(7) == {}
    assert store.get_announce_channel_id(7, NEWS) == default.id
    default.send.assert_awaited_once()
    assert NEWS in default.send.await_args.args[0]
    assert default.send.await_args.kwargs["delete_after"] == HINT_TTL_SECONDS


@pytest.mark.asyncio
async def test_reload_uses_cogs_package_prefix(cog):
    ctx = fake_context()
    await cog.reload_cog.callback(cog, ctx, "voice")

    cog.bot.reload_extension.assert_awaited_once_with("cogs.voice_notify")
    ctx.send.assert_awaited()
    assert "Reloaded" in ctx.send.await_args.args[0]
    assert "voice" in ctx.send.await_args.args[0]


@pytest.mark.asyncio
async def test_reload_announce_explains_it_is_not_a_cog(cog):
    ctx = fake_context()
    await cog.reload_cog.callback(cog, ctx, "announce")
    cog.bot.reload_extension.assert_not_awaited()
    assert "shared" in ctx.send.await_args.args[0]
    assert "Restart" in ctx.send.await_args.args[0]


@pytest.mark.asyncio
async def test_reload_without_a_name_shows_reload_usage(cog):
    ctx = fake_context()
    ctx.command = cog.reload_cog
    await cog.cog_command_error(
        ctx,
        commands.MissingRequiredArgument(cog.reload_cog.params["cog"]),
    )
    ctx.send.assert_awaited()
    assert f"!reload {_EXAMPLE_COG}" in ctx.send.await_args.args[0]
