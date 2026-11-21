"""Leave voice when nothing is playing, or a pause has lasted."""

from __future__ import annotations

import asyncio
import logging

from music.lavalink import IDLE_SECONDS, joining
from music.playback.remember import (
    _guild_id, _hold, _keep_loaded, _stored, _voice_player, forget_bot, refresh_line,
    save_and_clear,
)
from music.playback.start import _line_holding, _on_player, _player_paused

log = logging.getLogger(__name__)

async def leave_if_idle(guild: object | None) -> None:
    """Drop a join that a failed search started, once nothing is playing."""
    if guild is None:
        return
    pending = joining(guild)
    if pending is not None:
        try:
            await pending
        except Exception:
            log.warning("music could not finish the early voice join", exc_info=True)
    async with _hold(guild):
        player, line = _voice_player(guild), _stored(guild)
        # A song on the air stays. A saved one joins again later.
        if player is None or _on_player(player) is not None or (_line_holding(line) and not line.resting):
            return
        try:
            await player.disconnect()
        except Exception:
            log.warning("music could not leave an unused voice channel", exc_info=True)
        else:
            forget_bot(guild)

# A reload runs this file again. The wait already counting has to stay.
_pause_waits: dict[int, asyncio.Task] = _keep_loaded("_pause_waits", {}, globals())
_pause_token: dict[int, int] = _keep_loaded("_pause_token", {}, globals())
_pause_ready: set[int] = _keep_loaded("_pause_ready", set(), globals())

def _arm_pause_leave(guild: object | None) -> None:
    """A paused song gets the same wait as a player with nothing on."""
    guild_id = _guild_id(guild)
    if guild_id is None:
        return
    _cancel_pause_leave(guild)
    token = _pause_token.get(guild_id, 0) + 1
    _pause_token[guild_id] = token
    _pause_waits[guild_id] = asyncio.create_task(
        _leave_after_pause(guild, guild_id, token), name="music-pause-leave",
    )

def _cancel_pause_leave(guild: object | None) -> None:
    guild_id = _guild_id(guild)
    if guild_id is None:
        return
    if guild_id in _pause_token:
        _pause_token[guild_id] += 1
    _pause_ready.discard(guild_id)
    task = _pause_waits.get(guild_id)
    # The task that is leaving must not cancel itself.
    if task is None or task is asyncio.current_task():
        return
    _pause_waits.pop(guild_id, None)
    if not task.done():
        task.cancel()

async def _wait(seconds: float) -> None:
    await asyncio.sleep(seconds)

async def _leave_after_pause(guild: object, guild_id: int, token: int) -> None:
    await _wait(IDLE_SECONDS)
    if _pause_token.get(guild_id) != token or _pause_waits.get(guild_id) is not asyncio.current_task():
        return
    _pause_waits.pop(guild_id, None)
    player = _voice_player(guild)
    if not _player_paused(player) or _on_player(player) is None:
        return
    # The quiet-player event leaves and takes the card, the same as no song.
    _pause_ready.add(guild_id)
    dispatch = getattr(getattr(player, "client", None), "dispatch", None)
    try:
        if dispatch is not None:
            dispatch("wavelink_inactive_player", player)
            return
        if await leave_if_quiet(guild):
            await refresh_line(guild)
    except Exception:
        log.exception("music could not leave after a pause")

async def leave_if_quiet(guild: object | None) -> bool:
    """Leave when nothing is on, or a pause has lasted the quiet wait."""
    if guild is None:
        return False
    async with _hold(guild):
        player = _voice_player(guild)
        song = _on_player(player)
        if player is None or (song is not None and not _player_paused(player)):
            return False
        if song is not None:
            # A newer pause replaced this wait. Stay until that one finishes.
            guild_id = _guild_id(guild)
            if guild_id not in _pause_ready:
                return False
            _pause_ready.discard(guild_id)
            line = _stored(guild)
            if line is not None:
                line.paused = True
        try:
            await player.disconnect()
        except Exception:
            log.warning("music could not leave an idle voice channel", exc_info=True)
            # Discord can drop the link before Lavalink answers. The bot is out
            # either way, so the saved song is stored instead of left half on.
            if getattr(guild, "voice_client", None) is not None:
                return False
        save_and_clear(guild)
        return True

