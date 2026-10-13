"""ESPN scoreboard links and match facts for TheSportsDB cards."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fetch import FetchError, GetText, http_url, json_headers, load_json

from .scoreboard import EspnGame, parse_espn_scoreboard, parse_espn_summary

if TYPE_CHECKING:
    from .matchstate import MatchState

DEFAULT_ESPN_API = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_BOARD_CAP = 6
ESPN_SUMMARY_CAP = 6
ESPN_RESULT_DAYS = 4
US_TZ = ZoneInfo("America/New_York")
_KICKOFF_SLACK = timedelta(hours=12)
_ESPN_PHASE = {"pre": "scheduled", "in": "live", "post": "finished"}
_WATCH = {"live": 0, "over": 1, "skip": 2, "soon": 3}
_SPORT_PATH = {
    "soccer": "soccer",
    "basketball": "basketball",
    "ice hockey": "hockey",
    "american football": "football",
    "baseball": "baseball",
    "tennis": "tennis",
    "motorsport": "racing",
}
_US_LEAGUES = frozenset({"nba", "nfl", "mlb", "nhl", "wnba", "usa.1"})
LEAGUE_BY_ID = {
    "4328": ("soccer", "eng.1"), "4330": ("soccer", "sco.1"),
    "4331": ("soccer", "ger.1"), "4332": ("soccer", "ita.1"),
    "4334": ("soccer", "fra.1"), "4335": ("soccer", "esp.1"),
    "4337": ("soccer", "ned.1"), "4344": ("soccer", "por.1"),
    "4346": ("soccer", "usa.1"), "4351": ("soccer", "bra.1"),
    "4370": ("racing", "f1"), "4380": ("hockey", "nhl"),
    "4387": ("basketball", "nba"), "4391": ("football", "nfl"),
    "4424": ("baseball", "mlb"), "4480": ("soccer", "uefa.champions"),
    "4481": ("soccer", "uefa.europa"), "4482": ("soccer", "eng.fa"),
    "4483": ("soccer", "esp.copa_del_rey"), "4484": ("soccer", "fra.coupe_de_france"),
    "4485": ("soccer", "ger.dfb_pokal"), "4506": ("soccer", "ita.coppa_italia"),
    "4511": ("soccer", "esp.super_cup"), "4570": ("soccer", "eng.league_cup"),
    "5071": ("soccer", "uefa.europa.conf"),
}
LEAGUE_BY_NAME = {
    alias: (sport, slug)
    for sport, slug, names in (
        ("soccer", "eng.1", "englishpremierleague premierleague"),
        ("soccer", "esp.1", "spanishlaliga laliga"),
        ("soccer", "ger.1", "germanbundesliga bundesliga fussballbundesliga"),
        ("soccer", "ita.1", "italianseriea italyseriea"),
        ("soccer", "fra.1", "frenchligue1 ligue1"),
        ("soccer", "usa.1", "mls majorleaguesoccer americanmajorleaguesoccer"),
        ("soccer", "uefa.champions", "uefachampionsleague championsleague"),
        ("soccer", "uefa.europa", "uefaeuropaleague europaleague"),
        ("soccer", "uefa.europa.conf", "uefaconferenceleague uecl"),
        ("soccer", "ned.1", "dutcheredivisie eredivisie"),
        ("soccer", "por.1", "portugueseprimeiraliga primeiraliga liganos"),
        ("soccer", "sco.1", "scottishpremiership scottishpremierleague"),
        ("soccer", "bra.1", "brazilianseriea brasileiraoseriea"),
        ("soccer", "eng.fa", "facup footballassociationcup theemiratesfacup"),
        ("soccer", "esp.copa_del_rey", "copadelrey spanishcup"),
        ("soccer", "fra.coupe_de_france", "coupedefrance frenchcup"),
        ("soccer", "ger.dfb_pokal", "dfbpokal dfbcup germancup"),
        ("soccer", "ita.coppa_italia", "coppaitalia"),
        ("soccer", "esp.super_cup", "supercopadeespana spanishsupercup spanishsupercopa"),
        ("soccer", "eng.league_cup", "eflcup carabaocup footballleaguecup"),
        ("basketball", "nba", "nba nationalbasketballassociation"),
        ("basketball", "wnba", "wnba"),
        ("basketball", "euroleague", "euroleague turkishairlineseuroleague"),
        ("football", "nfl", "nfl nationalfootballleague"),
        ("baseball", "mlb", "mlb majorleaguebaseball"),
        ("hockey", "nhl", "nhl nationalhockeyleague"),
        ("racing", "f1", "formula1 formulaone"),
    )
    for alias in names.split()
}
_SITE = {
    "/soccer/": ("soccer", "match", "team-stats"),
    "/nba/": ("nba", "game", "boxscore"),
    "/wnba/": ("wnba", "game", "boxscore"),
    "/nfl/": ("nfl", "game", "boxscore"),
    "/mlb/": ("mlb", "game", "boxscore"),
    "/nhl/": ("nhl", "game", "boxscore"),
}
_BOX = (" quarters:", " periods:", " innings:", " sets:")
_NAME_SKIP = frozenset({"fc", "cf", "afc", "sc", "ac", "club", "de", "the"})
_NAME_WEAK = frozenset({"united", "city", "real", "sporting", "athletic", "milan"})
_GAME_ID_RE = re.compile(r"gameId(?:=|/)(\d+)", re.IGNORECASE)
_LETTER_NAME = re.compile(r"(?:LETTER|LIGATURE) ([A-Z]+(?:-[A-Z]+)?)")


def espn_game_id(url: str) -> str | None:
    match = _GAME_ID_RE.search(url)
    return match.group(1) if match else None


def espn_card_url(url: str, *, kind: str | None = None) -> str | None:
    """Keep a live ESPN URL; rewrite full time to the stats page."""
    found = http_url(url)
    if not found:
        return None
    game_id = espn_game_id(found)
    path = urlsplit(found).path.lower()
    site = next((item for needle, item in _SITE.items() if needle in path), None)
    if game_id is None or site is None:
        return found
    slug, live, stats = site
    if kind != "end" and f"/{live}/" in found:
        return found
    page = stats if kind == "end" else live
    return f"https://www.espn.com/{slug}/{page}/_/gameId/{game_id}"


def espn_league_path(sport: str, league: str, league_id: str = "") -> tuple[str, str] | None:
    by_id = LEAGUE_BY_ID.get(league_id.strip())
    by_name = LEAGUE_BY_NAME.get(re.sub(r"[^a-z0-9]+", "", league.casefold()))
    mapped = by_name or by_id
    if mapped is None or _SPORT_PATH.get(sport.strip().lower()) != mapped[0]:
        return None
    return mapped


def espn_scoreboard_stamp(league: str, day: datetime) -> str:
    zone = US_TZ if league in _US_LEAGUES else timezone.utc
    return day.astimezone(zone).strftime("%Y%m%d")


def espn_scoreboard_stamps(league: str, day: datetime) -> tuple[str, ...]:
    primary = espn_scoreboard_stamp(league, day)
    utc = day.astimezone(timezone.utc).strftime("%Y%m%d")
    return (primary,) if utc == primary else (primary, utc)


def espn_scoreboard_url(
    sport_path: str,
    league: str,
    day: datetime,
    *,
    base: str = DEFAULT_ESPN_API,
    stamp: str | None = None,
) -> str:
    used = stamp or espn_scoreboard_stamp(league, day)
    return f"{base.rstrip('/')}/{sport_path}/{league}/scoreboard?dates={used}"


def espn_summary_url(
    sport_path: str,
    league: str,
    event_id: str,
    *,
    base: str = DEFAULT_ESPN_API,
) -> str:
    return f"{base.rstrip('/')}/{sport_path}/{league}/summary?event={event_id}"


def pick_espn_game(
    club: str,
    games: Sequence[EspnGame],
    *,
    kickoff: datetime | None = None,
) -> EspnGame | None:
    """The followed club's game on this board. Same game id counts once."""
    by_id: dict[str, EspnGame] = {}
    for game in games:
        if espn_side(club, game) is None:
            continue
        key = espn_game_id(game.url) or game.url
        if not key:
            continue
        seen = by_id.get(key)
        if seen is None or (seen.kickoff is None and game.kickoff is not None):
            by_id[key] = game
    hits = list(by_id.values())
    if kickoff is not None:
        near = [
            game
            for game in hits
            if game.kickoff is not None and abs(game.kickoff - kickoff) <= _KICKOFF_SLACK
        ]
        if near:
            hits = near
        elif any(game.kickoff is not None for game in hits):
            return None
    if len(hits) == 1:
        return hits[0]
    if kickoff is None or len(hits) < 2:
        return None
    timed = [game for game in hits if game.kickoff is not None]
    if not timed:
        return None
    closest = min(abs(game.kickoff - kickoff) for game in timed)
    close = [game for game in timed if abs(game.kickoff - kickoff) == closest]
    return close[0] if len(close) == 1 else None


