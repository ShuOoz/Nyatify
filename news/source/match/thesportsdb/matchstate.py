"""TheSportsDB MatchState: fixtures and results."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fetch import json_rows
from news.source.match.common import (
    END_TAG,
    NEXT_TAG,
    SKIP_TAG,
    START_TAG,
    in_play,
    live_cap,
    parse_iso,
    status_key,
)
from .clubs import SportsdbClient, title_sport
from .espn import names_match
_DAY_BEFORE = 4
_DAY_AFTER = 2
# One national cup, from yesterday through two days ahead. Today is first.
_CUP_OFFSETS = (0, 1, 2, -1)
_EUROPE_LEAGUES = ("4480", "4481")
_CUP_BY_LEAGUE = {
    "4328": "4482",  # Premier League, FA Cup
    "4331": "4485",  # Bundesliga, DFB-Pokal
    "4332": "4506",  # Serie A, Coppa Italia
    "4334": "4484",  # Ligue 1, Coupe de France
    "4335": "4483",  # La Liga, Copa del Rey
}
_CLOCK_TAIL = re.compile(
    r"(?:\s*[-–—]\s*)?(?:\d{1,2}:\d{2}(?:\s*\+\d+)?|\d{1,3}['’]).*$"
)
_PERIODS = (
    (re.compile(r"\bhalf[\s-]*time\b|\bhalftime\b|\bht\b", re.I), "Halftime"),
    (re.compile(r"\bextra[\s-]*time\b|\bet\b", re.I), "Extra time"),
    (re.compile(r"\bpenalt|\bpso\b|\bshootout\b|\bpens?\b", re.I), "Penalties"),
    (re.compile(r"\bovertime\b|\bover\s*time\b|(?<![a-z])ot(?![a-z0-9])", re.I), "Overtime"),
    (re.compile(r"\b(?:q1|1st\s*q(?:tr|uarter)?)\b", re.I), "1st quarter"),
    (re.compile(r"\b(?:q2|2nd\s*q(?:tr|uarter)?)\b", re.I), "2nd quarter"),
    (re.compile(r"\b(?:q3|3rd\s*q(?:tr|uarter)?)\b", re.I), "3rd quarter"),
    (re.compile(r"\b(?:q4|4th\s*q(?:tr|uarter)?)\b", re.I), "4th quarter"),
    (re.compile(r"\b(?:p1|1st\s*period)\b", re.I), "1st period"),
    (re.compile(r"\b(?:p2|2nd\s*period)\b", re.I), "2nd period"),
    (re.compile(r"\b(?:p3|3rd\s*period)\b", re.I), "3rd period"),
    (re.compile(r"\b(?:1h|1st\s*half|first\s*half)\b", re.I), "1st half"),
    (re.compile(r"\b(?:2h|2nd\s*half|second\s*half)\b", re.I), "2nd half"),
)
_SKIP = ("postponed", "cancelled", "canceled", "abandoned", "suspended")
# Finals only. "pen", "shootout", and "complete" are in-play or too vague.
_FINISHED = (
    "finished",
    "full time",
    "fulltime",
    "ended",
    "awarded",
    "game over",
    "after extra",
    "after penalties",
    "after overtime",
)
_FINISHED_CODES = frozenset({"ft", "aet", "aot", "ap", "f ot", "f so"})
_LIVE_CODES = frozenset(
    {"1h", "2h", "ht", "et", "pen", "pso", "so", "q1", "q2", "q3", "q4", "ot"}
)
_LIVE = (
    "live",
    "in play",
    "inplay",
    "half time",
    "halftime",
    "extra time",
    "overtime",
    "shootout",
    "penalties",
)
_LIVE_CLOCK_RE = re.compile(
    r"^(?:q[1-4]|p[1-3]|ot\b|period\b|1st\b|2nd\b|3rd\b|4th\b|top\b|"
    r"bottom\b|inning\b|\d{1,3}')",
    re.IGNORECASE,
)
PHASE_RANK = {"scheduled": 1, "live": 2, "skip": 3, "finished": 4}


@dataclass(frozen=True)
class MatchState:
    match_id: str
    sport: str
    home: str
    away: str
    phase: str
    home_score: str | None = None
    away_score: str | None = None
    kickoff: datetime | None = None
    image_url: str | None = None
    league: str = ""
    league_id: str = ""
    score_url: str | None = None
    status: str = ""
    clock: str = ""
    facts: str = ""


def classify_phase(status: str) -> str:
    key = status_key(status)
    if not key:
        return "scheduled"
    if any(mark in key for mark in _SKIP):
        return "skip"
    if (
        key.startswith("final")
        or key in _FINISHED_CODES
        or any(mark in key for mark in _FINISHED)
    ):
        return "finished"
    if (
        set(key.split()) & _LIVE_CODES
        or key.endswith(" half")
        or any(mark in key for mark in _LIVE)
        or _LIVE_CLOCK_RE.match(key)
    ):
        return "live"
    return "scheduled"


def prefer_match_state(
    existing: MatchState | None, incoming: MatchState
) -> MatchState:
    """Keep the later stage when the same fixture appears on two SportsDB lists."""
    if existing is None:
        return incoming
    incoming_rank = PHASE_RANK.get(incoming.phase, 1)
    existing_rank = PHASE_RANK.get(existing.phase, 1)
    if incoming_rank != existing_rank:
        return incoming if incoming_rank > existing_rank else existing
    incoming_scored = bool(incoming.home_score and incoming.away_score)
    existing_scored = bool(existing.home_score and existing.away_score)
    if incoming_scored and not existing_scored:
        return incoming
    return existing


def period_label(*texts: str) -> str:
    blob = " ".join(part for part in texts if part).strip()
    if not blob:
        return ""
    for pattern, label in _PERIODS:
        if pattern.search(blob):
            return label
    stripped = _CLOCK_TAIL.sub("", blob).strip(" -–—")
    if stripped and not any(char.isdigit() for char in stripped):
        return stripped
    return ""


def _field(raw: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        text = str(raw.get(key) or "").strip()
        if text and text.lower() != "null":
            return text
    return ""


def _tba_clock(value: str) -> bool:
    """TheSportsDB writes 00:00:00 when kickoff is unknown."""
    text = value.strip()
    if "T" in text:
        text = text.split("T", 1)[1]
    return bool(
        re.match(r"0{1,2}:0{1,2}(?::0{1,2})?(?:\.\d+)?(?:Z|[+-].*)?$", text, re.I)
    )


def parse_kickoff(raw: Mapping[str, Any]) -> datetime | None:
    stamp = _field(raw, "strTimestamp")
    if stamp and not _tba_clock(stamp):
        parsed = parse_iso(stamp)
        if parsed is not None:
            return parsed
    date = _field(raw, "dateEvent")
    time = _field(raw, "strTime")
    if not date or not time or _tba_clock(time):
        return None
    return parse_iso(f"{date}T{time}")


def parse_sportsdb_events(payload: Any) -> list[MatchState]:
    rows = json_rows(payload, "events") or json_rows(payload, "results")
    states: list[MatchState] = []
    seen: set[str] = set()
    for row in rows:
        state = parse_sportsdb_event(row)
        if state is None or state.match_id in seen:
            continue
        seen.add(state.match_id)
        states.append(state)
    return states


def parse_sportsdb_event(raw: Mapping[str, Any]) -> MatchState | None:
    match_id = _field(raw, "idEvent")
    home = _field(raw, "strHomeTeam")
    away = _field(raw, "strAwayTeam")
    if not match_id or not (home or away):
        return None
    return MatchState(
        match_id=match_id,
        sport=_field(raw, "strSport") or "sport",
        home=home,
        away=away,
        phase=classify_phase(_field(raw, "strStatus")),
        home_score=_field(raw, "intHomeScore") or None,
        away_score=_field(raw, "intAwayScore") or None,
        kickoff=parse_kickoff(raw),
        image_url=_field(raw, "strThumb", "strPoster") or None,
        league=_field(raw, "strLeague"),
        league_id=_field(raw, "idLeague"),
        status=_field(raw, "strStatus"),
        facts=_field(raw, "strResult"),
    )


def _card_kind(state: MatchState, now: datetime) -> str | None:
    """Skip, Over, On, or Kickoff from phase + kickoff.

    Past the live cap a card is Over, even if the API never left scheduled.
    Live from the API is On during the preview/play window, or with no kickoff.
    After kickoff, still inside the cap, the clock can turn Kickoff into On.
    """
    if not state.match_id or not (state.home or state.away):
        return None
    if state.phase == "skip":
        return SKIP_TAG
    if state.phase == "finished":
        return END_TAG
    kickoff = state.kickoff
    if kickoff is not None and now > kickoff + live_cap(state.sport):
        return END_TAG
    if state.phase == "live" and (
        kickoff is None or in_play(kickoff, now, state.sport)
    ):
        return START_TAG
    if kickoff is not None and kickoff <= now:
        return START_TAG
    return NEXT_TAG


async def _fill_pinned(
    client: SportsdbClient,
    by_id: dict[str, MatchState],
    event_ids: Sequence[str],
) -> None:
    """One lookup for a posted card the free lists no longer return."""
    for event_id in event_ids:
        if event_id in by_id:
            continue
        for state in parse_sportsdb_events(
            await client.json("lookupevent.php", {"id": event_id}, "Event lookup")
        ):
            if state.match_id != event_id:
                continue
            by_id[state.match_id] = prefer_match_state(
                by_id.get(state.match_id), state
            )


async def _team_events(
    client: SportsdbClient,
    *,
    team_id: str,
    team_name: str,
    sport: str,
    league_id: str,
    now: datetime,
    pinned: Sequence[str] = (),
) -> tuple[list[MatchState], bool]:
    client.day_scan_cut = False
    by_id: dict[str, MatchState] = {}
    for path, params, what in (
        ("eventsnext.php", {"id": team_id}, "Team fixtures"),
        ("eventslast.php", {"id": team_id}, "Team results"),
    ):
        for state in parse_sportsdb_events(await client.json(path, params, what)):
            by_id[state.match_id] = prefer_match_state(by_id.get(state.match_id), state)
    if title_sport(sport).casefold() != "soccer" or not team_name:
        await _fill_pinned(client, by_id, pinned)
        return list(by_id.values()), True
    covered = _kickoff_dates(by_id.values())
    scanned = True
    for day in scan_days(now):
        if day in covered:
            continue
        for label in day_scan_labels(league_id):
            payload = await client.json(
                "eventsday.php", {"d": day, "l": label}, "Day fixtures"
            )
            if payload is None:
                scanned = False
                break
            for state in parse_sportsdb_events(payload):
                if names_match(team_name, state.home) or names_match(
                    team_name, state.away
                ):
                    by_id[state.match_id] = prefer_match_state(
                        by_id.get(state.match_id), state
                    )
        if not scanned:
            break
    if scanned:
        await _fill_result_gap(
            client,
            by_id,
            team_name=team_name,
            league_id=league_id,
            now=now,
        )
        await _fill_cup_days(
            client,
            by_id,
            team_name=team_name,
            league_id=league_id,
            now=now,
        )
    await _fill_pinned(client, by_id, pinned)
    return list(by_id.values()), scanned


async def _fill_result_gap(
    client: SportsdbClient,
    by_id: dict[str, MatchState],
    *,
    team_name: str,
    league_id: str,
    now: datetime,
) -> None:
    """Newest club-league days before the scan window. A cut does not fail the scan."""
    league = league_id.strip()
    if not league.isdigit():
        return
    covered = _kickoff_dates(by_id.values())
    kickoffs = [state.kickoff for state in by_id.values()]
    for day in result_gap_days(now, kickoffs):
        if day in covered:
            continue
        payload = await client.json(
            "eventsday.php", {"d": day, "l": league}, "Day fixtures"
        )
        if payload is None:
            return
        found = False
        for state in parse_sportsdb_events(payload):
            if not (
                names_match(team_name, state.home)
                or names_match(team_name, state.away)
            ):
                continue
            by_id[state.match_id] = prefer_match_state(
                by_id.get(state.match_id), state
            )
            found = True
        if found:
            return


async def _fill_cup_days(
    client: SportsdbClient,
    by_id: dict[str, MatchState],
    *,
    team_name: str,
    league_id: str,
    now: datetime,
) -> None:
    """National cup near today. A cut does not fail the scan.

    Runs after the result gap so a stale last-result search keeps its requests.
    Today is first, so the one spare request still sees a cup tie being played.
    """
    cup = _CUP_BY_LEAGUE.get(league_id.strip(), "")
    if not cup or client.day_scan_cut:
        return
    for day in cup_scan_days(now):
        payload = await client.json(
            "eventsday.php", {"d": day, "l": cup}, "Day fixtures"
        )
        if payload is None:
            return
        for state in parse_sportsdb_events(payload):
            if not (
                names_match(team_name, state.home)
                or names_match(team_name, state.away)
            ):
                continue
            by_id[state.match_id] = prefer_match_state(
                by_id.get(state.match_id), state
            )


def scan_days(now: datetime) -> list[str]:
    local = now.astimezone(timezone.utc).date()
    return [
        (local + timedelta(days=delta)).isoformat()
        for delta in range(-_DAY_BEFORE, _DAY_AFTER + 1)
    ]


def result_gap_days(
    now: datetime, kickoffs: Sequence[datetime | None]
) -> list[str]:
    """Newest first. Days after the latest past kickoff and before the scan window."""
    window = scan_days(now)
    if not window:
        return []
    window_start = datetime.fromisoformat(window[0]).date()
    today = now.astimezone(timezone.utc).date()
    newest: date | None = None
    for kickoff in kickoffs:
        if kickoff is None:
            continue
        stamp = kickoff if kickoff.tzinfo else kickoff.replace(tzinfo=timezone.utc)
        day = stamp.astimezone(timezone.utc).date()
        if day <= today and (newest is None or day > newest):
            newest = day
    if newest is None:
        return []
    first = newest + timedelta(days=1)
    if first >= window_start:
        return []
    cursor = window_start - timedelta(days=1)
    days: list[str] = []
    while cursor >= first:
        days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return days


def cup_scan_days(now: datetime) -> list[str]:
    """Today, then the next two days, then yesterday."""
    today = now.astimezone(timezone.utc).date()
    return [(today + timedelta(days=delta)).isoformat() for delta in _CUP_OFFSETS]


def day_scan_labels(league_id: str = "") -> tuple[str, ...]:
    labels: list[str] = []
    extra = league_id.strip()
    for label in (*_EUROPE_LEAGUES, extra):
        if label.isdigit() and label not in labels:
            labels.append(label)
    return tuple(labels)


def _kickoff_dates(states: Sequence[MatchState]) -> set[str]:
    days: set[str] = set()
    for state in states:
        if state.kickoff is None:
            continue
        days.add(state.kickoff.astimezone(timezone.utc).date().isoformat())
    return days
