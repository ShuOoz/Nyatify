"""TheSportsDB MatchState → FetchedItem."""

from __future__ import annotations

import re
from datetime import datetime

from fetch import FetchedItem, http_url
from news.source.match.common import (
    MATCH_IDLE_LINE,
    MATCH_TAG,
    NEXT_TAG,
    SKIP_TAG,
    START_TAG,
    kickoff_stamp,
    match_card_body,
    ordered_match_items,
    skip_label,
)
from .espn import espn_card_url
from .matchstate import (
    MatchState,
    _card_kind,
    period_label,
)


def scoreboard_url(
    home: str,
    away: str,
    match_id: str,
    *,
    score_url: str | None = None,
    kind: str | None = None,
) -> str:
    found = http_url(score_url)
    if found:
        return espn_card_url(found, kind=kind) or found
    slug = re.sub(r"[^a-z0-9]+", "-", f"{home} vs {away}".strip().lower()).strip("-")
    path = f"{match_id}-{slug}" if slug else match_id
    return f"https://www.thesportsdb.com/event/{path}"


def match_items(
    states: list[MatchState],
    *,
    account: str,
    now: datetime,
    source_name: str | None = None,
    platform: str = "thesportsdb",
) -> list[FetchedItem]:
    """Soonest kickoff first. Kickoff until the start; On while live or after
    kickoff inside the cap; Over when finished or past the cap."""
    return ordered_match_items(
        states,
        to_item=lambda state: _item_for_state(
            state,
            account=account,
            now=now,
            source_name=source_name,
            platform=platform,
        ),
        sort_at=lambda state: state.kickoff,
        now=now,
    )


def match_card_copy(state: MatchState, kind: str) -> tuple[str, str]:
    """Title has teams/score. Body is status, home, league, then phase or details."""
    vs = f"{state.home} vs {state.away}".strip() or "Upcoming"
    score = (
        f"{state.home_score}–{state.away_score}"
        if state.home_score is not None and state.away_score is not None
        else ""
    )
    titled = f"{state.home} {score} {state.away}" if score else vs
    if kind == NEXT_TAG:
        title, status, note = vs, "Not started", kickoff_stamp(state.kickoff)
    elif kind == START_TAG:
        title, status, note = titled, "On", period_label(state.clock, state.status) or "On"
    elif kind == SKIP_TAG:
        title, status, note = vs, skip_label(state.status), kickoff_stamp(state.kickoff)
    else:
        title, status, note = titled, "Over", state.facts.strip() or MATCH_IDLE_LINE
    body = match_card_body(
        status,
        state.home,
        _league_name(state),
        note,
        live=kind == START_TAG,
    )
    return title, body


def _league_name(state: MatchState) -> str:
    if state.league.strip():
        return state.league.strip()
    sport = state.sport.strip()
    return "" if sport.casefold() == "sport" else sport


def _item_for_state(
    state: MatchState,
    *,
    account: str,
    now: datetime,
    source_name: str | None,
    platform: str,
) -> FetchedItem | None:
    kind = _card_kind(state, now)
    if kind is None:
        return None
    title, text = match_card_copy(state, kind)
    tags = (MATCH_TAG, kind, state.sport.lower()) if state.sport else (MATCH_TAG, kind)
    return FetchedItem(
        item_id=f"{state.match_id}:{kind}",
        title=title,
        text=text,
        url=scoreboard_url(
            state.home,
            state.away,
            state.match_id,
            score_url=state.score_url,
            kind=kind,
        ),
        image_url=http_url(state.image_url),
        platform=platform,
        account=account,
        tags=tags,
        source_name=source_name,
        occurred_at=state.kickoff,
    )
