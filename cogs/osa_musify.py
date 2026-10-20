"""Play songs in a voice channel, line up the next ones, or show the player."""

from __future__ import annotations

import logging
from collections.abc import Callable

import discord
from discord import app_commands
from discord.ext import commands

from announce import defer_response, notice_text, quote_text, respond
from guild import GuildConfig, guild_config as default_guild_config
from music.commands.play import GenreList, MatchList
from music.commands.player import MusicPlayer, delete_panel
from music.commands.playlist import LiveLine, SavedPlaylist
from music.lavalink import connect_node, find_songs
from music.library import find_playlist, playlist_name
from music.playback import (
    advance_from_end,
    now_playing,
    playing_note,
    queue_tracks,
    queued,
    release,
    stop_track,
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
        self._browse: dict[tuple[int, int], discord.ui.LayoutView] = {}

    async def cog_load(self) -> None:
        await connect_node(self.bot)

    async def cog_unload(self) -> None:
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
            await self._open_genres(interaction)
            return
        await self._search(interaction, search)

    async def _open_genres(self, interaction: discord.Interaction) -> None:
        view = GenreList("spotify", self._show_player, self._search)
        try:
            await interaction.response.send_message(
                view=view,
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
    ) -> bool:
        """Show the results card. Search again passes the site already on screen.

        A card passes commit. It runs once, just before this message is replaced,
        so a failed search can leave the card up.
        """
        await defer_response(interaction)
        chosen = platform or NAME_PLATFORM
        held = False

        def _commit() -> None:
            nonlocal held
            if held or commit is None:
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
                    )
                    return True
                if commit is not None:
                    await _note_search(interaction, found)
                    return False
                await _answer(interaction, found)
                return True
            if not found.link:
                _commit()
                await self._show_matches(interaction, found.query, found.platform, found.tracks)
                return True
            return await self._start(interaction, found.tracks, found.platform, commit=_commit)
        except Exception:
            log.exception("music play failed")
            if commit is not None and not held:
                await _note_search(interaction, "Something went wrong while starting the song.")
                return False
            await _answer(interaction, "Something went wrong while starting the song.")
            return True

    async def _show_matches(
        self,
        interaction: discord.Interaction,
        query: str,
        platform: str,
        tracks: list,
        note: str | None = None,
    ) -> None:
        try:
            view = MatchList(
                query,
                platform,
                tracks,
                self._show_player,
                _user_id(interaction.user),
                on_playlist=self._show_line,
                on_playing=self._open_beside,
                note=note,
                on_search=self._search,
            )
            # The deferred reply is the thinking message. Components cannot keep that text.
            await interaction.edit_original_response(
                content=None,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("music play failed")
            await _answer(interaction, "Something went wrong while starting the song.")
            return
        await self._keep_browse(interaction, view)

    async def _keep_browse(self, interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
        """One chooser per person. Opening genres or search removes the other at once."""
        await _remember(interaction, view)
        key = _browse_key(interaction)
        if key is None:
            return
        prior = self._browse.get(key)
        self._browse[key] = view
        if prior is None or prior is view or prior.is_finished():
            return
        message = getattr(prior, "message", None)
        # Search again edits this same message. Deleting it would drop the new card.
        if _same_message(message, getattr(view, "message", None)):
            prior.stop()
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
    ) -> bool:
        try:
            line = await queue_tracks(interaction.user, interaction.guild, tracks, platform)
        except Exception:
            log.exception("music play failed")
            line = "Something went wrong while starting the song."
        if not playing_note(line):
            if commit is not None:
                await _note_search(interaction, line)
                return False
            await _answer(interaction, line)
            return True
        if commit is not None:
            commit()
        await self._show_player(interaction)
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

    async def _show_player(self, interaction: discord.Interaction, *, beside: bool = False) -> None:
        prior = self._card(interaction.guild)
        if beside and prior is not None:
            return
        if prior is not None:
            await prior.dismiss()
        panel = MusicPlayer(interaction.guild, discard=self._forget_panel)
        mentions = discord.AllowedMentions.none()
        try:
            if beside and interaction.response.is_done():
                panel.follow(await interaction.followup.send(
                    view=panel,
                    allowed_mentions=mentions,
                    wait=True,
                ))
            elif interaction.response.is_done():
                await interaction.edit_original_response(
                    content=None, view=panel, allowed_mentions=mentions,
                )
                panel.follow(await interaction.original_response())
            else:
                await interaction.response.send_message(view=panel, allowed_mentions=mentions)
                panel.follow(await interaction.original_response())
        except discord.HTTPException:
            log.exception("music player failed")
            panel.shutdown()
            if interaction.response.is_done():
                await _answer(interaction, "Something went wrong while opening the player.")
            else:
                await respond(
                    interaction,
                    notice_text("Something went wrong while opening the player."),
                )
            return
        self._panels.append(panel)

    @music.command(name="playlist", description="Show the live line, or a saved playlist")
    @app_commands.describe(name="Leave this empty to see the live line")
    async def playlist(self, interaction: discord.Interaction, name: str | None = None) -> None:
        label = playlist_name(name or "")
        if label is None:
            await self._show_line(interaction)
            return
        guild_id = _guild_num(interaction.guild)
        if guild_id is None:
            await respond(interaction, notice_text("Use this command in a server."))
            return
        saved = find_playlist(self.guild_config, guild_id, label)
        if saved is None:
            await respond(interaction, notice_text(f"No playlist named {label}."))
            return
        await self._show_list(interaction, SavedPlaylist(saved, self._show_player))

    async def _show_line(self, interaction: discord.Interaction) -> None:
        rows = queued(interaction.guild)
        if not rows:
            note = "Nothing is playing." if rows is None else "Nothing is after this song."
            await respond(interaction, notice_text(note))
            return
        await self._show_list(interaction, LiveLine(interaction.guild, self.guild_config))

    async def _show_list(self, interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
        try:
            await interaction.response.send_message(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("music playlist failed")
            if not interaction.response.is_done():
                await respond(interaction, notice_text("Something went wrong while showing the list."))
            return
        await _remember(interaction, view)

    @music.command(name="stop", description="Stop the music and leave the voice channel")
    async def stop(self, interaction: discord.Interaction) -> None:
        await defer_response(interaction)
        card = self._card(interaction.guild)
        if card is not None:
            await card.dismiss()
        try:
            line = await stop_track(interaction.guild)
        except Exception:
            log.exception("music stop failed")
            line = "Something went wrong while stopping the song."
        await _answer(interaction, line)


async def _remember(interaction: discord.Interaction, view: object) -> None:
    """Keep the sent card so it can be deleted when the view expires."""
    if not hasattr(view, "message"):
        return
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        log.warning("music panel could not be tracked", exc_info=True)


def _same_message(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    if left is right:
        return True
    a = getattr(left, "id", None)
    b = getattr(right, "id", None)
    return type(a) is int and a == b


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


def _browse_key(interaction: discord.Interaction) -> tuple[int, int] | None:
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


def _guild_num(guild: object | None) -> int | None:
    value = getattr(guild, "id", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OsaMusify(bot))
