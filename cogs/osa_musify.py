"""Play songs in a voice channel, line up the next ones, or show the player."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import discord
from discord import app_commands
from discord.ext import commands

from announce import defer_response, notice_text, quote_text, respond
from guild import GuildConfig, guild_config as default_guild_config
from music.panel import delete_panel, show_note
from music.play import GenreList, MatchList
from music.player.view import MusicPlayer
from music.playlist.card import LiveLine
from music.lavalink import connect_node, find_songs
from music.playback import (
    advance_from_end,
    member_in_voice,
    now_playing,
    playing_note,
    load_saved_lines,
    queue_tracks,
    refresh_line,
    release,
    save_open_lines,
    stop_track,
    leave_if_idle,
    warm_voice,
)
from music.source import NAME_PLATFORM, is_link, track_query

log = logging.getLogger(__name__)

REQUIRED_INTENTS = ("voice_states",)


class OsaMusify(commands.Cog):
    """Apple Music, YouTube, Spotify, SoundCloud, and NetEase."""

    music = app_commands.Group(
        name="music",
        description="Play a song in a voice channel",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot, guild_config: GuildConfig | None = None) -> None:
        self.bot = bot
        self.guild_config = default_guild_config if guild_config is None else guild_config
        self._panels: list[MusicPlayer] = []
        self._player_locks: dict[int, asyncio.Lock] = {}
        # Each person has their own play card and their own playlist card.
        self._browse: dict[tuple[int, int], discord.ui.LayoutView] = {}
        self._lines: dict[tuple[int, int], LiveLine] = {}

    async def cog_load(self) -> None:
        load_saved_lines()
        await connect_node(self.bot)

    async def cog_unload(self) -> None:
        save_open_lines()
        for panel in list(self._panels):
            panel.shutdown()

    def _forget_panel(self, panel: MusicPlayer) -> None:
        try:
            self._panels.remove(panel)
        except ValueError:
            return

    def _card(self, guild: object | None) -> MusicPlayer | None:
        guild_id = getattr(guild, "id", None)
        for panel in self._panels:
            if getattr(panel.guild, "id", None) == guild_id and not panel._closed:
                return panel
        return None

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        bot_id = getattr(getattr(self.bot, "user", None), "id", None)
        if getattr(member, "id", None) != bot_id or before.channel is None or after.channel is not None:
            return
        release(member.guild)
        await refresh_line(member.guild)
        guild_id = getattr(member.guild, "id", None)
        for panel in list(self._panels):
            if getattr(panel.guild, "id", None) == guild_id and not panel._closed:
                await panel.dismiss()

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: object) -> None:
        try:
            await advance_from_end(payload)
        except Exception:
            log.exception("music could not play the next song")

    @music.command(
        name="play",
        description="Play a song or a link. Leave it empty to pick a random song",
    )
    @app_commands.describe(
        search="A song name or a link. Leave this empty to pick a random song",
    )
    async def play(
        self,
        interaction: discord.Interaction,
        search: str | None = None,
    ) -> None:
        # The bot joins when a song is chosen, so a search can start from text chat.
        # A name searches Apple Music. A link from one of the five sites starts there.
        if search is None or not search.strip():
            await self._open_discover(interaction)
            return
        await self._search(interaction, search)

    async def _open_discover(self, interaction: discord.Interaction) -> None:
        view = GenreList(
            "spotify",
            self._open_from_card,
            self._search,
            interaction.guild,
            self.guild_config,
        )
        try:
            await interaction.response.send_message(
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("music play failed")
            if not interaction.response.is_done():
                await respond(
                    interaction,
                    notice_text("Something went wrong while starting the song."),
                )
            return
        await self._keep_browse(interaction, view)

    async def _search(
        self,
        interaction: discord.Interaction,
        search: str,
        platform: str | None = None,
        *,
        commit: Callable[[], None] | None = None,
        fresh: bool = False,
    ) -> bool:
        """Show the search card. Search again passes the site already on screen.

        A card passes commit. It runs once, just before this message is replaced,
        so a failed search can leave the card up. `fresh` posts a new play card
        and leaves the message that asked for the search alone.
        """
        await defer_response(interaction, ephemeral=True)
        chosen = platform or NAME_PLATFORM
        query = track_query(search) or ""
        # A link is going to play. Join while Lavalink is still looking it up.
        cold_link = (
            bool(query)
            and is_link(query)
            and getattr(interaction.guild, "voice_client", None) is None
        )
        if cold_link:
            warm_voice(interaction.user, interaction.guild)
        held = False
        started = False

        def _commit() -> None:
            nonlocal held
            if held or commit is None or fresh:
                return
            held = True
            commit()

        try:
            try:
                found = await find_songs(interaction.guild, search, chosen)
            except Exception:
                log.exception("music play failed")
                found = "Something went wrong while starting the song."
            if isinstance(found, str):
                # An empty or unanswered name still opens the card, so another platform can be tried.
                if _can_switch(found, search):
                    _commit()
                    await self._show_matches(
                        interaction,
                        " ".join(search.split()),
                        chosen,
                        [],
                        note=found,
                        fresh=fresh,
                    )
                    return True
                if commit is not None or fresh:
                    await _note_search(interaction, found)
                    return False
                await _answer(interaction, found)
                return True
            if not found.link:
                _commit()
                await self._show_matches(
                    interaction, found.query, found.platform, found.tracks, fresh=fresh,
                )
                return True
            started = await self._start(
                interaction,
                found.tracks,
                found.platform,
                commit=_commit,
                fresh=fresh,
                card=commit is not None or fresh,
            )
            return started
        except Exception:
            log.exception("music play failed")
            if (commit is not None or fresh) and not held:
                await _note_search(interaction, "Something went wrong while starting the song.")
                return False
            await _answer(interaction, "Something went wrong while starting the song.")
            return True
        finally:
            # A failed lookup must not leave the early join sitting in voice.
            # A song that did start still has its line, so this leaves it alone.
            if cold_link:
                asyncio.create_task(leave_if_idle(interaction.guild), name="music-idle")

    async def _show_matches(
        self,
        interaction: discord.Interaction,
        query: str,
        platform: str,
        tracks: list,
        note: str | None = None,
        fresh: bool = False,
    ) -> None:
        mentions = discord.AllowedMentions.none()
        try:
            view = MatchList(
                query,
                platform,
                tracks,
                self._open_from_card,
                _user_id(interaction.user),
                on_queue=self._show_line,
                on_playing=self._open_beside,
                note=note,
                on_search=self._search,
            )
            if fresh:
                # A new message, so the play card is the latest one. The asker stays up.
                view.message = await interaction.followup.send(
                    view=view,
                    ephemeral=True,
                    allowed_mentions=mentions,
                    wait=True,
                )
            else:
                # The deferred reply is the thinking message. Components cannot keep that text.
                await interaction.edit_original_response(
                    content=None,
                    view=view,
                    allowed_mentions=mentions,
                )
        except Exception:
            log.exception("music play failed")
            if fresh:
                await _note_search(interaction, "Something went wrong while starting the song.")
                return
            await _answer(interaction, "Something went wrong while starting the song.")
            return
        await self._keep_browse(interaction, view)

    async def _keep_browse(self, interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
        """One play card for this person. A new one removes only their previous card."""
        if getattr(view, "message", None) is None:
            await _remember(interaction, view)
        await self._keep_one(self._browse, view, _person_key(interaction))

    async def _drop_browse(self, interaction: discord.Interaction) -> None:
        key = _person_key(interaction)
        if key is None:
            return
        prior = self._browse.pop(key, None)
        if prior is None or prior.is_finished():
            return
        message = getattr(prior, "message", None)
        prior.stop()
        await delete_panel(message)

    async def _keep_one(
        self,
        held: dict,
        view: discord.ui.LayoutView,
        key: object,
    ) -> None:
        if key is None:
            return
        prior = held.get(key)
        held[key] = view
        if prior is None or prior is view or prior.is_finished():
            return
        message = getattr(prior, "message", None)
        # Search again edits this same message. Deleting it would drop the new card.
        # Stopping the old card unregisters the shared menu id, so the new card
        # has to be stored again or that menu click is dropped.
        if _same_message(message, getattr(view, "message", None)):
            prior.stop()
            _store_again(view, message)
            return
        prior.stop()
        await delete_panel(message)

    async def _start(
        self,
        interaction: discord.Interaction,
        tracks: list,
        platform: str,
        *,
        commit: Callable[[], None] | None = None,
        fresh: bool = False,
        card: bool = False,
    ) -> bool:
        # The voice join is already running for a link. Say which song it is
        # while that join finishes, then replace the line with the result.
        cold = (
            not fresh
            and not card
            and bool(tracks)
            and interaction.response.is_done()
            and getattr(interaction.guild, "voice_client", None) is None
        )
        status = None
        if cold:
            title = getattr(tracks[0], "title", None)
            name = " ".join(title.split()) if isinstance(title, str) and title.strip() else "that song"
            status = asyncio.create_task(
                show_note(interaction, f"Starting {name}", quote=True),
                name="music-starting",
            )
        try:
            line = await queue_tracks(interaction.user, interaction.guild, tracks, platform)
        except Exception:
            log.exception("music play failed")
            line = "Something went wrong while starting the song."
        if status is not None:
            try:
                await status
            except Exception:
                log.warning("music could not show the starting line", exc_info=True)
        if not line.startswith("Playing "):
            # Joining the queue is not a new start. The player opens when
            # this song is what begins playback. The queue card stays closed
            # until someone asks for it. A fresh search leaves the card that
            # asked, so the note is a follow-up and that message stays up.
            # The play card stays too until a song actually starts.
            if card:
                await _note_search(interaction, line)
                return False
            await _answer(interaction, line)
            return True
        if fresh:
            await self._drop_browse(interaction)
        if commit is not None:
            commit()
        # The play reply stays private. The player is a channel message.
        # Post them together so the line is not waiting on the player message.
        pending = interaction.response.is_done()
        if pending:
            told = _note_search(interaction, line) if fresh else _answer(interaction, line)
            await asyncio.gather(self._show_player(interaction, keep=True), told)
        else:
            await self._show_player(interaction, keep=fresh)
        return True

    @music.command(name="player", description="Show the music player")
    async def player(self, interaction: discord.Interaction) -> None:
        # Still in the voice channel after a song ends is enough. A second card
        # would refresh on its own, so the open one is dismissed and posted again.
        if now_playing(interaction.guild) is None:
            await respond(interaction, notice_text("Nothing is playing."))
            return
        await self._show_player(interaction)

    async def _open_beside(self, interaction: discord.Interaction) -> None:
        """A player next to the search card. The search card stays where it is."""
        await self._show_player(interaction, beside=True)

    async def _open_from_card(self, interaction: discord.Interaction) -> None:
        """Open the player from a card that should stay on screen."""
        await self._show_player(interaction, keep=True)

    def _player_lock(self, guild: object | None) -> asyncio.Lock:
        """One open at a time per server, so two starts cannot leave two players."""
        guild_id = _guild_num(guild)
        key = guild_id if guild_id is not None else id(guild)
        lock = self._player_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._player_locks[key] = lock
        return lock

    async def _show_player(
        self,
        interaction: discord.Interaction,
        *,
        beside: bool = False,
        keep: bool = False,
    ) -> None:
        async with self._player_lock(interaction.guild):
            await self._post_player(interaction, beside=beside, keep=keep)

    async def _post_player(
        self,
        interaction: discord.Interaction,
        *,
        beside: bool,
        keep: bool,
    ) -> None:
        prior = self._card(interaction.guild)
        if beside and prior is not None:
            await prior.redraw()
            return
        # Send the new card before dismissing the old one. A failed send then
        # leaves the player that was already in the channel.
        panel = MusicPlayer(interaction.guild, discard=self._forget_panel)
        mentions = discord.AllowedMentions.none()
        try:
            if interaction.response.is_done():
                # A private play card is already using this reply. The player
                # goes to the channel, where everyone can use it.
                channel = interaction.channel
                if channel is None:
                    log.warning("music player has no channel")
                    panel.shutdown()
                    await _player_error(interaction, keep)
                    return
                panel.follow(await channel.send(view=panel, allowed_mentions=mentions))
            else:
                await interaction.response.send_message(view=panel, allowed_mentions=mentions)
                panel.follow(await interaction.original_response())
        except discord.HTTPException:
            log.exception("music player failed")
            panel.shutdown()
            await _player_error(interaction, keep)
            return
        if prior is not None:
            await prior.dismiss()
        self._panels.append(panel)

    @music.command(name="playlist", description="Show the queue, playlists, and songs already played")
    async def playlist(self, interaction: discord.Interaction) -> None:
        await self._show_line(interaction)

    async def _show_line(self, interaction: discord.Interaction) -> None:
        await self._show_list(
            interaction,
            LiveLine(
                interaction.guild,
                self.guild_config,
                self._open_from_card,
                on_search=self._search,
                on_playing=self._open_beside,
            ),
        )

    async def _show_list(self, interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
        try:
            await interaction.response.send_message(
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("music playlist failed")
            if not interaction.response.is_done():
                await respond(interaction, notice_text("Something went wrong while showing the list."))
            return
        await _remember(interaction, view)
        await self._keep_one(self._lines, view, _person_key(interaction))

    @music.command(name="stop", description="Stop the music and leave the voice channel")
    async def stop(self, interaction: discord.Interaction) -> None:
        # The player buttons already require the voice channel. Stop does too,
        # so someone outside the room cannot disconnect the bot. A bot that
        # already left has no room to join, and the parked queue stays.
        if _bot_in_voice(interaction.guild) and not member_in_voice(interaction.user, interaction.guild):
            await respond(interaction, notice_text("Join the voice channel first."))
            return
        if not _bot_in_voice(interaction.guild):
            await respond(interaction, notice_text("Nothing is playing."))
            return
        await defer_response(interaction)
        try:
            line = await stop_track(interaction.guild)
        except Exception:
            log.exception("music stop failed")
            # The player stays. The music may still be going.
            await _answer(interaction, "Something went wrong while stopping the song.")
            return
        card = self._card(interaction.guild)
        if card is not None:
            await card.dismiss()
        await _answer(interaction, line)


async def _remember(interaction: discord.Interaction, view: object) -> None:
    """Keep the sent card so it can be deleted when the view expires."""
    if not hasattr(view, "message"):
        return
    try:
        message = await interaction.original_response()
    except discord.HTTPException:
        log.warning("music panel could not be tracked", exc_info=True)
        return
    follow = getattr(view, "follow", None)
    if follow is not None:
        follow(message)
        return
    view.message = message


def _store_again(view: discord.ui.LayoutView, message: object) -> None:
    """Put this card back on the message after the card it replaced was stopped."""
    if view.is_finished():
        return
    state = getattr(message, "_state", None)
    store = getattr(state, "store_view", None)
    message_id = getattr(message, "id", None)
    if store is None or type(message_id) is not int:
        return
    store(view, message_id)


def _same_message(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    if left is right:
        return True
    a = getattr(left, "id", None)
    b = getattr(right, "id", None)
    return type(a) is int and a == b


async def _player_error(interaction: discord.Interaction, keep: bool) -> None:
    """A fresh search must not replace the card that asked with this error."""
    if keep:
        await _note_search(interaction, "Something went wrong while opening the player.")
        return
    if interaction.response.is_done():
        await _answer(interaction, "Something went wrong while opening the player.")
        return
    await respond(interaction, notice_text("Something went wrong while opening the player."))


async def _note_search(interaction: discord.Interaction, line: str) -> None:
    """Tell the person, and leave the card they already have."""
    await respond(interaction, notice_text(line), ephemeral=True)


async def _answer(interaction: discord.Interaction, line: str) -> None:
    """Replace the thinking message. A second message would leave that one up."""
    try:
        await interaction.edit_original_response(content=quote_text(notice_text(line)), view=None)
    except discord.HTTPException:
        log.warning("music play could not answer", exc_info=True)


def _can_switch(found: str, search: str) -> bool:
    """A name can try another site. A link, a dead node, and other errors cannot."""
    query = track_query(search)
    if query and is_link(query):
        return False
    return found.startswith("Nothing on ") or found.endswith("did not answer.")


def _person_key(interaction: discord.Interaction) -> tuple[int, int] | None:
    """The play card belongs to one person in one server."""
    guild_id = _guild_num(interaction.guild)
    user_id = _user_id(interaction.user)
    if guild_id is None or user_id is None:
        return None
    return guild_id, user_id


def _user_id(user: object) -> int | None:
    value = getattr(user, "id", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _bot_in_voice(guild: object | None) -> bool:
    """True when the bot is connected to a voice channel in this server."""
    player = getattr(guild, "voice_client", None) if guild is not None else None
    channel = getattr(player, "channel", None) if player is not None else None
    channel_id = getattr(channel, "id", None)
    return isinstance(channel_id, int) and not isinstance(channel_id, bool) and channel_id > 0


def _guild_num(guild: object | None) -> int | None:
    value = getattr(guild, "id", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OsaMusify(bot))
