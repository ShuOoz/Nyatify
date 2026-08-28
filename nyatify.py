"""Nyatify entry point: a thin core that loads feature cogs independently."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from config import settings
from loader import required_intent_names, selected_extensions

log = logging.getLogger("nyatify")


def bot_intents(enabled_cogs: list[str] | None = None) -> discord.Intents:
    """Default intents plus only the feature intents the selected cogs need."""
    intents = discord.Intents.default()
    intents.voice_states = False
    intents.members = True
    for name in required_intent_names(enabled_cogs):
        if not hasattr(intents, name):
            log.warning("Ignoring unknown REQUIRED_INTENTS name %s", name)
            continue
        setattr(intents, name, True)
    return intents


class Nyatify(commands.Bot):
    def __init__(self, enabled_cogs: list[str] | None = None) -> None:
        if enabled_cogs is None:
            enabled_cogs = settings.enabled_cogs
        super().__init__(
            command_prefix=settings.command_prefix,
            intents=bot_intents(enabled_cogs),
        )
        self._enabled_cogs = enabled_cogs

    async def setup_hook(self) -> None:
        for extension in selected_extensions(self._enabled_cogs):
            try:
                await self.load_extension(extension)
                log.info("Loaded %s", extension)
            except Exception:
                log.exception("Skipped %s", extension)
        try:
            await self._sync_slash_commands()
        except Exception:
            log.exception("Failed to sync slash commands")

    async def _sync_slash_commands(self) -> None:
        """Publish slash commands to one Discord scope and empty the other.

        Guild copies and global copies of the same names show up twice in the
        command list. Keep only the scope selected by SYNC_GUILD_ID.
        """
        guild_id = settings.sync_guild_id
        keep = discord.Object(id=guild_id) if guild_id else None
        if keep is not None:
            self.tree.copy_global_to(guild=keep)
            synced = await self.tree.sync(guild=keep)
            log.info("Synced %s slash command(s) to guild %s", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %s global slash command(s)", len(synced))
        await self._clear_other_slash_scopes(keep)

    async def _clear_other_slash_scopes(
        self, keep: discord.abc.Snowflake | None
    ) -> None:
        if keep is not None:
            await self._clear_remote_slash_commands(guild=None)
        async for guild in self.fetch_guilds(limit=None):
            if keep is not None and guild.id == keep.id:
                continue
            try:
                await self._clear_remote_slash_commands(guild=guild)
            except discord.HTTPException:
                log.exception(
                    "Could not clear leftover slash commands in guild %s",
                    guild.id,
                )

    async def _clear_remote_slash_commands(
        self, *, guild: discord.abc.Snowflake | None
    ) -> None:
        remote = await self.tree.fetch_commands(guild=guild)
        if not remote:
            return
        local = list(self.tree.get_commands(guild=guild))
        self.tree.clear_commands(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        finally:
            for command in local:
                self.tree.add_command(command, guild=guild, override=True)
        if guild is None:
            log.info("Cleared %s leftover global slash command(s)", len(remote))
            return
        log.info(
            "Cleared %s leftover slash command(s) from guild %s",
            len(remote),
            guild.id,
        )

    async def on_ready(self) -> None:
        log.info(
            "Logged in as %s (%s)",
            self.user,
            self.user.id if self.user else "?",
        )
        loaded = ", ".join(sorted(self.extensions)) or "none"
        log.info("Loaded functions: %s", loaded)


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    configure_logging()
    bot = Nyatify()
    bot.run(settings.require_token(), log_handler=None)


if __name__ == "__main__":
    main()
