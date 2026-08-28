"""Owner-only commands to list, load, unload, and reload feature cogs."""

from __future__ import annotations

from pathlib import Path

import discord
from discord.ext import commands


COGS_PACKAGE = "cogs"


def _extension_name(name: str) -> str:
    name = name.strip()
    if name.startswith(f"{COGS_PACKAGE}."):
        return name
    return f"{COGS_PACKAGE}.{name}"


class Admin(commands.Cog):
    """Manage which functions are running without restarting the bot."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        await ctx.send("pong")

    @commands.command(name="cogs")
    @commands.is_owner()
    async def list_cogs(self, ctx: commands.Context) -> None:
        loaded = sorted(self.bot.extensions)
        available = sorted(
            f"{COGS_PACKAGE}.{path.stem}"
            for path in (Path(__file__).resolve().parent).glob("*.py")
            if not path.name.startswith("_")
        )
        loaded_text = "\n".join(f"- `{name}`" for name in loaded) or "- none"
        available_text = "\n".join(f"- `{name}`" for name in available) or "- none"
        embed = discord.Embed(title="Functions", color=discord.Color.from_str("#FFB6C1"))
        embed.add_field(name="Loaded", value=loaded_text, inline=False)
        embed.add_field(name="Available", value=available_text, inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="load")
    @commands.is_owner()
    async def load_cog(self, ctx: commands.Context, name: str) -> None:
        extension = _extension_name(name)
        await self.bot.load_extension(extension)
        await ctx.send(f"Loaded `{extension}`.")

    @commands.command(name="unload")
    @commands.is_owner()
    async def unload_cog(self, ctx: commands.Context, name: str) -> None:
        extension = _extension_name(name)
        if extension == f"{COGS_PACKAGE}.admin":
            await ctx.send("The admin function cannot be unloaded.")
            return
        await self.bot.unload_extension(extension)
        await ctx.send(f"Unloaded `{extension}`.")

    @commands.command(name="reload")
    @commands.is_owner()
    async def reload_cog(self, ctx: commands.Context, name: str) -> None:
        extension = _extension_name(name)
        await self.bot.reload_extension(extension)
        await ctx.send(f"Reloaded `{extension}`.")

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.NotOwner):
            await ctx.send("Only the bot owner can manage functions.")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: `!load`, `!unload`, or `!reload` plus a cog name.")
            return
        if isinstance(error, commands.ExtensionAlreadyLoaded):
            await ctx.send(f"`{error.name}` is already loaded.")
            return
        if isinstance(error, commands.ExtensionNotLoaded):
            await ctx.send(f"`{error.name}` is not loaded.")
            return
        if isinstance(error, commands.ExtensionNotFound):
            await ctx.send(f"`{error.name}` was not found.")
            return
        if isinstance(error, commands.ExtensionFailed):
            await ctx.send(f"`{error.name}` failed to load. Check the logs.")
            print(f"Extension failed: {error.name}\n{error.original}")
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
