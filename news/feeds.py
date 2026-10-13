"""Load a followed account's feed and return items not yet bookmarked."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import settings
from fetch import BookmarkStore, FetchError, FetchedItem, GetText, get_text
from news.source.match.common import pinned_event_ids
from news.source.match.thesportsdb.clubs import (
    DEFAULT_SPORTSDB_BASE,
    DEFAULT_SPORTSDB_KEY,
    SportsdbClient,
)
from news.source.registry import (
    fetch_fields,
    hold_bookmark,
    is_hard_skip,
    is_live_start,
    is_match_platform,
    source_named,
)

log = logging.getLogger(__name__)

# A sports follow can be seeded without bookmarking live or upcoming cards.
BOOKMARK_SEEDED = "*"


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)



@dataclass
class FeedSnapshot:
    """A source fetch with no bookmarking. Check and pull both start here."""

    name: str | None
    items: list[FetchedItem]
    sport: str | None = None
    kickoffs: tuple[datetime, ...] = ()
    team_id: str = ""
    schedule: bool = True
    certification: str = ""
    bio: str = ""


@dataclass
class PullResult:
    name: str | None
    new: list[FetchedItem]
    visible: list[FetchedItem] = field(default_factory=list)
    starts: list[FetchedItem] = field(default_factory=list)
    sport: str | None = None
    kickoffs: tuple[datetime, ...] = ()
    team_id: str = ""
    items: list[FetchedItem] = field(default_factory=list)
    schedule: bool = True
    certification: str = ""
    bio: str = ""
    opening: bool = False


class Fetcher:
    def __init__(
        self,
        *,
        get_text: GetText | None = None,
        bookmarks: BookmarkStore | None = None,
        bookmarks_path: Path | None = None,
        sportsdb_base: str | None = None,
        sportsdb_key: str | None = None,
        pandascore_token: str | None = None,
        link_cache_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
        bili_cookie: str | None = None,
        bili_call: Callable[..., Awaitable[Any]] | None = None,
        bili_post: Callable[..., Awaitable[Any]] | None = None,
        bili_buvid: str | None = None,
        weibo_cookie: str | None = None,
        weibo_call: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._get_text = get_text
        self._now = now
        self.sportsdb_base = sportsdb_base
        self.sportsdb_key = sportsdb_key
        self.sportsdb = SportsdbClient(
            self._get,
            base=sportsdb_base or DEFAULT_SPORTSDB_BASE,
            key=sportsdb_key or DEFAULT_SPORTSDB_KEY,
        )
        self.pandascore_token = pandascore_token
        self.link_cache_path = link_cache_path
        self.bili_cookie = bili_cookie or None
        self.bili_call = bili_call
        self.bili_post = bili_post
        self.bili_buvid = bili_buvid
        self.weibo_cookie = weibo_cookie or None
        self.weibo_call = weibo_call
        self.bookmarks = bookmarks or BookmarkStore(bookmarks_path)

    def _clock(self) -> datetime:
        return self._now() if self._now is not None else datetime.now(timezone.utc)

    def _source_kwargs(self, platform: str) -> dict[str, Any]:
        return {key: getattr(self, attr) for key, attr in fetch_fields(platform)}

    async def load(
        self,
        platform: str,
        account: str,
        *,
        limit: int = 3,
        include_hard_skip: bool = False,
    ) -> FeedSnapshot:
        loaded = await self._load(platform, account)
        items = loaded.items
        if not include_hard_skip:
            now = self._clock()
            items = [item for item in items if not is_hard_skip(item, now=now)]
        if not is_match_platform(platform):
            items = items[: max(0, limit)]
        return replace(loaded, items=items)

    async def search_accounts(
        self, platform: str, query: str, *, limit: int = 25
    ) -> list[tuple[str, str]]:
        source = source_named(platform)
        search = getattr(source, "search_accounts", None) if source else None
        if search is None:
            return []
        rows = await search(query, get=self._get, limit=limit, **self._source_kwargs(platform))
        return list(rows)[:limit]

    def mark_seen(self, platform: str, account: str, ids: Sequence[str]) -> None:
        """Bookmark these post ids."""
        self.bookmarks.remember(platform, account, list(dict.fromkeys(str(item_id) for item_id in ids if item_id)))

    def delivered_refs(self, platform: str, account: str, item_id: str) -> set[str]:
        return self.bookmarks.delivered_refs(platform, account, item_id)

    def delivered_item_ids(self, platform: str, account: str) -> set[str]:
        return self.bookmarks.delivered_item_ids(platform, account)

    def stored_shows(self, platform: str, account: str, ref: str) -> set[str]:
        return {
            item_id
            for item_id in self.delivered_item_ids(platform, account)
            if ref in self.delivered_refs(platform, account, item_id)
        }

    def mark_delivered(
        self, platform: str, account: str, item_id: str, refs: Sequence[str]
    ) -> None:
        self.bookmarks.mark_delivered(platform, account, item_id, refs)

    def prune_unused(self, keep: Iterable[tuple[str, str]]) -> int:
        return self.bookmarks.keep_only(keep)

    async def pull(
        self,
        platform: str,
        account: str,
        *,
        backfill: timedelta | None = None,
    ) -> PullResult:
        loaded = await self._load(platform, account)
        bookmark = loaded.team_id or account
        if bookmark != account:
            self.bookmarks.move(platform, account, bookmark)
        items = loaded.items
        now = self._clock()
        starts = [item for item in items if is_live_start(item)]
        if not any(item.item_id for item in items):
            return _result(loaded, [], [], starts)
        seeded = self.bookmarks.is_seeded(platform, bookmark)
        source = source_named(platform)
        catch = getattr(source, "catch_floor", None)
        if catch is not None and not seeded:
            floor_text = catch(items)
            if floor_text:
                self.bookmarks.remember_line(platform, bookmark, floor_text, now)
        floor, since = self.bookmarks.catch_line(platform, bookmark) if seeded else (None, None)
        new, visible, skip_ids, seed_ids = _split_items(
            items,
            now,
            self.bookmarks.seen_ids(platform, bookmark) if seeded else None,
            backfill=None if seeded else backfill,
            floor=floor,
            since=since,
            quiet=getattr(source, "is_quiet", None),
        )
        opening = not seeded
        if seeded:
            leave = {item.item_id for item in new} | skip_ids
            window = [
                item.item_id for item in items if item.item_id and item.item_id not in leave
            ]
            if window:
                self.bookmarks.remember(platform, bookmark, window)
        else:
            self.bookmarks.remember(platform, bookmark, seed_ids or [BOOKMARK_SEEDED])
        opened = _result(loaded, new, visible, starts, opening=opening)
        return _reopen_edits(self.bookmarks.words(platform, bookmark), opened)

    def note_words(self, platform: str, account: str, item_id: str, fingerprint: str) -> None:
        """Remember the words already judged, so an edit comes back after a restart."""
        self.bookmarks.note_words(platform, account, item_id, fingerprint)

    async def _load(self, platform: str, account: str) -> FeedSnapshot:
        source = source_named(platform)
        fetch = getattr(source, "fetch_items", None) if source is not None else None
        if fetch is None:
            raise FetchError(f"{platform} is not supported")
        kwargs = self._source_kwargs(platform)
        if is_match_platform(platform):
            kwargs["pinned_events"] = pinned_event_ids(self.delivered_item_ids(platform, account))
        if source.NAME in {"bilibili", "weibo"}:
            kwargs["seen"] = self.bookmarks.seen_ids(platform, account)
        try:
            raw = await fetch(account, get=self._get, now=self._now, **kwargs)
        finally:
            if source.NAME == "weibo":
                self.weibo_cookie = (getattr(settings, "weibo_cookie", None) or "").strip() or None
        if is_match_platform(platform):
            name, items, sport, team_id, scanned = raw
            watch = getattr(source, "watch_kickoffs", None)
            return FeedSnapshot(
                name, items, (sport or "").strip() or None,
                watch(items, now=self._clock()) if watch is not None else (), team_id, scanned,
            )
        name, items, certification, bio, *_ = raw
        cert = certification if isinstance(certification, str) else ""
        about = bio if isinstance(bio, str) else ""
        return FeedSnapshot(name, items, certification=cert, bio=about)

    async def _get(self, url: str, headers: dict[str, str] | None) -> str:
        return await get_text(url, headers, getter=self._get_text)


def _reopen_edits(words: Mapping[str, str], got: PullResult) -> PullResult:
    """A judged post comes back when its words are no longer the stored fingerprint."""
    from news import drafts

    known = {item.item_id for item in got.new}
    extra = []
    for item in got.items:
        if not item.item_id or item.item_id in known:
            continue
        saved = words.get(item.item_id)
        if saved is not None and saved != drafts.word_fingerprint(item):
            extra.append(item)
            known.add(item.item_id)
    return replace(got, new=[*got.new, *extra]) if extra else got


def _result(loaded: FeedSnapshot, new, visible, starts, *, opening: bool = False) -> PullResult:
    return PullResult(
        loaded.name, new, visible, starts, loaded.sport, loaded.kickoffs, loaded.team_id,
        loaded.items, loaded.schedule, loaded.certification, loaded.bio, opening=opening,
    )


def _split_items(
    items: Sequence[FetchedItem], now: datetime, seen: set[str] | None, *,
    backfill: timedelta | None = None, floor: int | None = None, since: datetime | None = None,
    quiet: Callable[[FetchedItem], bool] | None = None,
) -> tuple[list[FetchedItem], list[FetchedItem], set[str], list[str]]:
    """First pull (`seen is None`) seeds history. `backfill` posts that recent window.

    `floor` and `since` are the page that first caught a Bilibili follow up.
    An unseen post from that page stays history. A quiet list row is that page's id,
    not a card.
    """
    new: list[FetchedItem] = []
    visible: list[FetchedItem] = []
    skip_ids: set[str] = set()
    seed_ids: list[str] = []
    for item in items:
        if not item.item_id:
            continue
        if quiet is not None and quiet(item):
            if seen is None:
                seed_ids.append(item.item_id)
            else:
                skip_ids.add(item.item_id)
            continue
        if is_hard_skip(item, now=now):
            skip_ids.add(item.item_id)
            continue
        if seen is None:
            if hold_bookmark(item, now=now):
                visible.append(item)
            elif _posted_since(item, now, backfill):
                visible.append(item)
                new.append(item)
            else:
                seed_ids.append(item.item_id)
            continue
        visible.append(item)
        if item.item_id not in seen and not _before_line(item, floor, since):
            new.append(item)
    return new, visible, skip_ids, seed_ids


def _before_line(item: FetchedItem, floor: int | None, since: datetime | None) -> bool:
    """True when this post was already up at the catch-up line."""
    if floor is not None and item.item_id.isdigit() and int(item.item_id) <= floor:
        return True
    if since is None or item.occurred_at is None:
        return False
    return _aware(item.occurred_at) <= _aware(since)


def _posted_since(item: FetchedItem, now: datetime, window: timedelta | None) -> bool:
    if window is None or item.occurred_at is None:
        return False
    return _aware(item.occurred_at) >= _aware(now) - window


async def pull_locked(
    fetcher: Fetcher, platform: str, account: str, lock: asyncio.Lock, *,
    tries: int = 1, pause: bool = False, backfill: timedelta | None = None,
    wait: float = 0.0,
) -> PullResult:
    """`pause` waits `wait` seconds before every try, including the first."""
    last: FetchError | None = None
    for attempt in range(tries):
        if (pause or attempt) and wait:
            await asyncio.sleep(wait)
        try:
            async with lock:
                return await fetcher.pull(platform, account, backfill=backfill)
        except FetchError as exc:
            last = exc
    assert last is not None
    raise last
