"""Owner commands for cogs, plus per-server announcement channel setup."""

from __future__ import annotations

import importlib
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from announce import (
    ACCENT_COLOR,
    notify_feature_channel_gone,
)
from guild_config import (
    GuildConfig,
    announce_features,
    canonical_announce_feature,
    guild_config as default_guild_config,
    parse_snowflake,
)
from loader import (
    COGS_PACKAGE,
    extension_name,
    normalize_cog_name,
    public_cog_name,
    public_cog_names,
)


CORE_MODULES = {"announce"}
COMMAND_REPLY_SECONDS = 10.0
ANNOUNCE_SHOW_SECONDS = 20.0
REQUIRED_INTENTS = ("message_content",)
log = logging.getLogger(__name__)

CommandTarget = commands.Context | discord.Interaction


_PUBLIC_COGS = public_cog_names()
_COG_CHOICES = [app_commands.Choice(name=name, value=name) for name in _PUBLIC_COGS]
_ANNOUNCE_COG_CHOICES = [
    app_commands.Choice(name=name, value=name)
    for name in _PUBLIC_COGS
    if name != "admin"
]
_EXAMPLE_COG = next((name for name in _PUBLIC_COGS if name != "admin"), "cog")
_COG_OPTION = f"Cog, for example {_EXAMPLE_COG}"


def _not_a_cog_message(name: str) -> str | None:
    if normalize_cog_name(name).lower() not in CORE_MODULES:
        return None
    return (
        "`announce` is shared by every cog, not a loadable cog.\n"
        f"Reload a cog with `/reload {_EXAMPLE_COG}` or "
        f"`!reload {_EXAMPLE_COG}`.\n"
        "Restart the bot to apply announce.py changes."
    )


def _missing_arg_message(ctx: commands.Context) -> str:
    command = getattr(ctx, "command", None)
    name = getattr(command, "qualified_name", "") or ""
    if name in {"load", "unload", "reload"}:
        return f"Usage: `/{name} {_EXAMPLE_COG}` or `!{name} {_EXAMPLE_COG}`."
    return (
        "Usage: `/announce feature` or `!announce #channel`;\n"
        "`/load` or `!load` plus a cog."
    )


def _announce_usage() -> str:
    return (
        "`/announce show` or `!announce` — show channels\n"
        "`/announce feature` or `!announce #channel` — "
        "every cog uses this channel\n"
        "`/announce feature` with a cog, or "
        f"`!announce {_EXAMPLE_COG} #channel` — only that cog"
    )


def _unknown_announce_feature(name: str) -> str:
    known = announce_features()
    message = f"`{name}` is not an announcement cog."
    if known:
        listed = ", ".join(f"`{feature}`" for feature in known)
        message += f" Known: {listed}."
    return message


def _missing_intent_names(bot: commands.Bot, extension: str) -> tuple[str, ...]:
    intents = getattr(bot, "intents", None)
    missing: list[str] = []
    for name in getattr(importlib.import_module(extension), "REQUIRED_INTENTS", ()):
        if not getattr(intents, name, False):
            missing.append(name)
    return tuple(missing)


def _channel_id_from_arg(argument: str) -> int | None:
    text = argument.strip()
    if text.startswith("<#") and text.endswith(">"):
        text = text[2:-1]
    return parse_snowflake(text)


def _guild(target: CommandTarget) -> discord.Guild:
    guild = target.guild
    assert guild is not None
    return guild


