"""Owner commands for cogs, plus per-server announcement channel setup."""

from __future__ import annotations

import asyncio
import hmac
import importlib
import logging

import discord
from discord import app_commands
from discord.ext import commands

from announce import (
    ACCENT_COLOR,
    CommandTarget,
    command_error_message,
    defer_response,
    notice_text,
    notify_feature_channel_gone,
    owner_only,
    respond,
    unwrap_command_error,
)
from config import settings
from guild import (
    GuildConfig,
    announce_features,
    canonical_announce_feature,
    guild_config as default_guild_config,
)
from loader import (
    COGS_PACKAGE,
    extension_name,
    normalize_cog_name,
    public_cog_name,
    public_cog_names,
)
from news.follows import NEWS_FEATURES


CORE_MODULES = {"announce", "notice"}
HIDDEN_OWNER_COMMANDS = ("cogs", "load", "unload", "reload")
_HIDDEN_NEWS = ("cookie", "check")
_CommandParent = app_commands.CommandTree | app_commands.Group
ANNOUNCE_SHOW_SECONDS = 20.0
REQUIRED_INTENTS = ("message_content",)
log = logging.getLogger(__name__)


_PUBLIC_COGS = public_cog_names()
_COG_CHOICES = [app_commands.Choice(name=name, value=name) for name in _PUBLIC_COGS]
_EXAMPLE_COG = next((name for name in _PUBLIC_COGS if name != "admin"), "cog")
_COG_OPTION = f"Cog, for example {_EXAMPLE_COG}"


def _not_a_cog_message(name: str) -> str | None:
    public = normalize_cog_name(name)
    if public.lower() not in CORE_MODULES:
        return None
    return notice_text(f"`{public}` is shared. Restart to apply.")


def _unknown_announce_feature(name: str) -> str:
    known = ordered_announce_features()
    extra = "See `/announce show`." if known else None
    return notice_text(f"`{name}` is unknown.", extra)


def ordered_announce_features() -> tuple[str, ...]:
    names = {name for name in announce_features() if name != "admin"}
    voice = ("voice",) if "voice" in names else ()
    other = ("other",) if "other" in names else ()
    news = tuple(name for name in NEWS_FEATURES if name in names and name != "other")
    rest = tuple(sorted(names - {*voice, *NEWS_FEATURES}))
    return voice + rest + news + other


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


def _admin_stash(bot: commands.Bot) -> dict[str, app_commands.Command | app_commands.Group]:
    stash = getattr(bot, "_hidden_admin_commands", None)
    if not isinstance(stash, dict):
        stash = {}
        bot._hidden_admin_commands = stash
    return stash


def admin_password_matches(given: str) -> bool | None:
    """`None` when `ADMIN_PASSWORD` is empty."""
    expected = settings.admin_password
    if not expected:
        return None
    return hmac.compare_digest(given.encode(), expected.encode())


def _hidden_parent(tree: app_commands.CommandTree, group: str | None) -> _CommandParent | None:
    if group is None:
        return tree
    found = tree.get_command(group)
    return found if isinstance(found, app_commands.Group) else None


def apply_admin_command_visibility(bot: commands.Bot) -> None:
    """Drop or restore owner commands on the local tree.

    The next slash sync is what Discord shows. Hidden commands stay on the bot
    so a later sync can put them back. A restart leaves them hidden.
    """
    tree = bot.tree
    stash = _admin_stash(bot)
    show = bool(getattr(bot, "admin_commands_visible", False))
    slots = [(None, name) for name in HIDDEN_OWNER_COMMANDS]
    slots += [("news", name) for name in _HIDDEN_NEWS]
    for group, name in slots:
        parent = _hidden_parent(tree, group)
        if parent is None:
            continue
        key = name if group is None else f"{group}.{name}"
        if show:
            command = stash.get(key)
            if (
                isinstance(command, (app_commands.Command, app_commands.Group))
                and parent.get_command(name) is None
            ):
                parent.add_command(command)
            continue
        command = parent.get_command(name)
        if command is None:
            continue
        stash[key] = command
        parent.remove_command(name)


def _missing_intent_names(bot: commands.Bot, extension: str) -> tuple[str, ...]:
    intents = getattr(bot, "intents", None)
    missing: list[str] = []
    for name in getattr(importlib.import_module(extension), "REQUIRED_INTENTS", ()):
        if not getattr(intents, name, False):
            missing.append(name)
    return tuple(missing)


def _guild(target: CommandTarget) -> discord.Guild:
    guild = target.guild
    assert guild is not None
    return guild


