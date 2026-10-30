"""Setup shared by music tests. Production code does not import this."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import wavelink

from music.lavalink import GuildPlayer
from music.playback import queue_tracks, queued, stop_track

def _ready_node() -> MagicMock:
    node = MagicMock()
    node.players = []
    node._inactive_channel_tokens = None
    node._inactive_player_timeout = None
    node.client = None
    node.session_id = "lavalink-session"
    node.status = type("Status", (), {"name": "CONNECTED"})()
    node._update_player = AsyncMock()
    return node

def _linked_player(node: MagicMock) -> GuildPlayer:
    player = GuildPlayer(MagicMock(), MagicMock(), nodes=[node])
    guild = MagicMock()
    guild.id = 31
    player._guild = guild
    player.disconnect = AsyncMock()
    return player

class _Player(wavelink.Player):
    """A Player subclass that does not open a voice connection."""

    def __init__(self, channel) -> None:
        self.channel = channel
        self.play = AsyncMock()
        self.move_to = AsyncMock()
        self.pause = AsyncMock()
        self.stop = AsyncMock()
        self.disconnect = AsyncMock()
        self.seek = AsyncMock()
        self.set_volume = AsyncMock()
        self._track = None
        self._position = 0
        self._paused = False
        self._guild = None

    @property
    def guild(self):
        return self._guild

    @property
    def current(self):
        return self._track

    @property
    def position(self):
        # Wavelink reports its last mark while paused, and that mark can be the start.
        if self._paused and getattr(self, "_paused_mark", False):
            return getattr(self, "_last_position", 0) or 0
        return self._position

    @property
    def paused(self):
        return self._paused

def _ask():
    interaction = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.defer = AsyncMock()

    async def edit_message(*args, **kwargs):
        interaction.response.is_done.return_value = True

    interaction.response.edit_message = AsyncMock(side_effect=edit_message)
    interaction.edit_original_response = AsyncMock()
    interaction.delete_original_response = AsyncMock()
    interaction.original_response = AsyncMock()
    interaction.send = AsyncMock()
    interaction.user = MagicMock()
    interaction.guild = MagicMock()
    interaction.guild.id = 31
    return interaction

def _hearing(guild):
    """Attach a song so opening the player is allowed."""
    channel = MagicMock()
    channel.id = 19
    voice = _Player(channel)
    voice._track = _named("Song")
    voice._guild = guild
    guild.voice_client = voice
    return voice


def _named(title: str, author: str = "") -> MagicMock:
    track = MagicMock()
    track.title = title
    track.author = author
    return track

def _defer_marks_done(interaction) -> None:
    async def defer():
        interaction.response.is_done.return_value = True

    interaction.response.defer.side_effect = defer

def _press(player):
    interaction = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.user.voice.channel = player.channel
    return interaction

async def _playing():
    track = MagicMock()
    track.title = "I Ain't Worried"
    track.author = "OneRepublic"
    track.uri = "https://example.com/song"
    track.artwork = "https://example.com/art.jpg"
    track.length = 148_000
    channel = MagicMock()
    channel.id = 19
    player = _Player(channel)
    channel.connect = AsyncMock(return_value=player)
    member = MagicMock()
    member.id = 17
    member.voice.channel = channel
    guild = MagicMock()
    guild.id = 31
    guild.voice_client = None
    await stop_track(guild)
    assert await queue_tracks(member, guild, [track]) == "Playing I Ain't Worried."
    guild.voice_client = player
    player._track = track
    player._position = 0
    player._paused = False
    return guild, player, track

async def _cards_caught_up() -> None:
    """The queue card paints after the command returns. Tests that read it wait here."""
    for _ in range(4):
        pending = [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "music-line" and not task.done()
        ]
        if not pending:
            return
        await asyncio.gather(*pending)

def _card_text(view) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )

def _waiting(guild) -> list[str]:
    return [row.title for row in queued(guild) if not row.playing]

def _room(guild_id: int):
    guild = MagicMock()
    guild.id = guild_id
    guild.voice_client = None
    channel = MagicMock()
    player = _Player(channel)
    channel.connect = AsyncMock(return_value=player)

    async def play(track):
        player._track = track

    player.play.side_effect = play
    member = MagicMock()
    member.id = 9
    member.voice.channel = channel
    return guild, member, player

def _person(user_id: int):
    interaction = _ask()
    interaction.user.id = user_id
    interaction.guild.id = 31
    return interaction
