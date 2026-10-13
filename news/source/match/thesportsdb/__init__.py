"""TheSportsDB match cards via the team events API."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from fetch import FetchedItem, http_url
from .clubs import (
    DEFAULT_SPORTSDB_BASE, DEFAULT_SPORTSDB_KEY, SportsdbClient, club_alias, resolve_team,
)
from .espn import with_espn

# The registry reads hold_bookmark, pick_check, pick_preview, should_skip, and
# watch_kickoffs by name on this module.
from news.source.match.common import (
    hold_bookmark, is_match_item, pick_check, pick_preview, should_skip, watch_kickoffs,
)
from .matchstate import _team_events

NAME = "thesportsdb"
NAMES = frozenset({NAME})
ADD_EXAMPLE = "a club name"
CARD_KIND = "match"
FETCH_FIELDS = (
    ("sportsdb_base", "sportsdb_base"),
    ("sportsdb_key", "sportsdb_key"),
    ("client", "sportsdb"),
)
_TEAM_URL_RE = re.compile(r"thesportsdb\.com/team(?:\.php\?id=|/+)(\d+)", re.IGNORECASE)
_Get = Callable[[str, dict[str, str] | None], Any]


def parse_account(account: str, *, explicit: bool = False) -> str | None:
    raw = account.strip()
    if not raw:
        return None
    if raw.lower().startswith(f"{NAME}:"):
        raw = raw.split(":", 1)[1]
        explicit = True
    found = http_url(raw)
    if found:
        match = _TEAM_URL_RE.search(found)
        return match.group(1) if match else None
    token = " ".join(raw.split())
    if token.isdigit():
        return token
    if not explicit:
        return None
    alias = club_alias(token)
    return alias.team_id if alias is not None and alias.team_id.isdigit() else token


async def fetch_items(
    account: str, *, get: _Get, sportsdb_base: str | None = None,
    sportsdb_key: str | None = None, now: Callable[[], datetime] | None = None,
    client: SportsdbClient | None = None, pinned_events: Sequence[str] | None = None,
    **_kwargs: object,
) -> tuple[str | None, list[FetchedItem], str, str, bool]:
    from .cards import match_items

    base = sportsdb_base or DEFAULT_SPORTSDB_BASE
    key = sportsdb_key or DEFAULT_SPORTSDB_KEY
    stamp = (now or (lambda: datetime.now(timezone.utc)))()
    board = client or SportsdbClient(get, base=base, key=key)
    team_id, team_name, team_sport, team_league_id = await resolve_team(account, client=board)
    states, scanned = await _team_events(
        board, team_id=team_id, team_name=team_name, sport=team_sport,
        league_id=team_league_id, now=stamp, pinned=pinned_events or (),
    )
    states = await with_espn(states, get=get, now=stamp, club=team_name)
    items = match_items(states, account=account, now=stamp, source_name=team_name, platform=NAME)
    sport = team_sport or next((state.sport for state in states if state.sport), "")
    return team_name, items, sport, team_id, scanned


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
    "pick_preview",
    "previewable",
    "should_skip",
    "uses_ai",
    "watch_kickoffs",
]
