from __future__ import annotations

import json

from tests.helpers import utc

SLUG = "paper-rex-valorant"
NOW = utc(2026, 9, 29, 12)


from news.source.match.pandascore.http import (
    PandaScoreClient,
    SeriesScore,
    TeamRef,
    parse_match,
    stage_for,
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


def test_parse_match():
    case_stage_for_mapping()
    case_parse_orients_followed_home_and_live_note()
    case_scheduled_at_fills_when_begin_at_is_empty()

def case_stage_for_mapping():
    assert stage_for("not_started") == "next"
    assert stage_for("running") == "start"
    assert stage_for("finished") == "end"
    assert stage_for("canceled") == "skip"
    assert stage_for("postponed") == "skip"
    assert parse_match({"id": 1, "status": "unknown", "opponents": []}, followed_slug=SLUG) is None

def case_parse_orients_followed_home_and_live_note():
    flipped = _match_row(
        9,
        followed=_team(2, "gen-g-valorant", "Gen.G"),
        opponent=_team(1, SLUG, "Paper Rex"),
        followed_score=0,
        opponent_score=2,
    )
    # Wire lists Gen.G first; followed slug still becomes Home.
    flipped["opponents"] = [
        {"opponent": _team(2, "gen-g-valorant", "Gen.G")},
        {"opponent": _team(1, SLUG, "Paper Rex")},
    ]
    flipped["results"] = [
        {"team_id": 2, "score": 0},
        {"team_id": 1, "score": 2},
    ]
    match = parse_match(flipped, followed_slug=SLUG)
    assert match is not None
    assert match.followed.name == "Paper Rex"
    assert match.opponent.name == "Gen.G"
    assert match.series == SeriesScore(followed=2, opponent=0)
    assert match.best_of == 3

def case_scheduled_at_fills_when_begin_at_is_empty():
    row = _match_row(6, status="not_started", begin_at="")
    row["scheduled_at"] = "2026-09-29T09:00:00Z"
    match = parse_match(row, followed_slug=SLUG)
    assert match is not None
    assert match.scheduled_at is not None


async def test_a_later_list_replaces_not_started_and_a_pin_is_looked_up():
    rows = {
        "/matches/running": [_match_row(3, status="running")],
        "/matches/upcoming": [_match_row(4, status="not_started")],
        "/matches/past": [
            _match_row(3, status="finished"),
            _match_row(4, status="finished", followed_score=2, opponent_score=1),
        ],
        "/matches/9": _match_row(9, status="finished"),
    }

    async def get(url: str, _headers):
        for path, body in rows.items():
            if path in url:
                return json.dumps(body)
        return "[]"

    client = PandaScoreClient(get, token="t")
    team = TeamRef(slug=SLUG, name="Paper Rex", team_id=1)
    matches = await client.team_matches(team, ["9", "4"])
    by_id = {match.match_id: match for match in matches}
    assert by_id["3"].status == "running"
    assert by_id["4"].status == "finished"
    assert by_id["9"].status == "finished"
