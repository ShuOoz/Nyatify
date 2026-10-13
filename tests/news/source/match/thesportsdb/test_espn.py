from __future__ import annotations

from datetime import datetime, timedelta, timezone

from news.source.match.thesportsdb.espn import (
    espn_card_url,
    espn_facts,
    espn_game_id,
    espn_league_path,
    espn_scoreboard_stamp,
    espn_scoreboard_stamps,
    espn_scoreboard_url,
    espn_side,
    espn_summary_url,
    names_match,
    overlay_espn,
    pick_espn_game,
    with_espn,
)
from news.source.match.thesportsdb.scoreboard import EspnGame, parse_espn_scoreboard
from tests.helpers import FakeHttp, espn_board, espn_summary, match_state

ESPN = "https://www.espn.com/soccer/match/_/gameId/9"


def _side(place, name, *, short=None, score=None):
    team = {"displayName": name}
    if short:
        team["shortDisplayName"] = short
    row = {"homeAway": place, "team": team}
    if score is not None:
        row["score"] = score
    return row


def _event(href, home, away, **extra):
    home_side = _side(
        "home",
        home,
        short=extra.pop("home_short", None),
        score=extra.pop("home_score", None),
    )
    away_side = _side(
        "away",
        away,
        short=extra.pop("away_short", None),
        score=extra.pop("away_score", None),
    )
    date = extra.pop("date", None)
    event = {
        "links": [{"href": href}],
        "competitions": [{"competitors": [home_side, away_side], **extra}],
    }
    if date:
        event["date"] = date
    return event


def _espn(**kwargs):
    data = dict(home="Arsenal", away="Chelsea", url=ESPN)
    data.update(kwargs)
    return EspnGame(**data)


def _board(*events):
    return parse_espn_scoreboard({"events": list(events)})


async def test_espn_boards():
    case_league_paths_and_board_days_cover_soccer_and_us_sports()
    case_query_game_id_rewrites_to_the_match_path()
    case_pick_and_names_follow_the_club()
    case_facts_keep_a_box_and_join_scorers()
    case_overlay_fills_scores_and_retags_after_kickoff()
    await case_with_espn_overlays_the_board_then_ignores_a_miss()

