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

_MODES = {"name", "track", "playlist", "draw", "artist"}
_LINK = {"track", "playlist"}
# One attempt is long enough for the session to appear, and short enough to give up.
_SESSION_TRIES = 50
_VOICE_TRIES = 3
# No song, or a paused one, for this long leaves the voice channel. An empty room does not.
IDLE_SECONDS = 5 * 60
# A resumed song stays quiet until Lavalink has opened it, then this much longer,
# so the skip finishes before Discord is handed any audio.
_OPEN_WAIT = 5
_SEEK_HOLD = 1
# guild id -> (connect task, channel). A second play waits on the same attempt.
_joins: dict[int, tuple[asyncio.Task, object]] = {}


def _current_node() -> wavelink.Node | None:
    try:
        return wavelink.Pool.get_node()
    except wavelink.InvalidNodeException:
        return None


def _status(node: object) -> str | None:
    return getattr(getattr(node, "status", None), "name", None)


def _session_ready(node: object) -> bool:
    session_id = getattr(node, "session_id", None)
    return isinstance(session_id, str) and bool(session_id) and _status(node) == "CONNECTED"


def _keep_retrying(node: object) -> None:
    """Startup uses one try so the bot is not stuck. A later drop keeps trying."""
    if getattr(node, "_retries", None) == 0:
        node._retries = None


def _down(node: wavelink.Node) -> None:
    node._session_id = None
    node._status = wavelink.NodeStatus.DISCONNECTED


def node_is_ready() -> bool:
    """Search needs a live session. Once one exists, a later drop keeps trying."""
    node = _current_node()
    if node is None or not _session_ready(node):
        return False
    _keep_retrying(node)
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
    # retries=0 because the default loop never returns, and a dead node would block startup.
    # This wait is the only reason to leave on its own. Pausing uses it too. An empty room stays.
    node = wavelink.Node(
        uri=uri, password=password, client=bot, retries=0,
        inactive_player_timeout=IDLE_SECONDS, inactive_channel_tokens=None,
    )
    try:
        await wavelink.Pool.connect(nodes=[node], client=bot, cache_capacity=None)
    except wavelink.WavelinkException:
        log.exception("music could not connect to Lavalink")
        return
    # The first connect already returned. A miss leaves retries on, so a later drop comes back.
    if not await _wait_session(node):
        log.warning("Lavalink has no session, so a voice channel would close")


@dataclass(frozen=True)
class Found:
    query: str
    platform: str
    tracks: list[object]
    link: bool
    name: str | None = None


async def find_songs(
    guild: object | None,
    song: str,
    platform: str = "youtube",
    *,
    mode: str | None = None,
) -> Found | str:
    """Search without joining. A name or an artist stays a list until someone picks a row."""
    if guild is None:
        return "Use this command in a server."
    query = source.track_query(song)
    if query is None:
        return f"This plays {source.platform_line()}."
    if not query:
        return f"Say a song name, or paste an {source.platform_line(last='or')} link."
    share = source.apple_share(song)
    if source.needs_redirect(query):
        followed = await source.follow_redirect(query)
        if not followed:
            return "That link did not open."
        share = share or source.apple_share(followed)
        query = source.track_query(followed)
    if not node_is_ready():
        return offline_message()
    key = source.platform_key(query, platform)
    label = source.platform_label(query, platform)
    kind = mode if mode in _MODES else source.request_mode(query)
    try:
        results = await source.search_track(query, key)
    except wavelink.WavelinkException:
        log.exception("%s search failed", label)
        return f"{label} did not answer."
    tracks = source.take_tracks(results, kind)
    if key == "soundcloud":
        tracks = source.drop_previews(tracks)
    if not tracks:
        return f"Nothing on {label} matched that."
    if share:
        for track in tracks:
            source.remember_share(track, share)
    raw = getattr(results, "name", None) if kind == "playlist" else None
    name = (" ".join(raw.split()) or None) if isinstance(raw, str) else None
    return Found(query, key, tracks, kind in _LINK, name)


@dataclass(frozen=True)
class Opened:
    tracks: list[tuple[object, str]]
    skipped: list[str]


