"""Shared announcement delivery and slash-command replies.

Cogs subclass NotifyCog and post with send_announcement. This module picks the
channel and sends. notice.py remembers the message and deletes it later.
Commands answer the user with respond.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from news.follows import ordered_announce_features
from guild import (
    GuildConfig,
    canonical_announce_feature,
    parse_snowflake,
    register_announce_feature,
    unregister_announce_feature,
)
from notice import (
    DEFAULT_NOTICES_PATH,
    TemporaryNotices,
    notices_for,
)

log = logging.getLogger(__name__)

COMMAND_REPLY_SECONDS = 10.0
ACCENT_COLOR = discord.Color.from_str("#FFB6C1")

# In-server replies: bold `>` starter, then two spaces. quote_text adds the Discord quote.
NOTICE_PREFIX = "**>**  "
NOTICE_LINE_CHARS = 50
_VISIBLE_PREFIX_LEN = 3  # bold `>` + two spaces

CommandTarget = commands.Context | discord.Interaction


def _notice_body(text: str) -> str:
    """Strip one leading quote or old `[i]` mark. Longest match wins."""
    body = text.strip()
    for mark in (
        "> **>**  ", "> **>** ", NOTICE_PREFIX, "**>** ", "> > ", "> ",
        "**[i]** ", "**[i]**", "[i] ", "[i]",
    ):
        if body.startswith(mark):
            return body[len(mark) :].lstrip()
    return body


def notice_text(
    line: str, extra: str | None = None, *, starter: bool = True, brief: bool = False,
) -> str:
    """In-server copy. Default is `**>**  body`; ping uses starter=False.

    quote_text wraps the result. Trailing full stops are dropped.
    `brief` keeps an announcement on one short line. Command replies stay whole.
    """
    inner = _notice_body(line)
    if extra:
        flat = " ".join(_notice_body(extra.replace("\r\n", "\n").replace("\n", " ")).split())
        if flat:
            inner = f"{inner.rstrip()} {flat}"
    inner = inner.rstrip().removesuffix(".")
    limit = max(1, NOTICE_LINE_CHARS - (_VISIBLE_PREFIX_LEN if starter else 0))
    if brief and len(inner) > limit:
        inner = inner[: limit - 1] + "…"
    return f"{NOTICE_PREFIX}{inner}" if starter else inner


def quote_text(text: str) -> str:
    """Discord quote for bot text that is visible in the server. Safe to call twice."""
    body = text.replace("\r\n", "\n").rstrip("\n")
    if not body or body.lstrip().startswith(">>>"):
        return body
    return "\n".join(
        line if line.startswith("> ") or line == ">" else f"> {line}"
        for line in body.split("\n")
    )


class OwnerCheckFailure(app_commands.CheckFailure):
    """Slash command used by someone who is not the bot owner."""


def owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        is_owner = getattr(interaction.client, "is_owner", None)
        if not callable(is_owner) or not await is_owner(interaction.user):
            raise OwnerCheckFailure()
        return True

    return app_commands.check(predicate)


async def defer_response(target: CommandTarget, *, ephemeral: bool = False) -> None:
    interaction = target if isinstance(target, discord.Interaction) else getattr(target, "interaction", None)
    if isinstance(interaction, discord.Interaction) and not interaction.response.is_done():
        await interaction.response.defer(ephemeral=ephemeral)


async def respond(
    target: CommandTarget,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    ephemeral: bool = False,
    delete_after: float | None = COMMAND_REPLY_SECONDS,
    file: discord.File | None = None,
    files: list[discord.File] | None = None,
) -> discord.Message | None:
    kwargs = _attached([file, *(files or [])] if file is not None else files)
    if content is not None:
        # Ephemeral replies are only for the command user, not the server.
        kwargs["content"] = content if ephemeral else quote_text(content)
    if embed is not None:
        kwargs["embed"] = embed
    # followup.send is a webhook: it rejects delete_after, so delete afterwards.
    followup = (
        target.followup
        if isinstance(target, discord.Interaction) and target.response.is_done()
        else None
    )
    if ephemeral:
        kwargs["ephemeral"] = True
    elif delete_after is not None and followup is None:
        kwargs["delete_after"] = delete_after
    if followup is not None:
        message = await followup.send(**kwargs)
        if not ephemeral and delete_after is not None and message is not None:
            await message.delete(delay=delete_after)
        return message
    if isinstance(target, discord.Interaction):
        await target.response.send_message(**kwargs)
        return None
    # Prefix commands take the text as the first argument.
    text = kwargs.pop("content", None)
    return await target.send(**kwargs) if text is None else await target.send(text, **kwargs)


def unwrap_command_error(error: BaseException) -> BaseException:
    if isinstance(error, commands.HybridCommandError):
        original = error.original
        if isinstance(original, BaseException):
            error = original
    if isinstance(error, (commands.CommandInvokeError, app_commands.CommandInvokeError)):
        original = error.original
        if isinstance(original, BaseException):
            return original
    return error


def command_error_message(
    error: BaseException,
    *,
    owner: str | None = None,
    permissions: str | None = None,
) -> str | None:
    """Shared replies for owner / Manage Server / DM failures."""
    if isinstance(error, (commands.NotOwner, OwnerCheckFailure)):
        return owner
    if isinstance(error, (commands.MissingPermissions, app_commands.MissingPermissions)):
        return permissions
    if isinstance(error, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
        return notice_text("Use this command in a server.")
    return None


HINT_TTL_SECONDS = 20.0

HINT_SETUP = notice_text("No channel set. Use `/announce feature`.", brief=True)
HINT_FEATURE_GONE = notice_text("`{feature}` channel is gone. Used default.", brief=True)


@dataclass
class AnnounceTarget:
    channel: Any | None
    used_fallback: bool = False
    feature: str | None = None
    # Stored channel exists. Fetch/permission failure stays configured so we
    # do not wipe the id or tell the server to set one.
    configured: bool = False


class NotifyCog(commands.Cog):
    """Notice-store wiring. Subclass, set feature, post with send_announcement."""

    feature: str = ""
    feature_aliases: tuple[str, ...] = ()

    def __init__(
        self,
        bot: commands.Bot,
        *,
        notices_path: Path | None = DEFAULT_NOTICES_PATH,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.notices = notices_for(bot, path=notices_path, clock=clock)

    async def cog_load(self) -> None:
        if self.feature:
            register_announce_feature(self.feature, *self.feature_aliases)
        if self.bot.is_ready():
            await self._on_notify_ready()
        self.notices.start()

    async def cog_unload(self) -> None:
        self.notices.stop()
        if self.feature:
            unregister_announce_feature(self.feature)

    async def _on_notify_ready(self) -> None:
        """Drop notices for gone guilds/channels. Expired messages wait for purge."""
        self.notices.drop_unavailable(self.bot)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._on_notify_ready()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.notices.forget_guild(guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        channel_id = getattr(channel, "id", None)
        if channel_id:
            self.notices.forget_channel(channel_id)


def channel_can_send(channel: Any, guild: Any) -> bool:
    if not callable(getattr(channel, "send", None)):
        return False
    me = getattr(guild, "me", None)
    permissions_for = getattr(channel, "permissions_for", None)
    if me is None or not callable(permissions_for):
        return True
    try:
        return permissions_for(me).send_messages is not False
    except Exception:
        return True


async def fetch_guild_channel(
    guild: Any, channel_id: int | None
) -> tuple[Any | None, bool]:
    """(channel, confirmed_gone). Only Discord NotFound counts as gone."""
    if channel_id is None:
        return None, False
    channel = guild.get_channel(channel_id)
    if channel is not None:
        return channel, False
    fetch = getattr(guild, "fetch_channel", None)
    if not callable(fetch):
        return None, False
    try:
        return await fetch(channel_id), False
    except discord.NotFound:
        return None, True
    except discord.HTTPException:
        log.warning(
            "Could not fetch announce channel %s in guild %s",
            channel_id, getattr(guild, "id", "?"), exc_info=True,
        )
        return None, False


async def resolve_announce_target(
    guild: Any, store: GuildConfig, feature: str
) -> AnnounceTarget:
    """Prefer the function channel; if Discord says it is gone, use the default."""
    canon = canonical_announce_feature(feature) or feature
    override_id = store.get_feature_override_id(guild.id, feature)
    override, override_gone = await fetch_guild_channel(guild, override_id)
    if override is not None:
        return AnnounceTarget(channel=override, feature=canon, configured=True)
    if override_gone:
        store.clear_feature_announce_channel_id(guild.id, canon)
    elif override_id is not None:
        return AnnounceTarget(channel=None, feature=canon, configured=True)

    default_id = store.get_default_announce_channel_id(guild.id)
    default, default_gone = await fetch_guild_channel(guild, default_id)
    if default_gone and default_id is not None:
        store.forget_announce_channel(guild.id, default_id)
    return AnnounceTarget(
        channel=default,
        used_fallback=override_gone and default is not None,
        feature=canon,
        configured=default is not None or (default_id is not None and not default_gone),
    )


def find_hint_channel(guild: Any) -> Any | None:
    system = getattr(guild, "system_channel", None)
    candidates = [system] if system is not None else []
    candidates.extend(getattr(guild, "text_channels", None) or ())
    return next((channel for channel in candidates if channel_can_send(channel, guild)), None)


async def _send_timed_hint(destination: Any, text: str, error: str, *args: Any) -> bool:
    try:
        await destination.send(quote_text(text), delete_after=HINT_TTL_SECONDS)
    except discord.HTTPException:
        log.exception(error, *args)
        return False
    return True


async def notify_missing_announce_channel(guild: Any, store: GuildConfig) -> None:
    if not store.should_send_setup_hint(guild.id):
        return
    channel = find_hint_channel(guild)
    if channel is None:
        log.warning("No announcement channel for guild %s, and no place to post a hint.", guild.id)
        return
    if await _send_timed_hint(channel, HINT_SETUP, "Could not send setup hint in guild %s", guild.id):
        store.mark_setup_hint_sent(guild.id)


async def notify_feature_channel_gone(
    destination: Any, store: GuildConfig, guild_id: int, feature: str
) -> None:
    if not store.should_send_fallback_hint(guild_id, feature):
        return
    if await _send_timed_hint(
        destination, HINT_FEATURE_GONE.format(feature=feature),
        "Could not send fallback hint in guild %s", guild_id,
    ):
        store.mark_fallback_hint_sent(guild_id, feature)


def _attached(files: Sequence[discord.File] | None) -> dict[str, Any]:
    if not files:
        return {}
    if len(files) == 1:
        return {"file": files[0]}
    return {"files": list(files)}


async def _post(
    channel: Any,
    content: str | None,
    embed: discord.Embed | None,
    files: Sequence[discord.File] | None = None,
    *,
    quote: bool = True,
) -> tuple[Any, int | None]:
    """One message, or a ping plus a silent card. The card is returned."""
    posted = quote_text(content) if content and quote else content
    attached = _attached(files)
    if not (content and embed is not None):
        return await channel.send(content=posted, embed=embed, **attached), None
    ping = await channel.send(content=posted)
    try:
        message = await channel.send(embed=embed, silent=True, **attached)
    except Exception:
        try:
            await ping.delete()
        except discord.HTTPException:
            pass
        raise
    return message, ping.id


async def send_announcement(
    guild: Any,
    store: GuildConfig,
    feature: str,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    delete_after: float | None = None,
    notice_key: str | int | None = None,
    extra: dict[str, Any] | None = None,
    notices: TemporaryNotices | None = None,
    files: Sequence[discord.File] | None = None,
    quote: bool = True,
) -> Any | None:
    """Post in this guild's announce channel. Content+embed is ping then silent card."""
    seen: set[int] = set()
    fallback = False
    while True:
        target = await resolve_announce_target(guild, store, feature)
        if fallback and target.channel is not None:
            target.used_fallback = True
        if target.channel is None:
            state = "unavailable" if target.configured else "not set"
            log.warning("Announce channel for guild %s (%s) is %s; skipping.", guild.id, feature, state)
            if not target.configured:
                await notify_missing_announce_channel(guild, store)
            return None
        name = target.feature or feature
        if target.used_fallback:
            await notify_feature_channel_gone(target.channel, store, guild.id, name)
        try:
            message, ping_id = await _post(target.channel, content, embed, files, quote=quote)
        except discord.HTTPException as exc:
            gone_id = getattr(target.channel, "id", None)
            fresh = isinstance(exc, discord.NotFound) and gone_id is not None and int(gone_id) not in seen
            if not fresh:
                log.exception("Could not send %s announcement in guild %s", feature, guild.id)
                return None
            seen.add(int(gone_id))
            store.forget_announce_channel(guild.id, int(gone_id))
            log.warning(
                "Announce channel %s is gone in guild %s; retrying %s on the default.",
                gone_id, guild.id, feature,
            )
            fallback = True
            continue
        if ping_id is not None:
            extra = {**(extra or {}), "ping_message_id": ping_id}
        if delete_after is None:
            return message
        if notices is None:
            raise ValueError("send_announcement(..., delete_after=...) needs a TemporaryNotices store")
        subject = notice_key if notice_key is not None else parse_snowflake(getattr(message, "id", None)) or 0
        notices.track(name, guild.id, str(subject), message, ttl=delete_after, extra=extra)
        return message

