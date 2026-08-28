"""Nyatify entry point: a thin core that loads feature cogs independently."""

from __future__ import annotations

from pathlib import Path

import discord
from discord.ext import commands

from config import settings

COGS_DIR = Path(__file__).resolve().parent / "cogs"


class Nyatify(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix=settings.command_prefix, intents=intents)

    async def setup_hook(self) -> None:
        for path in sorted(COGS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            extension = f"cogs.{path.stem}"
            try:
                await self.load_extension(extension)
                print(f"Loaded {extension}")
            except Exception as exc:
                print(f"Skipped {extension}: {exc}")

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} ({self.user.id if self.user else '?'})")
        print(f"Loaded functions: {', '.join(sorted(self.extensions)) or 'none'}")


def main() -> None:
    bot = Nyatify()
    bot.run(settings.require_token())


if __name__ == "__main__":
    main()
