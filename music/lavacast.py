"""Lavalink's playback in a Discord voice channel."""

from __future__ import annotations

import asyncio
import logging
import time

import discord
import wavelink

import music.lavalink as node

log = logging.getLogger(__name__)

_VOICE_TRIES = 3
# guild id -> (connect task, channel). A second play waits on the same attempt.
_joins: dict[int, tuple[asyncio.Task, object]] = {}
# guild id -> when we last asked Discord for a new voice server.
_voice_renew: dict[int, float] = {}


async def _use_node(player: wavelink.Player, track: object) -> None:
    wanted = node._node_for("netease") if node._netease_track(track) else node._java_node()
    current = getattr(player, "node", None)
    switch = getattr(player, "switch_node", None)
    client = getattr(player, "client", None)
    if wanted is None or current is None or switch is None:
        return
    if not await node._ensure_node(wanted, client):
        return
    if getattr(current, "identifier", None) == getattr(wanted, "identifier", None):
        return
    # switch_node would replay this song. The encode only works on its own node.
    player._current = None
    try:
        await switch(wanted)
        if getattr(player, "connected", False):
            return
    except wavelink.InvalidNodeException:
        return
    except Exception:
        log.exception("music could not switch the voice node")
    await _revive_link(player, wanted, client)


def _clear_sent(player: object) -> None:
    player._sent_voice = None
    player._sent_node = None
    player._sent_session = None


async def _send_voice(player: wavelink.Player) -> None:
    _clear_sent(player)
    player._connected = False
    dispatcher = getattr(player, "_dispatch_voice_update", None)
    if dispatcher is not None:
        await dispatcher()


async def _revive_link(player: wavelink.Player, link_node: object, client: object) -> bool:
    if not await node._revive_node(link_node, client):
        return False
    await _send_voice(player)
    return True


async def _once_more(call, fresh) -> None:
    try:
        await call()
    except wavelink.WavelinkException as exc:
        if not node._missing_session(exc) or not await fresh():
            raise
        await call()


async def renew_voice(player: object) -> None:
    # Resending the endpoint that just closed connects to a dead socket.
    guild = getattr(player, "guild", None)
    channel = getattr(player, "channel", None)
    change = getattr(guild, "change_voice_state", None)
    if player is None or guild is None or channel is None or change is None:
        return
    guild_id = getattr(guild, "id", None)
    now = time.monotonic()
    if isinstance(guild_id, int) and now - _voice_renew.get(guild_id, 0) < 8:
        return
    if isinstance(guild_id, int):
        _voice_renew[guild_id] = now
    _clear_sent(player)
    try:
        await change(channel=channel, self_mute=False, self_deaf=True)
    except Exception:
        log.exception("music could not renew the voice link")


async def restore_player(player: wavelink.Player) -> bool:
    link_node = getattr(player, "node", None)
    if not await node._ensure_node(link_node, getattr(player, "client", None)):
        return False
    session = getattr(link_node, "session_id", None)
    if (
        getattr(player, "_connected", False)
        and getattr(player, "_sent_session", None) == session
        and node._session_ready(link_node)
    ):
        return True
    await _send_voice(player)
    return bool(getattr(player, "_connected", False))


