"""Slash commands for the news cog."""

from news.command.test import TestCommands
from news.command.user import UserCommands


class NewsSlash(TestCommands, UserCommands):
    """`/news` commands. Lives on NewsNotify."""
