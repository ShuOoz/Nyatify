"""Ask each followed account in a news poll.

Fetch it, save the follow, and post what is new. A ticket follow can teach
the artist name used later for Damai. A failed fetch parks that account.
Stopping the rest of a proxy belongs to that source.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ai import AI
from fetch import FetchError, FetchedItem, poll_delay, public_fetch_message
from guild import GuildConfig
from news.feeds import Fetcher, PullResult, deliver, pull_locked
from news.follows import GuildTopic, SourceKey, remember_source
from news.source.match.common import NEXT_TAG, is_match_item, kickoff_id, match_stage, started_keys
from news.source.post import bilibili as bili_source
from news.source.post.weibo.ticket import DamaiWatch, take_artist
from news.source.registry import apply_news_cookie, feed_fault, is_match_platform, source_named

log = logging.getLogger(__name__)

Announce = Callable[[str, str, Sequence[tuple[int, str]], FetchedItem], Awaitable[int]]
SaveCookie = Callable[[dict[str, Any]], Awaitable[object]]
WarnCookie = Callable[[SourceKey, list[GuildTopic], BaseException, float], Awaitable[None]]


@dataclass
class Poll:
    """Cog pieces one account ask uses. The sets are the cog's own."""

    fetcher: Fetcher
    fetch_lock: asyncio.Lock
    bili: bili_source.BiliCookies
    guild_config: GuildConfig
    damai: DamaiWatch
    ai: AI
    ai_lock: asyncio.Lock
    ai_on: bool
    skip_until: dict[SourceKey, float]
    seen_live: set[tuple[str, str, int]]
    bili_stopped: set[str]
    weibo_stopped: set[str]
    weibo_quiet: set[str]
    announce: Announce
    save_cookie: SaveCookie
    warn_cookie: WarnCookie
    # Ticket accounts this round did not ask. The 12-hour wait stays put.
    held_tickets: set[SourceKey] = field(default_factory=set)


def fetch_failure(exc: BaseException) -> str:
    """Message plus the underlying error and response body."""
    text = str(exc).strip()
    cause = exc.__cause__
    extra = public_fetch_message(str(cause)).strip() if cause is not None else ""
    if extra and extra not in text:
        text = f"{text}: {extra}" if text else extra
    body = (getattr(exc, "body", "") or "").strip()
    if body and body not in text:
        text = f"{text} | {body}" if text else body
    return text


def is_due(poll: Poll, source: SourceKey, only: set[SourceKey] | None) -> bool:
    """In this check, and not parked by an earlier failure."""
    parked = time.monotonic() < poll.skip_until.get(source, 0.0)
    return (only is None or source in only) and not parked


def hold_tickets(
    poll: Poll, platform: str, accounts: Sequence[str], tickets: set[str],
    only: set[SourceKey] | None,
) -> None:
    """Remember ticket accounts this round did not ask."""
    for account in accounts:
        if account not in tickets:
            continue
        source = (platform, account)
        if is_due(poll, source, only):
            poll.held_tickets.add(source)


async def pause_between(gap: float, jitter: float, rng: random.Random) -> None:
    if gap > 0:
        await asyncio.sleep(poll_delay(gap, jitter, rng, min_seconds=0.0))


async def poll_list(
    poll: Poll,
    sources: list[SourceKey],
    live: dict[SourceKey, list[GuildTopic]],
    now_wall: datetime,
    *,
    gap: float,
    jitter: float,
    backfill: timedelta | None,
    only: set[SourceKey] | None,
    rng: random.Random,
) -> tuple[int, bool]:
    posted = 0
    fetched = False
    for source in sources:
        if not is_due(poll, source, only):
            continue
        if fetched:
            await pause_between(gap, jitter, rng)
        posted += await poll_source(poll, source, live[source], now_wall, backfill=backfill)
        fetched = True
    return posted, fetched


async def poll_source(
    poll: Poll,
    source: SourceKey,
    live: list[GuildTopic],
    now_wall: datetime,
    *,
    backfill: timedelta | None = None,
) -> int:
    platform, account = source
    got = await _fetch_source(poll, source, live, backfill=backfill)
    if got is None:
        return 0
    if got.name or is_match_platform(platform):
        remember_source(
            poll.guild_config, {guild_id for guild_id, _ in live}, platform, account,
            got.name, now_wall, sport=got.sport, kickoffs=got.kickoffs,
            team_id=got.team_id, schedule=got.schedule,
        )
    account_key = got.team_id or account
    live = await take_artist(
        poll.damai, poll.guild_config, platform, account_key, live,
        display=(got.name or "").strip(), certification=got.certification, bio=got.bio,
        ai=poll.ai, lock=poll.ai_lock, ai_on=poll.ai_on,
    )
    posted = await deliver(poll.fetcher, platform, account_key, live, got, poll.announce)
    if is_match_platform(platform):
        poll.seen_live |= _accepted_starts(
            poll.fetcher, platform, account, account_key, live, got.items, got.team_id,
        )
    return posted


def _accepted_starts(fetcher, platform, account, bookmark, live, items, team_id):
    """Kickoffs whose card is already in the channel. A failed send stays on the fast poll."""
    keys = started_keys(platform, account, items, team_id)
    refs = {f"{guild_id}:{topic}" for guild_id, topic in live}
    if not keys or not refs:
        return keys
    kept = set()
    for item in items:
        if not is_match_item(item) or item.occurred_at is None:
            continue
        stage = match_stage(item)
        if not stage or stage == NEXT_TAG:
            continue
        if refs <= fetcher.delivered_refs(platform, bookmark, item.item_id):
            kept.add(kickoff_id(item.occurred_at))
    return {key for key in keys if key[2] in kept}


async def _fetch_source(
    poll: Poll,
    source: SourceKey,
    live: list[GuildTopic],
    *,
    backfill: timedelta | None = None,
) -> PullResult | None:
    platform, account = source
    if platform == "bilibili":
        await poll.bili.prepare(poll.fetcher)
    try:
        follow_up = False
        while True:
            try:
                return await pull_locked(
                    poll.fetcher, platform, account, poll.fetch_lock, backfill=backfill,
                )
            except FetchError as exc:
                failed = exc
                if follow_up:
                    break
                # Refresh uses the shared session, so it stays inside the fetch lock.
                async with poll.fetch_lock:
                    follow_up = await poll.bili.on_failure(
                        platform, account, exc,
                        apply_cookie=apply_news_cookie, save_result=poll.save_cookie,
                    )
                if not follow_up:
                    break
        await skip_failed(poll, source, live, failed)
        return None
    finally:
        if platform == "bilibili":
            poll.fetcher.bili_buvid = None


async def skip_failed(
    poll: Poll,
    source: SourceKey,
    live: list[GuildTopic],
    exc: FetchError,
    *,
    proxy: str | None = None,
) -> None:
    platform, account = source
    log.error("News fetch failed for %s %s: %s", *source, fetch_failure(exc))
    fault = feed_fault(platform, exc)
    if platform == "bilibili":
        fault = poll.bili.adjust(fault)
    if fault is None:
        return
    if fault.stop_round:
        halt = getattr(source_named(platform), "halt_round", None)
        if halt is not None:
            halt(poll, account, exc, proxy=proxy)
            return
    poll.skip_until[source] = time.monotonic() + fault.skip_seconds
    if fault.cookie:
        await poll.warn_cookie(source, live, exc, fault.skip_seconds)