def _send_kwargs(
    content: str | None,
    embed: discord.Embed | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    return kwargs


async def _respond(
    target: CommandTarget,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    ephemeral: bool = False,
    delete_after: float | None = COMMAND_REPLY_SECONDS,
) -> None:
    kwargs = _send_kwargs(content, embed)
    if isinstance(target, discord.Interaction):
        if ephemeral:
            kwargs["ephemeral"] = True
        elif delete_after is not None:
            kwargs["delete_after"] = delete_after
        if target.response.is_done():
            await target.followup.send(**kwargs)
            return
        await target.response.send_message(**kwargs)
        return
    if delete_after is not None:
        kwargs["delete_after"] = delete_after
    if content is None:
        await target.send(**kwargs)
        return
    kwargs.pop("content", None)
    await target.send(content, **kwargs)


class Admin(commands.Cog):
    """Manage which cogs are running without restarting the bot."""

    def __init__(
        self,
        bot: commands.Bot,
        guild_config: GuildConfig | None = None,
    ) -> None:
        self.bot = bot
        self.guild_config = default_guild_config if guild_config is None else guild_config

    @commands.hybrid_group(
        name="announce",
        invoke_without_command=True,
        description="Set or show announcement channels for this server",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.default_permissions(discord.Permissions(manage_guild=True))
    async def announce(
        self,
        ctx: commands.Context,
        first: str | None = None,
        second: str | None = None,
    ) -> None:
        """Set or show announcement channels for this server."""
        if ctx.invoked_subcommand is not None:
            return
        if first is None:
            await self._announce_show(ctx)
            return
        if second is None:
            channel = await self._resolve_text_channel(ctx, first)
            if channel is None:
                feature = canonical_announce_feature(first)
                if feature is not None:
                    await _respond(
                        ctx,
                        f"Pick a text channel, for example\n"
                        f"`!announce {feature} #channel` or `/announce feature`.",
                    )
                    return
                await _respond(ctx, f"That text channel was not found.\n{_announce_usage()}")
                return
            await self._announce_set_default(ctx, channel)
            return

        feature = canonical_announce_feature(first)
        if feature is None:
            await _respond(ctx, _unknown_announce_feature(first))
            return
        channel = await self._resolve_text_channel(ctx, second)
        if channel is None:
            await _respond(
                ctx,
                f"That text channel was not found.\n"
                f"Try `!announce {feature} #channel` or `/announce feature`.",
            )
            return
        await self._announce_set_feature(ctx, feature, channel)

    @announce.command(name="show", description="Show announcement channels")
    async def announce_show(self, ctx: commands.Context) -> None:
        await self._announce_show(ctx)

    @announce.command(name="feature", description="Set the announcement channel")
    @app_commands.describe(
        channel="Text channel for announcements",
        cog="Leave empty so every cog uses this channel",
    )
    @app_commands.choices(cog=_ANNOUNCE_COG_CHOICES)
    async def announce_feature(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        cog: str | None = None,
    ) -> None:
        if cog is None:
            await self._announce_set_default(ctx, channel)
            return
        feature = canonical_announce_feature(cog)
        if feature is None:
            await _respond(ctx, _unknown_announce_feature(cog))
            return
        await self._announce_set_feature(ctx, feature, channel)

    async def _announce_set_default(
        self,
        target: CommandTarget,
        channel: discord.TextChannel,
    ) -> None:
        guild = _guild(target)
        self.guild_config.set_announce_channel_id(guild.id, channel.id)
        log.info(
            "Default announce channel for guild %s set to %s",
            guild.id,
            channel.id,
        )
        await _respond(
            target,
            f"All announcement cogs will post in {channel.mention}.",
        )

    async def _announce_set_feature(
        self,
        target: CommandTarget,
        feature: str,
        channel: discord.TextChannel,
    ) -> None:
        guild = _guild(target)
        default_id = self.guild_config.get_default_announce_channel_id(guild.id)
        if default_id is not None and channel.id == default_id:
            self.guild_config.clear_feature_announce_channel_id(guild.id, feature)
            log.info(
                "Announce channel for guild %s function %s reset to default %s",
                guild.id,
                feature,
                channel.id,
            )
            await _respond(
                target,
                f"`{feature}` will use the default channel ({channel.mention}).",
            )
            return
        self.guild_config.set_feature_announce_channel_id(guild.id, feature, channel.id)
        log.info(
            "Announce channel for guild %s function %s set to %s",
            guild.id,
            feature,
            channel.id,
        )
        await _respond(target, f"`{feature}` will post in {channel.mention}.")

    async def _resolve_text_channel(
        self,
        ctx: commands.Context,
        argument: str,
    ) -> discord.TextChannel | None:
        guild = _guild(ctx)
        channel_id = _channel_id_from_arg(argument)
        if channel_id is not None:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
            if channel is not None:
                return None
        try:
            return await commands.TextChannelConverter().convert(ctx, argument)
        except commands.BadArgument:
            return None

    def _channel_mention(self, guild: discord.Guild, channel_id: int | None) -> str:
        if channel_id is None:
            return "not set"
        channel = guild.get_channel(channel_id)
        if channel is None:
            return f"missing (`{channel_id}`)"
        return channel.mention

    def _announce_show_embed(self, guild: discord.Guild) -> discord.Embed:
        default_id = self.guild_config.get_default_announce_channel_id(guild.id)
        overrides = self.guild_config.get_feature_overrides(guild.id)
        embed = discord.Embed(
            title="Announcement channels",
            color=ACCENT_COLOR,
        )
        embed.add_field(
            name="All cogs (default)",
            value=self._channel_mention(guild, default_id),
            inline=False,
        )
        for name in announce_features():
            if name in overrides:
                value = self._channel_mention(guild, overrides[name])
            elif default_id is None:
                value = "not set"
            else:
                value = "using default"
            embed.add_field(name=name, value=value, inline=True)
        return embed

    async def _announce_show(self, ctx: commands.Context) -> None:
        embed = self._announce_show_embed(_guild(ctx))
        await _respond(ctx, embed=embed, delete_after=ANNOUNCE_SHOW_SECONDS)

    async def cog_load(self) -> None:
        # Slash choices only update after tree.sync.
        # setup_hook syncs on boot; re-sync here so /reload admin publishes them.
        if not self.bot.is_ready():
            return
        sync = getattr(self.bot, "_sync_slash_commands", None)
        if callable(sync):
            await sync()

    @commands.hybrid_command(name="ping", description="Check that Nyatify is online")
    async def ping(self, ctx: commands.Context) -> None:
        await _respond(ctx, "pong")

    @commands.hybrid_command(name="cogs", description="List loaded and available cogs")
    @commands.is_owner()
    async def list_cogs(self, ctx: commands.Context) -> None:
        loaded = sorted(public_cog_name(name) for name in self.bot.extensions)
        available = public_cog_names()
        loaded_text = "\n".join(f"- `{name}`" for name in loaded) or "- none"
        available_text = "\n".join(f"- `{name}`" for name in available) or "- none"
        embed = discord.Embed(title="Cogs", color=ACCENT_COLOR)
        embed.add_field(name="Loaded", value=loaded_text, inline=False)
        embed.add_field(name="Available", value=available_text, inline=False)
        await _respond(ctx, embed=embed)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.guild_config.remove_guild(guild.id)
        log.info("Removed stored settings for guild %s", guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        if guild is None:
            return
        default_cleared, features = self.guild_config.forget_announce_channel(
            guild.id,
            channel.id,
        )
        if default_cleared:
            log.info(
                "Default announce channel %s deleted in guild %s",
                channel.id,
                guild.id,
            )
        if not features:
            return
        log.info(
            "Announce channel for %s deleted in guild %s; using default",
            ", ".join(features),
            guild.id,
        )
        default_id = self.guild_config.get_default_announce_channel_id(guild.id)
        default = None if default_id is None else guild.get_channel(default_id)
        if default is None:
            return
        for feature in features:
            await notify_feature_channel_gone(
                default,
                self.guild_config,
                guild.id,
                feature,
            )

    async def _resolve_cog_extension(
        self, ctx: commands.Context, cog: str
    ) -> str | None:
        blocked = _not_a_cog_message(cog)
        if blocked:
            await _respond(ctx, blocked)
            return None
        return extension_name(cog)

    @commands.hybrid_command(name="load", description="Load a cog without restarting")
    @commands.is_owner()
    @app_commands.describe(cog=_COG_OPTION)
    @app_commands.choices(cog=_COG_CHOICES)
    async def load_cog(self, ctx: commands.Context, cog: str) -> None:
        extension = await self._resolve_cog_extension(ctx, cog)
        if extension is None:
            return
        await self.bot.load_extension(extension)
        log.info("Loaded %s", extension)
        public = public_cog_name(extension)
        missing = _missing_intent_names(self.bot, extension)
        if missing:
            listed = ", ".join(f"`{name}`" for name in missing)
            await _respond(
                ctx,
                f"Loaded `{public}`, but this process is missing the {listed} "
                "intent. Restart the bot with that cog enabled to receive its events.",
            )
            return
        await _respond(ctx, f"Loaded `{public}`.")

    @commands.hybrid_command(name="unload", description="Unload a cog without restarting")
    @commands.is_owner()
    @app_commands.describe(cog=_COG_OPTION)
    @app_commands.choices(cog=_COG_CHOICES)
    async def unload_cog(self, ctx: commands.Context, cog: str) -> None:
        extension = await self._resolve_cog_extension(ctx, cog)
        if extension is None:
            return
        if extension == f"{COGS_PACKAGE}.admin":
            await _respond(ctx, "The admin cog cannot be unloaded.")
            return
        await self.bot.unload_extension(extension)
        log.info("Unloaded %s", extension)
        await _respond(ctx, f"Unloaded `{public_cog_name(extension)}`.")

    @commands.hybrid_command(name="reload", description="Reload a cog without restarting")
    @commands.is_owner()
    @app_commands.describe(cog=_COG_OPTION)
    @app_commands.choices(cog=_COG_CHOICES)
    async def reload_cog(self, ctx: commands.Context, cog: str) -> None:
        extension = await self._resolve_cog_extension(ctx, cog)
        if extension is None:
            return
        await self.bot.reload_extension(extension)
        log.info("Reloaded %s", extension)
        public = public_cog_name(extension)
        missing = _missing_intent_names(self.bot, extension)
        if missing:
            listed = ", ".join(f"`{name}`" for name in missing)
            await _respond(
                ctx,
                f"Reloaded `{public}`, but this process is missing the {listed} "
                "intent. Restart the bot with that cog enabled to receive its events.",
            )
            return
        await _respond(ctx, f"Reloaded `{public}`.")

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        cause = _unwrap_error(error)
        message = _shared_error_message(cause)
        if message is None and isinstance(cause, commands.MissingRequiredArgument):
            message = _missing_arg_message(ctx)
        elif message is None and isinstance(cause, commands.ChannelNotFound):
            message = "That text channel was not found in this server."
        elif message is None and isinstance(cause, commands.BadArgument):
            message = (
                "Pick a text channel, for example `/announce feature` or\n"
                "`!announce #announcements`."
            )
        if message is None:
            raise error
        await _respond(ctx, message)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        message = _shared_error_message(_unwrap_error(error))
        if message is None:
            raise error
        await _respond(interaction, message, ephemeral=True)


def _unwrap_error(error: BaseException) -> BaseException:
    if isinstance(error, commands.HybridCommandError):
        original = error.original
        if isinstance(original, BaseException):
            error = original
    if isinstance(error, (commands.CommandInvokeError, app_commands.CommandInvokeError)):
        original = error.original
        if isinstance(original, BaseException):
            return original
    return error


def _shared_error_message(error: BaseException) -> str | None:
    if isinstance(error, commands.NotOwner):
        return "Only the bot owner can manage cogs."
    if isinstance(error, (commands.MissingPermissions, app_commands.MissingPermissions)):
        return "You need Manage Server to change the announcement channel."
    if isinstance(error, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
        return "Use this command in a server."
    if isinstance(error, commands.ExtensionAlreadyLoaded):
        return f"`{error.name}` is already loaded."
    if isinstance(error, commands.ExtensionNotLoaded):
        return f"`{error.name}` is not loaded."
    if isinstance(error, commands.ExtensionNotFound):
        return f"`{error.name}` was not found."
    if isinstance(error, commands.ExtensionFailed):
        log.exception("Extension failed: %s", error.name, exc_info=error.original)
        return f"`{error.name}` failed to load. Check the logs."
    return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
