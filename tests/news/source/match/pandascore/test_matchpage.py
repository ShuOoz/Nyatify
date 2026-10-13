from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from tests.helpers import utc

SLUG = "paper-rex-valorant"
NOW = utc(2026, 9, 29, 12)


from news.source.match.pandascore.http import parse_match
from news.source.match.pandascore.matchpage import (
    LinkCache,
    _same_team,
    _squeeze_alias,
    alias_names,
    bo3_page,
    bo3_rows,
    bo3_teams,
    pick_bo3_row,
    pick_bo3_team,
    resolve_match_url,
)


def _team(team_id: int, slug: str, name: str) -> dict:
    return {"id": team_id, "slug": slug, "name": name}

def _match_row(
    match_id: int,
    *,
    status: str = "running",
    followed: dict | None = None,
    opponent: dict | None = None,
    begin_at: str = "2026-09-29T12:00:00Z",
    number_of_games: int = 3,
    followed_score: int = 1,
    opponent_score: int = 0,
    games: list | None = None,
    stream: str | None = "https://twitch.tv/main",
) -> dict:
    home = followed or _team(1, SLUG, "Paper Rex")
    away = opponent or _team(2, "gen-g-valorant", "Gen.G")
    return {
        "id": match_id,
        "status": status,
        "begin_at": begin_at,
        "number_of_games": number_of_games,
        "opponents": [
            {"opponent": home},
            {"opponent": away},
        ],
        "results": [
            {"team_id": home["id"], "score": followed_score},
            {"team_id": away["id"], "score": opponent_score},
        ],
        "games": games
        if games is not None
        else [
            {"position": 1, "status": "finished"},
            {"position": 2, "status": "running"},
            {"position": 3, "status": "not_started"},
        ],
        "league": {"name": "VCT Pacific", "image_url": "https://example.test/league.png"},
        "serie": {"name": "Stage 2"},
        "videogame": {"name": "Valorant"},
        "streams_list": [
            {"raw_url": "https://twitch.tv/other", "main": False},
            {"raw_url": stream, "main": True},
        ]
        if stream
        else [],
    }


async def test_match_pages(tmp_path):
    case_short_names_expand_to_full_team_names()
    case_team_names_match_across_spellings_and_keep_squads_apart()
    case_bo3_row_picks_the_opponent_on_that_day()
    bo3 = tmp_path / "bo3"
    bo3.mkdir()
    await case_major_games_use_bo3(bo3)
    stream = tmp_path / "stream"
    stream.mkdir()
    await case_other_games_use_the_stream(stream)
    miss = tmp_path / "miss"
    miss.mkdir()
    await case_miss_waits_then_looks_up_again(miss)
    err = tmp_path / "err"
    err.mkdir()
    await case_bo3_error_is_not_stored_as_a_miss(err)

def case_short_names_expand_to_full_team_names():
    raw = json.loads(
        (Path(__file__).resolve().parents[5] / "data" / "esports_aliases.json").read_text()
    )

    def games(nick: str) -> set[str]:
        return {game for game, clubs in raw.items() if nick in clubs}

    assert games("BLG") == {"League of Legends", "Valorant"}
    assert games("EDG") == {"League of Legends", "Valorant"}
    assert games("TL") == {"Counter-Strike", "League of Legends", "Valorant"}
    assert games("NAVI") == {"Counter-Strike", "Valorant"}
    assert games("GEN") == {"Counter-Strike", "League of Legends", "Valorant"}
    assert raw["Counter-Strike"]["RNG"]["name"] == "Renegades"
    assert raw["League of Legends"]["RNG"]["name"] == "Royal Never Give Up"
    assert raw["Valorant"]["RNG"]["name"] == "Royal Never Give Up"
    assert alias_names("NAVI") == ("Natus Vincere",)
    assert alias_names("Na'Vi") == ("Natus Vincere",)
    assert alias_names("BLG") == ("Bilibili Gaming",)
    assert alias_names("EDG") == ("EDward Gaming",)
    assert alias_names("PRX") == ("Paper Rex",)
    assert alias_names("TL") == ("Team Liquid",)
    assert alias_names("RNG") == ("Renegades", "Royal Never Give Up")
    assert alias_names("TS") == ("Team Spirit", "Team Secret")
    assert alias_names("TSW") == ("Team Secret Whales",)
    assert alias_names("FLY") == ("FlyQuest",)
    assert alias_names("SKT") == ("T1",)
    assert alias_names("MKOI") == ("Movistar KOI",)
    assert games("KOI") == {"League of Legends"}
    assert games("NS") == {"League of Legends", "Valorant"}
    assert games("ENVY") == {"Valorant"}
    assert "Apeks" not in raw["Valorant"]
    folded: dict[str, dict[str, str]] = {}
    for game, clubs in raw.items():
        for nick, spec in clubs.items():
            key = _squeeze_alias(nick)
            previous = folded.setdefault(game, {}).get(key)
            assert previous in (None, spec["name"])
            folded[game][key] = spec["name"]
    assert _same_team("NAVI", "Natus Vincere")
    assert _same_team("TL", "Team Liquid")
    assert _same_team("RNG", "Renegades")
    assert not _same_team("Renegades", "Royal Never Give Up")