def espn_side(name: str, game: EspnGame) -> str | None:
    """Which ESPN side this name is on, using every label ESPN printed."""
    home = _on_side(name, game.home, *game.home_labels)
    away = _on_side(name, game.away, *game.away_labels)
    if home == away:
        return None
    return "home" if home else "away"


def _on_side(name: str, *labels: str) -> bool:
    return bool(name.strip()) and any(names_match(name, label) for label in labels if label)


def names_match(left: str, right: str) -> bool:
    a, b = _name_key(left), _name_key(right)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 4 and short not in _NAME_WEAK and short == long.split()[-1]


def espn_facts(notes: Sequence[str]) -> str:
    unique = list(dict.fromkeys(line.strip() for line in notes if line and line.strip()))
    if unique and any(mark in unique[0] for mark in _BOX):
        return unique[0]
    return "\n".join(unique)


def _fact_parts(facts: str) -> tuple[str, ...]:
    parts: list[str] = []
    for line in facts.replace("\r\n", "\n").split("\n"):
        for part in line.split(" · "):
            text = part.strip()
            if text:
                parts.append(text)
    return tuple(parts)


async def with_espn(
    states: list[MatchState], *, get: GetText, now: datetime, club: str = ""
) -> list[MatchState]:
    """Spend board/summary budget on live, recent results, postponed, then previews."""
    boards: dict[tuple[str, str, str], list[EspnGame]] = {}
    spent = 0
    found: dict[str, EspnGame] = {}
    watching = sorted(
        ((_WATCH[view], state) for state in states if (view := _watch(state, now))),
        key=lambda row: (row[0], row[1].match_id),
    )
    for _, state in watching:
        game, spent = await _board(state, get, boards, spent, club)
        if game is not None:
            found[state.match_id] = game
    overlaid = [
        overlay_espn(state, found[state.match_id], from_board=True, now=now, club=club)
        if state.match_id in found
        else state
        for state in states
    ]
    used = 0
    filled: dict[str, MatchState] = {}
    for state in sorted(overlaid, key=lambda item: (_WATCH.get(_watch(item, now), 9), item.match_id)):
        extra = None
        path = espn_league_path(state.sport, state.league, state.league_id)
        game_id = espn_game_id(state.score_url or "")
        if _watch(state, now) in {"live", "over"} and path and game_id and used < ESPN_SUMMARY_CAP:
            used += 1
            extra = parse_espn_summary(
                await _json(get, espn_summary_url(path[0], path[1], game_id), "ESPN summary")
            )
        filled[state.match_id] = (
            overlay_espn(state, extra, from_board=False, now=now, club=club) if extra else state
        )
    return [filled[state.match_id] for state in overlaid]


