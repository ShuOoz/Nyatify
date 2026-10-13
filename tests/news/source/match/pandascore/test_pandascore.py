from __future__ import annotations

import json
from pathlib import Path

import pytest

from fetch import FetchError
from tests.helpers import utc

SLUG = "paper-rex-valorant"
NOW = utc(2026, 9, 29, 12)


from news.source.match.pandascore import (
    NAME,
    ambiguous_tokens,
    fetch_items,
    parse_account,
    previewable,
    search_accounts,
    uses_ai,
)
from news.source.match.pandascore.http import PandaScoreClient
from tests.helpers import fetched_item


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


async def test_pandascore_source(tmp_path):
    case_parse_account()
    await case_fetch_missing_token_raises()
    await case_search_expands_a_short_name_and_lists_a_shared_one()
    await case_fetch_items_builds_live_card(tmp_path)

def case_parse_account():
    assert parse_account("pandascore:paper-rex-valorant") == "paper-rex-valorant"
    assert parse_account("Paper Rex", explicit=True) == "Paper Rex"
    assert parse_account("paper-rex-valorant") is None
    assert parse_account("  ") is None
    assert parse_account("pandascore:") is None
    match = fetched_item(platform="pandascore", tags=("match", "start"))
    post = fetched_item(tags=("post",))
    assert uses_ai(match) is False and previewable(match) is True
    assert uses_ai(post) is True and previewable(post) is False
    assert ambiguous_tokens(
        FetchError("Name matches more than one team. Use pandascore:a, pandascore:b")
    ) == ["a", "b"]
    assert ambiguous_tokens(FetchError("down")) == []

async def case_fetch_missing_token_raises():
    async def get(_url: str, _headers: dict[str, str] | None) -> str:
        raise AssertionError("no network")

    with pytest.raises(FetchError, match="Set PANDASCORE_TOKEN"):
        await fetch_items(SLUG, get=get, token="")

async def case_search_expands_a_short_name_and_lists_a_shared_one():
    async def navi(url: str, headers: dict[str, str] | None) -> str:
        assert headers is not None
        assert "Natus" in url or "NAVI" in url
        return _search_body("natus-vincere", "NAVI", "Counter-Strike")

    assert await search_accounts("NAVI", get=navi, token="test-token") == [
        ("Natus Vincere · Counter-Strike", "natus-vincere")
    ]

    async def xlg(url: str, _headers: dict[str, str] | None) -> str:
        if "Xi" in url:
            return _search_body("xl-gaming", "XL Gaming", "Valorant")
        if "XLG" in url:
            return _search_body("zy-gaming", "XLG Gaming", "Valorant")
        return "[]"

    assert await search_accounts("XLG", get=xlg, token="test-token") == [
        ("Xi Lai Gaming · Valorant", "zy-gaming")
    ]
    hit = await PandaScoreClient(xlg, token="test-token").resolve_team("XLG")
    assert (hit.slug, hit.name, hit.game) == ("zy-gaming", "Xi Lai Gaming", "Valorant")

    async def rng(url: str, _headers: dict[str, str] | None) -> str:
        if "Renegades" in url:
            return _search_body("renegades", "Renegades", "Counter-Strike")
        return _search_body("royal-never-give-up", "Royal Never Give Up", "LoL")

    rows = await search_accounts("RNG", get=rng, token="test-token")
    assert ("Renegades · Counter-Strike", "renegades") in rows
    assert ("Royal Never Give Up · LoL", "royal-never-give-up") in rows
    with pytest.raises(FetchError, match="more than one team"):
        await PandaScoreClient(rng, token="test-token").resolve_team("RNG")

    async def slug(url: str, _headers: dict[str, str] | None) -> str:
        assert "bilibili-gaming-lol" in url
        return _search_body("bilibili-gaming-lol", "Bilibili Gaming", "LoL")

    chosen = await PandaScoreClient(slug, token="test-token").resolve_team(
        "bilibili-gaming-lol"
    )
    assert chosen.slug == "bilibili-gaming-lol"
    assert chosen.game == "LoL"

def _search_body(slug: str, name: str, game: str) -> str:
    return json.dumps(
        [{"slug": slug, "name": name, "current_videogame": {"name": game}}]
    )

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

async def case_fetch_items_builds_live_card(tmp_path: Path):
    bodies: dict[str, str] = {}
    team = {
        "id": 1,
        "slug": SLUG,
        "name": "Paper Rex",
        "current_videogame": {"name": "Valorant"},
    }
    bodies[
        "https://api.pandascore.co/teams?filter%5Bslug%5D=paper-rex-valorant"
    ] = json.dumps([team])
    row = _match_row(7)
    for path in ("running", "upcoming", "past"):
        suffix = f"https://api.pandascore.co/matches/{path}?filter%5Bopponent_id%5D={team['id']}"
        if path == "upcoming":
            suffix += "&sort=scheduled_at"
        if path == "past":
            suffix += "&sort=-scheduled_at&per_page=5"
        bodies[suffix] = json.dumps([row] if path == "running" else [])

    async def get(url: str, headers: dict[str, str] | None) -> str:
        assert headers is not None
        if "api.pandascore.co" in url:
            assert headers.get("Authorization") == "Bearer test-token"
        if "api.bo3.gg" in url:
            if "/matches?" in url:
                return _bo3_matches()
            return _bo3_search()
        if url not in bodies:
            raise FetchError(f"unexpected url {url}")
        return bodies[url]

    name, items, game, slug, scanned = await fetch_items(
        SLUG,
        get=get,
        token="test-token",
        link_cache_path=tmp_path / "links.json",
        now=lambda: NOW,
    )
    assert name == "Paper Rex"
    assert game == "Valorant"
    assert slug == SLUG
    assert scanned is True
    assert len(items) == 1
    item = items[0]
    assert item.item_id == "7:start"
    assert item.title == "Paper Rex 1–0 Gen.G"
    assert item.text.splitlines()[0] == "**Game Status** | On"
    assert "**Format** | Bo3" in item.text
    assert "Game Phase" not in item.text
    assert "**Details**" not in item.text
    assert item.platform == NAME
    assert item.url == "https://bo3.gg/valorant/matches/paper-rex-vs-gen-g-29-09-2026"