class GuildPlayer(wavelink.Player):
    def __init__(self, client=discord.utils.MISSING, channel=discord.utils.MISSING, *, nodes=None):
        if nodes is None:
            home = node._java_node()
            nodes = [home] if home is not None else None
        super().__init__(client, channel, nodes=nodes)

    async def connect(
        self,
        *,
        timeout: float = 10.0,
        reconnect: bool,
        self_deaf: bool = False,
        self_mute: bool = False,
    ) -> None:
        # Discord drops the bot when the link is late. A cancelled wait is not a timeout.
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
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise asyncio.CancelledError from exc
                if self._connection_event.is_set():
                    self._connected = True
                    return
                log.warning("Voice link was not ready (attempt %s)", attempt + 1)
                if attempt + 1 == _VOICE_TRIES:
                    raise

    async def on_voice_state_update(self, data) -> None:
        channel_id = data.get("channel_id")
        # Discord clears the channel before the link is ready. Destroying the player quits the room.
        if not channel_id:
            self._sent_voice = None
            self._sent_node = None
            if not self._connection_event.is_set():
                log.warning("Discord cleared the voice channel before the link was ready")
                return
        previous = getattr(self, "channel", None)
        await super().on_voice_state_update(data)
        # A cache miss clears the channel, so everyone already in the room looks outside it.
        if channel_id and getattr(self, "channel", None) is None:
            self.channel = _kept_channel(self, previous, channel_id)
        if channel_id:
            await self._dispatch_voice_update()

    def _voice_link(self) -> dict | None:
        voice = self._voice_state["voice"]
        session_id = voice.get("session_id")
        token = voice.get("token")
        endpoint = voice.get("endpoint")
        channel_id = self._voice_state.get("channel_id")
        if not session_id or not token or not endpoint:
            return None
        if isinstance(endpoint, str):
            endpoint = endpoint.removeprefix("wss://").removeprefix("ws://")
        return {
            "sessionId": session_id, "token": token,
            "endpoint": endpoint, "channelId": channel_id,
        }

    def _lose_link(self) -> None:
        # Wavelink would leave the channel. Stay, and let the next attempt send the link.
        self._connected = False
        _clear_sent(self)

    async def _dispatch_voice_update(self) -> None:
        assert self.guild is not None
        # The state update and the server update arrive as separate tasks. A second copy plays the song fast.
        if getattr(self, "_voice_send", None) is None:
            self._voice_send = asyncio.Lock()
        async with self._voice_send:
            link = self._voice_link()
            if link is None:
                voice = self._voice_state["voice"]
                log.info(
                    "Voice link is waiting (session=%s token=%s endpoint=%s channel=%s)",
                    bool(voice.get("session_id")), bool(voice.get("token")),
                    bool(voice.get("endpoint")), bool(self._voice_state.get("channel_id")),
                )
                return
            session = getattr(self.node, "session_id", None)
            if (
                link == getattr(self, "_sent_voice", None)
                and getattr(self, "_sent_node", None) is self.node
                and isinstance(session, str)
                and session == getattr(self, "_sent_session", None)
                and node._session_ready(self.node)
            ):
                self._connected = True
                self._connection_event.set()
                return
            if not await self._ensure_session():
                self._lose_link()
                log.warning("Voice link has Discord details but Lavalink has no session")
                return
            try:
                # A restarted Lavalink forgets the session while the socket still looks connected.
                await _once_more(
                    lambda: self.node._update_player(self.guild.id, data={"voice": link}),
                    self._open_fresh_session,
                )
            except wavelink.WavelinkException:
                self._lose_link()
                log.exception("Lavalink rejected the voice link")
                return
            self._connected = True
            self._connection_event.set()
            self._sent_voice = link
            self._sent_node = self.node
            self._sent_session = getattr(self.node, "session_id", None)
            log.info("Voice link is ready")

    async def _ensure_session(self) -> bool:
        return await node._ensure_node(self.node, self.client)

    async def _open_fresh_session(self) -> bool:
        node._down(self.node)
        return await self._ensure_session()


def _snowflake(raw: object) -> int | None:
    # A bool is an int in Python, and it is not a Discord id.
    if isinstance(raw, str) and raw.isdecimal():
        raw = int(raw)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw


def _channel_num(value: object | None) -> int | None:
    return _snowflake(getattr(value, "id", None))


def _state_channel_id(player: object | None) -> int | None:
    state = getattr(player, "_voice_state", None)
    return _snowflake(state.get("channel_id")) if isinstance(state, dict) else None


def _kept_channel(player: object, previous: object, channel_id: object) -> object | None:
    wanted = _snowflake(channel_id)
    if wanted is None:
        return None
    if _channel_num(previous) == wanted:
        return previous
    guild = getattr(player, "guild", None)
    found = getattr(guild, "get_channel", lambda _id: None)(wanted)
    return found if _channel_num(found) == wanted else None


def _same_room(player: wavelink.Player, channel: object) -> wavelink.Player | str:
    landed, wanted = _channel_num(getattr(player, "channel", None)), _channel_num(channel)
    if None not in (landed, wanted) and landed != wanted:
        return "Join the voice channel first."
    return player


def _pending(guild: object | None) -> tuple[asyncio.Task, object] | None:
    pair = _joins.get(gid) if (gid := _channel_num(guild)) is not None else None
    return pair if pair is not None and not pair[0].done() else None


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


