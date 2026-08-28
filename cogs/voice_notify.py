"""Notify the server when someone starts an empty voice channel."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable
from pathlib import Path

import discord
from discord.ext import commands

from announce import (
    ACCENT_COLOR,
    DEFAULT_NOTICES_PATH,
    NotifyCog,
    TemporaryNotice,
    send_announcement,
)
from config import write_text_atomic
from guild_config import (
    GuildConfig,
    guild_config as default_guild_config,
    parse_snowflake,
)

log = logging.getLogger(__name__)

FEATURE_VOICE = "voice"
REQUIRED_INTENTS = ("voice_states",)

# After the last human leaves, ignore leave+rejoin for this long (disconnects / spam).
# The start card is deleted as soon as that person leaves voice (or goes AFK).
RECONNECT_GRACE_SECONDS = 45.0
# How long a voice-start message stays in the announce channel.
# Expiry only removes the card. It does not allow another ping while they stay in voice.
NOTICE_TTL_SECONDS = 15 * 60
# Inbox ping. Posted as its own message so Discord does not flatten the card.
VOICE_NOTICE_CONTENT = "@everyone !!! OH MY GAH !!!"
VOICE_NOTICE_TITLE = "Hello Every Nya ฅ^•ﻌ•^ฅ"
# Embed image URLs only (no local files). Append to this tuple to extend the shuffle.
# GIFs must be 400×180 (width × height) so the card layout stays consistent.
VOICE_NOTICE_GIF_URLS: tuple[str, ...] = (
    "https://media.tenor.com/4KkjHEiAqhkAAAAC/azumanga.gif",
    "https://media.tenor.com/VrGpLKMHwjoAAAAC/azumanga.gif",
    "https://media.tenor.com/VMaJP2HXhOkAAAAC/azumanga.gif",
)

_VoiceRoom = discord.VoiceChannel | discord.StageChannel
_Presence = dict[tuple[int, int], tuple[discord.Member, _VoiceRoom]]


def _has_human(
    channel: _VoiceRoom,
    *,
    excluding: discord.Member | None = None,
) -> bool:
    exclude_id = None if excluding is None else excluding.id
    return any(
        not occupant.bot and occupant.id != exclude_id
        for occupant in channel.members
    )


def _is_afk_channel(channel: _VoiceRoom) -> bool:
    afk = getattr(channel.guild, "afk_channel", None)
    return afk is not None and channel.id == afk.id


def _voice_channels(guild: discord.Guild) -> tuple[_VoiceRoom, ...]:
    return (*guild.voice_channels, *guild.stage_channels)


class VoiceNotify(NotifyCog):
    """Signals when a voice channel goes from empty to occupied."""

    feature = FEATURE_VOICE
    feature_aliases = ("voice_notify",)

    def __init__(
        self,
        bot: commands.Bot,
        guild_config: GuildConfig | None = None,
        *,
        grace_seconds: float = RECONNECT_GRACE_SECONDS,
        ttl_seconds: float = NOTICE_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
        notices_path: Path | None = DEFAULT_NOTICES_PATH,
    ) -> None:
        super().__init__(bot, notices_path=notices_path, clock=clock)
        self.guild_config = default_guild_config if guild_config is None else guild_config
        # Rooms we already treated as a session (people inside, or last human left recently).
        self.active_channels: set[int] = set()
        # People who already got a start notice this voice stint (guild_id, member_id).
        # Kept after the 15-minute card is deleted, cleared when they leave or go AFK.
        self._announced: set[tuple[int, int]] = set()
        # When the last human left. After grace, the room is dropped from active_channels.
        self._emptied_at: dict[int, float] = {}
        self._join_locks: dict[int, asyncio.Lock] = {}
        self._grace_seconds = grace_seconds
        self._ttl_seconds = ttl_seconds
        self._now = self.notices._now
        self._gif_bag: list[str] = []
        self._last_gif: str | None = None

    async def cog_unload(self) -> None:
        self._save_sessions()
        await super().cog_unload()

    async def _on_notify_ready(self) -> None:
        if not self._announced and not self._emptied_at:
            self._load_sessions()
        present = self._sync_active_channels()
        await super()._on_notify_ready()
        await self._reconcile_voice_notices(present)
        self._save_sessions()

    def _sync_active_channels(self) -> _Presence:
        """Refresh sessions from occupancy. Return who is in a non-AFK voice room."""
        self._expire_grace()
        occupied: set[int] = set()
        present: _Presence = {}
        for guild in self.bot.guilds:
            for channel in _voice_channels(guild):
                if _is_afk_channel(channel):
                    continue
                humans = [
                    occupant for occupant in channel.members if not occupant.bot
                ]
                if not humans:
                    continue
                occupied.add(channel.id)
                for occupant in humans:
                    present[(guild.id, occupant.id)] = (occupant, channel)
        for channel_id in occupied:
            self._emptied_at.pop(channel_id, None)
        self.active_channels = occupied | set(self._emptied_at)
        for channel_id in set(self._join_locks) - self.active_channels:
            self._join_locks.pop(channel_id, None)
        return present

    async def _reconcile_voice_notices(self, present: _Presence) -> None:
        """Keep announced-this-stint only for people still in voice.

        Cards for people who left while we were down are removed so their next
        start can ping. Live cards follow them if they switched rooms.
        """
        self._announced.intersection_update(present)
        stale: list[TemporaryNotice] = []
        for notice in list(self.notices):
            if notice.feature != FEATURE_VOICE:
                continue
            try:
                key = (notice.guild_id, int(notice.subject))
            except (TypeError, ValueError):
                continue
            location = present.get(key)
            if location is None:
                stale.append(notice)
                continue
            self._announced.add(key)
            if notice.expires_at > self._now():
                member, channel = location
                await self._retarget_live_notice(member, channel, notice)
        for notice in stale:
            await self.notices.delete_now(notice)
            self._forget_notice(notice.guild_id, int(notice.subject))
        await self.notices.delete_expired()

    def _expire_grace(self) -> None:
        now = self._now()
        for channel_id, emptied in list(self._emptied_at.items()):
            if now - emptied >= self._grace_seconds:
                self._drop_channel(channel_id)

    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        return self._join_locks.setdefault(channel_id, asyncio.Lock())

    def _drop_channel(self, channel_id: int) -> None:
        self.active_channels.discard(channel_id)
        self._emptied_at.pop(channel_id, None)
        self._join_locks.pop(channel_id, None)

    def _drop_guild(self, guild: discord.Guild) -> None:
        for channel in _voice_channels(guild):
            self._drop_channel(channel.id)
        self._announced = {
            key for key in self._announced if key[0] != guild.id
        }

    def _sessions_path(self) -> Path | None:
        path = self.notices.path
        if path is None:
            return None
        return path.with_name("voice_sessions.json")

    def _load_sessions(self) -> None:
        path = self._sessions_path()
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("Could not read %s; starting with empty voice sessions.", path)
            return
        if not isinstance(raw, dict):
            return
        announced = raw.get("announced")
        if isinstance(announced, list):
            restored: set[tuple[int, int]] = set()
            for item in announced:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                guild_id = parse_snowflake(item[0])
                member_id = parse_snowflake(item[1])
                if guild_id is not None and member_id is not None:
                    restored.add((guild_id, member_id))
            self._announced = restored
        emptied = raw.get("emptied_at")
        if isinstance(emptied, list):
            restored_empty: dict[int, float] = {}
            for item in emptied:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                channel_id = parse_snowflake(item[0])
                try:
                    emptied_at = float(item[1])
                except (TypeError, ValueError):
                    continue
                if channel_id is not None:
                    restored_empty[channel_id] = emptied_at
            self._emptied_at = restored_empty

    def _save_sessions(self) -> None:
        path = self._sessions_path()
        if path is None:
            return
        payload = {
            "announced": sorted([guild_id, member_id] for guild_id, member_id in self._announced),
            "emptied_at": sorted(
                [channel_id, emptied_at]
                for channel_id, emptied_at in self._emptied_at.items()
            ),
        }
        try:
            write_text_atomic(
                path,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
            )
        except OSError:
            log.exception("Could not save voice sessions to %s", path)

    def _forget_notice(self, guild_id: int, member_id: int) -> None:
        self.notices.forget(FEATURE_VOICE, guild_id, str(member_id))

    async def _vanish_member_notice(
        self,
        member: discord.Member,
        guild: discord.Guild,
    ) -> None:
        self._announced.discard((guild.id, member.id))
        notice = self.notices.get(FEATURE_VOICE, guild.id, str(member.id))
        if notice is None:
            return
        await self.notices.delete_now(notice)
        # Drop even if Discord could not delete, so a new session can ping.
        self._forget_notice(guild.id, member.id)

    def _on_leave_channel(
        self,
        member: discord.Member,
        channel: _VoiceRoom,
    ) -> None:
        if _is_afk_channel(channel) or channel.id not in self.active_channels:
            return
        if not _has_human(channel, excluding=member):
            self._emptied_at[channel.id] = self._now()

    async def _on_join_channel(
        self,
        member: discord.Member,
        channel: _VoiceRoom,
    ) -> None:
        if member.bot:
            return
        async with self._lock_for(channel.id):
            guild_id = channel.guild.id
            member_key = (guild_id, member.id)
            others_present = _has_human(channel, excluding=member)
            known_session = channel.id in self.active_channels
            self._emptied_at.pop(channel.id, None)
            self.active_channels.add(channel.id)

            live = self.notices.live(FEATURE_VOICE, guild_id, str(member.id))
            if live is not None:
                if await self._retarget_live_notice(member, channel, live):
                    self._announced.add(member_key)
                    return
            elif member_key in self._announced:
                return
            if others_present or known_session:
                return

            if not await self._announce_channel_start(member, channel):
                self.active_channels.discard(channel.id)
                return
            self._announced.add(member_key)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        before_channel = before.channel
        after_channel = after.channel
        before_id = None if before_channel is None else before_channel.id
        after_id = None if after_channel is None else after_channel.id
        if before_id == after_id:
            return
        try:
            self._expire_grace()
            if before_channel is not None:
                self._on_leave_channel(member, before_channel)
            if after_channel is None or _is_afk_channel(after_channel):
                await self._vanish_member_notice(
                    member, (before_channel or after_channel).guild
                )
                return
            await self._on_join_channel(member, after_channel)
        finally:
            self._save_sessions()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self._drop_guild(guild)
        self._save_sessions()
        await super().on_guild_remove(guild)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            self._drop_channel(channel.id)
            self._save_sessions()
        await super().on_guild_channel_delete(channel)

    def _pick_gif(self) -> str:
        if not self._gif_bag:
            self._gif_bag = list(VOICE_NOTICE_GIF_URLS)
            random.shuffle(self._gif_bag)
            if (
                self._last_gif
                and len(self._gif_bag) > 1
                and self._gif_bag[-1] == self._last_gif
            ):
                self._gif_bag[0], self._gif_bag[-1] = (
                    self._gif_bag[-1],
                    self._gif_bag[0],
                )
        picked = self._gif_bag.pop()
        self._last_gif = picked
        return picked

    def _gif_for_notice(self, extra: dict) -> str:
        raw = extra.get("gif")
        if isinstance(raw, str) and raw in VOICE_NOTICE_GIF_URLS:
            return raw
        return self._pick_gif()

    def _voice_embed(
        self,
        member: discord.Member,
        channel: _VoiceRoom,
        started_at: float,
        image_url: str,
    ) -> discord.Embed:
        """Greeting title, Channel/Start on one labeled line, GIF below."""
        embed = discord.Embed(
            title=VOICE_NOTICE_TITLE,
            description=(
                f"**Channel** | {channel.mention} · **Start** | "
                f"<t:{int(started_at)}:R>"
            ),
            color=ACCENT_COLOR,
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.set_image(url=image_url)
        return embed

    async def _retarget_live_notice(
        self,
        member: discord.Member,
        channel: _VoiceRoom,
        notice: TemporaryNotice,
    ) -> bool:
        guild_id = channel.guild.id
        if notice.extra_id("voice_channel_id") == channel.id:
            return True
        try:
            message = await self.notices.resolve_message(notice)
        except discord.HTTPException:
            log.warning("Could not fetch voice notice for %s", member, exc_info=True)
            return False
        if message is None:
            await self.notices.delete_now(notice)
            self._forget_notice(guild_id, member.id)
            return False
        try:
            started_at = float(notice.extra.get("started_at"))
        except (TypeError, ValueError):
            started_at = notice.expires_at - self._ttl_seconds
        gif = self._gif_for_notice(notice.extra)
        embed = self._voice_embed(member, channel, started_at, gif)
        try:
            await message.edit(embed=embed)
        except discord.NotFound:
            # Card is already gone; still drop the companion ping.
            await self.notices.delete_now(notice)
            self._forget_notice(guild_id, member.id)
            return False
        except discord.HTTPException:
            log.warning("Could not update voice notice for %s", member, exc_info=True)
            return False
        notice.extra["voice_channel_id"] = channel.id
        notice.extra["gif"] = gif
        if notice.message is None:
            notice.message = message
        self.notices.save()
        log.info("Updated voice start in %s by %s", channel, member)
        return True

    async def _announce_channel_start(
        self,
        member: discord.Member,
        channel: _VoiceRoom,
    ) -> bool:
        started_at = int(self._now())
        gif = self._pick_gif()
        embed = self._voice_embed(member, channel, started_at, gif)
        message = await send_announcement(
            channel.guild,
            self.guild_config,
            FEATURE_VOICE,
            content=VOICE_NOTICE_CONTENT,
            embed=embed,
            delete_after=self._ttl_seconds,
            notice_key=member.id,
            extra={
                "voice_channel_id": channel.id,
                "started_at": started_at,
                "gif": gif,
            },
            notices=self.notices,
        )
        if not message:
            return False
        log.info("Announced voice start in %s by %s", channel, member)
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceNotify(bot))