async def open_one(guild: object | None, song: object) -> tuple[tuple[object, str] | None, str, str]:
    """One saved link. The track keeps its platform. A miss names the song and why."""
    raw_title = getattr(song, "title", None)
    title = raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else "Unknown"
    url = getattr(song, "url", None)
    platform = getattr(song, "platform", "youtube")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None, title, "That song has no link to play."
    from music.playback.remember import keep_track, ready_track

    cached = await ready_track(url)
    if cached is not None:
        return (cached, str(platform)), title, ""
    found = await find_songs(guild, url, str(platform), mode="track")
    if isinstance(found, str):
        return None, title, found
    keep_track(url, found.tracks[0])
    return (found.tracks[0], found.platform), title, ""


async def open_recordings(guild: object | None, songs: list) -> Opened | str:
    """Load each saved link. A song that does not resolve is named in skipped."""
    opened: list[tuple[object, str]] = []
    skipped: list[str] = []
    error = "Nothing on YouTube matched that."
    for song in songs:
        track, title, reason = await open_one(guild, song)
        if track is None:
            skipped.append(title)
            error = reason or error
            continue
        opened.append(track)
    return Opened(opened, skipped) if opened else error


def _missing_session(exc: BaseException) -> bool:
    return isinstance(exc, wavelink.LavalinkException) and exc.status == 404


async def _wait_session(node: wavelink.Node) -> bool:
    """True when the session appears. A miss leaves the node retrying."""
    for _ in range(_SESSION_TRIES):
        if _session_ready(node):
            node._retries = None
            return True
        if _status(node) == "DISCONNECTED":
            break
        await asyncio.sleep(0.1)
    _keep_retrying(node)
    return False


async def _drop_socket(node: wavelink.Node) -> None:
    """Close the old socket without letting its close handler open a second session."""
    socket = getattr(node, "_websocket", None)
    if socket is None:
        return

    async def stay_closed() -> None:
        return None

    socket.connect = stay_closed
    task = getattr(socket, "keep_alive_task", None)
    if task is not None and not task.done():
        task.cancel()
        current = asyncio.current_task()
        try:
            await task
        except asyncio.CancelledError:
            if current is not None and current.cancelling():
                raise
        except Exception:
            log.warning("music could not stop the old Lavalink socket", exc_info=True)
    raw = getattr(socket, "socket", None)
    if raw is None:
        return
    try:
        await raw.close()
    except Exception:
        log.warning("music could not close the old Lavalink socket", exc_info=True)


async def _connect_once(node: wavelink.Node, client: object) -> bool:
    """One reconnect. A failed attempt clears every player; put them back."""
    players, previous = dict(node._players), node._retries
    node._retries = 0
    try:
        await node._connect(client=client)  # type: ignore[arg-type]
    except wavelink.WavelinkException:
        log.exception("Could not reconnect Lavalink")
        return False
    finally:
        node._retries = previous
        if players and not node._players:
            node._players = players
    return True


