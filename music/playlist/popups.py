"""Popups for creating, emptying, deleting, and saving."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from music.archive import SavedList
from music.panel import (
    _note,
    _selected,
)
from music.playlist.card import (
    HISTORY,
    PICK,
    _ENTRY,
    _defer,
    _song_noun,
)


_Apply = Callable[[discord.Interaction, str], Awaitable[None]]


def _plain(text: str) -> str:
    return " ".join(text.split())


def _same(text: str, phrase: str) -> bool:
    return _plain(text).casefold() == phrase


class NamePlaylist(discord.ui.Modal):
    """A name, or a playlist link from any of the five sites."""

    query = discord.ui.Label(
        text="Name or playlist link",
        component=discord.ui.TextInput(placeholder="Name or link", max_length=_ENTRY),
    )

    def __init__(self, apply: _Apply) -> None:
        super().__init__(title="Create")
        self._apply = apply

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = _plain(str(self.query.component))
        if not name:
            await _note(interaction, "Name the playlist.")
            return
        await self._apply(interaction, name)


def means_yes(text: str) -> bool:
    """YES, yes, and Yes empty the list. Any other word leaves it."""
    return _same(text, "yes")


def empty_hint(panel: str, *, yours: bool = False) -> str:
    """The line under the warning, in the style of GitHub's delete box."""
    if panel == HISTORY and yours:
        cleared = "your played songs"
    elif panel == HISTORY:
        cleared = "every played song"
    else:
        cleared = "every waiting song"
    return f'This permanently clears {cleared}. Type "yes" to confirm'


class EmptyList(discord.ui.Modal):
    """Same kind of panel as Search. yes empties the list already on screen."""

    query = discord.ui.Label(
        text="This action cannot be undone.",
        description='Type "yes" to confirm',
        component=discord.ui.TextInput(placeholder="yes", max_length=16),
    )

    def __init__(self, apply: _Apply, panel: str, *, yours: bool = False) -> None:
        super().__init__(title="Empty")
        self._apply = apply
        self.panel = panel
        self.query.description = empty_hint(panel, yours=yours)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not means_yes(str(self.query.component)):
            await _defer(interaction)
            return
        await self._apply(interaction, self.panel)


# Only this phrase removes a playlist. Any case works, the same as yes on Empty.
REMOVE_PHRASE = "yes, remove it"


def means_remove(text: str) -> bool:
    """yes, remove it removes the playlist. Any other phrase leaves it."""
    return _same(text, REMOVE_PHRASE)


def remove_hint() -> str:
    """The grey line under the warning."""
    return f'This permanently removes this playlist. Type "{REMOVE_PHRASE}" to confirm'


class RemovePlaylist(discord.ui.Modal):
    """Same kind of panel as Empty. The phrase has to match."""

    query = discord.ui.Label(
        text="This action cannot be undone.",
        description=f'Type "{REMOVE_PHRASE}" to confirm',
        component=discord.ui.TextInput(placeholder=REMOVE_PHRASE, max_length=32),
    )

    def __init__(self, apply: Callable[[discord.Interaction], Awaitable[None]]) -> None:
        super().__init__(title="Remove")
        self._apply = apply
        self.query.description = remove_hint()

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not means_remove(str(self.query.component)):
            await _defer(interaction)
            return
        await self._apply(interaction)


class FindInList(discord.ui.Modal):
    """A song or an artist inside the open playlist."""

    query = discord.ui.Label(
        text="Song or artist",
        component=discord.ui.TextInput(placeholder="Name", max_length=200),
    )

    def __init__(self, apply: _Apply) -> None:
        super().__init__(title="Search")
        self._apply = apply

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = _plain(str(self.query.component))
        if not query:
            await _note(interaction, "Name a song or an artist.")
            return
        await self._apply(interaction, query)


def _playlist_options(lists: list[SavedList]) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=saved.name,
            value=saved.name,
            description=f"{len(saved.songs)} {_song_noun(len(saved.songs))}",
        )
        for saved in lists[:25]
    ]


class _PickPlaylist(discord.ui.Modal):
    """One saved playlist, chosen from a menu."""

    def __init__(self, title: str, custom_id: str, lists: list[SavedList], apply: _Apply) -> None:
        super().__init__(title=title)
        self._apply = apply
        self.menu = discord.ui.Select(
            custom_id=custom_id,
            placeholder=PICK,
            min_values=1,
            max_values=1,
            options=_playlist_options(lists),
        )
        self.add_item(discord.ui.Label(text="Playlist", component=self.menu))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = _selected(self.menu)
        if not name:
            await _note(interaction, "Pick a playlist.")
            return
        await self._apply(interaction, name)


class QueueList(_PickPlaylist):
    """A playlist joins the queue from the first song, in order."""

    def __init__(self, lists: list[SavedList], apply: _Apply) -> None:
        super().__init__("Queue", "music-queue-playlist", lists, apply)


class SaveSong(_PickPlaylist):
    """One played song joins the playlist they pick."""

    def __init__(self, lists: list[SavedList], apply: _Apply) -> None:
        super().__init__("Save", "music-save-playlist", lists, apply)
