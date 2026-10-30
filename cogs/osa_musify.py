"""Play songs in a voice channel, line up the next ones, or show the player."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

import guild
from announce import defer_response, notice_text, quote_text, respond
from guild import GuildConfig
from music.lavalink import connect_node, find_songs
from music.panel import delete_panel, show_note, visible_note
from music.play import GenreList, MatchList
from music.playback import (
    advance_from_end, paused_away, leave_if_idle, leave_if_quiet, load_saved_lines,
    now_playing, queue_tracks, refresh_line, save_and_clear, save_open_lines, warm_voice,
)
from music.player.card import MusicPlayer, clear_stage, settle, stage, staged
from music.playlist.card import PlaylistCard
from music.source import NAME_SEARCH_PLATFORM, is_link, track_query

log = logging.getLogger(__name__)

REQUIRED_INTENTS = ("voice_states",)
_SILENT = discord.AllowedMentions.none()

_START_FAILED = "Something went wrong while starting the song."
_PLAYER_FAILED = "Something went wrong while opening the player."
_LIST_FAILED = "Something went wrong while showing the list."


class OsaMusify(commands.Cog):
    """Apple Music, YouTube, Spotify, SoundCloud, and NetEase."""

    music = app_commands.Group(
        name="music",
        description="Play a song in a voice channel",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot, guild_config: GuildConfig | None = None) -> None:
        self.bot = bot
        self.guild_config = guild.guild_config if guild_config is None else guild_config
        self._panels: list[MusicPlayer] = []
        # Closed with the cross. A later song stays hidden until someone asks for the player.
        self._closed: set[int] = set()
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}
        # One play card per person. One playlist card per server.
        self._browse: dict[tuple[int, int], discord.ui.LayoutView] = {}
        self._playlist_cards: dict[int, PlaylistCard] = {}

    async def cog_load(self) -> None:
        load_saved_lines()
        await connect_node(self.bot)

    async def cog_unload(self) -> None:
        save_open_lines()
        for panel in list(self._panels):
            panel.shutdown()

    def _forget_panel(self, panel: MusicPlayer) -> None:
        if panel in self._panels:
            self._panels.remove(panel)

    def _open_panel(self, guild: object | None) -> MusicPlayer | None:
        return next(self._matching(guild), None)

    def _matching(self, guild: object | None):
        guild_id = getattr(guild, "id", None)
        for panel in self._panels:
            if getattr(panel.guild, "id", None) == guild_id and not panel._closed:
                yield panel

    def _lock(self, name: str, guild: object | None) -> asyncio.Lock:
        guild_id = _record_id(guild)
        key = (name, id(guild) if guild_id is None else guild_id)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState,
    ) -> None:
        bot_id = getattr(getattr(self.bot, "user", None), "id", None)
        if getattr(member, "id", None) != bot_id or before.channel is None or after.channel is not None:
            return
        save_and_clear(member.guild)
        await self._after_leave(member.guild)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: object) -> None:
        """Five quiet minutes leave. The card goes with the bot."""
        guild = getattr(player, "guild", None)
        if await leave_if_quiet(guild):
            await self._after_leave(guild)

    async def _after_leave(self, guild: object | None) -> None:
        await refresh_line(guild)
        await self._dismiss_player(guild)

    async def _dismiss_player(self, guild: object | None) -> None:
        for panel in list(self._matching(guild)):
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
    async def play(self, interaction: discord.Interaction, search: str | None = None) -> None:
        if search is None or not search.strip():
            await self._open_discover(interaction)
        else:
            await self._search(interaction, search)

    async def _open_discover(self, interaction: discord.Interaction) -> None:
        view = GenreList(
            "spotify", self._open_from_card, self._search, interaction.guild, self.guild_config,
        )
        view.on_preview = self._open_from_card
        try:
            await _send_private(interaction, view)
        except discord.HTTPException:
            log.exception("music play failed")
            if not interaction.response.is_done():
                await respond(interaction, notice_text(_START_FAILED))
            return
        await self._keep_browse(interaction, view)

    async def _search(
        self,
        interaction: discord.Interaction,
        search: str,
        platform: str | None = None,
        *,
        replace_card: Callable[[], None] | None = None,
        new_message: bool = False,
    ) -> bool:
        """`replace_card` runs once before replacing the asking card. False leaves it up.

        `new_message` posts a new play card and leaves the message that asked alone.
        """
        await defer_response(interaction, ephemeral=True)
        chosen = platform or NAME_SEARCH_PLATFORM
        side_note = replace_card is not None or new_message
        query = track_query(search) or ""
        # A link is going to play. Join while Lavalink is still looking it up.
        cold_link = bool(
            query and is_link(query)
            and getattr(interaction.guild, "voice_client", None) is None
            and not paused_away(interaction.guild)
        )
        if cold_link:
            warm_voice(interaction.user, interaction.guild)
        replaced = False

        def replace_once() -> None:
            nonlocal replaced
            if not replaced and replace_card is not None and not new_message:
                replaced = True
                replace_card()

        try:
            found = await _caught(find_songs, interaction.guild, search, chosen)
            if isinstance(found, str):
                if not _can_switch(found, search):
                    await _speak(interaction, found, side_note=side_note)
                    return not side_note
                shown, rows, note, site = " ".join(search.split()), [], found, chosen
            elif found.link:
                return await self._start(
                    interaction, found.tracks, found.platform, replace_card=replace_once, new_message=new_message, side_note=side_note,
                )
            else:
                shown, rows, note, site = found.query, found.tracks, None, found.platform
            replace_once()
            await self._show_matches(interaction, shown, site, rows, note=note, new_message=new_message)
            return True
        except Exception:
            log.exception("music play failed")
            # The search card was already replaced, so the failure has to use this reply.
            stayed = side_note and not replaced
            await _speak(interaction, _START_FAILED, side_note=stayed)
            return not stayed
        finally:
            # A failed lookup must not leave the early join sitting in voice.
            if cold_link:
                asyncio.create_task(leave_if_idle(interaction.guild), name="music-idle")

    async def _show_matches(
        self,
        interaction: discord.Interaction,
        query: str,
        platform: str,
        tracks: list,
        note: str | None = None,
        new_message: bool = False,
    ) -> None:
        try:
            view = MatchList(
                query, platform, tracks, self._open_from_card, _record_id(interaction.user),
                on_queue=self._show_playlist, on_playing=self._redraw_player, note=note,
                on_search=self._search, on_player=self._reopen_from_card,
            )
            view.on_preview = self._open_from_card
            await _send_private(interaction, view, new_message=new_message, edit=not new_message)
        except Exception:
            log.exception("music play failed")
            await _speak(interaction, _START_FAILED, side_note=new_message)
            return
        await self._keep_browse(interaction, view)

    async def _keep_browse(self, interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
        if getattr(view, "message", None) is None:
            await _remember(interaction, view)
        await self._keep_one(self._browse, view, _person_key(interaction))

    async def _drop_browse(self, interaction: discord.Interaction) -> None:
        key = _person_key(interaction)
        prior = None if key is None else self._browse.pop(key, None)
        if prior is None or prior.is_finished():
            return
        prior.stop()
        await delete_panel(getattr(prior, "message", None))

    async def _keep_one(self, cards: dict, view: discord.ui.LayoutView, key: object) -> None:
        if key is None:
            return
        prior = cards.get(key)
        cards[key] = view
        if prior is None or prior is view:
            return
        message = getattr(prior, "message", None)
        current = getattr(view, "message", None)
        left_id = getattr(message, "id", None)
        same = message is not None and current is not None and (
            message is current
            or (type(left_id) is int and left_id == getattr(current, "id", None))
        )
        # Search again edits this message. Stopping the old card drops its menu.
        if not prior.is_finished():
            prior.stop()
        if not same:
            await delete_panel(message)
            return
        if view.is_finished():
            return
        state = getattr(message, "_state", None)
        store = getattr(state, "store_view", None)
        message_id = getattr(message, "id", None)
        if store is None or type(message_id) is not int:
            return
        store(view, message_id)

    async def _start(
        self,
        interaction: discord.Interaction,
        tracks: list,
        platform: str,
        *,
        replace_card: Callable[[], None] | None = None,
        new_message: bool = False,
        side_note: bool = False,
    ) -> bool:
        replied = interaction.response.is_done()
        status = opening = None
        if tracks:
            joined = getattr(interaction.guild, "voice_client", None) is not None
            if replied and not new_message and not side_note and not joined:
                title = getattr(tracks[0], "title", None)
                name = " ".join(title.split()) if isinstance(title, str) and title.strip() else "Unknown"
                status = asyncio.create_task(
                    show_note(interaction, f"Starting {name}", quote=True), name="music-starting",
                )
            if (
                not _on_now(interaction.guild)
                and not self._is_closed(interaction.guild)
                and not paused_away(interaction.guild)
            ):
                warm_voice(interaction.user, interaction.guild)
                stage(interaction.guild, tracks[0])
                opening = asyncio.create_task(
                    self._show_player(interaction, spare_reply=True), name="music-player-soon",
                )
        note = await _caught(
            queue_tracks, interaction.user, interaction.guild, tracks, platform,
        )
        await _await_logged(status, "music could not show the starting line")
        await _await_logged(opening, "music player could not open early")
        playing = note.startswith("Playing ")
        kept = False
        if opening is not None and (playing or replied):
            kept = await settle(interaction.guild)
        elif opening is not None:
            # The early card used this reply. Drop it so the error can take the message.
            clear_stage(interaction.guild)
            panel = self._open_panel(interaction.guild)
            if panel is not None:
                panel._coming = None
                panel.shutdown()
        if not playing:
            await _speak(interaction, note, side_note=side_note)
            return not side_note
        if new_message:
            await self._drop_browse(interaction)
        if replace_card is not None:
            replace_card()
        done = interaction.response.is_done()
        if kept:
            # The card posted while the song was starting is already the player.
            if done and replied:
                await _speak(interaction, note, side_note=new_message)
            return True
        player = self._show_player(interaction, spare_reply=done or new_message)
        if done:
            await asyncio.gather(player, _speak(interaction, note, side_note=new_message))
        else:
            await player
        return True

    @music.command(name="player", description="Show the music player")
    async def player(self, interaction: discord.Interaction) -> None:
        if _on_now(interaction.guild):
            await self._show_player(interaction, reopen=True)
        else:
            await respond(interaction, notice_text("Nothing is playing."))

    async def _redraw_player(self, interaction: discord.Interaction) -> None:
        await self._show_player(interaction, redraw=True)

    async def _open_from_card(self, interaction: discord.Interaction) -> None:
        await self._show_player(interaction, spare_reply=True)

    async def _reopen_from_card(self, interaction: discord.Interaction) -> None:
        await self._show_player(interaction, spare_reply=True, reopen=True)

    def _mark_closed(self, panel: MusicPlayer) -> None:
        self._set_closed(panel.guild, True)

    def _is_closed(self, guild: object | None) -> bool:
        return _record_id(guild) in self._closed

    def _mark_open(self, guild: object | None) -> None:
        self._set_closed(guild, False)

    def _set_closed(self, guild: object | None, on: bool) -> None:
        guild_id = _record_id(guild)
        if guild_id is not None:
            (self._closed.add if on else self._closed.discard)(guild_id)

    async def _show_player(
        self,
        interaction: discord.Interaction,
        *,
        redraw: bool = False,
        spare_reply: bool = False,
        reopen: bool = False,
    ) -> None:
        async with self._lock("player", interaction.guild):
            if reopen:
                self._mark_open(interaction.guild)
            elif self._is_closed(interaction.guild):
                if not interaction.response.is_done():
                    await interaction.response.defer()
                return
            if not _on_now(interaction.guild) and staged(interaction.guild) is None:
                if not interaction.response.is_done():
                    await respond(interaction, notice_text("Nothing is playing."))
                return
            prior = self._open_panel(interaction.guild)
            if redraw and prior is not None:
                await prior.redraw()
                return
            # Send the new card first. A failed send leaves the player already in the channel.
            panel = MusicPlayer(
                interaction.guild, discard=self._forget_panel,
                conceal=self._mark_closed, store=self.guild_config,
            )
            posted = await _post_panel(interaction, panel)
            if posted is None:
                panel.shutdown()
                await _player_error(interaction, spare_reply)
                return
            if prior is not None:
                await prior.dismiss()
            self._panels.append(panel)

    @music.command(name="playlist", description="Show the queue, playlists, and songs already played")
    async def playlist(self, interaction: discord.Interaction) -> None:
        await self._show_playlist(interaction, replace=True)

    async def _show_playlist(self, interaction: discord.Interaction, *, replace: bool = False) -> None:
        await self._show_list(
            interaction,
            PlaylistCard(
                interaction.guild, self.guild_config, self._open_from_card,
                on_search=self._search, on_playing=self._redraw_player,
            ),
            replace=replace,
        )

    async def _show_list(
        self,
        interaction: discord.Interaction,
        view: discord.ui.LayoutView,
        *,
        replace: bool = False,
    ) -> None:
        # Ack before the lock so a second command does not expire while the first card is sent.
        if replace and not interaction.response.is_done():
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                await _list_failed(interaction)
                return
        async with self._lock("playlist", interaction.guild):
            try:
                edit = replace or interaction.response.is_done()
                send = interaction.edit_original_response if edit else interaction.response.send_message
                await send(view=view, allowed_mentions=_SILENT)
            except discord.HTTPException:
                await _list_failed(interaction)
                return
            await _remember(interaction, view)
            await self._keep_one(self._playlist_cards, view, _record_id(interaction.guild))


async def _send_private(
    interaction: discord.Interaction,
    view: discord.ui.LayoutView,
    *,
    new_message: bool = False,
    edit: bool = False,
) -> None:
    payload = {"view": view, "allowed_mentions": _SILENT}
    if new_message:
        view.message = await interaction.followup.send(**payload, ephemeral=True, wait=True)
    elif edit:
        await interaction.edit_original_response(content=None, **payload)
    else:
        await interaction.response.send_message(**payload, ephemeral=True)


async def _caught(call: Callable[..., Awaitable[object]], /, *args: object):
    try:
        return await call(*args)
    except Exception:
        log.exception("music play failed")
        return _START_FAILED


async def _post_panel(interaction: discord.Interaction, panel: MusicPlayer) -> discord.Message | None:
    try:
        done = interaction.response.is_done()
        channel = interaction.channel
        if done and channel is None:
            log.warning("music player has no channel")
            return None
        upload = panel.take_bar()
        extra = {} if upload is None else {"file": upload}
        if done:
            posted = await channel.send(view=panel, allowed_mentions=_SILENT, **extra)
        else:
            await interaction.response.send_message(view=panel, allowed_mentions=_SILENT, **extra)
            posted = await interaction.original_response()
        panel.follow(posted)
        await panel._cite(posted)
    except discord.HTTPException:
        log.exception("music player failed")
        return None
    return posted


async def _await_logged(task: asyncio.Task | None, warning: str) -> None:
    if task is None:
        return
    try:
        await task
    except Exception:
        log.warning(warning, exc_info=True)


async def _speak(interaction: discord.Interaction, note: str, *, side_note: bool) -> None:
    shown = visible_note(note)
    if side_note and shown is not None:
        await respond(interaction, notice_text(shown), ephemeral=True)
        return
    try:
        if shown is None:
            await interaction.delete_original_response()
        else:
            await interaction.edit_original_response(
                content=quote_text(notice_text(shown)), view=None,
            )
    except discord.HTTPException:
        log.warning(
            "music play could not close the add note" if side_note else "music play could not answer",
            exc_info=True,
        )


async def _remember(interaction: discord.Interaction, view: object) -> None:
    if not hasattr(view, "message"):
        return
    try:
        message = await interaction.original_response()
    except discord.HTTPException:
        log.warning("music panel could not be tracked", exc_info=True)
        return
    follow = getattr(view, "follow", None)
    if follow is None:
        view.message = message
        return
    follow(message)


async def _player_error(interaction: discord.Interaction, spare_reply: bool) -> None:
    if spare_reply or interaction.response.is_done():
        await _speak(interaction, _PLAYER_FAILED, side_note=spare_reply)
        return
    await respond(interaction, notice_text(_PLAYER_FAILED))


async def _list_failed(interaction: discord.Interaction) -> None:
    log.exception("music playlist failed")
    if not interaction.response.is_done():
        await respond(interaction, notice_text(_LIST_FAILED))


def _can_switch(found: str, search: str) -> bool:
    """A name can try another site. A link or a dead node cannot."""
    query = track_query(search)
    if query and is_link(query):
        return False
    return found.startswith("Nothing on ") or found.endswith("did not answer.")


def _on_now(guild: object | None) -> bool:
    """A song is actually playing. An empty voice room is not enough."""
    state = now_playing(guild)
    return state is not None and state.has_track


def _person_key(interaction: discord.Interaction) -> tuple[int, int] | None:
    guild_id, user_id = _record_id(interaction.guild), _record_id(interaction.user)
    if guild_id is None or user_id is None:
        return None
    return guild_id, user_id


def _record_id(holder: object | None) -> int | None:
    """A positive snowflake. A bool is an int in Python, and it is not an id."""
    number = getattr(holder, "id", None)
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return None
    return number


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OsaMusify(bot))
