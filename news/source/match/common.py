"""Match rules both sources share: card text, the 6-hour window, and which card to post."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone

from fetch import FetchedItem

# Must outlast the slow match poll, so that wake cannot step over kickoff.
START_LEAD = timedelta(minutes=20)
# Keep the fast poll if the card is still the preview after the announced time.
START_LATE = timedelta(minutes=60)
START_POLL_SECONDS = 30.0
START_POLL_JITTER_SECONDS = 5.0

MATCH_TAG = "match"
NEXT_TAG = "next"
START_TAG = "start"
END_TAG = "end"
SKIP_TAG = "skip"

MATCH_IDLE_LINE = "Enjoy the rest of your day."
MATCH_DETAIL_HEADING = "**Details**"
MATCH_PHASE_HEADING = "Game Phase"

PREVIEW_BEFORE = timedelta(hours=6)
DELETE_AFTER_END = timedelta(days=1, hours=12)
_LIVE_CAP = {
    "soccer": timedelta(hours=4),
    "basketball": timedelta(hours=3, minutes=30),
    "american football": timedelta(hours=5),
    "baseball": timedelta(hours=5),
    "ice hockey": timedelta(hours=4),
    "tennis": timedelta(hours=8),
}
_LIVE_CAP_DEFAULT = timedelta(hours=4)
# Same folds title_sport applied before this lookup.
_CAP_ALIAS = {"football": "american football", "hockey": "ice hockey"}
_STAGE_TAGS = {MATCH_TAG, START_TAG, END_TAG, NEXT_TAG, SKIP_TAG}
_ROLE_TAGS = (START_TAG, NEXT_TAG, END_TAG, SKIP_TAG)
_PREVIEW_ROLES = ("live", "preview", "skip", "result", "later")
# /news check. True: next kickoff. False: most recent result. Flip by hand.
CHECK_FORWARD = False


def is_match_item(item: FetchedItem) -> bool:
    return MATCH_TAG in item.tags


def match_stage(item: FetchedItem) -> str | None:
    """First of next/start/end/skip present in tags, else None."""
    return next(
        (tag for tag in (NEXT_TAG, START_TAG, END_TAG, SKIP_TAG) if tag in item.tags),
        None,
    )


def kickoff_stamp(kickoff: datetime | None) -> str:
    """Discord absolute and relative timestamps, or a TBA line."""
    if kickoff is None:
        return "Kickoff soon"
    aware = kickoff if kickoff.tzinfo is not None else kickoff.replace(tzinfo=timezone.utc)
    unix = int(aware.timestamp())
    return f"<t:{unix}:F> · <t:{unix}:R>"


def ordered_match_items(rows, *, to_item, sort_at, now: datetime) -> list[FetchedItem]:
    """Soonest kickoff first. Drop an undated next card unless it is the preview pick."""
    fallback = datetime.max.replace(tzinfo=timezone.utc)
    ranked: list[tuple[datetime, int, FetchedItem]] = []
    for index, row in enumerate(rows):
        item = to_item(row)
        if item is not None:
            ranked.append((sort_at(row) or fallback, index, item))
    ranked.sort()
    items = [item for _stamp, _index, item in ranked]
    preview = pick_preview(items, now=now)
    return [
        item
        for item in items
        if item.occurred_at is not None or NEXT_TAG not in item.tags or item is preview
    ]


def parse_iso(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def match_card_body(
    status: str,
    home: str,
    league: str,
    note: str,
    *,
    live: bool,
    serie: str = "",
) -> str:
    """Status, optional home or stage, league, then live phase or Details + note."""
    lines = [f"**Game Status** | {status}"]
    if home.strip():
        lines.append(f"**Home** | {home.strip()}")
    if serie.strip():
        lines.append(f"**Stage** | {serie.strip()}")
    if league.strip():
        lines.append(f"**League** | {league.strip()}")
    if live:
        lines.append(f"{MATCH_PHASE_HEADING} | {note}")
    else:
        lines.extend(["", MATCH_DETAIL_HEADING, note])
    return "\n".join(lines)


def status_key(status: str) -> str:
    key = " ".join(
        status.lower().replace("_", " ").replace("-", " ").replace("/", " ").split()
    )
    return key[7:].strip() if key.startswith("status ") else key


def skip_label(status: str) -> str:
    key = status_key(status)
    for mark, label in (
        ("postponed", "Postponed"),
        ("cancelled", "Cancelled"),
        ("canceled", "Cancelled"),
        ("abandoned", "Abandoned"),
        ("suspended", "Suspended"),
    ):
        if mark in key:
            return label
    return "Postponed"


def live_cap(sport: str = "") -> timedelta:
    """How long to treat a match as on if the API never says finished."""
    key = _CAP_ALIAS.get(sport.strip().casefold(), sport.strip().casefold())
    return _LIVE_CAP.get(key, _LIVE_CAP_DEFAULT)


def in_play(kickoff: datetime, now: datetime, sport: str = "") -> bool:
    return kickoff - PREVIEW_BEFORE <= now <= kickoff + live_cap(sport)


def _aware(stamp: datetime) -> datetime:
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def kickoff_id(stamp: datetime) -> int:
    return int(_aware(stamp).timestamp())


def start_due(kickoff: datetime, now: datetime) -> bool:
    """True while a preview kickoff should be fetched every few seconds."""
    begin, current = _aware(kickoff), _aware(now)
    return begin - START_LEAD <= current <= begin + START_LATE


def sources_awaiting_start(
    rows: Iterable[tuple[str, str, Sequence[datetime]]],
    seen: set[tuple[str, str, int]],
    now: datetime,
) -> list[tuple[str, str]]:
    """Clubs whose kickoff is in the start window and whose card is still the preview.

    Both match feeds store the same kickoff list, so one check covers them.
    """
    found: list[tuple[str, str]] = []
    for platform, account, kickoffs in rows:
        if any(
            start_due(stamp, now) and (platform, account, kickoff_id(stamp)) not in seen
            for stamp in kickoffs
        ):
            found.append((platform, account))
    return found


def started_keys(
    platform: str,
    account: str,
    items: Iterable[FetchedItem],
    team_id: str = "",
) -> set[tuple[str, str, int]]:
    """Kickoffs whose card has left the preview, under the follow id and any renamed team id."""
    names = {account}
    renamed = team_id.strip()
    if renamed and renamed != account:
        names.add(renamed)
    keys: set[tuple[str, str, int]] = set()
    for item in items:
        if not is_match_item(item) or item.occurred_at is None:
            continue
        stage = match_stage(item)
        if not stage or stage == NEXT_TAG:
            continue
        kick = kickoff_id(item.occurred_at)
        keys.update((platform, name, kick) for name in names)
    return keys


def hold_until(kickoff: datetime, sport: str = "") -> datetime:
    return kickoff + live_cap(sport) + DELETE_AFTER_END


def watch_open(kickoff: datetime, now: datetime, sport: str = "") -> bool:
    """Preview through the result card's hold, when ESPN recap can still land."""
    return kickoff - PREVIEW_BEFORE <= now <= hold_until(kickoff, sport)


