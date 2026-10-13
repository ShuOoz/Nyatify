"""PandaScore team matches, with BO3 card links."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fetch import FetchError, FetchedItem
# The registry reads hold_bookmark, pick_check, should_skip, and watch_kickoffs
# by name on this module.
from news.source.match.common import (
    hold_bookmark, is_match_item, pick_check, should_skip, watch_kickoffs,
)

from .cards import items_for_matches
from .http import PandaScoreClient
from .matchpage import DEFAULT_LINK_CACHE, LinkCache, _same_team, alias_names, resolve_match_url

NAME = "pandascore"
NAMES = frozenset({NAME})
ADD_EXAMPLE = "a team name"
CARD_KIND = "match"
FETCH_FIELDS = (("token", "pandascore_token"), ("link_cache_path", "link_cache_path"))
_Get = Callable[[str, dict[str, str] | None], Any]


def parse_account(account: str, *, explicit: bool = False) -> str | None:
    """Store the PandaScore slug, or an explicit name resolved at fetch time."""
    raw = account.strip()
    if raw.lower().startswith(f"{NAME}:"):
        raw = raw.split(":", 1)[1].strip()
        explicit = True
    token = " ".join(raw.split())
    return token if token and explicit else None


async def search_accounts(
    query: str, *, get: _Get, token: str | None = None, **_kwargs: object
) -> list[tuple[str, str]]:
    typed = query.strip()
    if not typed:
        return []
    client = PandaScoreClient(get, token=token)
    names = alias_names(typed)
    rows: list[tuple[str, str]] = []
    try:
        hits = await client.find_teams(typed)
    except FetchError:
        return rows
    for hit in hits:
        title = next((official for official in names if _same_team(hit.name, official)), hit.name)
        label = f"{title} · {hit.game}" if hit.game else title
        rows.append((label, hit.slug))
    return rows


async def fetch_items(
    account: str, *, get: _Get, now: Callable[[], datetime] | None = None,
    token: str | None = None, link_cache: LinkCache | None = None,
    link_cache_path: Path | None = None, pinned_events: Sequence[str] | None = None,
    **_kwargs: object,
) -> tuple[str | None, list[FetchedItem], str, str, bool]:
    stamp = (now or (lambda: datetime.now(timezone.utc)))()
    client = PandaScoreClient(get, token=token)
    hit = await client.resolve_team(account)
    matches = await client.team_matches(hit, pinned_events or ())
    cache = link_cache or LinkCache(link_cache_path or DEFAULT_LINK_CACHE)
    urls: dict[str, str] = {}
    for match in matches:
        resolved = await resolve_match_url(match, cache=cache, get=get, now=stamp)
        if resolved.url:
            urls[match.match_id] = resolved.url
    items = items_for_matches(
        matches, account=hit.slug, now=stamp, source_name=hit.name, urls=urls, platform=NAME
    )
    game = hit.game or next((match.game for match in matches if match.game), "")
    return hit.name, items, game, hit.slug, True


def uses_ai(item: FetchedItem) -> bool:
    return not is_match_item(item)


def previewable(item: FetchedItem) -> bool:
    return is_match_item(item)


def ambiguous_tokens(exc: BaseException) -> list[str]:
    text = str(exc)
    if "more than one team" not in text.lower():
        return []
    return [token.strip() for token in re.findall(rf"{re.escape(NAME)}:([^,]+)", text) if token.strip()]


__all__ = [
    "ADD_EXAMPLE",
    "CARD_KIND",
    "NAME",
    "NAMES",
    "ambiguous_tokens",
    "fetch_items",
    "hold_bookmark",
    "parse_account",
    "pick_check",
    "previewable",
    "search_accounts",
    "should_skip",
    "uses_ai",
    "watch_kickoffs",
]