def case_league_paths_and_board_days_cover_soccer_and_us_sports():
    by_id = {
        "4328": ("soccer", "eng.1"),
        "4330": ("soccer", "sco.1"),
        "4331": ("soccer", "ger.1"),
        "4332": ("soccer", "ita.1"),
        "4334": ("soccer", "fra.1"),
        "4335": ("soccer", "esp.1"),
        "4337": ("soccer", "ned.1"),
        "4344": ("soccer", "por.1"),
        "4346": ("soccer", "usa.1"),
        "4351": ("soccer", "bra.1"),
        "4370": ("racing", "f1"),
        "4380": ("hockey", "nhl"),
        "4387": ("basketball", "nba"),
        "4391": ("football", "nfl"),
        "4424": ("baseball", "mlb"),
        "4480": ("soccer", "uefa.champions"),
        "4481": ("soccer", "uefa.europa"),
        "4482": ("soccer", "eng.fa"),
        "4483": ("soccer", "esp.copa_del_rey"),
        "4484": ("soccer", "fra.coupe_de_france"),
        "4485": ("soccer", "ger.dfb_pokal"),
        "4506": ("soccer", "ita.coppa_italia"),
        "4511": ("soccer", "esp.super_cup"),
        "4570": ("soccer", "eng.league_cup"),
        "5071": ("soccer", "uefa.europa.conf"),
    }
    sport = {
        "racing": "Motorsport",
        "hockey": "Ice Hockey",
        "football": "American Football",
        "baseball": "Baseball",
        "basketball": "Basketball",
        "soccer": "Soccer",
    }
    for league_id, path in by_id.items():
        assert espn_league_path(sport[path[0]], "", league_id) == path
    assert espn_league_path("Soccer", "", "4472") is None
    assert espn_league_path("Soccer", "German Bundesliga", "4332") == ("soccer", "ger.1")
    assert espn_league_path("Soccer", "Italian Serie A", "4331") == ("soccer", "ita.1")
    assert espn_league_path("Soccer", "FA Cup", "4472") == ("soccer", "eng.fa")
    assert espn_league_path("Soccer", "Spanish La Liga", "4335") == ("soccer", "esp.1")
    assert espn_league_path("Soccer", "FA Cup", "4482") == ("soccer", "eng.fa")
    assert espn_league_path("Soccer", "EFL Cup", "4570") == ("soccer", "eng.league_cup")
    assert espn_league_path("Basketball", "NBA") == ("basketball", "nba")
    assert espn_league_path("Basketball", "National Basketball Association") == (
        "basketball",
        "nba",
    )
    assert espn_league_path("Basketball", "WNBA") == ("basketball", "wnba")
    assert espn_league_path("Basketball", "EuroLeague") == ("basketball", "euroleague")
    assert espn_league_path("Soccer", "NBA") is None
    assert espn_league_path("Basketball", "Premier League") is None
    assert espn_league_path("Soccer", "Obscure Cup") is None
    assert espn_league_path("Soccer", "Serie A", "4351") == ("soccer", "bra.1")
    assert espn_league_path("Soccer", "Italian Serie A", "4351") == ("soccer", "ita.1")
    utc_day = datetime(2026, 9, 6, 14, 15, tzinfo=timezone.utc)
    assert espn_scoreboard_url("soccer", "esp.1", utc_day).endswith(
        "/soccer/esp.1/scoreboard?dates=20260906"
    )
    nba_night = datetime(2026, 9, 6, 2, 30, tzinfo=timezone.utc)
    assert espn_scoreboard_url("basketball", "nba", nba_night).endswith(
        "/basketball/nba/scoreboard?dates=20260905"
    )
    assert espn_scoreboard_stamps("nba", nba_night) == ("20260905", "20260906")


def case_query_game_id_rewrites_to_the_match_path():
    raw = "https://www.espn.com/soccer/match?gameId=401882859/barcelona-sevilla"
    report = "https://www.espn.com/soccer/report?gameId=401882859"
    assert espn_game_id(raw) == espn_game_id(report) == "401882859"
    assert espn_card_url(raw) == "https://www.espn.com/soccer/match/_/gameId/401882859"
    assert (
        espn_card_url(report, kind="end")
        == "https://www.espn.com/soccer/team-stats/_/gameId/401882859"
    )


