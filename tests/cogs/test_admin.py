"""Isolated tests for the admin function. No Discord login."""

from __future__ import annotations

import pytest
from discord.ext import commands

from cogs.admin import Admin, _extension_name
from tests.helpers import fake_bot, fake_context


@pytest.fixture
def cog():
    return Admin(fake_bot())


@pytest.mark.asyncio
async def test_ping_replies(cog):
    ctx = fake_context()
    await cog.ping.callback(cog, ctx)
    ctx.send.assert_awaited_once_with("pong")


@pytest.mark.asyncio
async def test_load_uses_cogs_package_prefix(cog):
    ctx = fake_context()
    await cog.load_cog.callback(cog, ctx, "voice_notify")
    cog.bot.load_extension.assert_awaited_once_with("cogs.voice_notify")
    ctx.send.assert_awaited()


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
    ctx.send.assert_awaited_once_with("Only the bot owner can manage functions.")


def test_extension_name_accepts_short_or_full_form():
    assert _extension_name("voice_notify") == "cogs.voice_notify"
    assert _extension_name("cogs.voice_notify") == "cogs.voice_notify"