class GuildPlayer(wavelink.Player):
    """Stay in the channel until Lavalink has the link. Leaving is stop and Close."""

    async def connect(
        self,
        *,
        timeout: float = 10.0,
        reconnect: bool,
        self_deaf: bool = False,
        self_mute: bool = False,
    ) -> None:
        # Discord drops the bot when the link is late. Ask again before the wait is over.
        last: wavelink.ChannelTimeoutException | None = None
        for attempt in range(_VOICE_TRIES):
            try:
                await super().connect(
                    timeout=timeout / _VOICE_TRIES,
                    reconnect=reconnect,
                    self_deaf=self_deaf,
                    self_mute=self_mute,
                )
                return
            except wavelink.ChannelTimeoutException as exc:
                # Wavelink turns a cancelled wait into this timeout. A real timeout tries again.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise asyncio.CancelledError from exc
                last = exc
                if self._connection_event.is_set():
                    self._connected = True
                    return
                log.warning("Voice link was not ready (attempt %s)", attempt + 1)
        assert last is not None
        raise last

    async def on_voice_state_update(self, data) -> None:
        channel_id = data.get("channel_id")
        # A cleared channel before the link is ready is Discord dropping the attempt.
        # Destroying the player here is what quits the room.
        if not channel_id and not self._connection_event.is_set():
            log.warning("Discord cleared the voice channel before the link was ready")
            return
        previous = getattr(self, "channel", None)
        await super().on_voice_state_update(data)
        # Wavelink replaces the channel with a cache lookup. A miss clears it,
        # and then everyone already in the room looks like they are outside it.
        if channel_id and getattr(self, "channel", None) is None:
            self.channel = _kept_channel(self, previous, channel_id)
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
                bool(session_id), bool(token), bool(endpoint), bool(channel_id),
            )
            return
        if not await self._ensure_session():
            self._connected = False
            log.warning("Voice link has Discord details but Lavalink has no session")
            return
        if isinstance(endpoint, str):
            endpoint = endpoint.removeprefix("wss://").removeprefix("ws://")
        request = {"voice": {
            "sessionId": session_id, "token": token,
            "endpoint": endpoint, "channelId": channel_id,
        }}
        for attempt in range(2):
            try:
                await self.node._update_player(self.guild.id, data=request)
            except wavelink.WavelinkException as exc:
                # A restarted Lavalink forgets the session while the socket still looks connected.
                if attempt == 0 and _missing_session(exc) and await self._open_fresh_session():
                    continue
                # Wavelink would leave the channel. Stay, and let the next attempt send the link.
                self._connected = False
                log.exception("Lavalink rejected the voice link")
                return
            self._connected = True
            self._connection_event.set()
            log.info("Voice link is ready")
            return

    async def _ensure_session(self) -> bool:
        node = self.node
        if _session_ready(node):
            _keep_retrying(node)
            return True
        if _status(node) != "CONNECTING":
            log.warning("Lavalink session is down; connecting again")
            await _drop_socket(node)
            _down(node)
            if not await _connect_once(node, self.client) and not _session_ready(node):
                _keep_retrying(node)
                return False
        return await _wait_session(node)

    async def _open_fresh_session(self) -> bool:
        _down(self.node)
        return await self._ensure_session()

    async def _track_start(self, payload) -> None:
        await super()._track_start(payload)
        opened = getattr(self, "_resume_open", None)
        if opened is not None:
            opened.set()


def _snowflake(raw: object) -> int | None:
    """A Discord id. A bool is an int in Python, and it is not an id."""
    if isinstance(raw, str) and raw.isdecimal():
        raw = int(raw)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw


def _channel_num(value: object | None) -> int | None:
    return _snowflake(getattr(value, "id", None))


def _state_channel_id(player: object | None) -> int | None:
    """The channel id kept on the player when its channel object is gone."""
    state = getattr(player, "_voice_state", None)
    if isinstance(state, dict):
        return _snowflake(state.get("channel_id"))
    return None


