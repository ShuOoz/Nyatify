from __future__ import annotations

from tests.helpers import utc

SLUG = "paper-rex-valorant"
NOW = utc(2026, 9, 29, 12)


from news.source.match.common import kickoff_stamp
from news.source.match.pandascore.cards import card_copy
from news.source.match.pandascore.http import parse_match


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


def test_pandascore_cards():
    case_card_copy_series_title_format_and_pregame_time()
    case_card_names_join_the_league_and_keep_the_stage()

def case_card_copy_series_title_format_and_pregame_time():
    match = parse_match(_match_row(5, status="finished"), followed_slug=SLUG)
    assert match is not None
    title, body = card_copy(match, "end")
    assert title == "Paper Rex 1–0 Gen.G"
    assert body == (
        "**Game Status** | Over\n"
        "**Format** | Bo3\n"
        "**Stage** | 2\n"
        "**League** | VCT Pacific"
    )
    assert "**Details**" not in body
    assert "Game Phase" not in body
    live = parse_match(_match_row(6), followed_slug=SLUG)
    assert live is not None
    _, live_body = card_copy(live, "start")
    assert live_body.splitlines()[0] == "**Game Status** | On"
    assert "Game Phase" not in live_body
    preview = parse_match(_match_row(4, status="not_started"), followed_slug=SLUG)
    assert preview is not None and preview.scheduled_at is not None
    _, preview_body = card_copy(preview, "next")
    assert preview_body.splitlines()[0] == "**Game Status** | Not started"
    assert preview_body.splitlines()[-1] == kickoff_stamp(preview.scheduled_at)

def case_card_names_join_the_league_and_keep_the_stage():
    event = _match_row(7, status="finished")
    event["league"] = {"name": "VCT"}
    event["serie"] = {"name": "Champions"}
    event["tournament"] = {"name": "Group"}
    joined = parse_match(event, followed_slug=SLUG)
    assert joined is not None
    assert joined.league == "VCT Champions"
    assert joined.serie == "Group"
    _, event_body = card_copy(joined, "end")
    assert "**League** | VCT Champions" in event_body
    assert "**Stage** | Group" in event_body
    stage = _match_row(8, status="finished")
    stage["league"] = {"name": "VCT CN"}
    stage["serie"] = {"name": "Stage 1"}
    numbered = parse_match(stage, followed_slug=SLUG)
    assert numbered is not None
    assert numbered.league == "VCT CN"
    assert numbered.serie == "1"
    _, stage_body = card_copy(numbered, "end")
    assert "**League** | VCT CN" in stage_body
    assert "**Stage** | 1" in stage_body
