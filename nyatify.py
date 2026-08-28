"""Nyatify entry point: a thin core that loads feature cogs independently."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from config import settings
from loader import selected_extensions

log = logging.getLogger("nyatify")


class Nyatify(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix=settings.command_prefix, intents=intents)

    async def setup_hook(self) -> None:
        for extension in selected_extensions(settings.enabled_cogs):
            try:
                await self.load_extension(extension)
                log.info("Loaded %s", extension)
            except Exception:
                log.exception("Skipped %s", extension)

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