def case_pick_and_names_follow_the_club():
    soccer = _board(
        _event(
            "https://www.espn.com/soccer/match/_/gameId/401882888/barcelona-valencia",
            "Valencia",
            "Barcelona",
        )
    )
    assert pick_espn_game("FC Barcelona", soccer)
    assert pick_espn_game("Arsenal", soccer) is None
    nba = _board(
        _event(
            "https://www.espn.com/nba/game/_/gameId/401902644/heat-raptors",
            "Toronto Raptors",
            "Miami Heat",
        )
    )
    assert pick_espn_game("Miami Heat", nba)
    assert espn_side("Barcelona", soccer[0]) == "away"
    assert espn_side("Arsenal", soccer[0]) is None
    for left, right, matched in (
        ("LA", "Atlanta", False),
        ("FC Barcelona", "Barcelona", True),
        ("New York Knicks", "Knicks", True),
        ("New York Knicks", "York", False),
        ("Inter Milan", "Internazionale", True),
        ("Inter Milan", "Inter Miami", False),
        ("United", "Newcastle United", False),
        ("Real", "Real Madrid", False),
        ("Milan", "Inter Milan", False),
        ("Milan", "AC Milan", True),
        ("Bodø/Glimt", "Bodo Glimt", True),
    ):
        assert names_match(left, right) is matched
    bodo = _board(
        _event(
            "https://www.espn.com/soccer/match/_/gameId/401915443",
            "Bayern Munich",
            "Bodo/Glimt",
        )
    )
    assert pick_espn_game("Bayern Munich", bodo)
    union = _board(
        _event(
            "https://www.espn.com/soccer/match/_/gameId/401884790",
            "Bayern Munich",
            "1. FC Union Berlin",
        )
    )
    assert pick_espn_game("Bayern Munich", union)
    listed = _event(
        "https://www.espn.com/soccer/match/_/gameId/401884790",
        "1. FC Union Berlin",
        "Bayern Munich",
        home_short="Union Berlin",
        date="2026-09-18T18:30Z",
    )
    union_listed = _board(listed, listed)
    assert pick_espn_game(
        "Union Berlin",
        union_listed,
        kickoff=datetime(2026, 9, 18, 18, 30, tzinfo=timezone.utc),
    )
    united = _board(
        _event(
            "https://www.espn.com/soccer/match/_/gameId/1/newcastle-manchester-united",
            "Manchester United",
            "Newcastle United",
        ),
        _event(
            "https://www.espn.com/soccer/match/_/gameId/2/manchester-city-leicester-city",
            "Manchester City",
            "Leicester City",
        ),
    )
    assert pick_espn_game("Manchester United", united)
    assert pick_espn_game("Newcastle United", united)
    assert pick_espn_game("Manchester City", united)
    twins = _board(
        _event(
            "https://www.espn.com/soccer/match/_/gameId/3/barcelona-valencia",
            "Barcelona",
            "Valencia",
        ),
        _event(
            "https://www.espn.com/soccer/match/_/gameId/4/barcelona-valencia-b",
            "FC Barcelona",
            "Valencia CF",
        ),
    )
    assert pick_espn_game("Barcelona", twins) is None
    friday = datetime(2026, 9, 18, 18, 30, tzinfo=timezone.utc)
    saturday = datetime(2026, 9, 19, 16, 30, tzinfo=timezone.utc)
    bayern_days = _board(
        _event(
            "https://www.espn.com/soccer/match/_/gameId/401884790",
            "Bayern Munich",
            "1. FC Union Berlin",
            date="2026-09-18T18:30Z",
        ),
        _event(
            "https://www.espn.com/soccer/match/_/gameId/401884791",
            "Bayern Munich",
            "Werder Bremen",
            date="2026-09-19T16:30Z",
        ),
    )
    picked = pick_espn_game("Bayern Munich", bayern_days, kickoff=friday)
    assert picked is not None
    assert "401884790" in picked.url
    later = pick_espn_game("Bayern Munich", bayern_days, kickoff=saturday)
    assert later is not None
    assert "401884791" in later.url
    inter = _board(
        _event(
            "https://www.espn.com/soccer/match/_/gameId/401915451/internazionale-real-madrid",
            "Real Madrid",
            "Internazionale",
        )
    )
    assert pick_espn_game("Real Madrid", inter)
    assert pick_espn_game("Inter Milan", inter)
    assert pick_espn_game("Inter Miami", inter) is None


def case_facts_keep_a_box_and_join_scorers():
    assert espn_facts(["Lakers quarters: 19 23", "ignored"]) == "Lakers quarters: 19 23"
    assert espn_facts([" Raphinha 3' ", "", "Raphinha 3'", "Adeyemi 22'"]) == (
        "Raphinha 3'\nAdeyemi 22'"
    )
    assert espn_summary_url("football", "nfl", "401772510").endswith(
        "/football/nfl/summary?event=401772510"
    )


