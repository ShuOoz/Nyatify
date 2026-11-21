"""The Lavalink node, and a search that does not join."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import wavelink
from discord.ext import commands

from config import settings
from music import source

log = logging.getLogger(__name__)
_MODES = {"name", "track", "playlist", "draw", "artist"}
_LINK = {"track", "playlist"}
# One attempt is long enough for the session to appear, and short enough to give up.
_SESSION_TRIES = 50
# A public node answers 429 when several Spotify links open together. The song can still play.
_RATE_TRIES = 3
_RATE_WAIT = 3
_rate_lock = asyncio.Lock()
# No song, or a paused one, for this long leaves the voice channel. An empty room does not.
IDLE_SECONDS = 5 * 60
JAVA_NODE = "lavalink"
NETEASE_NODE = "nodelink"
_opened: dict[str, object] = {}
_opening: dict[str, asyncio.Task] = {}

def _accept_nodelink_stats() -> None:
    # NodeLink reports cpu.nodelinkLoad. Wavelink requires cpu.lavalinkLoad.
    from wavelink.payloads import StatsEventCPU
    original = StatsEventCPU.__init__

    def __init__(self, data: dict) -> None:
        if "lavalinkLoad" not in data:
            data = {**data, "lavalinkLoad": float(data.get("nodelinkLoad") or 0)}
        original(self, data)

    StatsEventCPU.__init__ = __init__  # type: ignore[method-assign]

_accept_nodelink_stats()

def _current_node() -> wavelink.Node | None:
    try:
        return wavelink.Pool.get_node()
    except wavelink.InvalidNodeException:
        return None

def _named_node(name: str) -> wavelink.Node | None:
    try:
        return wavelink.Pool.get_node(name)
    except wavelink.InvalidNodeException:
        return None

def _java_node() -> wavelink.Node | None:
    return _named_node(JAVA_NODE) or _current_node()

def _netease_configured() -> bool:
    return bool(settings.nodelink_uri and settings.nodelink_password)

def _node_for(platform: str | None) -> wavelink.Node | None:
    if platform == "netease" and _netease_configured():
        return _named_node(NETEASE_NODE)
    return _java_node()

def _netease_track(track: object) -> bool:
    if getattr(track, "source", None) == "netease":
        return True
    uri = getattr(track, "uri", None)
    return isinstance(uri, str) and "music.163.com" in uri

def _status(node: object) -> str | None:
    return getattr(getattr(node, "status", None), "name", None)

def _session_ready(node: object) -> bool:
    session_id = getattr(node, "session_id", None)
    return isinstance(session_id, str) and bool(session_id) and _status(node) == "CONNECTED"

def _keep_retrying(node: object) -> None:
    # Startup uses one try so the bot is not stuck. A later drop keeps trying.
    if getattr(node, "_retries", None) == 0:
        node._retries = None

def _down(node: wavelink.Node) -> None:
    node._session_id = None
    node._status = wavelink.NodeStatus.DISCONNECTED
    # The voice link belonged to the session that just died.
    for player in list(getattr(node, "_players", {}).values()):
        player._sent_session = None

def node_is_ready() -> bool:
    node = _java_node()
    if node is None or not _session_ready(node):
        return False
    _keep_retrying(node)
    return True

def offline_message(platform: str | None = None) -> str:
    if platform == "netease" and _netease_configured():
        return "NodeLink is not connected."
    if not settings.lavalink_uri or not settings.lavalink_password:
        return "Set LAVALINK_URI and LAVALINK_PASSWORD in config.txt."
    return "Lavalink is not connected."

def _netease_level(volume: object) -> object:
    # A NetEase file sits about 3.5 dB under the YouTube master. The card still shows the slider.
    if isinstance(volume, bool) or not isinstance(volume, int):
        return volume
    return min(1000, max(0, (volume * 3) // 2))

def _lift_netease(node: object) -> None:
    original = getattr(node, "_update_player", None)
    if original is None:
        return

    async def update(guild_id, /, *, data, replace=False):
        if isinstance(data, dict) and "volume" in data:
            data = {**data, "volume": _netease_level(data["volume"])}
        return await original(guild_id, data=data, replace=replace)
    node._update_player = update

def _make_node(uri: str, password: str, bot: commands.Bot, name: str) -> wavelink.Node:
    # retries=0 because the default loop never returns, and a dead node would block startup.
    # This wait is the only reason to leave on its own. Pausing uses it too. An empty room stays.
    node = wavelink.Node(
        uri=uri, password=password, identifier=name, client=bot, retries=0,
        inactive_player_timeout=IDLE_SECONDS, inactive_channel_tokens=None,
    )
    if name == NETEASE_NODE:
        _lift_netease(node)
    return node

async def connect_node(bot: commands.Bot) -> None:
    uri = settings.lavalink_uri
    password = settings.lavalink_password
    if not uri or not password:
        log.info("music has no Lavalink node configured")
        return
    nodes = [_make_node(uri, password, bot, JAVA_NODE)]
    if _netease_configured():
        nodes.append(_make_node(settings.nodelink_uri, settings.nodelink_password, bot, NETEASE_NODE))
    try:
        await wavelink.Pool.connect(nodes=nodes, client=bot, cache_capacity=None)
    except wavelink.WavelinkException:
        log.exception("music could not connect to Lavalink")
        return
    # The first connect already returned. A miss leaves retries on, so a later drop comes back.
    if not await _wait_session(nodes[0]):
        log.warning("Lavalink has no session, so a voice channel would close")
    if len(nodes) > 1 and not await _wait_session(nodes[1]):
        log.warning("NodeLink has no session, so NetEase would not play")

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
    key = source.platform_key(query, platform)
    if key == "netease" and _netease_configured():
        node = _node_for("netease")
        if node is None or not _session_ready(node):
            return offline_message("netease")
        _keep_retrying(node)
    elif not node_is_ready():
        return offline_message()
    label = source.platform_label(query, platform)
    kind = mode if mode in _MODES else source.request_mode(query)
    results = await _search(query, key, label)
    if results is None:
        # An album or playlist the node refuses is this platform. A missed name is the node.
        if kind == "playlist":
            return f"{label} does not support playlist mode."
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

async def _search(query: str, key: str, label: str):
    node = _node_for(key)
    extra = {"node": node} if node is not None else {}
    for attempt in range(_RATE_TRIES):
        try:
            if attempt:
                async with _rate_lock:
                    await asyncio.sleep(_RATE_WAIT)
                    return await source.search_track(query, key, **extra)
            return await source.search_track(query, key, **extra)
        except wavelink.NodeException as exc:
            if exc.status != 429 or attempt + 1 == _RATE_TRIES:
                log.exception("%s search failed", label)
                return None
            log.warning("%s search was rate limited", label)
        except wavelink.WavelinkException:
            log.exception("%s search failed", label)
            return None
    return None

def _link(value: object) -> str | None:
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None

def keep_track(url: object, track: object) -> None:
    if (link := _link(url)) is not None:
        _opened[link] = track

def peek_track(url: object) -> object | None:
    return None if (link := _link(url)) is None else _opened.get(link)

def warm_saved(guild: object | None, song: object) -> None:
    url = getattr(song, "url", None)
    live = getattr(getattr(guild, "voice_client", None), "current", None)
    if isinstance(url, str) and getattr(live, "uri", None) == url:
        keep_track(url, live)
        return
    platform = getattr(song, "platform", None)
    link = _link(url)
    if link is None or link in _opened or link in _opening:
        return
    chosen = platform if isinstance(platform, str) and platform else "youtube"
    _opening[link] = asyncio.create_task(_warm(guild, link, chosen), name="music-warm-link")

async def _wait_open(task: asyncio.Task | None) -> object | None:
    if task is None:
        return None
    try:
        return await task
    except Exception:
        log.warning("music could not keep a saved song ready", exc_info=True)
        return None

async def load_link(url: str, fetch):
    link = _link(url)
    if link is None:
        return await fetch()
    task = _opening.get(link)
    if task is not None and not task.done():
        result = await _wait_open(task)
        if (found := peek_track(url)) is not None:
            return found, None
        # A miss is that load's answer. A warm-up that stored nothing can still be searched.
        if result is not None:
            return result
    _opening[link] = task = asyncio.create_task(fetch(), name="music-open-link")
    task.add_done_callback(lambda done, key=link: _opening.pop(key, None) if _opening.get(key) is done else None)
    return await task

async def ready_track(url: object) -> object | None:
    found = peek_track(url)
    if found is None:
        link = _link(url)
        await _wait_open(None if link is None else _opening.get(link))
        found = peek_track(url)
    return found

async def _warm(guild: object | None, url: str, platform: str) -> None:
    try:
        found = await find_songs(guild, url, platform, mode="track")
        if not isinstance(found, str) and found.tracks:
            keep_track(url, found.tracks[0])
    except Exception:
        log.warning("music could not open a saved song early", exc_info=True)
    finally:
        _opening.pop(url, None)

async def open_one(guild: object | None, song: object) -> tuple[tuple[object, str] | None, str, str]:
    raw_title = getattr(song, "title", None)
    title = raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else "Unknown"
    url = getattr(song, "url", None)
    platform = getattr(song, "platform", "youtube")
    if _link(url) is None:
        return None, title, "That song has no link to play."
    cached = await ready_track(url)
    if cached is not None:
        return (cached, str(platform)), title, ""

    async def fetch():
        found = await find_songs(guild, url, str(platform), mode="track")
        if isinstance(found, str) or not found.tracks:
            missed = f"Nothing on {source.platform_label('', str(platform))} matched that."
            return found if isinstance(found, str) else missed
        keep_track(url, found.tracks[0])
        return found.tracks[0], found.platform
    found = await load_link(url, fetch)
    if isinstance(found, str):
        return None, title, found
    track, opened = found
    return (track, str(opened or platform)), title, ""

async def open_recordings(guild: object | None, songs: list) -> Opened | str:
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
    # Closing the socket runs its handler, which would open a second session.
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
    # A failed attempt clears every player. Put them back.
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

async def _ensure_node(node: wavelink.Node | None, client: object | None) -> bool:
    if node is None:
        return False
    if _session_ready(node):
        _keep_retrying(node)
        return True
    if client is None:
        return False
    if _status(node) == "CONNECTING":
        return await _wait_session(node)
    log.warning("Lavalink session is down; connecting again")
    return await _revive_node(node, client)

async def _revive_node(node: wavelink.Node | None, client: object | None) -> bool:
    # A half-open tunnel still looks connected. Close it and open one new session.
    if node is None or client is None:
        return False
    await _drop_socket(node)
    _down(node)
    if await _connect_once(node, client) or _session_ready(node):
        return await _wait_session(node)
    _keep_retrying(node)
    return False

def _open_play(data: dict) -> dict:
    # Wavelink sends endTime null on every play. NodeLink treats any endTime as a seek.
    # A start at the beginning is not a seek either.
    body = {key: value for key, value in data.items() if key != "endTime"}
    if body.get("position") == 0:
        body.pop("position")
    return body

from music.lavacast import (
    GuildPlayer,
    _VOICE_TRIES,
    _adopt,
    _call_play,
    _channel_num,
    _clear_sent,
    _enter,
    _forget_join,
    _join,
    _joins,
    _kept_channel,
    _once_more,
    _pending,
    _play_track,
    _rejoin,
    _replaced,
    _revive_link,
    _same_room,
    _seen,
    _send_voice,
    _snowflake,
    _start_join,
    _state_channel_id,
    _use_node,
    _voice_renew,
    joining,
    joining_channel,
    renew_voice,
    restore_player,
)
