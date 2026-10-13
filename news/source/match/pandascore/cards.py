"""PandaScore Match → FetchedItem."""

from __future__ import annotations

from datetime import datetime

from fetch import FetchedItem, http_url
from news.source.match.common import (
    MATCH_TAG,
    NEXT_TAG,
    SKIP_TAG,
    START_TAG,
    kickoff_stamp,
    ordered_match_items,
    skip_label,
)

from .http import Match, Stage, stage_for


def card_copy(match: Match, stage: Stage) -> tuple[str, str]:
    """Title is series score. Pregame time sits on the last line. No phase or details."""
    vs = f"{match.followed.name} vs {match.opponent.name}".strip() or "Upcoming"
    if match.series is not None:
        titled = (
            f"{match.followed.name} {match.series.followed}–"
            f"{match.series.opponent} {match.opponent.name}"
        )
    else:
        titled = vs
    stamp = ""
    if stage == NEXT_TAG:
        title, status = vs, "Not started"
        stamp = kickoff_stamp(match.scheduled_at) if match.scheduled_at else ""
    elif stage == START_TAG:
        title, status = titled, "On"
    elif stage == SKIP_TAG:
        title, status = vs, skip_label(match.status)
    else:
        title, status = titled, "Over"
    return title, _card_body(status, match, stamp)


def _card_body(status: str, match: Match, stamp: str = "") -> str:
    lines = [f"**Game Status** | {status}"]
    if match.best_of:
        lines.append(f"**Format** | Bo{match.best_of}")
    if match.serie.strip():
        lines.append(f"**Stage** | {match.serie.strip()}")
    if match.league.strip():
        lines.append(f"**League** | {match.league.strip()}")
    if stamp:
        lines.append(stamp)
    return "\n".join(lines)


def items_for_matches(
    matches: list[Match],
    *,
    account: str,
    now: datetime,
    source_name: str | None,
    urls: dict[str, str],
    platform: str = "pandascore",
) -> list[FetchedItem]:
    return ordered_match_items(
        matches,
        to_item=lambda match: _item_for_match(
            match,
            account=account,
            source_name=source_name,
            url=urls.get(match.match_id) or match.stream_url or "",
            platform=platform,
        ),
        sort_at=lambda match: match.scheduled_at,
        now=now,
    )


def _item_for_match(
    match: Match,
    *,
    account: str,
    source_name: str | None,
    url: str,
    platform: str,
) -> FetchedItem | None:
    stage = stage_for(match.status)
    if stage is None:
        return None
    title, text = card_copy(match, stage)
    game = match.game.lower()
    tags = (MATCH_TAG, stage, game) if game else (MATCH_TAG, stage)
    return FetchedItem(
        item_id=f"{match.match_id}:{stage}",
        title=title,
        text=text,
        url=http_url(url) or "",
        image_url=http_url(match.image_url),
        platform=platform,
        account=account,
        tags=tags,
        source_name=source_name,
        occurred_at=match.scheduled_at,
    )

