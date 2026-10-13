"""PandaScore HTTP boundary and Match domain types."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlencode

from fetch import FetchError, load_json
from news.source.match.common import parse_iso

WireStatus = Literal["not_started", "running", "finished", "canceled", "postponed"]
Stage = Literal["next", "start", "end", "skip"]

DEFAULT_BASE = "https://api.pandascore.co"
PAST_PER_PAGE = 5
NAME = "pandascore"
_Get = Callable[[str, dict[str, str] | None], Any]

_STAGE: dict[str, Stage] = {
    "not_started": "next",
    "running": "start",
    "finished": "end",
    "canceled": "skip",
    "postponed": "skip",
}


@dataclass(frozen=True)
class TeamRef:
    slug: str
    name: str
    game: str = ""
    team_id: int | None = None


@dataclass(frozen=True)
class SeriesScore:
    followed: int
    opponent: int


@dataclass(frozen=True)
class Match:
    match_id: str
    status: WireStatus
    followed: TeamRef
    opponent: TeamRef
    scheduled_at: datetime | None
    series: SeriesScore | None
    best_of: int | None
    league: str
    serie: str
    game: str
    stream_url: str | None
    image_url: str | None


def stage_for(status: WireStatus) -> Stage | None:
    return _STAGE.get(status)


def parse_match(row: object, *, followed_slug: str) -> Match | None:
    if not isinstance(row, Mapping):
        return None
    match_id = str(row.get("id") or "").strip()
    status = str(row.get("status") or "").strip()
    if not match_id or status not in _STAGE:
        return None
    followed, opponent = _sides(row, followed_slug)
    if followed is None or opponent is None:
        return None
    scheduled = parse_iso(str(row.get("scheduled_at") or row.get("begin_at") or ""))
    series = _series_score(row, followed_id=_team_id(row, followed.slug))
    league, stage_name = _card_names(row)
    return Match(
        match_id=match_id,
        status=status,  # type: ignore[arg-type]
        followed=followed,
        opponent=opponent,
        scheduled_at=scheduled,
        series=series,
        best_of=_int(row.get("number_of_games")),
        league=league,
        serie=stage_name,
        game=_named(row, "videogame"),
        stream_url=_main_stream(row.get("streams_list")),
        image_url=_image(row),
    )


def _opponents(row: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for entry in row.get("opponents") or []:
        opp = entry.get("opponent") if isinstance(entry, Mapping) else None
        if isinstance(opp, Mapping):
            yield opp


def _sides(
    row: Mapping[str, Any], followed_slug: str
) -> tuple[TeamRef | None, TeamRef | None]:
    wanted = followed_slug.casefold()
    followed: TeamRef | None = None
    others: list[TeamRef] = []
    for opp in _opponents(row):
        slug = str(opp.get("slug") or "").strip()
        name = str(opp.get("name") or "").strip() or slug
        if not slug and not name:
            continue
        ref = TeamRef(slug=slug or name.casefold().replace(" ", "-"), name=name)
        if slug.casefold() == wanted or str(opp.get("id") or "") == followed_slug:
            followed = ref
        else:
            others.append(ref)
    if followed is None or not others:
        return followed, None
    return followed, others[0]


def _team_id(row: Mapping[str, Any], slug: str) -> int | None:
    wanted = slug.casefold()
    for opp in _opponents(row):
        if str(opp.get("slug") or "").casefold() == wanted:
            return _int(opp.get("id"))
    return None


def _series_score(row: Mapping[str, Any], *, followed_id: int | None) -> SeriesScore | None:
    if followed_id is None:
        return None
    followed_score = None
    opponent_score = None
    for entry in row.get("results") or []:
        if not isinstance(entry, Mapping):
            continue
        team_id = _int(entry.get("team_id"))
        score = _int(entry.get("score"))
        if team_id is None or score is None:
            continue
        if team_id == followed_id:
            followed_score = score
        else:
            opponent_score = score
    if followed_score is None or opponent_score is None:
        return None
    return SeriesScore(followed=followed_score, opponent=opponent_score)


def _named(row: Mapping[str, Any], key: str) -> str:
    block = row.get(key)
    if isinstance(block, Mapping):
        return str(block.get("name") or "").strip()
    return ""


def _card_names(row: Mapping[str, Any]) -> tuple[str, str]:
    """League line is the event. Stage line is the phase inside it.

    A serie titled like Stage 1 is already the phase. Champions is the event,
    so it joins the league and the tournament name is the phase.
    """
    league = _named(row, "league")
    serie = _named(row, "serie")
    tournament = _named(row, "tournament")
    fallback = str(row.get("name") or "").strip()
    if _serie_is_phase(serie):
        return league or fallback, _phase_value(serie)
    event = _join_event(league, serie) or fallback
    return event, _phase_value(tournament)


def _serie_is_phase(name: str) -> bool:
    key = name.strip().casefold()
    return key.isdigit() or key.startswith("stage")


def _phase_value(name: str) -> str:
    text = name.strip()
    if text.casefold().startswith("stage"):
        rest = text[5:].strip(" :-.")
        return rest or text
    return text


def _join_event(league: str, serie: str) -> str:
    if not serie:
        return league
    if not league or serie.casefold().startswith(league.casefold()):
        return serie
    return f"{league} {serie}"


def _main_stream(streams: object) -> str | None:
    if not isinstance(streams, list):
        return None
    first = None
    for entry in streams:
        if not isinstance(entry, Mapping):
            continue
        url = str(entry.get("raw_url") or "").strip()
        if not url:
            continue
        if first is None:
            first = url
        if entry.get("main") is True:
            return url
    return first


def _image(row: Mapping[str, Any]) -> str | None:
    for key in ("league", "serie"):
        block = row.get(key)
        if isinstance(block, Mapping):
            url = str(block.get("image_url") or "").strip()
            if url:
                return url
    return None


def _int(value: object) -> int | None:
    if value is None or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class PandaScoreClient:
    def __init__(self, get: _Get, *, token: str | None, base: str = DEFAULT_BASE) -> None:
        self._get = get
        self._token = (token or "").strip()
        self._base = base.rstrip("/")

    def _require_token(self) -> str:
        if not self._token:
            raise FetchError("Set PANDASCORE_TOKEN")
        return self._token

    async def running(self, team: TeamRef) -> list[Match]:
        return await self._matches("/matches/running", team)

    async def upcoming(self, team: TeamRef) -> list[Match]:
        return await self._matches("/matches/upcoming", team, sort="scheduled_at")

    async def past(self, team: TeamRef) -> list[Match]:
        return await self._matches(
            "/matches/past", team, sort="-scheduled_at", per_page=str(PAST_PER_PAGE)
        )

    async def team_matches(
        self, team: TeamRef, pinned: Sequence[str] = ()
    ) -> list[Match]:
        """Running, then upcoming, then past. A later list replaces a not-started row.

        Pinned ids missing from those lists are looked up so an open card can finish.
        """
        out: dict[str, Match] = {}
        for batch in (await self.running(team), await self.upcoming(team), await self.past(team)):
            for match in batch:
                current = out.get(match.match_id)
                if current is None or (
                    current.status == "not_started" and match.status != "not_started"
                ):
                    out[match.match_id] = match
        for event_id in pinned:
            if event_id and event_id not in out:
                found = await self._match(event_id, team)
                if found is not None:
                    out[found.match_id] = found
        return list(out.values())

    async def _match(self, match_id: str, team: TeamRef) -> Match | None:
        try:
            payload = await self._json(f"/matches/{match_id}", {}, "PandaScore match")
        except FetchError:
            return None
        return parse_match(payload, followed_slug=team.slug)

    async def resolve_team(self, account: str) -> TeamRef:
        token = " ".join(account.split())
        if not token:
            raise FetchError("No team")
        from news.source.match.pandascore.matchpage import _same_team, alias_names

        names = alias_names(token)
        if " " not in token and token == token.lower():
            hits = await self._teams({"filter[slug]": token})
            chosen = next((hit for hit in hits if hit.slug == token), None)
            if chosen is not None:
                return chosen
            if not names:
                raise FetchError(f"No team {token}")
        hits = await self.find_teams(token)
        if not hits:
            raise FetchError(f"No team named {token}")
        if not names:
            exact = [hit for hit in hits if hit.name.casefold() == token.casefold()]
            hits = exact or hits
        if len(hits) > 1:
            ids = ", ".join(f"{NAME}:{hit.slug}" for hit in hits)
            raise FetchError(f"Name matches more than one team. Use {ids}")
        hit = hits[0]
        title = next((official for official in names if _same_team(hit.name, official)), hit.name)
        return TeamRef(slug=hit.slug, name=title, game=hit.game, team_id=hit.team_id)

    async def find_teams(self, typed: str) -> list[TeamRef]:
        """Teams for a nick or official name.

        Asks for each alias name and the typed text, plus the acronym when the
        text is one word. Alias hits stay only when the name still matches.
        """
        from news.source.match.pandascore.matchpage import _same_team, alias_names, team_queries

        token = " ".join(typed.split())
        if not token:
            return []
        self._require_token()
        names = alias_names(token)
        hits: list[TeamRef] = []
        seen: set[str] = set()

        async def take(params: dict[str, str]) -> None:
            for hit in await self._teams(params):
                if hit.slug in seen:
                    continue
                seen.add(hit.slug)
                hits.append(hit)

        for query in team_queries(token):
            await take({"search[name]": query})
        if " " not in token:
            await take({"filter[acronym]": token})
        if not names:
            return hits
        return [hit for hit in hits if any(_same_team(hit.name, official) for official in names)]

    async def _matches(self, path: str, team: TeamRef, **extra: str) -> list[Match]:
        if team.team_id is None:
            raise FetchError(f"No team id for {team.slug}")
        params = {"filter[opponent_id]": str(team.team_id), **extra}
        payload = await self._json(path, params, "PandaScore matches")
        if not isinstance(payload, list):
            return []
        matches = (parse_match(row, followed_slug=team.slug) for row in payload)
        return [match for match in matches if match is not None]

    async def _teams(self, params: Mapping[str, str]) -> list[TeamRef]:
        payload = await self._json("/teams", params, "PandaScore teams")
        if not isinstance(payload, list):
            return []
        hits: list[TeamRef] = []
        for row in payload:
            if not isinstance(row, Mapping):
                continue
            slug = str(row.get("slug") or "").strip()
            name = str(row.get("name") or "").strip()
            if not slug or not name:
                continue
            game = ""
            current = row.get("current_videogame")
            if isinstance(current, Mapping):
                game = str(current.get("name") or "").strip()
            hits.append(TeamRef(slug=slug, name=name, game=game, team_id=_int(row.get("id"))))
        return hits

    async def _json(self, path: str, params: Mapping[str, str], what: str) -> Any:
        token = self._require_token()
        query = urlencode({key: value for key, value in params.items() if value})
        url = f"{self._base}{path}"
        if query:
            url = f"{url}?{query}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Nyatify"}
        body = await self._get(url, headers)
        return load_json(body, what=what)
