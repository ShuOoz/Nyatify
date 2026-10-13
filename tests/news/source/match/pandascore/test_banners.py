from __future__ import annotations

from pathlib import Path

from tests.helpers import utc

SLUG = "paper-rex-valorant"
NOW = utc(2026, 9, 29, 12)


from news.source.match.pandascore.banners import banner_picture
from news.source.match.pandascore.cards import items_for_matches
from news.source.match.pandascore.http import parse_match
from news.source.post.common import image_size


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

def _banner(platform, *tags):
    return banner_picture(type("Item", (), {"platform": platform, "tags": tags})())


def test_pandascore_banners():
    case_game_banners_are_thin_strips()
    case_banner_follows_the_game_name_on_the_card()

def case_game_banners_are_thin_strips():
    root = Path(__file__).resolve().parents[5] / "image/news_image/banners"
    named = (
        ("valorant", "valorant.jpg"),
        ("counter-strike", "cs2.jpg"),
        ("lol", "lol.jpg"),
        ("dota 2", "vs.jpg"),
    )
    for tag, filename in named:
        picture = _banner("pandascore", "match", "start", tag)
        assert picture is not None
        assert picture.filename == filename
        assert picture.data == (root / filename).read_bytes()
        assert image_size(picture.data) == (1500, 500)
    assert _banner("bilibili") is None
    assert _banner("thesportsdb", "match", "start", "soccer") is None

def case_banner_follows_the_game_name_on_the_card():
    expected = {
        "Valorant": "valorant.jpg",
        "Counter-Strike": "cs2.jpg",
        "LoL": "lol.jpg",
        "League of Legends": "lol.jpg",
        "Dota 2": "vs.jpg",
    }
    for name, filename in expected.items():
        row = _match_row(1)
        row["videogame"] = {"name": name}
        match = parse_match(row, followed_slug=SLUG)
        assert match is not None
        items = items_for_matches(
            [match],
            account=SLUG,
            now=NOW,
            source_name="Paper Rex",
            urls={},
        )
        assert items
        banner = banner_picture(items[0])
        assert banner is not None
        assert banner.filename == filename
        assert name.lower() in items[0].tags
