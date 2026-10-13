from __future__ import annotations

import json

import pytest

from fetch import FetchError
from news.source.match.thesportsdb import clubs
from news.source.match.thesportsdb.clubs import (
    SPORTSDB_MINUTE_CAP,
    SportsdbClient,
    TeamHit,
    club_already_followed,
    official_club_name,
    pick_team,
    search_club_suggestions,
    sport_for_club,
    suggest_clubs,
)


async def test_clubs():
    case_suggest_clubs_covers_alias_typo_and_ambiguous_nicks()
    await case_search_club_suggestions_uses_thesportsdb_and_drops_youth()
    case_pick_team_prefers_the_famous_club_or_asks_for_an_id()
    case_another_squad_counts_once_the_team_id_matches()
    await case_eventsday_waits_out_the_minute_cap()

def case_suggest_clubs_covers_alias_typo_and_ambiguous_nicks():
    clubs._club_aliases = None
    names = {alias.name for alias in suggest_clubs("gunners")}
    assert "Arsenal" in names
    blank = suggest_clubs("")
    assert blank
    assert len(blank) <= 25

    fuzzy = suggest_clubs("los angelos")
    sports = {alias.name: alias.sport for alias in fuzzy}
    assert sports["Los Angeles Lakers"] == "Basketball"
    assert sports["Los Angeles Clippers"] == "Basketball"
    assert sports["Los Angeles Kings"] == "Ice Hockey"

    rangers = {alias.name for alias in suggest_clubs("rangers")}
    assert "Rangers FC" in rangers
    assert "New York Rangers" in rangers
    assert clubs.club_alias("rangers") is None
    giants = {alias.name for alias in suggest_clubs("giants")}
    assert "New York Giants" in giants
    assert "San Francisco Giants" in giants
    assert official_club_name("76ers") == "Philadelphia 76ers"
    assert official_club_name("134113") == "Philadelphia 76ers"
    assert sport_for_club("76ers", stored="Soccer") == "Basketball"
    assert sport_for_club("Barcelona") == "Soccer"


async def case_search_club_suggestions_uses_thesportsdb_and_drops_youth():
    payload = {
        "teams": [
            {
                "idTeam": "134867",
                "strTeam": "Los Angeles Lakers",
                "strSport": "Basketball",
                "strLeague": "NBA",
                "idLeague": "4387",
            },
            {
                "idTeam": "134846",
                "strTeam": "Los Angeles Kings",
                "strSport": "Ice Hockey",
                "strLeague": "NHL",
                "idLeague": "4380",
            },
            {
                "idTeam": "134868",
                "strTeam": "Los Angeles Galaxy",
                "strSport": "Soccer",
                "strLeague": "American Major League Soccer",
                "idLeague": "4346",
            },
            {
                "idTeam": "1",
                "strTeam": "LA Youth",
                "strSport": "Soccer",
                "strLeague": "NCAA Division 1",
            },
        ]
    }

    async def get(url: str, headers):
        del url, headers
        return json.dumps(payload)

    found = await search_club_suggestions("los angelos", get=get)
    labeled = {f"{alias.name} · {alias.sport}" for alias in found}
    assert "Los Angeles Lakers · Basketball" in labeled
    assert "Los Angeles Kings · Ice Hockey" in labeled
    assert "Los Angeles Galaxy · Soccer" in labeled
    assert all("Youth" not in alias.name for alias in found)


def case_pick_team_prefers_the_famous_club_or_asks_for_an_id():
    cases = (
        ([TeamHit("1", "Arsenal")], "Arsenal", ("1", "Arsenal")),
        (
            [
                TeamHit("147946", "Milan Youth", sport="Soccer", league="_No League Soccer"),
                TeamHit(
                    "133667",
                    "AC Milan",
                    sport="Soccer",
                    league="Italian Serie A",
                    league_id="4332",
                ),
            ],
            "AC Milan",
            ("133667", "AC Milan"),
        ),
        (
            [
                TeamHit(
                    "144836",
                    "FC Barcelona",
                    sport="Rugby",
                    league="Spanish División de Honor",
                ),
                TeamHit(
                    "133739",
                    "Barcelona",
                    sport="Soccer",
                    league="Spanish La Liga",
                    league_id="4335",
                ),
            ],
            "Barcelona",
            ("133739", "Barcelona"),
        ),
        (
            [
                TeamHit(
                    "144836",
                    "FC Barcelona",
                    sport="Rugby",
                    league="Spanish División de Honor",
                )
            ],
            "FC Barcelona",
            ("144836", "FC Barcelona"),
        ),
    )
    for hits, query, want in cases:
        assert pick_team(hits, query) == want
    with pytest.raises(FetchError, match="more than one"):
        pick_team([TeamHit("1", "A"), TeamHit("2", "B")], "x")


async def case_eventsday_waits_out_the_minute_cap():
    calls: list[str] = []
    clock = {"t": 1_000.0}

    async def get(url: str, _headers):
        calls.append(url)
        return "{}"

    async def sleep(seconds: float) -> None:
        clock["t"] += seconds

    client = SportsdbClient(
        get, base="http://sportsdb.test", key="k",
        monotonic=lambda: clock["t"], sleep=sleep,
    )
    client._hits = [clock["t"]] * SPORTSDB_MINUTE_CAP
    payload = await client.json("eventsday.php", {"d": "2026-09-08", "l": "4480"}, "Day")
    assert payload == {}
    assert client.day_scan_cut is False
    assert calls
    calls.clear()
    payload = await client.json("eventsnext.php", {"id": "1"}, "Next")
    assert payload == {}
    assert calls

    async def boom(url: str, headers):
        del url, headers
        raise FetchError("HTTP 500 from sportsdb")

    failing = SportsdbClient(boom, base="http://sportsdb.test", key="k")
    assert (
        await failing.json("eventsday.php", {"d": "2026-09-08", "l": "4480"}, "Day")
        is None
    )
    assert failing.day_scan_cut is True
    with pytest.raises(FetchError, match="HTTP 500"):
        await failing.json("eventsnext.php", {"id": "1"}, "Next")


def case_another_squad_counts_once_the_team_id_matches():
    saved = [
        {
            "platform": "pandascore",
            "account": slug,
            "topic": "match",
            "label": "Bilibili Gaming",
        }
        for slug in (
            "bilibili-gaming-lol",
            "bilibili-gaming-valorant",
            "bilibili-gaming-cs-go",
            "bilibili-gaming-dota-2",
        )
    ]
    latest = saved[-1]
    assert club_already_followed(
        saved,
        latest,
        team_id=latest["account"],
        source_name="Bilibili Gaming",
        ignore_self=True,
    ) is False
    typed = {"platform": "pandascore", "account": "BLG", "topic": "match", "label": ""}
    assert club_already_followed(
        saved,
        typed,
        team_id="bilibili-gaming-lol",
        source_name="Bilibili Gaming",
    ) is True