def overlay_espn(
    state: MatchState,
    game: EspnGame,
    *,
    from_board: bool,
    now: datetime | None = None,
    club: str = "",
) -> MatchState:
    home, away = _scores_for_state(state, game, club)
    keep = not from_board and state.home_score and state.away_score
    prior = () if from_board else _fact_parts(state.facts)
    notes = tuple(dict.fromkeys([*prior, *(game.notes or game.leaders)]))
    return replace(
        state,
        score_url=(game.url or state.score_url) if from_board else state.score_url,
        clock=game.detail or state.clock,
        home_score=state.home_score if keep else (home or state.home_score),
        away_score=state.away_score if keep else (away or state.away_score),
        facts=espn_facts(notes) or state.facts,
        phase=_phase(state, game, now=now),
    )


def _scores_for_state(state: MatchState, game: EspnGame, club: str) -> tuple[str, str]:
    """Map ESPN's home/away scores onto SportsDB's home/away."""
    side = espn_side(state.home, game)
    if side is None and club.strip():
        ours = espn_side(club, game)
        on_home = _on_side(club, state.home) and not _on_side(club, state.away)
        on_away = _on_side(club, state.away) and not _on_side(club, state.home)
        if ours and on_home:
            side = ours
        elif ours and on_away:
            side = "home" if ours == "away" else "away"
    if side == "away":
        return game.away_score, game.home_score
    return game.home_score, game.away_score


