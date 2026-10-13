"""ESPN scoreboard and summary payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, NamedTuple

from fetch import http_url, json_rows

_ESPN_PHASE = {"pre", "in", "post"}
_PERIOD_UNIT = (
    (re.compile(r"soccer|uefa|fifa"), ""),
    (re.compile(r"nba|wnba|nfl|ncaaf|ncaab|college-football|basketball"), "quarters"),
    (re.compile(r"nhl|hockey"), "periods"),
    (re.compile(r"mlb|baseball"), "innings"),
    (re.compile(r"tennis|atp|wta"), "sets"),
)
_GOAL_TAIL = re.compile(r"\s+Goal(?:\s*[—–-].*)?$", re.I)
_GOAL_AT = re.compile(r"(\d+['’])")
_GOAL_TEAM = re.compile(r"\(([^)]+)\)")


class EspnGame(NamedTuple):
    home: str
    away: str
    url: str
    venue: str = ""
    detail: str = ""
    notes: tuple[str, ...] = ()
    leaders: tuple[str, ...] = ()
    home_score: str = ""
    away_score: str = ""
    state: str = ""
    home_labels: tuple[str, ...] = ()
    away_labels: tuple[str, ...] = ()
    kickoff: datetime | None = None


def parse_espn_scoreboard(payload: Any) -> list[EspnGame]:
    return [game for event in json_rows(payload, "events") if (game := _event(event))]


def parse_espn_summary(payload: Any) -> EspnGame | None:
    if not isinstance(payload, dict):
        return None
    scoring = _notes(payload)
    game = _event(payload.get("header"))
    if game is None:
        return EspnGame(home="", away="", url="", notes=tuple(scoring)) if scoring else None
    return game._replace(
        notes=tuple(dict.fromkeys([*game.notes, *scoring])),
        venue=game.venue or _venue(payload.get("gameInfo")),
        leaders=game.leaders or tuple(_leaders(payload.get("boxscore"))),
    )


def _pick(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _at(blob: Any, *keys: str) -> Any:
    cur = blob
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _maps(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _comp(event: Mapping[str, Any]) -> Mapping[str, Any]:
    rows = event.get("competitions")
    first = rows[0] if isinstance(rows, list) and rows else event
    return first if isinstance(first, dict) else event


def _sides(event: Mapping[str, Any]) -> tuple[Mapping[str, Any], dict[str, dict[str, Any]]]:
    first = _comp(event)
    sides: dict[str, dict[str, Any]] = {}
    for row in _maps(first.get("competitors")):
        side = _pick(row.get("homeAway")).lower()
        if side in {"home", "away"}:
            sides[side] = row
    return first, sides


def _event(raw: Any) -> EspnGame | None:
    if not isinstance(raw, dict):
        return None
    first, sides = _sides(raw)
    home, away = sides.get("home"), sides.get("away")
    if home is None or away is None:
        return None

    def side(row: Mapping[str, Any]) -> tuple[str, str]:
        team = row.get("team") if isinstance(row.get("team"), dict) else {}
        return _pick(team.get("displayName"), team.get("name"), team.get("shortDisplayName")), _pick(row.get("score"))

    home_name, home_score = side(home)
    away_name, away_score = side(away)
    url = _href(raw)
    if not (home_name and away_name and url):
        return None
    return EspnGame(
        home_name,
        away_name,
        url,
        _venue(first) or _venue(raw),
        _clock(first) or _clock(raw),
        tuple(_texts(first.get("notes") or raw.get("notes"), "headline", "text")),
        tuple(_leaders(first) or _leaders(raw)),
        home_score,
        away_score,
        _status(first) or _status(raw),
        _team_labels(home),
        _team_labels(away),
        _kickoff(raw, first),
    )


def _team_labels(row: Mapping[str, Any]) -> tuple[str, ...]:
    team = row.get("team") if isinstance(row.get("team"), dict) else {}
    labels: list[str] = []
    for key in ("displayName", "name", "shortDisplayName", "nickname"):
        text = _pick(team.get(key))
        if text and text not in labels:
            labels.append(text)
    return tuple(labels)


def _kickoff(event: Mapping[str, Any], first: Mapping[str, Any]) -> datetime | None:
    text = _pick(event.get("date"), first.get("date"), first.get("startDate"))
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _venue(blob: Any) -> str:
    return _pick(_at(blob, "venue", "fullName"), _at(blob, "venue", "displayName"))


def _status(blob: Mapping[str, Any]) -> str:
    key = _pick(_at(blob, "status", "type", "state")).lower()
    return key if key in _ESPN_PHASE else ""


def _clock(blob: Mapping[str, Any]) -> str:
    typed = _at(blob, "status", "type")
    if isinstance(typed, dict):
        return _pick(typed.get("detail"), typed.get("shortDetail"), typed.get("description"))
    return _pick(_at(blob, "status", "displayClock"))


def _notes(payload: Mapping[str, Any]) -> list[str]:
    periods = _period_line(payload)
    if periods:
        return [periods]
    split = _goals_by_side(payload)
    if split:
        return split
    plays = _texts(payload.get("scoringPlays"), "text", "shortText")
    if plays:
        return plays[-8:]
    goals = [
        line
        for row in _maps(payload.get("keyEvents"))
        if row.get("scoringPlay") and (line := _goal_line(row))
    ]
    if goals:
        return goals
    headline = _pick(_at(payload, "article", "headline"))
    return [headline] if headline else []


def _goals_by_side(payload: Mapping[str, Any]) -> list[str]:
    header = payload.get("header")
    if not isinstance(header, dict):
        return []
    _, sides = _sides(header)
    home, away = sides.get("home"), sides.get("away")
    if home is None or away is None:
        return []
    rows = [
        *_maps(payload.get("scoringPlays")),
        *[row for row in _maps(payload.get("keyEvents")) if row.get("scoringPlay")],
    ]
    buckets: dict[str, list[str]] = {"home": [], "away": []}
    for row in rows:
        line = _goal_line(row)
        side = _goal_side(row, home, away)
        if not line or side not in buckets or line in buckets[side]:
            continue
        buckets[side].append(line)
    if not (buckets["home"] or buckets["away"]):
        return []
    return [
        "Home",
        *(buckets["home"] or ["-"]),
        "Away",
        *(buckets["away"] or ["-"]),
    ]


def _goal_side(
    row: Mapping[str, Any], home: Mapping[str, Any], away: Mapping[str, Any]
) -> str:
    team = _pick(
        _at(row, "team", "displayName"),
        _at(row, "team", "shortDisplayName"),
        _at(row, "team", "name"),
    )
    if not team:
        found = _GOAL_TEAM.search(_pick(row.get("text"), row.get("shortText")))
        team = found.group(1).strip() if found else ""
    if team and _same_side(team, home):
        return "home"
    if team and _same_side(team, away):
        return "away"
    side = _pick(row.get("homeAway")).lower()
    return side if side in {"home", "away"} else ""


def _same_side(team: str, competitor: Mapping[str, Any]) -> bool:
    from .espn import names_match

    blob = competitor.get("team") if isinstance(competitor.get("team"), dict) else {}
    return any(
        names_match(team, _pick(blob.get(key)))
        for key in ("displayName", "name", "shortDisplayName", "nickname", "abbreviation")
        if blob.get(key)
    )


def _goal_line(row: Mapping[str, Any]) -> str:
    blob = _pick(row.get("shortText"), row.get("text"))
    name = re.sub(r"\s+at\s+\d+['’]$", "", blob, flags=re.I)
    name = _GOAL_TAIL.sub("", name).strip() or blob
    name = " ".join(_GOAL_TEAM.sub("", name).split()).strip(" -")
    minute = _pick(_at(row, "clock", "displayValue"))
    if not minute:
        found = _GOAL_AT.search(blob)
        minute = found.group(1) if found else ""
    return f"{name} {minute}".strip() if name else ""


def _period_line(payload: Mapping[str, Any]) -> str:
    header = payload.get("header")
    if not isinstance(header, dict):
        return ""
    blob = " ".join(
        _pick(raw.get("slug"), raw.get("abbreviation"), raw.get("name")) if isinstance(raw, dict) else str(raw or "")
        for raw in (header.get("league"), header.get("sport"))
    ).casefold()
    unit = next((label for pattern, label in _PERIOD_UNIT if pattern.search(blob)), "")
    if not unit:
        return ""
    _, sides = _sides(header)
    parts: list[str] = []
    for side in ("home", "away"):
        row = sides.get(side)
        if row is None:
            continue
        scores = _line_scores(row.get("linescores"))
        name = _short(row)
        if name and len(scores) >= 2:
            parts.append(f"{name} {unit}: {' '.join(scores)}")
    return "; ".join(parts)


def _short(competitor: Mapping[str, Any]) -> str:
    team = competitor.get("team") if isinstance(competitor.get("team"), dict) else {}
    return _pick(
        team.get("shortDisplayName"),
        team.get("nickname"),
        team.get("abbreviation"),
        team.get("name"),
        team.get("displayName"),
    )


def _line_scores(rows: Any) -> list[str]:
    values: list[str] = []
    for row in _maps(rows):
        raw = row.get("displayValue")
        if raw is None:
            raw = row.get("value")
        text = str(raw).strip() if raw is not None else ""
        if text:
            values.append(text.split(".")[0])
    return values


def _texts(rows: Any, *keys: str) -> list[str]:
    return [text for row in _maps(rows) if (text := _pick(*(row.get(key) for key in keys)))]


def _leaders(blob: Any) -> list[str]:
    lines: list[str] = []
    for block in _maps(blob.get("leaders") if isinstance(blob, dict) else None):
        rows = block.get("leaders")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            continue
        athlete = rows[0].get("athlete") if isinstance(rows[0].get("athlete"), dict) else {}
        name = _pick(athlete.get("displayName"), athlete.get("shortName"))
        if not name:
            continue
        label = _pick(block.get("displayName"), block.get("name"))
        value = _pick(rows[0].get("displayValue"), rows[0].get("value"))
        lines.append(" ".join(part for part in (name, value, label) if part))
        if len(lines) >= 3:
            break
    return lines


def _href(event: Mapping[str, Any]) -> str | None:
    from .espn import espn_card_url, espn_game_id

    hrefs: list[str] = []
    for blob in (event, _comp(event)):
        for link in _maps(blob.get("links")):
            found = http_url(str(link.get("href") or ""))
            if found:
                hrefs.append(found)
    chosen = next(
        (found for found in hrefs if espn_game_id(found) and "espn.com" in found.lower()),
        hrefs[0] if hrefs else None,
    )
    # Scoreboard links are `?gameId=123/slug`. The slash belongs in the path;
    # left in the query, ESPN sends the click to the sport homepage.
    if chosen and "gameid=" in chosen.casefold():
        return espn_card_url(chosen) or chosen
    return chosen
