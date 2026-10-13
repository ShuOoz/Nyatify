"""Saved announcements: timed notices and the recent-post queue.

announce.py sends a message, then asks this store to remember it.
This module does not choose a channel or send a card.
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
from discord.ext import tasks

from config import BASE_DIR, write_text_atomic
from guild import canonical_announce_feature, parse_snowflake

log = logging.getLogger(__name__)

DEFAULT_NOTICES_PATH = BASE_DIR / "data" / "announce_notices.json"
PURGE_INTERVAL_SECONDS = 30

_pending_deletes: set[asyncio.Task[Any]] = set()


def _spawn(coro: Any) -> None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        return
    _pending_deletes.add(task)
    task.add_done_callback(_pending_deletes.discard)


def _canonical_feature(feature: str) -> str:
    return canonical_announce_feature(feature) or feature


def _notice_key(feature: str, guild_id: int, subject: str) -> tuple[str, int, str]:
    return (_canonical_feature(feature), guild_id, str(subject))


@dataclass
class QueuedPost:
    """One post card kept until a newer card pushes it out of its channel."""

    guild_id: int
    channel_id: int
    message_id: int
    message: Any = None

    def record(self) -> dict[str, int]:
        return {
            "channel_id": self.channel_id,
            "guild_id": self.guild_id,
            "message_id": self.message_id,
        }

    @staticmethod
    def parse(item: Any) -> QueuedPost | None:
        if not isinstance(item, dict):
            return None
        guild_id = parse_snowflake(item.get("guild_id"))
        channel_id = parse_snowflake(item.get("channel_id"))
        message_id = parse_snowflake(item.get("message_id"))
        if guild_id is None or channel_id is None or message_id is None:
            return None
        return QueuedPost(guild_id, channel_id, message_id)


@dataclass
class TemporaryNotice:
    """One posted notice deleted when expires_at is reached."""

    feature: str
    guild_id: int
    subject: str
    channel_id: int
    message_id: int
    expires_at: float
    message: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def extra_id(self, name: str) -> int:
        return parse_snowflake(self.extra.get(name)) or 0

    def key(self) -> tuple[str, int, str]:
        return (self.feature, self.guild_id, self.subject)

    def record(self) -> dict[str, Any]:
        data = {
            "channel_id": self.channel_id,
            "expires_at": self.expires_at,
            "feature": self.feature,
            "guild_id": self.guild_id,
            "message_id": self.message_id,
            "subject": self.subject,
        }
        if self.extra:
            data["extra"] = self.extra
        return data

    @staticmethod
    def parse(item: Any) -> TemporaryNotice | None:
        if not isinstance(item, dict):
            return None
        ids = [parse_snowflake(item.get(name)) for name in ("guild_id", "channel_id", "message_id")]
        feature, subject, extra = item.get("feature"), item.get("subject"), item.get("extra")
        try:
            expires_at = float(item["expires_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if None in ids or not feature or subject is None:
            return None
        return TemporaryNotice(
            feature=_canonical_feature(str(feature)),
            guild_id=ids[0],
            subject=str(subject),
            channel_id=ids[1],
            message_id=ids[2],
            expires_at=expires_at,
            extra=dict(extra) if isinstance(extra, dict) else {},
        )


class TemporaryNotices:
    """Persist and delete self-removing announcements for every notify function.

    Timed notices expire on a clock. keep_recent remembers post cards per
    channel and deletes the oldest once a channel is past its cap.
    """

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
        self._posts: dict[int, list[QueuedPost]] = {}
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
        self._loop_users = 0 if force else max(0, self._loop_users - 1)
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
        if self._notices.get(notice.key()) is not notice:
            return False
        del self._notices[notice.key()]
        return True

    async def delete_now(self, notice: TemporaryNotice) -> bool:
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
        if previous is not None and (
            previous.message_id != notice.message_id
            or previous.channel_id != notice.channel_id
        ):
            _spawn(self._delete_message(previous))
        return notice

    def forget(self, feature: str, guild_id: int, subject: str) -> None:
        if self._notices.pop(_notice_key(feature, guild_id, subject), None) is None:
            return
        self.save()

    def post_ids(self, channel_id: int) -> tuple[int, ...]:
        return tuple(post.message_id for post in self._posts.get(channel_id, ()))

    async def keep_recent(self, guild_id: int, message: Any, keep: int) -> None:
        """Remember this message and delete older ones in its channel past keep.

        A delete that fails stays at the front, so a newer card is left up.
        The same message id is remembered once.
        """
        channel_id = parse_snowflake(getattr(getattr(message, "channel", None), "id", None))
        message_id = parse_snowflake(getattr(message, "id", None))
        if channel_id is None or message_id is None:
            return
        row = self._posts.setdefault(channel_id, [])
        if any(post.message_id == message_id for post in row):
            return
        row.append(QueuedPost(guild_id, channel_id, message_id, message))
        self.save()
        await self._trim_posts(channel_id, keep)

    def forget_guild(self, guild_id: int) -> None:
        self._retain(lambda notice: notice.guild_id != guild_id)
        self._drop_posts(lambda post: post.guild_id != guild_id)

    def forget_channel(self, channel_id: int) -> None:
        self._retain(lambda notice: notice.channel_id != channel_id)
        self._drop_posts(lambda post: post.channel_id != channel_id)

    def forget_missing_channels(
        self, channel_exists: Callable[[int, int], bool]
    ) -> None:
        self._retain(
            lambda notice: channel_exists(notice.guild_id, notice.channel_id)
        )
        self._drop_posts(
            lambda post: channel_exists(post.guild_id, post.channel_id)
        )

    def drop_unavailable(self, bot: Any) -> None:
        guilds = {
            guild.id: guild
            for guild in getattr(bot, "guilds", None) or []
            if getattr(guild, "id", None) is not None
        }

        def channel_exists(guild_id: int, channel_id: int) -> bool:
            guild = guilds.get(guild_id)
            getter = getattr(guild, "get_channel", None) if guild is not None else None
            return callable(getter) and getter(channel_id) is not None

        self.forget_missing_channels(channel_exists)

    def _retain(self, keep: Callable[[TemporaryNotice], bool]) -> None:
        kept = {
            key: notice for key, notice in self._notices.items() if keep(notice)
        }
        if len(kept) == len(self._notices):
            return
        self._notices = kept
        self.save()

    def _drop_posts(self, keep: Callable[[QueuedPost], bool]) -> None:
        kept: dict[int, list[QueuedPost]] = {}
        changed = False
        for channel_id, row in self._posts.items():
            left = [post for post in row if keep(post)]
            if len(left) != len(row):
                changed = True
            if left:
                kept[channel_id] = left
        if len(kept) != len(self._posts):
            changed = True
        if not changed:
            return
        self._posts = kept
        self.save()

    async def _trim_posts(self, channel_id: int, keep: int) -> None:
        row = self._posts.get(channel_id)
        if not row:
            return
        changed = False
        while len(row) > keep:
            if not await self._delete_post(row[0]):
                break
            row.pop(0)
            changed = True
        if not row:
            self._posts.pop(channel_id, None)
            changed = True
        if changed:
            self.save()

    async def _delete_post(self, post: QueuedPost) -> bool:
        notice = TemporaryNotice(
            feature="post",
            guild_id=post.guild_id,
            subject=str(post.message_id),
            channel_id=post.channel_id,
            message_id=post.message_id,
            expires_at=0,
            message=post.message,
        )
        return await self._delete_one(notice, post.message_id)

    def save(self) -> None:
        if self.path is None:
            return
        payload: dict[str, Any] = {
            "notices": [
                notice.record()
                for notice in self._notices.values()
                if notice.channel_id and notice.message_id
            ]
        }
        posts = [
            post.record()
            for row in self._posts.values()
            for post in row
            if post.channel_id and post.message_id
        ]
        if posts:
            payload["post_queue"] = posts
        try:
            write_text_atomic(
                self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n"
            )
        except OSError:
            log.exception("Could not save temporary notices to %s", self.path)

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception(
                "Could not read %s; starting with no saved notices.", self.path
            )
            return
        if not isinstance(raw, dict):
            return
        records = raw.get("notices")
        if isinstance(records, list):
            for item in records:
                notice = TemporaryNotice.parse(item)
                if notice is not None:
                    self._notices[notice.key()] = notice
        queued = raw.get("post_queue")
        if not isinstance(queued, list):
            return
        for item in queued:
            post = QueuedPost.parse(item)
            if post is not None:
                self._posts.setdefault(post.channel_id, []).append(post)

    async def resolve_message(
        self, notice: TemporaryNotice, *, message_id: int | None = None
    ) -> Any | None:
        target_id = notice.message_id if message_id is None else message_id
        if message_id is None and notice.message is not None:
            return notice.message
        channel = getattr(notice.message, "channel", None) or self.bot.get_channel(
            notice.channel_id
        )
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
        return getter(target_id) if callable(getter) else None

    async def _delete_one(self, notice: TemporaryNotice, message_id: int) -> bool:
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
            ok = await self._delete_one(notice, ping_id) and ok
        return ok

    async def delete_expired(self) -> None:
        now = self._now()
        expired = [
            notice for notice in list(self._notices.values()) if notice.expires_at <= now
        ]
        changed = False
        for notice in expired:
            if await self._delete_message(notice) and self._discard(notice):
                changed = True
        if changed:
            self.save()


_notices_by_bot: WeakKeyDictionary[Any, TemporaryNotices] = WeakKeyDictionary()


def notices_for(
    bot: Any, *, path: Path | None = None, clock: Callable[[], float] | None = None
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
