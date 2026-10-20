"""The Lavalink node, the voice link, and a search that does not join."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import discord
import wavelink
from discord.ext import commands

from config import settings
from music import source

log = logging.getLogger(__name__)


def node_is_ready() -> bool:
    try:
        wavelink.Pool.get_node()
    except wavelink.InvalidNodeException:
        return False
    return True


def offline_message() -> str:
    if not settings.lavalink_uri or not settings.lavalink_password:
        return "Set LAVALINK_URI and LAVALINK_PASSWORD in config.txt."
    return "Lavalink is not connected."


async def connect_node(bot: commands.Bot) -> None:
    uri = settings.lavalink_uri
    password = settings.lavalink_password
    if not uri or not password:
        log.info("music has no Lavalink node configured")
        return
    # One attempt at startup. The default retry loop never returns, so a dead
    # node would block startup and the slash commands would never sync.
    # After a session exists, later drops keep trying. A dead session is what
    # makes Discord remove the bot from the voice channel.
    node = wavelink.Node(uri=uri, password=password, client=bot, retries=0)
    try:
        await wavelink.Pool.connect(nodes=[node], client=bot, cache_capacity=None)
    except wavelink.WavelinkException:
        log.exception("music could not connect to Lavalink")
        return
    for _ in range(50):
        if _session_ready(node):
            node._retries = None
            return
        if getattr(node.status, "name", None) == "DISCONNECTED":
            break
        await asyncio.sleep(0.1)
    log.warning("Lavalink has no session, so a voice channel would close")


@dataclass(frozen=True)
class Found:
    query: str
    platform: str
    tracks: list[object]
    link: bool


async def find_songs(
    guild: object | None,
    song: str,
    platform: str = "youtube",
    *,
    mode: str | None = None,
) -> Found | str:
    """Search without joining. A name stays a list until someone picks a row."""
    if guild is None:
        return "Use this command in a server."
    query = source.track_query(song)
    if query is None:
        return f"This plays {source.platform_line()}."
    if not query:
        return f"Say a song name, or paste an {source.platform_line(last='or')} link."
    if source.needs_redirect(query):
        followed = await source.follow_redirect(query)
        if not followed:
            return "That link did not open."
        query = followed
    if not node_is_ready():
        return offline_message()
    key = source.platform_key(query, platform)
    label = source.platform_label(query, platform)
    kind = mode if mode in ("name", "track", "playlist", "draw", "artist") else source.request_mode(query)
    try:
        results = await source.search_track(query, key)
    except wavelink.WavelinkException:
        log.exception("%s search failed", label)
        return f"{label} did not answer."
    tracks = source.take_tracks(results, kind)
    if key == "soundcloud":
        tracks = source.drop_previews(tracks, search=kind in ("name", "draw"))
    if not tracks:
        return f"Nothing on {label} matched that."
    return Found(query, key, tracks, kind in ("track", "playlist", "artist"))


@dataclass(frozen=True)
class Opened:
    tracks: list[tuple[object, str]]
    skipped: list[str]


async def open_recordings(guild: object | None, songs: list) -> Opened | str:
    """Load each saved link. A song that does not resolve is named in skipped."""
    opened: list[tuple[object, str]] = []
    skipped: list[str] = []
    error = "Nothing on YouTube matched that."
    for song in songs:
        title = _title(song)
        url = getattr(song, "url", None)
        platform = getattr(song, "platform", "youtube")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            error = "That song has no link to play."
            skipped.append(title)
            continue
        found = await find_songs(guild, url, str(platform), mode="track")
        if isinstance(found, str):
            error = found
            skipped.append(title)
            continue
        if not found.tracks:
            label = source.platform_label("", found.platform)
            error = f"Nothing on {label} matched that."
            skipped.append(title)
            continue
        opened.append((found.tracks[0], found.platform))
    if not opened:
        return error
    return Opened(opened, skipped)


def _title(song: object) -> str:
    title = getattr(song, "title", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    return "that song"


def _missing_session(exc: BaseException) -> bool:
    return isinstance(exc, wavelink.LavalinkException) and exc.status == 404


def _session_ready(node: object) -> bool:
    session_id = getattr(node, "session_id", None)
    status = getattr(getattr(node, "status", None), "name", None)
    return isinstance(session_id, str) and bool(session_id) and status == "CONNECTED"


class GuildPlayer(wavelink.Player):
    """Stay in the voice channel while the Lavalink link is finished."""

    async def connect(
        self,
        *,
        timeout: float = 10.0,
        reconnect: bool,
        self_deaf: bool = False,
        self_mute: bool = False,
    ) -> None:
        # Discord removes the bot when the voice link is late. Ask again
        # inside the same wait. Leaving is reserved for stop and Close.
        attempts = 3
        last: wavelink.ChannelTimeoutException | None = None
        for attempt in range(attempts):
            try:
                await super().connect(
                    timeout=timeout / attempts,
                    reconnect=reconnect,
                    self_deaf=self_deaf,
                    self_mute=self_mute,
                )
            except wavelink.ChannelTimeoutException as exc:
                last = exc
                if self._connection_event.is_set():
                    self._connected = True
                    return
                log.warning("Voice link was not ready (attempt %s)", attempt + 1)
                continue
            return
        assert last is not None
        raise last

    async def on_voice_state_update(self, data) -> None:
        channel_id = data.get("channel_id")
        # A cleared channel before the link is ready is Discord dropping the
        # attempt. Destroying the player here is what quits the room.
        if not channel_id and not self._connection_event.is_set():
            log.warning("Discord cleared the voice channel before the link was ready")
            return
        await super().on_voice_state_update(data)
        if channel_id:
            await self._dispatch_voice_update()

    async def _dispatch_voice_update(self) -> None:
        assert self.guild is not None
        voice = self._voice_state["voice"]
        session_id = voice.get("session_id")
        token = voice.get("token")
        endpoint = voice.get("endpoint")
        channel_id = self._voice_state.get("channel_id")
        if not session_id or not token or not endpoint:
            log.info(
                "Voice link is waiting (session=%s token=%s endpoint=%s channel=%s)",
                bool(session_id),
                bool(token),
                bool(endpoint),
                bool(channel_id),
            )
            return
        if not await self._ensure_session():
            log.warning("Voice link has Discord details but Lavalink has no session")
            return
        if isinstance(endpoint, str):
            endpoint = endpoint.removeprefix("wss://").removeprefix("ws://")
        request = {
            "voice": {
                "sessionId": session_id,
                "token": token,
                "endpoint": endpoint,
                "channelId": channel_id,
            }
        }
        for attempt in range(2):
            try:
                await self.node._update_player(self.guild.id, data=request)
            except wavelink.WavelinkException as exc:
                # A restarted Lavalink forgets the old session but the socket
                # can still look connected. Open a new one and send the link once more.
                if attempt == 0 and _missing_session(exc) and await self._open_fresh_session():
                    continue
                # Wavelink leaves the channel when this update fails. Stay and
                # let the next attempt send the link again.
                log.exception("Lavalink rejected the voice link")
                return
            self._connected = True
            self._connection_event.set()
            log.info("Voice link is ready")
            return

    async def _open_fresh_session(self) -> bool:
        node = self.node
        socket = getattr(node, "_websocket", None)
        if socket is not None:
            task = getattr(socket, "keep_alive_task", None)
            if task is not None:
                task.cancel()
            raw = getattr(socket, "socket", None)
            if raw is not None:
                try:
                    await raw.close()
                except Exception:
                    log.warning("music could not close the old Lavalink socket", exc_info=True)
        node._session_id = None
        node._status = wavelink.NodeStatus.DISCONNECTED
        previous = node._retries
        node._retries = 0
        log.warning("Lavalink session is gone; opening a new one")
        try:
            await node._connect(client=self.client)
        except wavelink.WavelinkException:
            log.exception("Could not reconnect Lavalink")
            return False
        finally:
            node._retries = previous
        for _ in range(30):
            if _session_ready(node):
                node._retries = None
                return True
            await asyncio.sleep(0.1)
        return False

    async def _ensure_session(self) -> bool:
        node = self.node
        if _session_ready(node):
            return True
        if getattr(node.status, "name", None) != "CONNECTING":
            log.warning("Lavalink session is down; connecting again")
            try:
                await node._connect(client=self.client)
            except wavelink.WavelinkException:
                log.exception("Could not reconnect Lavalink")
                return _session_ready(node)
        for _ in range(30):
            if _session_ready(node):
                node._retries = None
                return True
            await asyncio.sleep(0.1)
        return False


def _current_node() -> wavelink.Node | None:
    try:
        return wavelink.Pool.get_node()
    except wavelink.InvalidNodeException:
        return None


def _replaced(player: wavelink.Player) -> bool:
    """A voice link left behind by a reload, or tied to a node that is gone."""
    if isinstance(player, GuildPlayer):
        node = _current_node()
        return node is not None and player.node is not node
    return player.__class__.__module__ == GuildPlayer.__module__


async def _rejoin(player: object, channel: object) -> wavelink.Player:
    """Leave and enter once, so the channel gets a voice link this process owns."""
    try:
        await player.disconnect()  # type: ignore[attr-defined]
    except Exception:
        log.warning("music could not drop the old voice link", exc_info=True)
    joined = await channel.connect(cls=GuildPlayer, self_deaf=True)  # type: ignore[attr-defined]
    return joined


async def _join(guild: object, channel: object) -> wavelink.Player | str:
    player = getattr(guild, "voice_client", None)
    try:
        if isinstance(player, wavelink.Player):
            if _replaced(player):
                return await _rejoin(player, channel)
            if player.channel != channel:
                await player.move_to(channel)
            elif isinstance(player, GuildPlayer) and not player.connected:
                await player.connect(timeout=24, reconnect=True, self_deaf=True)
            return player
        if player is not None:
            return "I am already in a voice channel."
        joined = await channel.connect(cls=GuildPlayer, self_deaf=True)
    except (discord.ClientException, discord.Forbidden, wavelink.WavelinkException):
        log.exception("Could not join the voice channel")
        return "I could not join that voice channel."
    return joined


async def _play_track(player: wavelink.Player, track: object) -> None:
    try:
        await player.play(track)
    except wavelink.LavalinkException as exc:
        channel = getattr(player, "channel", None)
        if not _missing_session(exc) or channel is None:
            raise
        # The session that owned this link is gone. Join again so Discord
        # sends a voice link the new session can use.
        fresh = await _rejoin(player, channel)
        await fresh.play(track)
