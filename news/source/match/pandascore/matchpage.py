"""Match page links for esports cards, with a stream fallback."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from config import BASE_DIR, write_text_atomic
from news.source.match.common import parse_iso

from .http import Match

_Get = Callable[[str, dict[str, str] | None], Any]

DEFAULT_LINK_CACHE = BASE_DIR / "data" / "match_links.json"
ESPORTS_ALIASES_PATH = BASE_DIR / "data" / "esports_aliases.json"
LINK_RETRY_AFTER = timedelta(hours=12)
USER_AGENT = "Nyatify news bot (https://github.com/nyatify/Nyatify)"


@dataclass(frozen=True)
class ResolvedLink:
    url: str
    source: Literal["bo3", "stream", "none"]


class LinkCache:
    """Keyed by PandaScore match id. Hits stick. Misses stick until a later retry."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self._rows = {
                str(key): value
                for key, value in data.items()
                if isinstance(value, dict)
            }

    def _save(self) -> None:
        write_text_atomic(
            self.path,
            json.dumps(self._rows, ensure_ascii=False, indent=2) + "\n",
        )

    def get(self, match_id: str) -> str | None:
        row = self._rows.get(match_id)
        if row is None:
            return None
        url = row.get("url")
        return url if isinstance(url, str) and url.strip() else None

    def is_due_for_retry(self, match_id: str, *, now: datetime) -> bool:
        row = self._rows.get(match_id)
        if row is None:
            return True
        at = parse_iso(str(row.get("at") or ""))
        if at is None:
            return True
        return now - at >= LINK_RETRY_AFTER

    def remember_hit(self, match_id: str, url: str) -> None:
        self._rows[match_id] = {"url": url, "at": datetime.now(timezone.utc).isoformat()}
        self._save()

    def remember_miss(self, match_id: str, *, now: datetime) -> None:
        self._rows[match_id] = {"url": None, "at": now.isoformat()}
        self._save()

    def miss_holding(self, match_id: str, *, now: datetime) -> bool:
        """A miss waits out the retry."""
        row = self._rows.get(match_id)
        if row is None or row.get("url"):
            return False
        return not self.is_due_for_retry(match_id, now=now)


_FILLER = frozenset({"team", "esports", "esport", "gaming", "clan"})
_SQUAD = frozenset(
    {"academy", "young", "youth", "junior", "juniors", "challengers", "prospects"}
)
_CLOSE_RATIO = 0.92
_CLOSE_MIN = 6
_esports_aliases: dict[str, str] | None = None
_alias_labels: dict[str, tuple[str, ...]] | None = None
_CS_GAME = "counter-strike"


def _same_team(left: str, right: str) -> bool:
    a_core, a_squad = _team_identity(left)
    b_core, b_squad = _team_identity(right)
    if not a_core or not b_core or a_squad != b_squad:
        return False
    if a_core == b_core and not _filler_nick(left, right):
        return True
    if min(len(a_core), len(b_core)) < _CLOSE_MIN:
        return False
    if a_core.startswith(b_core) or b_core.startswith(a_core):
        return False
    return SequenceMatcher(None, a_core, b_core).ratio() >= _CLOSE_RATIO