def case_overlay_fills_scores_and_retags_after_kickoff():
    now = datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc)
    kickoff = now - timedelta(hours=2)
    later = now + timedelta(hours=3)
    extra = _espn(home_score="2", away_score="1", state="post", notes=("Saka 12'",))
    blank = match_state("9", phase="finished")
    filled = overlay_espn(blank, extra, from_board=False)
    assert filled.home_score == "2"
    assert filled.away_score == "1"
    assert filled.facts == "Saka 12'"
    kept = overlay_espn(
        match_state("9", phase="finished", home_score="3", away_score="0"),
        extra,
        from_board=False,
    )
    assert (kept.home_score, kept.away_score) == ("3", "0")
    swapped = overlay_espn(
        blank,
        _espn(home="Chelsea", away="Arsenal", url="", home_score="1", away_score="2", state="post"),
        from_board=False,
    )
    assert (swapped.home_score, swapped.away_score) == ("2", "1")
    union_home = overlay_espn(
        match_state(
            "9",
            home="Union Berlin",
            away="Bayern Munich",
            phase="finished",
        ),
        _espn(
            home="1. FC Union Berlin",
            away="Bayern Munich",
            url="",
            home_score="0",
            away_score="7",
            home_labels=("1. FC Union Berlin", "Union Berlin"),
            away_labels=("Bayern Munich",),
        ),
        from_board=True,
        club="Union Berlin",
    )
    assert (union_home.home_score, union_home.away_score) == ("0", "7")

    live = _espn(state="in", detail="Extra Time")
    over = _espn(state="post")
    soccer = dict(kickoff=kickoff, sport="Soccer")
    assert (
        overlay_espn(
            match_state("9", phase="finished", **soccer), live, from_board=True, now=now
        ).phase
        == "live"
    )
    assert (
        overlay_espn(
            match_state("9", phase="live", **soccer), over, from_board=True, now=now
        ).phase
        == "finished"
    )
    assert (
        overlay_espn(
            match_state("9", phase="live", kickoff=now - timedelta(hours=5), sport="Soccer"),
            over,
            from_board=True,
            now=now,
        ).phase
        == "finished"
    )
    preview = match_state("9", kickoff=later, sport="Soccer")
    assert overlay_espn(preview, live, from_board=True, now=now).phase == "scheduled"
    assert overlay_espn(preview, over, from_board=True, now=now).phase == "scheduled"
    assert (
        overlay_espn(
            match_state("9", phase="scheduled", status="Match Finished", **soccer),
            _espn(url="", detail="HT"),
            from_board=True,
            now=now,
        ).phase
        == "live"
    )
    assert (
        overlay_espn(
            match_state("9", phase="scheduled", status="Postponed", **soccer),
            _espn(url="", detail="Q3 - 4:12"),
            from_board=True,
            now=now,
        ).phase
        == "live"
    )


async def case_with_espn_overlays_the_board_then_ignores_a_miss():
    now = datetime(2026, 9, 6, 16, tzinfo=timezone.utc)
    kickoff = datetime(2026, 9, 6, 15, tzinfo=timezone.utc)
    state = match_state(
        "9",
        phase="live",
        kickoff=kickoff,
        league="Premier League",
        league_id="4328",
    )
    quiet = FakeHttp({})
    untouched = await with_espn([state], get=quiet, now=now, club="  ")
    assert quiet.urls == []
    assert untouched[0].phase == "live"

    missed = FakeHttp({})
    kept = await with_espn([state], get=missed, now=now, club="Arsenal")
    assert kept[0].home_score is None
    assert kept[0].phase == "live"
    assert missed.urls

    href = "https://www.espn.com/soccer/match/_/gameId/401882888/arsenal-chelsea"
    stamp = espn_scoreboard_stamp("eng.1", kickoff)
    board_url = espn_scoreboard_url("soccer", "eng.1", kickoff, stamp=stamp)
    summary_url = espn_summary_url("soccer", "eng.1", "401882888")
    http = FakeHttp(
        {
            board_url: espn_board(href, "Arsenal", "Chelsea"),
            summary_url: espn_summary(href, "Arsenal", "Chelsea", "2", "1", "Saka 12'"),
        }
    )
    filled = await with_espn([state], get=http, now=now, club="Arsenal")
    assert filled[0].home_score == "2"
    assert filled[0].away_score == "1"
    assert "Saka 12'" in filled[0].facts
    assert filled[0].score_url and "401882888" in filled[0].score_url
