"""Notify the server when someone starts an empty voice channel."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from config import settings

log = logging.getLogger(__name__)


class VoiceNotify(commands.Cog):
    """Signals when a voice channel goes from empty to occupied."""

    def __init__(
        self,
        bot: commands.Bot,
        channel_id: int | None = None,
    ) -> None:
        self.bot = bot
        self.channel_id = (
            settings.voice_notify_channel_id if channel_id is None else channel_id
        )
        self.active_channels: set[int] = set()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if after.channel is not None and before.channel is None:
            channel = after.channel
            log.debug(
                "%s joined %s (%s members)",
                member,
                channel,
                len(channel.members),
            )
            if len(channel.members) == 1 and channel.id not in self.active_channels:
                self.active_channels.add(channel.id)
                await self._announce_channel_start(member, channel)
        elif before.channel is not None and after.channel is None:
            log.debug("%s left %s", member, before.channel)
            if len(before.channel.members) == 0:
                self.active_channels.discard(before.channel.id)
                log.debug("Voice channel %s is empty again", before.channel.id)

    async def _announce_channel_start(
        self,
        member: discord.Member,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        if self.channel_id is None:
            log.warning("VOICE_NOTIFY_CHANNEL_ID is not set; skipping announcement.")
            return

        notification_channel = self.bot.get_channel(self.channel_id)
        if notification_channel is None:
            log.warning("Voice notify channel %s was not found.", self.channel_id)
            return

        embed = discord.Embed(
            title="Hello Every Nya ฅ^•ﻌ•^ฅ",
            color=discord.Color.from_str("#FFB6C1"),
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member.mention}", inline=True)
        embed.add_field(
            name="Channel Occupancy",
            value=f"{len(channel.members)} member(s)",
            inline=True,
        )
        await notification_channel.send(
            content="@everyone Someone started the voice channel",
            embed=embed,
        )
        log.info("Announced voice start in %s by %s", channel, member)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceNotify(bot))