def _name_words(name: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", name.casefold())
    if tokens and tokens[0] == "the" and len(tokens) > 1:
        tokens = tokens[1:]
    return tokens


def _filler_nick(left: str, right: str) -> bool:
    """'BRO Esports' is not OKSavingsBank BRION. 'XLG Gaming' still is Xi Lai Gaming.

    A name that already contains the core word, such as 'G2 Esports', is that team.
    """
    for nick_side, other in ((left, right), (right, left)):
        words = _name_words(nick_side)
        core = [token for token in words if token not in _FILLER and token not in _SQUAD]
        if len(core) != 1 or not any(token in _FILLER for token in words):
            continue
        other_words = [token for token in _name_words(other) if token not in _SQUAD]
        if len(other_words) < 2 or core[0] in other_words:
            continue
        initials = "".join(token[0] for token in other_words)
        if core[0] != initials:
            return True
    return False


def _team_identity(name: str) -> tuple[str, tuple[str, ...]]:
    tokens = re.findall(r"[a-z0-9]+", name.casefold())
    if tokens and tokens[0] == "the" and len(tokens) > 1:
        tokens = tokens[1:]
    squad = tuple(token for token in tokens if token in _SQUAD)
    core = [token for token in tokens if token not in _FILLER and token not in _SQUAD]
    if not core:
        core = [token for token in tokens if token not in _SQUAD]
    squeezed = "".join(core)
    return load_esports_aliases().get(squeezed, squeezed), squad


def alias_names(query: str) -> tuple[str, ...]:
    """Full names for a short name. The same nick under two games stays two teams."""
    load_esports_aliases()
    key = _squeeze_alias(query)
    if _alias_labels is None or not key:
        return ()
    return _alias_labels.get(key, ())


def team_queries(typed: str) -> tuple[str, ...]:
    """Official names plus the typed nick.

    PandaScore often indexes the short name, not the expanded one. Searching
    only the official name drops teams such as XLG.
    """
    token = " ".join(typed.split())
    if not token:
        return ()
    names = alias_names(token)
    queries = list(names) if names else [token]
    if token.casefold() not in {item.casefold() for item in queries}:
        queries.append(token)
    return tuple(queries)


def load_esports_aliases(path: Path | None = None) -> dict[str, str]:
    """Squeezed nick or official name -> squeezed official name.

    The file is grouped by game, like sports aliases are grouped by sport.
    When a short name is shared, Counter-Strike supplies the single identity.
    Search keeps every game.
    """
    global _esports_aliases, _alias_labels
    if path is None and _esports_aliases is not None and _alias_labels is not None:
        return _esports_aliases
    table: dict[str, str] = {}
    labels: dict[str, tuple[str, ...]] = {}
    used = path or ESPORTS_ALIASES_PATH
    raw: object = {}
    if used.is_file():
        try:
            raw = json.loads(used.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    if isinstance(raw, dict):
        for game, clubs in raw.items():
            if not isinstance(clubs, dict):
                continue
            cs = str(game).strip().casefold() == _CS_GAME
            for nick, spec in clubs.items():
                official = _alias_name(spec)
                if not official:
                    continue
                canon = _squeeze_alias(official)
                if not canon:
                    continue
                for label in (str(nick), official):
                    key = _squeeze_alias(label)
                    if key and (cs or key not in table):
                        table[key] = canon
                nick_key = _squeeze_alias(str(nick))
                if nick_key:
                    _add_label(labels, nick_key, official)
                folded = _squeeze_alias(official)
                if folded:
                    labels.setdefault(folded, (official,))
    if path is None:
        _esports_aliases = table
        _alias_labels = labels
    return table


def _alias_name(spec: object) -> str:
    if isinstance(spec, str):
        return spec.strip()
    if isinstance(spec, dict):
        name = spec.get("name")
        if isinstance(name, str):
            return name.strip()
    return ""


def _add_label(labels: dict[str, tuple[str, ...]], key: str, official: str) -> None:
    current = labels.get(key, ())
    if official not in current:
        labels[key] = (*current, official)


def _squeeze_alias(name: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", name.casefold())
    if tokens and tokens[0] == "the" and len(tokens) > 1:
        tokens = tokens[1:]
    core = [token for token in tokens if token not in _FILLER and token not in _SQUAD]
    return "".join(core or [token for token in tokens if token not in _SQUAD])


_BO3 = "https://bo3.gg"
_BO3_API = "https://api.bo3.gg/api/v1"


@dataclass(frozen=True)
class _Bo3Game:
    discipline: str
    section: str


# Counter-Strike has no section. The other three live under their own path.
_BO3_GAMES = {
    "counter-strike": _Bo3Game("1", ""),
    "counter-strike 2": _Bo3Game("1", ""),
    "cs2": _Bo3Game("1", ""),
    "valorant": _Bo3Game("2", "valorant"),
    "league of legends": _Bo3Game("3", "lol"),
    "lol": _Bo3Game("3", "lol"),
    "dota 2": _Bo3Game("4", "dota2"),
    "dota2": _Bo3Game("4", "dota2"),
}


@dataclass(frozen=True)
class Bo3Row:
    url: str
    left: str
    right: str
    when: datetime


def bo3_teams(body: str) -> list[tuple[str, str]]:
    """Teams from a BO3 search: id, name. Academy sides stay separate."""
    payload = json.loads(body)
    rows = payload.get("results") if isinstance(payload, dict) else None
    found: list[tuple[str, str]] = []
    if not isinstance(rows, list):
        return found
    for row in rows:
        if not isinstance(row, dict):
            continue
        team_id = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        if team_id and name:
            found.append((team_id, name))
    return found


def bo3_page(section: str, slug: str) -> str:
    path = f"{section}/matches/{slug}" if section else f"matches/{slug}"
    return f"{_BO3}/{path}"


def bo3_rows(body: str, *, section: str = "") -> list[Bo3Row]:
    """Series in the date window. Each row already carries its match slug."""
    payload = json.loads(body)
    rows = payload.get("results") if isinstance(payload, dict) else None
    found: list[Bo3Row] = []
    if not isinstance(rows, list):
        return found
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").strip()
        when = parse_iso(str(row.get("start_date") or ""))
        left = _bo3_side(row.get("team1"))
        right = _bo3_side(row.get("team2"))
        if not slug or when is None or not left or not right:
            continue
        found.append(Bo3Row(bo3_page(section, slug), left, right, when))
    return found


def pick_bo3_team(teams: list[tuple[str, str]], name: str) -> str | None:
    hits = [team_id for team_id, label in teams if _same_team(name, label)]
    if len(hits) == 1:
        return hits[0]
    exact = [
        team_id
        for team_id, label in teams
        if label.casefold() == name.casefold()
    ]
    return exact[0] if len(exact) == 1 else None


def pick_bo3_row(rows: list[Bo3Row], *, opponent: str, when: datetime) -> str | None:
    """The series whose other team is the opponent, on this day or the day beside it."""
    day = when.astimezone(timezone.utc).date()
    ranked: list[tuple[int, str]] = []
    for row in rows:
        left = _same_team(opponent, row.left)
        right = _same_team(opponent, row.right)
        if left == right:
            continue
        delta = abs((row.when.astimezone(timezone.utc).date() - day).days)
        if delta <= 1:
            ranked.append((delta, row.url))
    if not ranked:
        return None
    best = min(delta for delta, _url in ranked)
    tied = [url for delta, url in ranked if delta == best]
    return tied[0] if len(tied) == 1 else None


def _bo3_page(url: str) -> bool:
    folded = url.casefold()
    return "bo3.gg/" in folded and "/matches/" in folded


def _bo3_side(team: object) -> str:
    if not isinstance(team, dict):
        return ""
    return str(team.get("name") or "").strip()


def _bo3_url(path: str, params: dict[str, str]) -> str:
    return f"{_BO3_API}{path}?{urlencode(params)}"


def _bo3_span(when: datetime) -> tuple[str, str]:
    """Dates covering this day and the day on either side."""
    day = when.astimezone(timezone.utc).date()
    return (day - timedelta(days=2)).isoformat(), (day + timedelta(days=2)).isoformat()


async def find_bo3_url(
    get: _Get,
    *,
    team: str,
    opponent: str,
    when: datetime,
    game: _Bo3Game,
) -> str | None:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    search = await get(
        _bo3_url(
            "/filters/teams",
            {
                "page[offset]": "0",
                "page[limit]": "20",
                "filter[teams.discipline_id][eq]": game.discipline,
                "search_text": team,
            },
        ),
        headers,
    )
    team_id = pick_bo3_team(bo3_teams(str(search or "")), team)
    if team_id is None:
        return None
    start, end = _bo3_span(when)
    page = await get(
        _bo3_url(
            "/matches",
            {
                "scope": "widget-matches",
                "page[offset]": "0",
                "page[limit]": "50",
                "sort": "start_date",
                "filter[matches.status][in]": "finished,defwin,upcoming,current",
                "filter[matches.team_ids][overlap]": team_id,
                "filter[matches.start_date][gt]": start,
                "filter[matches.start_date][lt]": end,
                "filter[matches.discipline_id][eq]": game.discipline,
                "with": "teams",
            },
        ),
        headers,
    )
    return pick_bo3_row(
        bo3_rows(str(page or ""), section=game.section),
        opponent=opponent,
        when=when,
    )


async def _resolve_bo3(
    match: Match,
    *,
    cache: LinkCache,
    get: _Get,
    now: datetime,
) -> ResolvedLink:
    """The four majors use the BO3 match list. Only a saved BO3 page is reused."""
    game = _BO3_GAMES.get(match.game.casefold())
    if game is None:
        return stream_or_none(match)
    cached = cache.get(match.match_id)
    saved = cached if cached and _bo3_page(cached) else None
    if saved and not cache.is_due_for_retry(match.match_id, now=now):
        return ResolvedLink(saved, "bo3")
    if cache.miss_holding(match.match_id, now=now):
        return stream_or_none(match)
    if match.scheduled_at is None:
        return stream_or_none(match)
    try:
        page = await find_bo3_url(
            get,
            team=match.followed.name,
            opponent=match.opponent.name,
            when=match.scheduled_at,
            game=game,
        )
    except Exception:
        if saved:
            return ResolvedLink(saved, "bo3")
        return stream_or_none(match)
    if page:
        cache.remember_hit(match.match_id, page)
        return ResolvedLink(page, "bo3")
    if saved:
        return ResolvedLink(saved, "bo3")
    cache.remember_miss(match.match_id, now=now)
    return stream_or_none(match)


def stream_or_none(match: Match) -> ResolvedLink:
    if match.stream_url:
        return ResolvedLink(match.stream_url, "stream")
    return ResolvedLink("", "none")


async def resolve_match_url(
    match: Match,
    *,
    cache: LinkCache,
    get: _Get,
    now: datetime,
) -> ResolvedLink:
    if match.game.casefold() in _BO3_GAMES:
        return await _resolve_bo3(match, cache=cache, get=get, now=now)
    return stream_or_none(match)