ANNOUNCE_SHOW_SECONDS = 20.0


def _unknown_announce_feature(name: str) -> str:
    known = ordered_announce_features()
    extra = "See `/announce show`." if known else None
    return notice_text(f"`{name}` is unknown.", extra)


async def _announce_feature_autocomplete(
    _interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    needle = current.lower()
    return [
        app_commands.Choice(name=name, value=name)
        for name in ordered_announce_features()
        if needle in name
    ][:25]


def _guild(target: CommandTarget) -> discord.Guild:
    guild = target.guild
    assert guild is not None
    return guild


class AnnounceCommands:
    """`/announce` and the cleanup when that channel is deleted. Admin inherits this."""

    announce = app_commands.Group(
        name="announce",
        description="Set or show announcement channels for this server",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @announce.command(name="show", description="Show announcement channels")
    async def announce_show(self, interaction: discord.Interaction) -> None:
        await respond(
            interaction,
            embed=self._announce_show_embed(_guild(interaction)),
            delete_after=ANNOUNCE_SHOW_SECONDS,
        )

    @announce.command(name="feature", description="Set the announcement channel")
    @app_commands.describe(
        channel="Text channel for announcements",
        cog="Leave empty so every function uses this channel",
    )
    @app_commands.autocomplete(cog=_announce_feature_autocomplete)
    async def announce_feature(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        cog: str | None = None,
    ) -> None:
        feature = None if cog is None else canonical_announce_feature(cog)
        if cog is not None and feature is None:
            await respond(interaction, _unknown_announce_feature(cog))
            return
        await self._set_announce(interaction, channel, feature)

    async def _set_announce(
        self,
        target: CommandTarget,
        channel: discord.TextChannel,
        feature: str | None,
    ) -> None:
        guild = _guild(target)
        config = self.guild_config
        if feature is None:
            config.set_announce_channel_id(guild.id, channel.id)
            log.info("Default announce channel for guild %s set to %s", guild.id, channel.id)
            text = f"All posts go to {channel.mention}."
        elif channel.id == config.get_default_announce_channel_id(guild.id):
            config.clear_feature_announce_channel_id(guild.id, feature)
            log.info(
                "Announce channel for guild %s function %s reset to default %s",
                guild.id, feature, channel.id,
            )
            text = f"`{feature}` uses default {channel.mention}."
        else:
            config.set_feature_announce_channel_id(guild.id, feature, channel.id)
            log.info(
                "Announce channel for guild %s function %s set to %s",
                guild.id, feature, channel.id,
            )
            text = f"`{feature}` posts in {channel.mention}."
        await respond(target, notice_text(text))

    def _channel_mention(self, guild: discord.Guild, channel_id: int | None) -> str:
        if channel_id is None:
            return "not set"
        channel = guild.get_channel(channel_id)
        if channel is None:
            return f"missing (`{channel_id}`)"
        return channel.mention

    def _announce_show_embed(self, guild: discord.Guild) -> discord.Embed:
        overrides = self.guild_config.get_feature_overrides(guild.id)
        names = ordered_announce_features()
        default_id = self.guild_config.get_default_announce_channel_id(guild.id)
        rows = [("Default", self._channel_mention(guild, default_id))]
        rows.extend(
            (
                name.capitalize(),
                self._channel_mention(guild, overrides[name]) if name in overrides else "using default",
            )
            for name in names
        )
        if not names:
            rows.append(("Functions", "none"))
        embed = discord.Embed(title="Announcement channels", color=ACCENT_COLOR)
        for name, value in rows:
            embed.add_field(name=name, value=value, inline=False)
        return embed

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        if guild is None:
            return
        default_cleared, features = self.guild_config.forget_announce_channel(guild.id, channel.id)
        if default_cleared:
            log.info("Default announce channel %s deleted in guild %s", channel.id, guild.id)
        if not features:
            return
        log.info(
            "Announce channel for %s deleted in guild %s; using default",
            ", ".join(features), guild.id,
        )
        default_id = self.guild_config.get_default_announce_channel_id(guild.id)
        default = None if default_id is None else guild.get_channel(default_id)
        if default is None:
            return
        for feature in features:
            await notify_feature_channel_gone(default, self.guild_config, guild.id, feature)