def case_team_names_match_across_spellings_and_keep_squads_apart():
    assert _same_team("Vitality", "Team Vitality")
    assert _same_team("G2", "G2 Esports")
    assert _same_team("FaZe", "FaZe Clan")
    assert _same_team("NAVI", "Natus Vincere")
    assert _same_team("9 Pandas", "9Pandas")
    assert _same_team("Virtus.pro", "VP")
    assert _same_team("C9", "Cloud9")
    assert _same_team("EG", "Evil Geniuses")
    assert _same_team("FNC", "Fnatic")
    assert _same_team("TL", "Team Liquid")
    assert _same_team("coL", "Complexity")
    assert _same_team("mousesports", "MOUZ")
    assert _same_team("Made in Brazil", "MIBR")
    assert _same_team("GEN", "Gen.G")
    assert _same_team("KOI", "Movistar KOI")
    assert _same_team("RED", "RED Canids")
    assert _same_team("The MongolZ", "MongolZ")
    assert _same_team("Vitality Academy", "Team Vitality Academy")
    assert not _same_team("Vitality", "Vitality Academy")
    assert not _same_team("Aurora Gaming", "Aurora Young Blood")
    assert not _same_team("G2", "G2 Ares")
    assert alias_names("KRX") == ("Kiwoom DRX",)
    assert not _same_team("Kiwoom DRX Challengers", "Kiwoom DRX")
    assert _same_team("XLG Gaming", "Xi Lai Gaming")
    assert not _same_team("BRO Esports", "OKSavingsBank BRION")
    assert _same_team("G2 Esports", "G2 Esports")
    assert _same_team("Bilibili Gaming", "Bilibili Gaming")
    assert _same_team("Team Vitality", "Team Vitality")
    assert _same_team("Team Liquid", "Team Liquid")
    assert _same_team("FaZe Clan", "FaZe Clan")

@pytest.mark.asyncio
async def case_major_games_use_bo3(tmp_path: Path):
    games = (
        ("Counter-Strike", "1", "https://bo3.gg/matches/paper-rex-vs-gen-g-29-09-2026"),
        ("Valorant", "2", "https://bo3.gg/valorant/matches/paper-rex-vs-gen-g-29-09-2026"),
        ("LoL", "3", "https://bo3.gg/lol/matches/paper-rex-vs-gen-g-29-09-2026"),
        ("Dota 2", "4", "https://bo3.gg/dota2/matches/paper-rex-vs-gen-g-29-09-2026"),
    )
    for game, discipline, page in games:
        row = _match_row(11)
        row["videogame"] = {"name": game}
        match = parse_match(row, followed_slug=SLUG)
        assert match is not None
        cache = LinkCache(tmp_path / f"{discipline}.json")
        calls: list[str] = []

        async def get(url: str, _headers: dict[str, str] | None) -> str:
            calls.append(url)
            if "/matches?" in url:
                return _bo3_matches()
            return _bo3_search()

        resolved = await resolve_match_url(match, cache=cache, get=get, now=NOW)
        mark = f"discipline_id%5D%5Beq%5D={discipline}"
        assert calls and "api.bo3.gg/api/v1/filters/teams" in calls[0]
        assert mark in calls[0]
        assert mark in calls[1]
        assert "Paper+Rex" in calls[0] or "Paper%20Rex" in calls[0]
        assert resolved.url == page
        assert resolved.source == "bo3"
        assert cache.get("11") == resolved.url

