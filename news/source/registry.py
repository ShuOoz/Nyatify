"""Look up a fetch platform and parse follow strings."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from functools import cache
from inspect import isawaitable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fetch import FetchedItem
    from news.source.post.common import FeedFault

# Canonical platform order: match cards first, then post sources.
FETCH_PLATFORMS = ("thesportsdb", "pandascore", "bilibili", "weibo")
PLATFORM_LABELS = {
    "thesportsdb": "TheSportsDB",
    "pandascore": "PandaScore",
    "bilibili": "Bilibili",
    "weibo": "Weibo",
}
# Poll posts at most this many cards per account per round. Match cards are uncapped.
FLOOD_CAP = 5


@cache
def _sources() -> tuple[Any, ...]:
    """Sniff order: specific URL shapes first, bare digits (Bilibili) before club ids."""
    from news.source.match import pandascore, thesportsdb
    from news.source.post import bilibili, weibo

    return (bilibili, weibo, pandascore, thesportsdb)


@cache
def _by_name() -> dict[str, Any]:
    return {name: source for source in _sources() for name in source.NAMES}


def source_named(platform: str) -> Any | None:
    return _by_name().get(platform.strip().lower())


def fetch_fields(platform: str) -> tuple[tuple[str, str], ...]:
    """Kwarg name and fetcher attribute for this platform's fetch_items."""
    source = source_named(platform)
    fields = getattr(source, "FETCH_FIELDS", ()) if source is not None else ()
    return tuple(fields)


def card_kind(item: FetchedItem | str) -> str:
    platform = item if isinstance(item, str) else item.platform
    source = source_named(platform)
    return source.CARD_KIND if source is not None else "post"


def is_match_platform(platform: str) -> bool:
    """True for any CARD_KIND == \"match\" source. Sole sports/match behavior gate."""
    return card_kind(platform) == "match"


def hold_bookmark(item: FetchedItem, now: datetime | None = None) -> bool:
    """Match cards stay unbookmarked until poll is allowed to post them."""
    check = getattr(source_named(item.platform), "hold_bookmark", None)
    return bool(check(item, now=now)) if check is not None else False


def is_live_start(item: FetchedItem) -> bool:
    return card_kind(item) == "match" and "start" in item.tags


def is_live_room(item: FetchedItem) -> bool:
    """Bilibili live-room go-live card. Other topics may post it; game/ticket must not."""
    check = getattr(source_named(item.platform), "is_live_room", None)
    return bool(check(item)) if check is not None else False


def shows_post(item: FetchedItem, topic: str) -> bool:
    """True when this source shows the post on that topic."""
    check = getattr(source_named(item.platform), "shows_post", None)
    return True if check is None else bool(check(item, topic))


def note_list_post(
    item: FetchedItem, account: str, audience: Collection[tuple[int, str]], handled: Collection[str],
) -> bool:
    """True when this source keeps the bookmark for a list post."""
    mark = getattr(source_named(item.platform), "note_list_post", None)
    return bool(mark(item, account, audience, handled)) if mark is not None else False


def for_model(item: FetchedItem) -> FetchedItem:
    """The post the model should read. Feed markers stay off it."""
    clean = getattr(source_named(item.platform), "for_model", None)
    return item if clean is None else clean(item)


def flood_cap(platform: str) -> int | None:
    """Max cards per poll round. None means no cap (match cards)."""
    return None if card_kind(platform) == "match" else FLOOD_CAP


def canonical_fetch_platform(platform: str) -> str:
    source = source_named(platform)
    return source.NAME if source is not None else platform.strip().lower()


def normalize_fetch_platform(platform: str) -> str | None:
    key = canonical_fetch_platform(platform)
    return key if key in FETCH_PLATFORMS else None


def normalize_fetch_account(platform: str, account: str) -> str | None:
    account = account.strip()
    source = source_named(platform)
    if not account or source is None:
        return None
    return source.parse_account(account, explicit=True)


def feed_display_name(title: str, platform: str) -> str | None:
    name = " ".join(title.split())
    if not name or name.lower().startswith(("http://", "https://")):
        return None
    clean = getattr(source_named(platform), "display_name", None)
    if clean is not None:
        name = clean(name)
    return name or None


def is_hard_skip(item: FetchedItem, now: datetime | None = None) -> bool:
    """The item's own source decides. Other sources do not inspect it."""
    check = getattr(source_named(item.platform), "should_skip", None)
    return bool(check(item, now=now)) if check is not None else False


def uses_ai(item: FetchedItem) -> bool:
    check = getattr(source_named(item.platform), "uses_ai", None)
    return True if check is None else bool(check(item))


def previewable(item: FetchedItem) -> bool:
    check = getattr(source_named(item.platform), "previewable", None)
    if check is not None:
        return bool(check(item))
    return not is_hard_skip(item)


def pick_preview(items: list[FetchedItem], *, now: datetime | None = None) -> FetchedItem | None:
    for source in _sources():
        pick = getattr(source, "pick_preview", None)
        if pick is not None:
            return pick(items, now=now)
    return None


def pick_check(items: list[FetchedItem], *, now: datetime | None = None) -> FetchedItem | None:
    for source in _sources():
        pick = getattr(source, "pick_check", None)
        if pick is not None:
            found = pick(items, now=now)
            if found is not None:
                return found
    return pick_preview(items, now=now)


def add_account_help(platform: str | None = None) -> str:
    source = source_named(platform) if platform else None
    if source is not None:
        return f"Use {source.ADD_EXAMPLE}."
    return "Use a club, uid, or Weibo uid."


def feed_fault(platform: str, exc: BaseException) -> FeedFault | None:
    check = getattr(source_named(platform), "feed_fault", None)
    return check(exc) if check is not None else None


def cookie_hint(platform: str, exc: BaseException) -> str | None:
    fault = feed_fault(platform, exc)
    return "Use `/news cookie`." if fault is not None and fault.cookie else None


def ambiguous_tokens(platform: str, exc: BaseException) -> list[str]:
    parse = getattr(source_named(platform), "ambiguous_tokens", None)
    return list(parse(exc) or []) if parse is not None else []


def auth_expired_message(platform: str) -> str:
    source = source_named(platform)
    return getattr(source, "AUTH_EXPIRED", "Cookie expired. Use `/news cookie`.")


async def apply_news_cookie(
    site: str, *, cookie: str | None, fallback_cookie: str | None = None,
    fallback_ac_time: str | None = None,
) -> dict[str, Any]:
    source = source_named(site)
    if source is None:
        raise ValueError("Pick Bilibili or Weibo")
    apply = getattr(source, "apply_cookie", None)
    if apply is None:
        raise ValueError(f"{site} does not take a cookie")
    result = apply(
        cookie=cookie, fallback_cookie=fallback_cookie, fallback_ac_time=fallback_ac_time
    )
    return await result if isawaitable(result) else result