class Admin(commands.Cog):
    """Manage which cogs are running without restarting the bot."""

    announce = app_commands.Group(
        name="announce",
        description="Set or show announcement channels for this server",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(
        self,
        bot: commands.Bot,
        guild_config: GuildConfig | None = None,
    ) -> None:
        self.bot = bot
        self.guild_config = default_guild_config if guild_config is None else guild_config

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    @announce.command(name="show", description="Show announcement channels")
    async def announce_show(self, interaction: discord.Interaction) -> None:
        await self._announce_show(interaction)

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
        if cog is None:
            await self._set_announce(interaction, channel, None)
            return
        feature = canonical_announce_feature(cog)
        if feature is None:
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
        if feature is None:
            self.guild_config.set_announce_channel_id(guild.id, channel.id)
            log.info("Default announce channel for guild %s set to %s", guild.id, channel.id)
            await respond(target, notice_text(f"All posts go to {channel.mention}."))
            return
        default_id = self.guild_config.get_default_announce_channel_id(guild.id)
        if default_id is not None and channel.id == default_id:
            self.guild_config.clear_feature_announce_channel_id(guild.id, feature)
            log.info(
                "Announce channel for guild %s function %s reset to default %s",
                guild.id,
                feature,
                channel.id,
            )
            await respond(target, notice_text(f"`{feature}` uses default {channel.mention}."))
            return
        self.guild_config.set_feature_announce_channel_id(guild.id, feature, channel.id)
        log.info(
            "Announce channel for guild %s function %s set to %s",
            guild.id,
            feature,
            channel.id,
        )
        await respond(target, notice_text(f"`{feature}` posts in {channel.mention}."))

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
        embed = discord.Embed(title="Announcement channels", color=ACCENT_COLOR)
        embed.add_field(
            name="Default",
            value=self._channel_mention(guild, default_id),
            inline=False,
        )
        names = ordered_announce_features()
        for name in names:
            value = (
                self._channel_mention(guild, overrides[name])
                if name in overrides
                else "using default"
            )
            embed.add_field(name=name.capitalize(), value=value, inline=False)
        if not names:
            embed.add_field(name="Functions", value="none", inline=False)
        return embed

    async def _announce_show(self, target: CommandTarget) -> None:
        await respond(
            target,
            embed=self._announce_show_embed(_guild(target)),
            delete_after=ANNOUNCE_SHOW_SECONDS,
        )

    @commands.hybrid_command(name="ping", description="Check that Nyatify is online")
    @commands.guild_only()
    @app_commands.guild_only()
    async def ping(self, ctx: commands.Context) -> None:
        await respond(ctx, notice_text("pong", starter=False))

    @app_commands.command(name="admin", description="Show or hide owner commands")
    @app_commands.guild_only()
    @app_commands.describe(
        password="ADMIN_PASSWORD. Leave empty to hide the owner commands",
    )
    async def admin_commands(
        self, interaction: discord.Interaction, password: str | None = None
    ) -> None:
        given = (password or "").strip()
        visible = bool(getattr(self.bot, "admin_commands_visible", False))
        if not given and visible:
            await self._set_admin_commands_visible(interaction, shown=False)
            return
        if not settings.admin_password:
            await respond(
                interaction,
                notice_text("Set ADMIN_PASSWORD in config.txt."),
                ephemeral=True,
            )
            return
        if not given:
            await respond(interaction, notice_text("Enter the password."), ephemeral=True)
            return
        if not admin_password_matches(given):
            await respond(interaction, notice_text("Wrong password."), ephemeral=True)
            return
        await self._set_admin_commands_visible(interaction, shown=not visible)

    async def _set_admin_commands_visible(
        self, interaction: discord.Interaction, *, shown: bool
    ) -> None:
        await defer_response(interaction, ephemeral=True)
        previous = bool(getattr(self.bot, "admin_commands_visible", False))
        self.bot.admin_commands_visible = shown
        try:
            keep = await self._publish_commands()
        except Exception:
            self.bot.admin_commands_visible = previous
            self._restore_command_list()
            log.exception("Could not sync admin commands")
            await respond(interaction, notice_text("Could not update commands."), ephemeral=True)
            return
        line = "Admin commands shown." if shown else "Admin commands hidden."
        extra = None if settings.sync_guild_id else "May take an hour"
        log.info("Admin commands %s", "shown" if shown else "hidden")
        await respond(interaction, notice_text(line, extra), ephemeral=True)
        self._clear_other_scopes(keep)

    async def _publish_commands(self):
        sync = getattr(self.bot, "_sync_active_slash_commands", None)
        if not callable(sync):
            raise RuntimeError("Slash sync is unavailable")
        return await sync()

    def _clear_other_scopes(self, keep) -> None:
        """Drop duplicate commands elsewhere after the user already has a reply."""
        clear = getattr(self.bot, "_clear_other_slash_scopes", None)
        if not callable(clear):
            return
        pending = getattr(self, "_scope_clear", None)
        if pending is not None and not pending.done():
            return

        async def run() -> None:
            try:
                await clear(keep)
            except Exception:
                log.exception("Could not clear leftover slash commands")

        self._scope_clear = asyncio.create_task(run())

    def _restore_command_list(self) -> None:
        prepare = getattr(self.bot, "_prepare_slash_commands", None)
        if callable(prepare):
            prepare()
            return
        apply_admin_command_visibility(self.bot)

    @app_commands.command(name="cogs", description="List loaded and available cogs")
    @app_commands.guild_only()
    @owner_only()
    async def list_cogs(self, interaction: discord.Interaction) -> None:
        loaded = sorted(public_cog_name(name) for name in self.bot.extensions)
        available = public_cog_names()
        embed = discord.Embed(title="Cogs", color=ACCENT_COLOR)
        embed.add_field(name="Loaded", value=_cog_lines(loaded), inline=False)
        embed.add_field(name="Available", value=_cog_lines(available), inline=False)
        await respond(interaction, embed=embed)

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
            guild.id, channel.id
        )
        if default_cleared:
            log.info(
                "Default announce channel %s deleted in guild %s", channel.id, guild.id
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
                default, self.guild_config, guild.id, feature
            )

    async def _swap_cog(
        self, interaction: discord.Interaction, cog: str, *, action: str, verb: str
    ) -> None:
        extension = await self._resolve_cog_extension(interaction, cog)
        if extension is None:
            return
        if action == "unload" and extension == f"{COGS_PACKAGE}.admin":
            await respond(interaction, notice_text("The admin cog cannot be unloaded."))
            return
        if action == "load":
            await self.bot.load_extension(extension)
        elif action == "unload":
            await self.bot.unload_extension(extension)
        else:
            await self.bot.reload_extension(extension)
        log.info("%s %s", verb, extension)
        if action == "unload":
            await respond(
                interaction, notice_text(f"Unloaded `{public_cog_name(extension)}`.")
            )
            return
        await self._report_cog(interaction, extension, verb)

    async def _resolve_cog_extension(
        self, target: CommandTarget, cog: str
    ) -> str | None:
        blocked = _not_a_cog_message(cog)
        if blocked:
            await respond(target, blocked)
            return None
        return extension_name(cog)

    async def _report_cog(
        self, interaction: discord.Interaction, extension: str, verb: str
    ) -> None:
        public = public_cog_name(extension)
        missing = _missing_intent_names(self.bot, extension)
        if not missing:
            await respond(interaction, notice_text(f"{verb} `{public}`."))
            return
        listed = ", ".join(f"`{name}`" for name in missing)
        await respond(interaction, notice_text(f"{verb} `{public}`. Need {listed}."))

    @app_commands.command(name="load", description="Load a cog without restarting")
    @app_commands.guild_only()
    @owner_only()
    @app_commands.describe(cog=_COG_OPTION)
    @app_commands.choices(cog=_COG_CHOICES)
    async def load_cog(self, interaction: discord.Interaction, cog: str) -> None:
        await self._swap_cog(interaction, cog, action="load", verb="Loaded")

    @app_commands.command(name="unload", description="Unload a cog without restarting")
    @app_commands.guild_only()
    @owner_only()
    @app_commands.describe(cog=_COG_OPTION)
    @app_commands.choices(cog=_COG_CHOICES)
    async def unload_cog(self, interaction: discord.Interaction, cog: str) -> None:
        await self._swap_cog(interaction, cog, action="unload", verb="Unloaded")

    @app_commands.command(name="reload", description="Reload a cog without restarting")
    @app_commands.guild_only()
    @owner_only()
    @app_commands.describe(cog=_COG_OPTION)
    @app_commands.choices(cog=_COG_CHOICES)
    async def reload_cog(self, interaction: discord.Interaction, cog: str) -> None:
        await self._swap_cog(interaction, cog, action="reload", verb="Reloaded")

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        await self._answer_error(ctx, error)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        await self._answer_error(interaction, error, ephemeral=True)

    async def _answer_error(
        self, target: CommandTarget, error: BaseException, *, ephemeral: bool = False
    ) -> None:
        message = _shared_error_message(unwrap_command_error(error))
        if message is None:
            raise error
        await respond(target, notice_text(message), ephemeral=ephemeral)


def _cog_lines(names: list[str]) -> str:
    return "\n".join(f"- `{name}`" for name in names) or "- none"


def _shared_error_message(error: BaseException) -> str | None:
    message = command_error_message(
        error,
        owner=notice_text("Only the bot owner can manage cogs."),
        permissions=notice_text("Need Manage Server to set announce channels."),
    )
    if message is not None:
        return message
    if isinstance(error, commands.ExtensionAlreadyLoaded):
        return notice_text(f"`{error.name}` is already loaded.")
    if isinstance(error, commands.ExtensionNotLoaded):
        return notice_text(f"`{error.name}` is not loaded.")
    if isinstance(error, commands.ExtensionNotFound):
        return notice_text(f"`{error.name}` was not found.")
    if isinstance(error, commands.ExtensionFailed):
        log.exception("Extension failed: %s", error.name, exc_info=error.original)
        return notice_text(f"`{error.name}` failed to load. Check logs.")
    return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
    # Boot syncs in setup_hook. A reload syncs here, after commands are back.
    if not bot.is_ready():
        return
    sync = getattr(bot, "_sync_slash_commands", None)
    if callable(sync):
        await sync()
