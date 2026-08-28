from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import commands


class FakeClock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def fake_guild(guild_id: int = 1) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.get_channel.return_value = None
    guild.system_channel = None
    guild.text_channels = []
    guild.me = None
    guild.voice_channels = []
    guild.stage_channels = []
    guild.afk_channel = None
    guild.fetch_channel = AsyncMock(
        side_effect=discord.NotFound(MagicMock(), {"message": "Unknown Channel"})
    )
    return guild


def fake_member(name: str = "Nya", user_id: int = 1, *, bot: bool = False) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.display_avatar.url = "https://example.test/avatar.png"
    member.bot = bot
    member.__str__.return_value = name
    return member


def fake_voice_channel(
    channel_id: int = 10,
    members: list | None = None,
    guild: MagicMock | None = None,
) -> MagicMock:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = channel_id
    channel.members = members if members is not None else []
    channel.guild = fake_guild() if guild is None else guild
    channel.mention = f"<#{channel_id}>"
    channel.__str__.return_value = f"voice-{channel_id}"
    return channel


def fake_voice_state(channel=None) -> MagicMock:
    state = MagicMock(spec=discord.VoiceState)
    state.channel = channel
    return state


def fake_text_channel(
    channel_id: int = 99,
    guild: MagicMock | None = None,
) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.guild = fake_guild() if guild is None else guild
    channel.mention = f"<#{channel_id}>"
    posted = MagicMock()
    posted.id = channel_id * 1000
    posted.channel = channel
    posted.edit = AsyncMock()
    posted.delete = AsyncMock()
    channel.send = AsyncMock(return_value=posted)
    channel.get_partial_message = MagicMock(return_value=posted)
    return channel


def fake_context(guild: MagicMock | None = None) -> MagicMock:
    posted = MagicMock()
    posted.delete = AsyncMock()
    ctx = MagicMock(spec=commands.Context)
    ctx.send = AsyncMock(return_value=posted)
    ctx.guild = guild
    ctx.invoked_subcommand = None
    ctx.interaction = None
    return ctx


def fake_bot(get_channel_result=None) -> MagicMock:
    bot = MagicMock(spec=commands.Bot)
    bot.get_channel.return_value = get_channel_result
    bot.guilds = []
    bot.is_ready.return_value = False
    bot.extensions = {}
    bot.load_extension = AsyncMock()
    bot.unload_extension = AsyncMock()
    bot.reload_extension = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    bot.fetch_channel = AsyncMock(return_value=get_channel_result)
    return bot