def watch_role(kind: str, kickoff: datetime | None, sport: str, now: datetime) -> str:
    if kind == START_TAG:
        return "live"
    if kind == NEXT_TAG:
        return "preview" if kickoff is None or kickoff <= now + PREVIEW_BEFORE else "later"
    if kickoff is None or kind not in {END_TAG, SKIP_TAG}:
        return "idle"
    if hold_until(kickoff, sport) < now:
        return "idle"
    return "result" if kind == END_TAG else "skip"


def match_sport(item: FetchedItem) -> str:
    return next((tag for tag in item.tags if tag not in _STAGE_TAGS), "")


def item_role(item: FetchedItem, now: datetime) -> str:
    kind = next((tag for tag in _ROLE_TAGS if tag in item.tags), "")
    return watch_role(kind, item.occurred_at, match_sport(item), now)


def should_skip(item: FetchedItem, *, now: datetime | None = None) -> bool | None:
    if MATCH_TAG not in item.tags:
        return None
    return item_role(item, now or datetime.now(timezone.utc)) == "later"


def pick_preview(
    items: list[FetchedItem], *, now: datetime | None = None
) -> FetchedItem | None:
    """Live, then soonest preview, then postponed, then latest result, then the next fixture."""
    stamp = now or datetime.now(timezone.utc)
    matches = [item for item in items if is_match_item(item)]
    by_role = {role: [] for role in _PREVIEW_ROLES}
    for item in matches:
        role = item_role(item, stamp)
        if role in by_role:
            by_role[role].append(item)
    for role, tagged in by_role.items():
        if not tagged:
            continue
        if role in {"preview", "later"}:
            return min(tagged, key=_soonest)
        if role == "result":
            return max(tagged, key=_soonest)
        return tagged[0]
    return matches[0] if matches else None


