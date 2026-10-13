from __future__ import annotations

from datetime import datetime, timedelta, timezone

from news.source.match.thesportsdb import pick_preview
from news.source.match.thesportsdb.cards import (
    match_card_copy,
    match_items,
    scoreboard_url,
)
from tests.helpers import match_state


def test_match_cards_preview_on_and_full_time():
    soccer = "https://www.espn.com/soccer/match/_/gameId/401882888/barcelona-valencia"
    assert (
        scoreboard_url("Arsenal", "Chelsea", "1")
        == "https://www.thesportsdb.com/event/1-arsenal-vs-chelsea"
    )
    assert scoreboard_url("Barcelona", "Valencia", "1", score_url=soccer).endswith(
        "/barcelona-valencia"
    )
    assert (
        scoreboard_url("Barcelona", "Valencia", "1", score_url=soccer, kind="end")
        == "https://www.espn.com/soccer/team-stats/_/gameId/401882888"
    )
    for live, end in (
        (
            "https://www.espn.com/nba/game/_/gameId/401902644/heat-raptors",
            "https://www.espn.com/nba/boxscore/_/gameId/401902644",
        ),
        (
            "https://www.espn.com/nfl/game/_/gameId/401772510",
            "https://www.espn.com/nfl/boxscore/_/gameId/401772510",
        ),
    ):
        assert scoreboard_url("A", "B", "1", score_url=live) == live
        assert scoreboard_url("A", "B", "1", score_url=live, kind="end") == end

    now = datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc)
    kickoff = datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc)
    future = datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc)
    live = match_state(
        "10",
        phase="live",
        kickoff=kickoff,
        score_url="https://www.espn.com/soccer/match/_/gameId/10/arsenal-chelsea",
        home_score="1",
        away_score="0",
        clock="2H",
    )
    done = match_state(
        "9",
        away="City",
        phase="finished",
        home_score="2",
        away_score="1",
        kickoff=datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc),
        score_url="https://www.espn.com/soccer/match/_/gameId/401882903/arsenal-city",
        league="Premier League",
        facts="Saka 2",
    )
    items = match_items(
        [
            match_state("11", away="Liverpool", kickoff=future),
            live,
            done,
            match_state("12", away="Spurs", kickoff=kickoff),
            match_state(
                "8",
                away="Everton",
                home_score="2",
                away_score="0",
                kickoff=datetime(2026, 9, 5, 13, 0, tzinfo=timezone.utc),
            ),
        ],
        account="133604",
        now=now,
    )
    assert [item.item_id for item in items] == [
        "9:end",
        "8:start",
        "10:start",
        "12:start",
        "11:next",
    ]
    live_card = next(item for item in items if item.item_id == "10:start")
    assert live_card.title == "Arsenal 1–0 Chelsea"
    assert live_card.text.splitlines()[-1] == "Game Phase | 2nd half"
    done_card = next(item for item in items if item.item_id == "9:end")
    assert done_card.title == "Arsenal 2–1 City"
    assert done_card.text.endswith("Saka 2")
    assert done_card.url == "https://www.espn.com/soccer/team-stats/_/gameId/401882903"
    assert pick_preview(items, now=now).item_id == "8:start"

    nil = match_items(
        [match_state("7", away="Brighton", home_score="0", away_score="0", kickoff=kickoff)],
        account="133604",
        now=now,
    )
    assert nil[0].item_id == "7:start"
    late = match_items(
        [match_state("12", away="Spurs", kickoff=kickoff)],
        account="133604",
        now=datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc),
    )
    assert late[0].item_id == "12:end"
    assert late[0].title == "Arsenal vs Spurs"
    assert "–" not in late[0].title
    stale_live = match_items(
        [match_state("12", away="Spurs", phase="live", kickoff=kickoff)],
        account="133604",
        now=datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc),
    )
    assert stale_live[0].item_id == "12:end"
    skewed = match_items(
        [match_state("13", phase="live", kickoff=now + timedelta(hours=3))],
        account="133604",
        now=now,
    )
    assert skewed[0].item_id == "13:start"
    far = match_items(
        [match_state("14", phase="live", kickoff=now + timedelta(days=2))],
        account="133604",
        now=now,
    )
    assert far[0].item_id == "14:next"
    untitled = match_items([match_state("16")], account="133604", now=now)[0]
    assert untitled.item_id == "16:next"
    dated = match_state("18", away="City", kickoff=future)
    assert [
        item.item_id
        for item in match_items(
            [match_state("16"), match_state("17", away="Spurs"), dated],
            account="133604",
            now=now,
        )
    ] == ["18:next"]

    hoop = match_state(
        "2",
        sport="Basketball",
        home="Lakers",
        away="Celtics",
        phase="live",
        home_score="80",
        away_score="77",
        league="NBA",
        clock="Q3 - 4:12",
        facts="LeBron James 24 Points",
    )
    on_title, on_text = match_card_copy(hoop, "start")
    assert on_title == "Lakers 80–77 Celtics"
    assert "3rd quarter" in on_text
    _, preview = match_card_copy(
        match_state(kickoff=datetime(2026, 9, 5, 19, 0, tzinfo=timezone.utc), league="Premier League"),
        "next",
    )
    assert preview.startswith("**Game Status** | Not started")
    _, idle = match_card_copy(match_state(phase="finished"), "end")
    assert idle.endswith("Enjoy the rest of your day.")
    postponed = match_items(
        [
            match_state(
                "11",
                away="Liverpool",
                phase="skip",
                status="Match Postponed",
                kickoff=now + timedelta(hours=5),
            )
        ],
        account="133604",
        now=now,
    )
    assert postponed[0].item_id == "11:skip"
    assert postponed[0].text.startswith("**Game Status** | Postponed")
    clock_on = match_items(
        [match_state("20", kickoff=now - timedelta(hours=1))],
        account="133604",
        now=now,
    )
    assert clock_on[0].item_id == "20:start"
    past_window = match_items(
        [match_state("21", kickoff=now - timedelta(hours=5))],
        account="133604",
        now=now,
    )
    assert past_window[0].item_id == "21:end"