def _kept_channel(player: object, previous: object, channel_id: object) -> object | None:
    """The channel to keep when the cache lookup comes back empty."""
    try:
        wanted = int(channel_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if wanted <= 0:
        return None
    if _channel_num(previous) == wanted:
        return previous
    guild = getattr(player, "guild", None)
    found = getattr(guild, "get_channel", lambda _id: None)(wanted)
    return found if _channel_num(found) == wanted else None


def _replaced(player: wavelink.Player) -> bool:
    """A player left by a reload, or tied to a node this process no longer has."""
    if isinstance(player, GuildPlayer):
        node = _current_node()
        return node is not None and player.node is not node
    return player.__class__.__module__ == GuildPlayer.__module__


def _pending(guild: object | None) -> tuple[asyncio.Task, object] | None:
    guild_id = _channel_num(guild)
    pair = None if guild_id is None else _joins.get(guild_id)
    return None if pair is None or pair[0].done() else pair


def joining(guild: object | None) -> asyncio.Task | None:
    pair = _pending(guild)
    return None if pair is None else pair[0]


def joining_channel(guild: object | None) -> object | None:
    pair = _pending(guild)
    return None if pair is None else pair[1]


def _forget_join(guild_id: int, task: asyncio.Task) -> None:
    pair = _joins.get(guild_id)
    if pair is not None and pair[0] is task:
        _joins.pop(guild_id, None)


async def _enter(channel: object) -> wavelink.Player:
    return await channel.connect(cls=GuildPlayer, self_deaf=True)  # type: ignore[attr-defined]


async def _rejoin(player: object, channel: object) -> wavelink.Player:
    try:
        await player.disconnect()  # type: ignore[attr-defined]
    except Exception:
        log.warning("music could not drop the old voice link", exc_info=True)
    return await _enter(channel)


def _same_room(player: wavelink.Player, channel: object) -> wavelink.Player | str:
    landed = _channel_num(getattr(player, "channel", None))
    wanted = _channel_num(channel)
    if landed is not None and wanted is not None and landed != wanted:
        return "Join the voice channel first."
    return player


def _start_join(guild: object, channel: object) -> asyncio.Task:
    task = asyncio.create_task(_enter(channel), name="music-join")
    guild_id = _channel_num(guild)
    if guild_id is not None:
        _joins[guild_id] = (task, channel)
        task.add_done_callback(lambda done, gid=guild_id: _forget_join(gid, done))
    return task


async def _adopt(player: wavelink.Player, channel: object) -> wavelink.Player:
    """A live player takes the current node. A player left by a reload joins again."""
    node = _current_node()
    if isinstance(player, GuildPlayer) and node is not None and player.node is not node:
        try:
            await player.switch_node(node)
            return player
        except Exception:
            log.warning("music could not keep the voice link", exc_info=True)
    return await _rejoin(player, channel)


async def _join(guild: object, channel: object) -> wavelink.Player | str:
    try:
        # discord.py publishes the voice client before the link is ready.
        inflight = joining(guild)
        if inflight is not None:
            return _same_room(await inflight, channel)
        player = getattr(guild, "voice_client", None)
        if not isinstance(player, wavelink.Player):
            if player is not None:
                return "I am already in a voice channel."
            return _same_room(await _start_join(guild, channel), channel)
        if _replaced(player):
            return await _adopt(player, channel)
        # Same room, even when the channel object was cleared and only the id remains.
        linked = _channel_num(getattr(player, "channel", None)) or _state_channel_id(player)
        wanted = _channel_num(channel)
        same = linked is not None and linked == wanted
        if not same and getattr(player, "channel", None) != channel:
            await player.move_to(channel)
        elif isinstance(player, GuildPlayer) and not player._connection_event.is_set():
            await player.connect(timeout=24, reconnect=True, self_deaf=True)
        return player
    except (discord.ClientException, discord.Forbidden, wavelink.WavelinkException):
        log.exception("Could not join the voice channel")
        return "I could not join that voice channel."


async def _play_track(player: wavelink.Player, track: object, *, start: int = 0) -> wavelink.Player:
    """Play this track. A missing session joins again, and the new player is returned.

    `start` is where the song begins, in milliseconds. A paused song comes back there,
    held quiet until Lavalink has opened it. The caller resumes it.
    """
    placed = {"start": start, "paused": True} if start > 0 else {}
    try:
        await _open(player, track, placed)
    except wavelink.LavalinkException as exc:
        channel = getattr(player, "channel", None)
        if channel is None:
            channel = _kept_channel(player, None, _state_channel_id(player))
        if not _missing_session(exc) or channel is None:
            raise
        player = await _rejoin(player, channel)
        await _open(player, track, placed)
    return player


async def _open(player: wavelink.Player, track: object, placed: dict) -> None:
    quiet = placed.get("paused") is True and isinstance(player, GuildPlayer)
    if quiet:
        player._resume_open = asyncio.Event()
    try:
        await player.play(track, **placed)
        if quiet:
            await _let_open(player)
    finally:
        if quiet:
            player._resume_open = None


async def _let_open(player: GuildPlayer) -> None:
    """Stay quiet while the skip lands. Then the caller can let Discord hear it."""
    opened = player._resume_open
    try:
        await asyncio.wait_for(opened.wait(), _OPEN_WAIT)
    except asyncio.TimeoutError:
        return
    await asyncio.sleep(_SEEK_HOLD)