def _watch(state: MatchState, now: datetime) -> str | None:
    """ESPN budget class from the card stage, plus the result and hold windows."""
    from news.source.match.common import END_TAG, NEXT_TAG, PREVIEW_BEFORE, SKIP_TAG, START_TAG, hold_until
    from .matchstate import _card_kind

    kind = _card_kind(state, now)
    if kind is None:
        return None
    kickoff, sport = state.kickoff, state.sport
    if kind == SKIP_TAG:
        return "skip" if kickoff is not None and hold_until(kickoff, sport) >= now else None
    if kind == END_TAG:
        if kickoff is None:
            return None
        held = hold_until(kickoff, sport) >= now or now - kickoff <= timedelta(days=ESPN_RESULT_DAYS)
        return "over" if held else None
    if kind == START_TAG:
        return "live"
    if kind == NEXT_TAG and (kickoff is None or kickoff <= now + PREVIEW_BEFORE):
        return "soon"
    return None


def _phase(state: MatchState, game: EspnGame, *, now: datetime | None = None) -> str:
    """ESPN retags only after kickoff. `in` can reopen extra time; `post` is Over."""
    from news.source.match.common import live_cap
    from .matchstate import PHASE_RANK

    if state.phase == "skip":
        return "skip"
    incoming = _ESPN_PHASE.get(game.state) or _clock_phase(game.detail, state.clock, state.status)
    if incoming is None:
        return state.phase
    wall = now or datetime.now(timezone.utc)
    kickoff = state.kickoff
    if kickoff is not None and kickoff > wall:
        return state.phase
    if incoming == "live":
        return "live" if kickoff is None or wall <= kickoff + live_cap(state.sport) else state.phase
    if incoming == "finished":
        return "finished"
    if PHASE_RANK.get(incoming, 1) >= PHASE_RANK.get(state.phase, 1):
        return incoming
    return state.phase


def _clock_phase(*texts: str) -> str | None:
    from .matchstate import classify_phase

    for text in texts:
        phase = classify_phase(text or "")
        if phase != "scheduled":
            return phase
    return None


async def _board(
    state: MatchState,
    get: GetText,
    boards: dict[tuple[str, str, str], list[EspnGame]],
    spent: int,
    club: str,
) -> tuple[EspnGame | None, int]:
    path = espn_league_path(state.sport, state.league, state.league_id)
    if path is None or state.kickoff is None or not club.strip():
        return None, spent
    sport_path, league = path
    dates = list(espn_scoreboard_stamps(league, state.kickoff))
    for delta in (-1, 1):
        extra = espn_scoreboard_stamp(league, state.kickoff + timedelta(days=delta))
        if extra not in dates:
            dates.append(extra)
    pool: list[EspnGame] = []
    for stamp in dates:
        key = (sport_path, league, stamp)
        if key not in boards:
            if spent >= ESPN_BOARD_CAP:
                continue
            spent += 1
            payload = await _json(
                get,
                espn_scoreboard_url(sport_path, league, state.kickoff, stamp=stamp),
                "ESPN scoreboard",
            )
            boards[key] = parse_espn_scoreboard(payload) if payload is not None else []
        pool.extend(boards[key])
        game = pick_espn_game(club, pool, kickoff=state.kickoff)
        if game is not None:
            return game, spent
    return None, spent


async def _json(get: GetText, url: str, what: str) -> Any | None:
    try:
        return load_json(await get(url, json_headers()), what=what)
    except FetchError:
        return None


def _name_key(name: str) -> str:
    from .clubs import official_club_name

    raw = name.strip()
    skipped = " ".join(word for word in raw.casefold().split() if word not in _NAME_SKIP)
    text = official_club_name(raw)
    if text == raw:
        text = official_club_name(skipped)
    folded: list[str] = []
    for ch in unicodedata.normalize("NFKD", text.casefold()):
        if unicodedata.combining(ch):
            continue
        if ch.isascii() and ch.isalnum():
            folded.append(ch)
        elif ch.isascii():
            folded.append(" ")
        else:
            match = _LETTER_NAME.search(unicodedata.name(ch, ""))
            folded.append(match.group(1).replace("-", "").casefold() if match else " ")
    return " ".join(word for word in "".join(folded).split() if word not in _NAME_SKIP)
