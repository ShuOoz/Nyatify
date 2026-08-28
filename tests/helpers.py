from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import commands


def fake_member(name: str = "Nya", user_id: int = 1) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.display_avatar.url = "https://example.test/avatar.png"
    member.__str__.return_value = name
    return member


def fake_voice_channel(channel_id: int = 10, members: list | None = None) -> MagicMock:
    channel = MagicMock()
    channel.id = channel_id
    channel.members = members if members is not None else []
    channel.__str__.return_value = f"voice-{channel_id}"
    return channel


def fake_voice_state(channel=None) -> MagicMock:
    state = MagicMock(spec=discord.VoiceState)
    state.channel = channel
    return state


def fake_text_channel() -> MagicMock:
    channel = MagicMock()
    channel.send = AsyncMock()
    return channel


def fake_context() -> MagicMock:
    ctx = MagicMock(spec=commands.Context)
    ctx.send = AsyncMock()
    return ctx


def fake_bot(get_channel_result=None) -> MagicMock:
    bot = MagicMock(spec=commands.Bot)
    bot.get_channel.return_value = get_channel_result
    bot.extensions = {}
    bot.load_extension = AsyncMock()
    bot.unload_extension = AsyncMock()
    bot.reload_extension = AsyncMock()
    return bot