def pick_next_match(
    items: list[FetchedItem], *, now: datetime | None = None
) -> FetchedItem | None:
    """The game on now, otherwise the next kickoff, otherwise the latest result."""
    stamp = now or datetime.now(timezone.utc)
    matches = [item for item in items if is_match_item(item)]
    live = [item for item in matches if START_TAG in item.tags]
    if live:
        return max(live, key=_soonest)
    upcoming = [item for item in matches if NEXT_TAG in item.tags]
    if upcoming:
        return min(upcoming, key=_soonest)
    future = [
        item
        for item in matches
        if item.occurred_at is not None and item.occurred_at > stamp
    ]
    if future:
        return min(future, key=_soonest)
    ended = [item for item in matches if END_TAG in item.tags]
    if ended:
        return max(ended, key=_soonest)
    started = [
        item
        for item in matches
        if item.occurred_at is not None and item.occurred_at <= stamp
    ]
    if started:
        return max(started, key=_soonest)
    return matches[0] if matches else None


def pick_latest_match(
    items: list[FetchedItem], *, now: datetime | None = None
) -> FetchedItem | None:
    """The game that has started most recently, or the next kickoff if none have."""
    stamp = now or datetime.now(timezone.utc)
    matches = [item for item in items if is_match_item(item)]
    live = [item for item in matches if START_TAG in item.tags]
    if live:
        return max(live, key=_soonest)
    started = [
        item
        for item in matches
        if item.occurred_at is not None and item.occurred_at <= stamp
    ]
    if started:
        return max(started, key=_soonest)
    ended = [item for item in matches if END_TAG in item.tags]
    if ended:
        return max(ended, key=_soonest)
    upcoming = [item for item in matches if NEXT_TAG in item.tags]
    if upcoming:
        return min(upcoming, key=_soonest)
    return matches[0] if matches else None


def pick_check(
    items: list[FetchedItem], *, now: datetime | None = None
) -> FetchedItem | None:
    """`CHECK_FORWARD` picks the next kickoff. Flip it to preview the latest result."""
    picker = pick_next_match if CHECK_FORWARD else pick_latest_match
    return picker(items, now=now)


def _soonest(item: FetchedItem) -> tuple[int, datetime]:
    stamp = item.occurred_at
    if stamp is None:
        return (1, datetime.max.replace(tzinfo=timezone.utc))
    return (0, stamp)


def pinned_event_ids(item_ids: Iterable[str]) -> tuple[str, ...]:
    """Posted preview or live cards, until the final card is delivered."""
    stages: dict[str, set[str]] = {}
    for item_id in item_ids:
        event_id, sep, stage = str(item_id).rpartition(":")
        if not sep or not event_id.isdigit() or stage not in _ROLE_TAGS:
            continue
        stages.setdefault(event_id, set()).add(stage)
    return tuple(
        sorted(
            event_id
            for event_id, seen in stages.items()
            if seen & {NEXT_TAG, START_TAG} and not seen & {END_TAG, SKIP_TAG}
        )
    )


def watch_kickoffs(
    items: Sequence[FetchedItem], *, now: datetime | None = None
) -> tuple[datetime, ...]:
    """Kickoffs poll should wake on while the card is still in the watch window."""
    stamp = now or datetime.now(timezone.utc)
    return tuple(sorted({
        item.occurred_at
        for item in items
        if item.occurred_at is not None and hold_bookmark(item, now=stamp)
    }))


def match_delete_after(item: FetchedItem, now: datetime) -> float | None:
    """Seconds the card should stay. Full time and postponed: 1.5 days. Else until kickoff+cap+1.5 days."""
    if not is_match_item(item):
        return None
    if END_TAG in item.tags or SKIP_TAG in item.tags:
        return DELETE_AFTER_END.total_seconds()
    until = hold_until(item.occurred_at or now, match_sport(item))
    return max(60.0, (until - now).total_seconds())


def hold_bookmark(item: FetchedItem, *, now: datetime | None = None) -> bool:
    """Keep live, preview, later, postponed, and recent result ids unbookmarked."""
    return is_match_item(item) and item_role(item, now or datetime.now(timezone.utc)) != "idle"
