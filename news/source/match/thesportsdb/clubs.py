"""Club names, aliases, and TheSportsDB team search."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlencode

from config import BASE_DIR
from fetch import FetchError, GetText, json_headers, json_rows, load_json
from .espn import LEAGUE_BY_ID, LEAGUE_BY_NAME

DEFAULT_SPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json"
DEFAULT_SPORTSDB_KEY = "123"
SPORTSDB_CACHE_TTL = 300.0
SPORTSDB_MINUTE_CAP = 28
CLUB_ALIASES_PATH = BASE_DIR / "data" / "sports_aliases.json"
_FUZZY_CUTOFF = 0.78
_FUZZY_MIN_LEN = 4
_CLUB_SEARCH_SECONDS = 2.0
_SPORT_TITLES = {
    "soccer": "Soccer",
    "basketball": "Basketball",
    "american football": "American Football",
    "football": "American Football",
    "baseball": "Baseball",
    "ice hockey": "Ice Hockey",
    "hockey": "Ice Hockey",
    "tennis": "Tennis",
}
_MINOR_LEAGUE_RE = re.compile(
    r"ncaa|ncaaf|ncaab|college|g league|gleague|naia|youth|u-?1[79]|u-?21|"
    r"reserve|amateur|high school|njcaa|gliac|rugby|esports|e.?sports|"
    r"futsal|beach|oberliga|national 3|_no league|womens super league|"
    r"women's super league",
    re.IGNORECASE,
)


class ClubAlias(NamedTuple):
    name: str
    sport: str
    team_id: str = ""


class TeamHit(NamedTuple):
    team_id: str
    name: str
    sport: str = ""
    league: str = ""
    league_id: str = ""
    short: str = ""
    alternate: str = ""
    keywords: str = ""
    loved: int = 0


_club_aliases: dict[str, tuple[ClubAlias, ...]] | None = None


def sportsdb_url(
    path: str,
    params: Mapping[str, str],
    *,
    base: str = DEFAULT_SPORTSDB_BASE,
    key: str = DEFAULT_SPORTSDB_KEY,
) -> str:
    root = f"{base.rstrip('/')}/{key}/{path.lstrip('/')}"
    query = urlencode(params)
    return f"{root}?{query}" if query else root


class SportsdbClient:
    """Shared JSON memo for lookup, next/last, and hole-day boards."""

    def __init__(
        self,
        get: Callable[..., Any],
        *,
        base: str,
        key: str,
        ttl: float = SPORTSDB_CACHE_TTL,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._get = get
        self._base = base
        self._key = key
        self._ttl = ttl
        self._monotonic = monotonic or time.monotonic
        self._memo: dict[str, tuple[float, Any]] = {}
        self._hits: list[float] = []
        self.day_scan_cut = False

    async def json(self, path: str, params: Mapping[str, str], what: str) -> Any:
        url = sportsdb_url(path, params, base=self._base, key=self._key)
        hit = self._memo.get(url)
        now = self._monotonic()
        if hit is not None and hit[0] > now:
            return hit[1]
        if path == "eventsday.php" and self._over_cap(now):
            self.day_scan_cut = True
            return None
        try:
            payload = load_json(await self._get(url, json_headers()), what=what)
        except FetchError:
            if path != "eventsday.php":
                raise
            self.day_scan_cut = True
            return None
        self._hits.append(now)
        self._memo[url] = (now + self._ttl, payload)
        return payload

    def _over_cap(self, now: float) -> bool:
        self._hits = [stamp for stamp in self._hits if now - stamp < 60]
        return len(self._hits) >= SPORTSDB_MINUTE_CAP


def title_sport(kind: str) -> str:
    key = kind.strip().casefold()
    if not key:
        return ""
    return _SPORT_TITLES.get(key, kind.strip()[:1].upper() + kind.strip()[1:])


def _query_key(query: str) -> str:
    return " ".join(query.casefold().split())


def load_club_aliases(path: Path | None = None) -> dict[str, tuple[ClubAlias, ...]]:
    global _club_aliases
    if path is None and _club_aliases is not None:
        return _club_aliases
    table: dict[str, list[ClubAlias]] = {}
    used = path or CLUB_ALIASES_PATH
    raw = json.loads(used.read_text(encoding="utf-8")) if used.is_file() else {}
    if isinstance(raw, dict):
        for sport, clubs in raw.items():
            if not isinstance(clubs, dict):
                continue
            titled = title_sport(str(sport)) or str(sport).strip()
            for nick, spec in clubs.items():
                entry = _parse_alias(str(nick), spec, titled)
                if entry is None:
                    continue
                for token in (str(nick), entry.name, entry.team_id):
                    folded = _query_key(token)
                    if not folded:
                        continue
                    bucket = table.setdefault(folded, [])
                    if not any(_same_club(existing, entry) for existing in bucket):
                        bucket.append(entry)
    frozen = {key: tuple(value) for key, value in table.items()}
    if path is None:
        _club_aliases = frozen
    return frozen


def club_aliases(query: str) -> tuple[ClubAlias, ...]:
    return load_club_aliases().get(_query_key(query), ())


def club_alias(query: str) -> ClubAlias | None:
    hits = club_aliases(query)
    return hits[0] if len(hits) == 1 else None


def suggest_clubs(query: str = "", *, limit: int = 25) -> list[ClubAlias]:
    """Unique clubs whose name or alias is close to query. Empty query lists some."""
    needle = _query_key(query)
    unique: dict[str, ClubAlias] = {}
    for key, hits in load_club_aliases().items():
        for alias in hits:
            ident = _club_ident(alias)
            if ident in unique or not _club_matches(needle, alias, key):
                continue
            unique[ident] = alias
    return _ranked_clubs(unique.values(), needle, limit)


async def search_club_suggestions(
    query: str,
    *,
    get: GetText | None = None,
    sportsdb_base: str | None = None,
    sportsdb_key: str | None = None,
    limit: int = 25,
) -> list[ClubAlias]:
    """Local aliases plus TheSportsDB search. Sport is kept so namesakes stay distinct."""
    cap = max(0, limit)
    local = suggest_clubs(query, limit=max(cap, 25))
    typed = query.strip()
    if get is None or len(_query_key(typed)) < 3:
        return local[:cap]
    term = _search_term(typed, local)
    url = sportsdb_url(
        "searchteams.php",
        {"t": term},
        base=sportsdb_base or DEFAULT_SPORTSDB_BASE,
        key=sportsdb_key or DEFAULT_SPORTSDB_KEY,
    )
    try:
        body = await asyncio.wait_for(get(url, json_headers()), _CLUB_SEARCH_SECONDS)
        hits = parse_sportsdb_team_list(load_json(body, what="Team search"))
    except (FetchError, TimeoutError):
        return local[:cap]
    alias = club_alias(term)
    remote = [
        ClubAlias(
            name=hit.name,
            sport=title_sport(hit.sport) or hit.sport,
            team_id=hit.team_id,
        )
        for hit in sorted(hits, key=lambda row: _team_rank(row, term, alias), reverse=True)
        if _is_famous_league(hit) or not _is_minor_league(hit)
    ]
    unique: dict[str, ClubAlias] = {}
    for item in (*local, *remote):
        ident = _club_ident(item)
        existing = unique.get(ident)
        if existing is None or (not existing.team_id and item.team_id):
            unique[ident] = item
    return _ranked_clubs(unique.values(), _query_key(typed), cap)


def official_club_name(account: str, label: str = "") -> str:
    hit = _named_club(account, label)
    return hit.name if hit is not None else (label or account).strip()


def sport_for_club(account: str, label: str = "", stored: str = "") -> str:
    hit = _named_club(account, label)
    return hit.sport if hit is not None else title_sport(stored)


def parse_sportsdb_team_list(payload: Any) -> list[TeamHit]:
    found: list[TeamHit] = []
    seen: set[str] = set()
    for team in json_rows(payload, "teams"):
        hit = _team_hit(team)
        if hit is None or hit.team_id in seen:
            continue
        seen.add(hit.team_id)
        found.append(hit)
    return found


def pick_team(matches: Sequence[TeamHit], query: str) -> tuple[str, str]:
    if not matches:
        raise FetchError(f"No team named {query}")
    alias = club_alias(query)
    ranked = sorted(
        matches,
        key=lambda hit: (_team_rank(hit, query, alias), hit.name),
        reverse=True,
    )
    best = ranked[0]
    if len(ranked) == 1 or _team_rank(best, query, alias) > _team_rank(ranked[1], query, alias):
        return best.team_id, best.name
    ids = ", ".join(f"thesportsdb:{hit.team_id}" for hit in ranked[:5])
    raise FetchError(f"Name matches more than one team. Use {ids}")


async def resolve_team(
    account: str,
    *,
    client: SportsdbClient,
) -> tuple[str, str, str, str]:
    aliases = club_aliases(account)
    if len(aliases) > 1:
        tokens = ", ".join(
            f"thesportsdb:{hit.team_id or hit.name}" for hit in aliases
        )
        raise FetchError(f"Name matches more than one team. Use {tokens}")
    alias = aliases[0] if aliases else None
    lookup = (alias.team_id or alias.name) if alias is not None else account
    if lookup.isdigit():
        matches = parse_sportsdb_team_list(
            await client.json("lookupteam.php", {"id": lookup}, "Team lookup")
        )
        if not matches:
            raise FetchError(f"No team {lookup}")
        row = matches[0]
        team_id, name, sport, league_id = (
            row.team_id,
            row.name,
            row.sport,
            row.league_id,
        )
    else:
        hits = {
            hit.team_id: hit
            for hit in parse_sportsdb_team_list(
                await client.json("searchteams.php", {"t": lookup}, "Team search")
            )
        }
        team_id, name = pick_team(list(hits.values()), account)
        row = hits.get(team_id)
        sport = row.sport if row is not None else ""
        league_id = row.league_id if row is not None else ""
    if alias is not None:
        name, sport = alias.name, alias.sport
    return team_id, name, sport, league_id


def _named_club(account: str, label: str = "") -> ClubAlias | None:
    for raw in (label, account):
        hit = club_alias(raw)
        if hit is not None:
            return hit
    return None


def _parse_alias(nick: str, spec: object, sport: str) -> ClubAlias | None:
    if isinstance(spec, str):
        name, team_id = spec.strip(), ""
    elif isinstance(spec, dict):
        name = str(spec.get("name") or nick).strip()
        team_id = str(spec.get("id") or "").strip()
    else:
        return None
    return ClubAlias(name=name, sport=sport, team_id=team_id) if name else None


def _same_club(left: ClubAlias, right: ClubAlias) -> bool:
    if left.team_id and right.team_id:
        return left.team_id == right.team_id
    return _query_key(left.name) == _query_key(right.name) and left.sport == right.sport


def _club_ident(alias: ClubAlias) -> str:
    return alias.team_id or f"{_query_key(alias.name)}\0{alias.sport}"


def _club_matches(needle: str, alias: ClubAlias, key: str) -> bool:
    if not needle:
        return True
    name = _query_key(alias.name)
    if needle in key or needle in name:
        return True
    return len(needle) >= _FUZZY_MIN_LEN and _alias_score(needle, alias, key) >= _FUZZY_CUTOFF


def _alias_score(needle: str, alias: ClubAlias, key: str = "") -> float:
    """Best ratio against the nick, name, id, and city prefixes ('los angeles')."""
    if not needle:
        return 0.0
    name = _query_key(alias.name)
    words = name.split()
    options = [name, key, _query_key(alias.team_id)]
    options.extend(" ".join(words[:index]) for index in range(1, len(words)))
    return max(
        (SequenceMatcher(None, needle, option).ratio() for option in options if option),
        default=0.0,
    )


def _suggest_rank(alias: ClubAlias, needle: str) -> tuple:
    name = _query_key(alias.name)
    sport = alias.sport.casefold()
    if not needle:
        return (sport, name)
    if name == needle:
        return (0, sport, name)
    if name.startswith(needle):
        return (1, sport, name)
    if needle in name:
        return (2, sport, name)
    return (3, -_alias_score(needle, alias), sport, name)


def _ranked_clubs(aliases: Iterable[ClubAlias], needle: str, limit: int) -> list[ClubAlias]:
    return sorted(aliases, key=lambda alias: _suggest_rank(alias, needle))[: max(0, limit)]


def _search_term(query: str, local: Sequence[ClubAlias]) -> str:
    """Typo queries search the shared city/name prefix of local hits ('los angeles')."""
    raw = query.strip()
    needle = _query_key(query)
    if not local or any(needle in _query_key(alias.name) for alias in local):
        return raw
    parts = [_query_key(alias.name).split() for alias in local]
    shared: list[str] = []
    for words in zip(*parts):
        if len(set(words)) != 1:
            break
        shared.append(words[0])
    return " ".join(shared) or _query_key(local[0].name)


def _field(raw: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        text = str(raw.get(key) or "").strip()
        if text:
            return text
    return ""


def _team_hit(raw: Mapping[str, Any]) -> TeamHit | None:
    team_id = _field(raw, "idTeam")
    name = _field(raw, "strTeam")
    if not team_id.isdigit() or not name:
        return None
    loved = _field(raw, "intLoved")
    return TeamHit(
        team_id=team_id,
        name=name,
        sport=_field(raw, "strSport"),
        league=_field(raw, "strLeague"),
        league_id=_field(raw, "idLeague"),
        short=_field(raw, "strTeamShort"),
        alternate=_field(raw, "strTeamAlternate", "strAlternate"),
        keywords=_field(raw, "strKeywords"),
        loved=int(loved) if loved.isdigit() else 0,
    )


def _is_famous_league(hit: TeamHit) -> bool:
    if hit.league_id in LEAGUE_BY_ID:
        return True
    key = re.sub(r"[^a-z0-9]+", "", hit.league.casefold())
    return key in LEAGUE_BY_NAME


def _is_minor_league(hit: TeamHit) -> bool:
    return bool(_MINOR_LEAGUE_RE.search(f"{hit.league} {hit.sport}"))


def _team_rank(hit: TeamHit, query: str, alias: ClubAlias | None) -> tuple:
    """Earlier fields win: alias, exact name, famous league, then weaker name hits."""
    q = _query_key(query)
    name = _query_key(hit.name)
    name_tokens = name.split()
    query_tokens = q.split()
    alt = _query_key(f"{hit.alternate} {hit.keywords}")
    famous = _is_famous_league(hit)
    return (
        bool(alias and alias.team_id and hit.team_id == alias.team_id),
        bool(alias and name == _query_key(alias.name)),
        name == q,
        famous,
        not _is_minor_league(hit),
        not (alias and hit.sport and title_sport(hit.sport) != alias.sport),
        bool(query_tokens and name_tokens[-len(query_tokens) :] == query_tokens),
        bool(q and q in name_tokens),
        bool(hit.short and hit.short.casefold() == q),
        bool(q and q in alt),
        bool(famous and alias is None and hit.sport.casefold() == "soccer"),
        min(hit.loved, 8),
    )
