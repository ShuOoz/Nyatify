"""Helpers for the playlist card."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from music.playback import queue_tracks, stop_track
from music.playlist.card import LABEL_EMPTY
from tests.music.support import _Player, _ask, _named

async def _line_up(guild_id: int, titles: list[str]):
    guild = MagicMock()
    guild.id = guild_id
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    channel.id = guild_id + 1
    player = _Player(channel)
    channel.connect = AsyncMock(return_value=player)

    async def play(track):
        player._track = track

    player.play.side_effect = play
    member = MagicMock()
    member.id = guild_id + 2
    member.voice.channel = channel
    tracks = []
    for title in titles:
        track = _named(title)
        track.uri = f"https://example.com/{title.replace(' ', '-').lower()}"
        tracks.append(track)
    note = await queue_tracks(member, guild, tracks)
    guild.voice_client = player
    return guild, member, player, tracks, note

async def _open_empty(view, press):
    await _button(view, LABEL_EMPTY).callback(press)
    return press.response.send_modal.await_args.args[0]

async def _answer_empty(view, press, word: str):
    modal = await _open_empty(view, press)
    modal.query.component._value = word
    await modal.on_submit(press)
    return modal

def _press(user=None):
    interaction = _ask()
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()
    if user is not None:
        interaction.user = user
    return interaction

def _text(view) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )

def _button(view, label, index=None):
    for item in view.walk_children():
        if not isinstance(item, discord.ui.Button):
            continue
        if item.label != label and getattr(item, "panel", None) != label:
            continue
        if index is not None and getattr(item, "index", None) != index:
            continue
        return item
    raise AssertionError((label, index))

def _sections(view):
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Section)]

def _after_menu(view):
    for item in view.walk_children():
        if not isinstance(item, discord.ui.Container):
            continue
        children = item.children
        for index, child in enumerate(children):
            if not isinstance(child, discord.ui.ActionRow):
                continue
            if any(isinstance(piece, discord.ui.Select) for piece in child.children):
                return children[index + 1]
    raise AssertionError("menu has nothing under it")

def _gap_under_menu(view):
    gap = _after_menu(view)
    assert isinstance(gap, discord.ui.Separator)
    assert gap.visible is False
    assert gap.spacing == discord.SeparatorSpacing.small

def _menu(view):
    found = [item for item in view.walk_children() if isinstance(item, discord.ui.Select)]
    assert len(found) == 1
    return found[0]