@pytest.mark.asyncio
async def case_other_games_use_the_stream(tmp_path: Path):
    row = _match_row(11)
    row["videogame"] = {"name": "Overwatch"}
    match = parse_match(row, followed_slug=SLUG)
    assert match is not None
    cache = LinkCache(tmp_path / "links.json")

    async def get(url: str, _headers: dict[str, str] | None) -> str:
        raise AssertionError(url)

    resolved = await resolve_match_url(match, cache=cache, get=get, now=NOW)
    assert resolved.url == "https://twitch.tv/main"
    assert resolved.source == "stream"
    assert "11" not in cache._rows

def _bo3_search() -> str:
    return json.dumps(
        {
            "results": [
                {"id": 624, "slug": "paper-rex", "name": "Paper Rex"},
                {"id": 9, "slug": "paper-rex-academy", "name": "Paper Rex Academy"},
            ]
        }
    )

def _bo3_matches() -> str:
    return json.dumps(
        {
            "results": [
                {
                    "slug": "paper-rex-vs-furia-20-09-2026",
                    "start_date": "2026-09-20T12:00:00.000+00:00",
                    "team1": {"name": "Paper Rex"},
                    "team2": {"name": "FURIA"},
                },
                {
                    "slug": "paper-rex-vs-gen-g-29-09-2026",
                    "start_date": "2026-09-29T12:00:00.000+00:00",
                    "team1": {"name": "Paper Rex"},
                    "team2": {"name": "Gen.G"},
                },
                {
                    "slug": "g2-esports-vs-paper-rex-29-09-2026",
                    "start_date": "2026-09-29T15:00:00.000+00:00",
                    "team1": {"name": "G2 Esports"},
                    "team2": {"name": "Paper Rex"},
                },
            ]
        }
    )

@pytest.mark.asyncio
async def case_miss_waits_then_looks_up_again(tmp_path: Path):
    row = _match_row(11)
    row["videogame"] = {"name": "Counter-Strike"}
    match = parse_match(row, followed_slug=SLUG)
    assert match is not None

    async def blocked(url: str, _headers: dict[str, str] | None) -> str:
        raise AssertionError(url)

    fresh = LinkCache(tmp_path / "fresh.json")
    fresh.remember_miss("11", now=NOW)
    waited = await resolve_match_url(match, cache=fresh, get=blocked, now=NOW)
    assert waited.url == "https://twitch.tv/main"
    assert waited.source == "stream"

    async def lookup(url: str, _headers: dict[str, str] | None) -> str:
        if "/matches?" in url:
            return _bo3_matches()
        return _bo3_search()

    stale = LinkCache(tmp_path / "stale.json")
    stale.remember_miss("11", now=NOW - timedelta(hours=12))
    found = await resolve_match_url(match, cache=stale, get=lookup, now=NOW)
    assert found.url == "https://bo3.gg/matches/paper-rex-vs-gen-g-29-09-2026"
    assert found.source == "bo3"

def case_bo3_row_picks_the_opponent_on_that_day():
    teams = bo3_teams(_bo3_search())
    assert pick_bo3_team(teams, "Paper Rex") == "624"
    assert pick_bo3_team(teams, "Paper Rex Academy") == "9"
    rows = bo3_rows(_bo3_matches())
    assert pick_bo3_row(rows, opponent="Gen.G", when=NOW) == (
        "https://bo3.gg/matches/paper-rex-vs-gen-g-29-09-2026"
    )
    earlier = utc(2026, 9, 20, 12)
    assert pick_bo3_row(rows, opponent="FURIA", when=earlier) == (
        "https://bo3.gg/matches/paper-rex-vs-furia-20-09-2026"
    )
    assert pick_bo3_row(rows, opponent="FURIA", when=NOW) is None
    slug = "paper-rex-vs-gen-g-29-09-2026"
    for game, folder in (
        ("", "matches"),
        ("valorant", "valorant/matches"),
        ("lol", "lol/matches"),
        ("dota2", "dota2/matches"),
    ):
        assert bo3_page(game, slug) == f"https://bo3.gg/{folder}/{slug}"

@pytest.mark.asyncio
async def case_bo3_error_is_not_stored_as_a_miss(tmp_path: Path):
    row = _match_row(9)
    row["videogame"] = {"name": "LoL"}
    match = parse_match(row, followed_slug=SLUG)
    assert match is not None
    cache = LinkCache(tmp_path / "links.json")

    async def get(_url: str, _headers: dict[str, str] | None) -> str:
        return "<html>"

    resolved = await resolve_match_url(match, cache=cache, get=get, now=NOW)
    assert resolved.source == "stream"
    assert "9" not in cache._rows
