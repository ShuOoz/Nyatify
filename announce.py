"""Shared announcement delivery for every notify function.

Cogs should subclass NotifyCog and call send_announcement; they must not look
up text channels themselves. Each function chooses how long a message lives;
this module does the deleting.

Use send_announcement(..., delete_after=) for notices that must survive a restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

import discord
from discord.ext import commands, tasks

from config import BASE_DIR, write_text_atomic
from guild_config import (
    GuildConfig,
    canonical_announce_feature,
    parse_snowflake,
    register_announce_feature,
    unregister_announce_feature,
)

log = logging.getLogger(__name__)

DEFAULT_NOTICES_PATH = BASE_DIR / "data" / "announce_notices.json"
PURGE_INTERVAL_SECONDS = 30
# Setup / fallback hints stay this long, then delete themselves.
HINT_TTL_SECONDS = 20.0
ACCENT_COLOR = discord.Color.from_str("#FFB6C1")

HINT_SETUP = (
    "Nyatify has no announcement channel in this server.\n"
    "An admin can set one with `/announce feature` or "
    "`!announce #channel`."
)

HINT_FEATURE_GONE = (
    "The `{feature}` announcement channel is gone.\n"
    "This used the default channel.\n"
    "An admin can pick a new one with `/announce feature` or "
    "`!announce {feature} #channel`."
)

_pending_deletes: set[asyncio.Task[Any]] = set()


def _spawn(coro: Any) -> None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        return
    _pending_deletes.add(task)
    task.add_done_callback(_pending_deletes.discard)


@dataclass
class AnnounceTarget:
    channel: Any | None
    used_fallback: bool = False
    feature: str | None = None
    # True when this guild has a stored channel. False means none is set.
    # Unavailable (fetch failed, missing perms) stays configured so we do not
    # wipe the saved id or send the "please set a channel" hint.
    configured: bool = False


@dataclass
class TemporaryNotice:
    """One posted notice that should delete itself when expires_at is reached."""

    feature: str
    guild_id: int
    subject: str
    channel_id: int
    message_id: int
    expires_at: float
    message: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def extra_id(self, name: str) -> int:
        """Read a Discord snowflake stored in extra (any notify function)."""
        return parse_snowflake(self.extra.get(name)) or 0

    def key(self) -> tuple[str, int, str]:
        return (self.feature, self.guild_id, self.subject)


def _canonical_feature(feature: str) -> str:
    return canonical_announce_feature(feature) or feature


def _notice_key(feature: str, guild_id: int, subject: str) -> tuple[str, int, str]:
    return (_canonical_feature(feature), guild_id, str(subject))


class TemporaryNotices:
    """Persist and delete self-removing announcements for every notify function."""

    def __init__(
        self,
        bot: Any,
        *,
        path: Path | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.bot = bot
        self.path = path
        self._now = time.time if clock is None else clock
        self._notices: dict[tuple[str, int, str], TemporaryNotice] = {}
        self._loop_users = 0
        self._purge = tasks.loop(seconds=PURGE_INTERVAL_SECONDS)(self._purge_once)
        self._purge.before_loop(self._wait_until_ready)
        self._load()

    def __len__(self) -> int:
        return len(self._notices)

    def __iter__(self):
        return iter(self._notices.values())

    def start(self) -> None:
        self._loop_users += 1
        if self._purge.is_running():
            return
        try:
            self._purge.start()
        except RuntimeError:
            self._purge.restart()

    def stop(self, *, force: bool = False) -> None:
        if force:
            self._loop_users = 0
        else:
            self._loop_users = max(0, self._loop_users - 1)
        if self._loop_users == 0 and self._purge.is_running():
            self._purge.cancel()

    async def _wait_until_ready(self) -> None:
        wait = getattr(self.bot, "wait_until_ready", None)
        if callable(wait):
            await wait()

    async def _purge_once(self) -> None:
        try:
            await self.delete_expired()
        except Exception:
            log.exception("Temporary notice cleanup failed")

    def get(self, feature: str, guild_id: int, subject: str) -> TemporaryNotice | None:
        return self._notices.get(_notice_key(feature, guild_id, subject))

    def live(self, feature: str, guild_id: int, subject: str) -> TemporaryNotice | None:
        notice = self.get(feature, guild_id, subject)
        if notice is None or notice.expires_at <= self._now():
            return None
        return notice

    def _discard(self, notice: TemporaryNotice) -> bool:
        key = notice.key()
        if self._notices.get(key) is not notice:
            return False
        del self._notices[key]
        return True

    async def delete_now(self, notice: TemporaryNotice) -> bool:
        """Delete this notice's Discord message and drop it if the delete worked."""
        if not await self._delete_message(notice):
            return False
        if self._discard(notice):
            self.save()
        return True

    def track(
        self,
        feature: str,
        guild_id: int,
        subject: str,
        message: Any,
        *,
        ttl: float,
        extra: dict[str, Any] | None = None,
    ) -> TemporaryNotice:
        channel_id = parse_snowflake(getattr(getattr(message, "channel", None), "id", None))
        message_id = parse_snowflake(getattr(message, "id", None))
        key = _notice_key(feature, guild_id, subject)
        previous = self._notices.get(key)
        notice = TemporaryNotice(
            feature=key[0],
            guild_id=guild_id,
            subject=key[2],
            channel_id=channel_id or 0,
            message_id=message_id or 0,
            expires_at=self._now() + ttl,
            message=message,
            extra=dict(extra or {}),
        )
        self._notices[key] = notice
        self.save()
        if (
            previous is not None
            and (
                previous.message_id != notice.message_id
                or previous.channel_id != notice.channel_id
            )
        ):
            _spawn(self._delete_message(previous))
        return notice

    def forget(self, feature: str, guild_id: int, subject: str) -> None:
        if self._notices.pop(_notice_key(feature, guild_id, subject), None) is None:
            return
        self.save()

    def forget_guild(self, guild_id: int) -> None:
        self._retain(lambda notice: notice.guild_id != guild_id)

    def forget_channel(self, channel_id: int) -> None:
        """Drop notices posted in a channel that no longer exists."""
        self._retain(lambda notice: notice.channel_id != channel_id)

    def forget_missing_channels(
        self,
        channel_exists: Callable[[int, int], bool],
    ) -> None:
        """Drop notices whose (guild, channel) is known to be gone.

        channel_exists(guild_id, channel_id) should return True to keep the notice.
        """
        self._retain(
            lambda notice: channel_exists(notice.guild_id, notice.channel_id)
        )

    def drop_unavailable(self, bot: Any) -> None:
        """Drop notices whose guild or channel is no longer on this bot."""
        guilds = {
            guild.id: guild
            for guild in getattr(bot, "guilds", None) or []
            if getattr(guild, "id", None) is not None
        }

        def channel_exists(guild_id: int, channel_id: int) -> bool:
            guild = guilds.get(guild_id)
            if guild is None:
                return False
            getter = getattr(guild, "get_channel", None)
            return callable(getter) and getter(channel_id) is not None

        self.forget_missing_channels(channel_exists)

    def _retain(self, keep: Callable[[TemporaryNotice], bool]) -> None:
        kept = {
            key: notice
            for key, notice in self._notices.items()
            if keep(notice)
        }
        if len(kept) == len(self._notices):
            return
        self._notices = kept
        self.save()

    def save(self) -> None:
        if self.path is None:
            return
        payload = {
            "notices": [
                _notice_to_record(notice)
                for notice in self._notices.values()
                if notice.channel_id and notice.message_id
            ]
        }
        try:
            write_text_atomic(
                self.path,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
            )
        except OSError:
            log.exception("Could not save temporary notices to %s", self.path)

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("Could not read %s; starting with no saved notices.", self.path)
            return
        records = raw.get("notices") if isinstance(raw, dict) else None
        if not isinstance(records, list):
            return
        for item in records:
            notice = _notice_from_record(item)
            if notice is None:
                continue
            self._notices[notice.key()] = notice

    async def resolve_message(
        self, notice: TemporaryNotice, *, message_id: int | None = None
    ) -> Any | None:
        target_id = notice.message_id if message_id is None else message_id
        if message_id is None and notice.message is not None:
            return notice.message
        channel = getattr(notice.message, "channel", None)
        if channel is None:
            channel = self.bot.get_channel(notice.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(notice.channel_id)
            except discord.NotFound:
                return None
            except discord.HTTPException:
                log.warning(
                    "Could not fetch announce channel %s for a notice",
                    notice.channel_id,
                    exc_info=True,
                )
                raise
        getter = getattr(channel, "get_partial_message", None)
        if callable(getter):
            return getter(target_id)
        return None

    async def _delete_one(
        self, notice: TemporaryNotice, message_id: int
    ) -> bool:
        try:
            message = await self.resolve_message(
                notice,
                message_id=None if message_id == notice.message_id else message_id,
            )
        except discord.HTTPException:
            return False
        if message is None:
            return True
        try:
            await message.delete()
        except discord.NotFound:
            return True
        except Exception:
            log.warning(
                "Could not delete expired %s notice %s",
                notice.feature,
                message_id,
                exc_info=True,
            )
            return False
        log.info("Deleted %s notice %s", notice.feature, message_id)
        return True

    async def _delete_message(self, notice: TemporaryNotice) -> bool:
        ok = await self._delete_one(notice, notice.message_id)
        ping_id = notice.extra_id("ping_message_id")
        if ping_id and ping_id != notice.message_id:
            if not await self._delete_one(notice, ping_id):
                ok = False
        return ok

    async def delete_expired(self) -> None:
        if not self._notices:
            return
        now = self._now()
        expired = [
            notice
            for notice in self._notices.values()
            if notice.expires_at <= now
        ]
        changed = False
        for notice in expired:
            if await self._delete_message(notice) and self._discard(notice):
                changed = True
        if changed:
            self.save()


def _notice_to_record(notice: TemporaryNotice) -> dict[str, Any]:
    record: dict[str, Any] = {
        "channel_id": notice.channel_id,
        "expires_at": notice.expires_at,
        "feature": notice.feature,
        "guild_id": notice.guild_id,
        "message_id": notice.message_id,
        "subject": notice.subject,
    }
    if notice.extra:
        record["extra"] = notice.extra
    return record


def _notice_from_record(item: Any) -> TemporaryNotice | None:
    if not isinstance(item, dict):
        return None
    guild_id = parse_snowflake(item.get("guild_id"))
    channel_id = parse_snowflake(item.get("channel_id"))
    message_id = parse_snowflake(item.get("message_id"))
    try:
        expires_at = float(item["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if guild_id is None or channel_id is None or message_id is None:
        return None
    raw_feature = item.get("feature")
    subject = item.get("subject")
    if not raw_feature or subject is None:
        return None
    extra_raw = item.get("extra")
    extra: dict[str, Any] = dict(extra_raw) if isinstance(extra_raw, dict) else {}
    return TemporaryNotice(
        feature=_canonical_feature(str(raw_feature)),
        guild_id=guild_id,
        subject=str(subject),
        channel_id=channel_id,
        message_id=message_id,
        expires_at=expires_at,
        extra=extra,
    )


_notices_by_bot: WeakKeyDictionary[Any, TemporaryNotices] = WeakKeyDictionary()


def notices_for(
    bot: Any,
    *,
    path: Path | None = None,
    clock: Callable[[], float] | None = None,
) -> TemporaryNotices:
    """One TemporaryNotices store per bot, shared by every notify function."""
    try:
        return _notices_by_bot[bot]
    except (KeyError, TypeError):
        pass
    notices = TemporaryNotices(bot, path=path, clock=clock)
    try:
        _notices_by_bot[bot] = notices
    except TypeError:
        pass
    return notices


class NotifyCog(commands.Cog):
    """Shared notice-store wiring for every notify function.

    Subclass this, set `feature` (and optional aliases), then post with
    send_announcement. Guild/channel cleanup and the purge loop are shared so a
    future function such as game_news does not copy voice_notify listeners.
    """

    feature: str = ""
    feature_aliases: tuple[str, ...] = ()

    def __init__(
        self,
        bot: commands.Bot,
        *,
        notices_path: Path | None = DEFAULT_NOTICES_PATH,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.notices = notices_for(bot, path=notices_path, clock=clock)

    async def cog_load(self) -> None:
        if self.feature:
            register_announce_feature(self.feature, *self.feature_aliases)
        if self.bot.is_ready():
            await self._on_notify_ready()
        self.notices.start()

    async def cog_unload(self) -> None:
        self.notices.stop()
        if self.feature:
            unregister_announce_feature(self.feature)

    async def _on_notify_ready(self) -> None:
        """Drop notices for gone guilds/channels. Expired messages stay until
        the purge loop (or a subclass that must inspect them first) deletes them.
        """
        self.notices.drop_unavailable(self.bot)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._on_notify_ready()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.notices.forget_guild(guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        channel_id = getattr(channel, "id", None)
        if channel_id:
            self.notices.forget_channel(channel_id)


def channel_can_send(channel: Any, guild: Any) -> bool:
    if not callable(getattr(channel, "send", None)):
        return False
    me = getattr(guild, "me", None)
    permissions_for = getattr(channel, "permissions_for", None)
    if me is None or not callable(permissions_for):
        return True
    try:
        return permissions_for(me).send_messages is not False
    except Exception:
        return True


async def fetch_guild_channel(
    guild: Any, channel_id: int | None
) -> tuple[Any | None, bool]:
    """Load a channel from cache, then from Discord.

    Returns (channel, confirmed_gone). confirmed_gone is True only when
    Discord answers NotFound. Missing cache, missing send permission, and
    other HTTP errors are not treated as deleted.
    """
    if channel_id is None:
        return None, False
    channel = guild.get_channel(channel_id)
    if channel is not None:
        return channel, False
    fetch = getattr(guild, "fetch_channel", None)
    if not callable(fetch):
        return None, False
    try:
        return await fetch(channel_id), False
    except discord.NotFound:
        return None, True
    except discord.HTTPException:
        log.warning(
            "Could not fetch announce channel %s in guild %s",
            channel_id,
            getattr(guild, "id", "?"),
            exc_info=True,
        )
        return None, False


async def resolve_announce_target(
    guild: Any,
    store: GuildConfig,
    feature: str,
) -> AnnounceTarget:
    """Prefer the function channel; if Discord says it is gone, use the default."""
    canon = _canonical_feature(feature)
    override_id = store.get_feature_override_id(guild.id, feature)
    override, override_gone = await fetch_guild_channel(guild, override_id)
    if override is not None:
        return AnnounceTarget(channel=override, feature=canon, configured=True)

    if override_gone:
        store.clear_feature_announce_channel_id(guild.id, canon)
    elif override_id is not None:
        return AnnounceTarget(channel=None, feature=canon, configured=True)

    default_id = store.get_default_announce_channel_id(guild.id)
    default, default_gone = await fetch_guild_channel(guild, default_id)
    if default_gone and default_id is not None:
        store.forget_announce_channel(guild.id, default_id)

    if default is not None:
        return AnnounceTarget(
            channel=default,
            used_fallback=override_gone,
            feature=canon,
            configured=True,
        )
    if default_id is not None and not default_gone:
        return AnnounceTarget(channel=None, feature=canon, configured=True)
    return AnnounceTarget(channel=None, feature=canon, configured=False)


def find_hint_channel(guild: Any) -> Any | None:
    system = getattr(guild, "system_channel", None)
    if system is not None and channel_can_send(system, guild):
        return system
    for channel in getattr(guild, "text_channels", []) or []:
        if channel_can_send(channel, guild):
            return channel
    return None


async def _send_timed_hint(destination: Any, text: str, error: str, *args: Any) -> bool:
    try:
        await destination.send(text, delete_after=HINT_TTL_SECONDS)
    except discord.HTTPException:
        log.exception(error, *args)
        return False
    return True


async def notify_missing_announce_channel(guild: Any, store: GuildConfig) -> None:
    if not store.should_send_setup_hint(guild.id):
        return
    channel = find_hint_channel(guild)
    if channel is None:
        log.warning(
            "No announcement channel for guild %s, and no place to post a hint.",
            guild.id,
        )
        return
    if await _send_timed_hint(
        channel, HINT_SETUP, "Could not send setup hint in guild %s", guild.id
    ):
        store.mark_setup_hint_sent(guild.id)


async def notify_feature_channel_gone(
    destination: Any,
    store: GuildConfig,
    guild_id: int,
    feature: str,
) -> None:
    if not store.should_send_fallback_hint(guild_id, feature):
        return
    if await _send_timed_hint(
        destination,
        HINT_FEATURE_GONE.format(feature=feature),
        "Could not send fallback hint in guild %s",
        guild_id,
    ):
        store.mark_fallback_hint_sent(guild_id, feature)


async def send_announcement(
    guild: Any,
    store: GuildConfig,
    feature: str,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    delete_after: float | None = None,
    notice_key: str | int | None = None,
    extra: dict[str, Any] | None = None,
    notices: TemporaryNotices | None = None,
) -> Any | None:
    """Post a function's notice in this guild's announce channel.

    Handles missing channels, deleted function channels (fall back to default),
    one-time in-server hints, and send failures. Any notify cog should call this
    instead of looking up channels itself.

    Pass delete_after (seconds) and notices to delete the message later, including
    after a restart. notice_key is how that function looks the notice up again;
    extra is optional JSON-safe metadata.

    Content plus an embed is two messages: a ping (inbox text) and a silent card.
    The returned message is the card. The ping id is stored as ping_message_id.

    Returns the posted message, or None if nothing was sent.
    """
    attempted: set[int] = set()
    force_fallback_hint = False
    split = bool(content) and embed is not None
    while True:
        target = await resolve_announce_target(guild, store, feature)
        if force_fallback_hint and target.channel is not None:
            target.used_fallback = True
        if target.channel is None:
            log.warning(
                "Announce channel for guild %s (%s) is %s; skipping.",
                guild.id,
                feature,
                "unavailable" if target.configured else "not set",
            )
            if not target.configured:
                await notify_missing_announce_channel(guild, store)
            return None

        name = target.feature or feature
        if target.used_fallback:
            await notify_feature_channel_gone(
                target.channel,
                store,
                guild.id,
                name,
            )

        try:
            if split:
                ping = await target.channel.send(content=content)
                try:
                    message = await target.channel.send(embed=embed, silent=True)
                except Exception:
                    try:
                        await ping.delete()
                    except discord.HTTPException:
                        pass
                    raise
                extra = dict(extra or {})
                extra["ping_message_id"] = ping.id
            else:
                message = await target.channel.send(content=content, embed=embed)
        except discord.NotFound:
            gone_id = getattr(target.channel, "id", None)
            if gone_id is not None and gone_id not in attempted:
                attempted.add(int(gone_id))
                store.forget_announce_channel(guild.id, int(gone_id))
                log.warning(
                    "Announce channel %s is gone in guild %s; retrying %s on the default.",
                    gone_id,
                    guild.id,
                    feature,
                )
                force_fallback_hint = True
                continue
            log.exception(
                "Could not send %s announcement in guild %s",
                feature,
                guild.id,
            )
            return None
        except discord.HTTPException:
            log.exception(
                "Could not send %s announcement in guild %s",
                feature,
                guild.id,
            )
            return None
        break

    if delete_after is not None:
        if notices is None:
            raise ValueError(
                "send_announcement(..., delete_after=...) needs a TemporaryNotices store"
            )
        subject = (
            str(notice_key)
            if notice_key is not None
            else str(parse_snowflake(getattr(message, "id", None)) or 0)
        )
        notices.track(
            name,
            guild.id,
            subject,
            message,
            ttl=delete_after,
            extra=extra,
        )
    return message