def _replaced(player: wavelink.Player) -> bool:
    if isinstance(player, GuildPlayer):
        # A node defines equality and is not hashable, so a set of them raises.
        ours = [item for item in (node._java_node(), node._named_node(node.NETEASE_NODE)) if item is not None]
        return bool(ours) and all(player.node is not item for item in ours)
    # A reload leaves the old class. It has lived in lavalink and in music.voice.
    home = player.__class__.__module__
    return home == GuildPlayer.__module__ or home in ("music.lavalink", "music.voice")


async def _enter(channel: object) -> wavelink.Player:
    return await channel.connect(cls=GuildPlayer, self_deaf=True)  # type: ignore[attr-defined]


async def _rejoin(player: object, channel: object) -> wavelink.Player:
    try:
        await player.disconnect()  # type: ignore[attr-defined]
    except Exception:
        log.warning("music could not drop the old voice link", exc_info=True)
    return await _enter(channel)


def _start_join(guild: object, channel: object) -> asyncio.Task:
    task = asyncio.create_task(_enter(channel), name="music-join")
    guild_id = _channel_num(guild)
    if guild_id is not None:
        _joins[guild_id] = (task, channel)
        task.add_done_callback(lambda done, gid=guild_id: _forget_join(gid, done))
    return task


async def _adopt(player: wavelink.Player, channel: object) -> wavelink.Player:
    home = node._java_node()
    if isinstance(player, GuildPlayer) and home is not None and player.node is not home:
        try:
            await player.switch_node(home)
            return player
        except Exception:
            log.warning("music could not keep the voice link", exc_info=True)
    return await _rejoin(player, channel)


def _seen(guild: object, result: wavelink.Player | str) -> wavelink.Player | str:
    if not isinstance(result, str):
        from music.playback.remember import note_bot_in
        note_bot_in(guild)
    return result


async def _join(guild: object, channel: object) -> wavelink.Player | str:
    try:
        # discord.py publishes the voice client before the link is ready.
        inflight = joining(guild)
        if inflight is not None:
            return _seen(guild, _same_room(await inflight, channel))
        player = getattr(guild, "voice_client", None)
        if not isinstance(player, wavelink.Player):
            if player is not None:
                return "I am already in a voice channel."
            return _seen(guild, _same_room(await _start_join(guild, channel), channel))
        if _replaced(player):
            return _seen(guild, await _adopt(player, channel))
        here = getattr(player, "channel", None)
        linked = _channel_num(here) or _state_channel_id(player)
        wanted = _channel_num(channel)
        if here != channel and (linked is None or linked != wanted):
            await player.move_to(channel)
        elif isinstance(player, GuildPlayer) and not player._connection_event.is_set():
            await player.connect(timeout=24, reconnect=True, self_deaf=True)
        return _seen(guild, player)
    except (discord.ClientException, discord.Forbidden, wavelink.WavelinkException):
        log.exception("Could not join the voice channel")
        return "I could not join that voice channel."


async def _call_play(player: wavelink.Player, track: object, placed: dict) -> None:
    link_node = getattr(player, "node", None)
    original = getattr(link_node, "_update_player", None)
    if link_node is None or original is None:
        await player.play(track, **placed)
        return

    async def update(guild_id, /, *, data, replace=False):
        if isinstance(data, dict) and "track" in data:
            data = node._open_play(data)
        return await original(guild_id, data=data, replace=replace)

    link_node._update_player = update
    try:
        await player.play(track, **placed)
    finally:
        if getattr(link_node, "_update_player", None) is update:
            link_node._update_player = original


async def _play_track(player: wavelink.Player, track: object, *, start: int = 0) -> wavelink.Player:
    await node._use_node(player, track)
    # A paused player would keep the next song quiet. This play is the unpause.
    placed: dict = {"paused": False} if start > 0 or getattr(player, "paused", None) is True else {}
    if start > 0:
        placed["start"] = start
    try:
        # The new session does not have this player until the voice link arrives.
        await _once_more(lambda: _call_play(player, track, placed), lambda: restore_player(player))
    except wavelink.LavalinkException as exc:
        if node._missing_session(exc):
            room = getattr(player, "channel", None) or _kept_channel(player, None, _state_channel_id(player))
            if room is not None:
                player = await _rejoin(player, room)
                await _call_play(player, track, placed)
                return player
        link_node = getattr(player, "node", None)
        if exc.status == 500 and await _revive_link(player, link_node, getattr(player, "client", None)):
            await _call_play(player, track, placed)
            return player
        raise
    return player
