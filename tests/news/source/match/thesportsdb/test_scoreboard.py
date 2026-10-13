from __future__ import annotations

from datetime import datetime, timezone

from news.source.match.thesportsdb.scoreboard import (
    parse_espn_scoreboard,
    parse_espn_summary,
)


def test_scoreboard():
    case_event_url_kickoff_and_live_fields()
    case_summary_notes_split_goals_and_period_boxes()
    case_summary_headline_venue_and_empty_payloads()


def _side(place, name, *, short=None, score=None, lines=None):
    team = {"displayName": name}
    if short:
        team["shortDisplayName"] = short
    row = {"homeAway": place, "team": team}
    if score is not None:
        row["score"] = score
    if lines is not None:
        row["linescores"] = [{"value": n} for n in lines]
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


def _header(
    href,
    home,
    away,
    *,
    league=None,
    sport=None,
    shorts=(None, None),
    scores=(None, None),
    lines=(None, None),
):
    header = {
        "links": [{"href": href}],
        "competitions": [
            {
                "competitors": [
                    _side(
                        "home", home, short=shorts[0], score=scores[0], lines=lines[0]
                    ),
                    _side(
                        "away", away, short=shorts[1], score=scores[1], lines=lines[1]
                    ),
                ]
            }
        ],
    }
    if league:
        header["league"] = {"slug": league}
    if sport:
        header["sport"] = {"name": sport}
    return header


def _goal(text=None, *, short=None, team=None, clock=None, home_away=None, scoring=None):
    play = {}
    if scoring is not None:
        play["scoringPlay"] = scoring
    if text is not None:
        play["text"] = text
    if short is not None:
        play["shortText"] = short
    if team is not None:
        play["team"] = {"displayName": team}
    if clock is not None:
        play["clock"] = {"displayValue": clock}
    if home_away is not None:
        play["homeAway"] = home_away
    return play


def _board(*events):
    return parse_espn_scoreboard({"events": list(events)})


def _notes(header=None, *, plays=(), events=()):
    body = {}
    if header is not None:
        body["header"] = header
    if plays:
        body["scoringPlays"] = list(plays)
    if events:
        body["keyEvents"] = list(events)
    parsed = parse_espn_summary(body)
    assert parsed is not None
    return parsed.notes


def case_event_url_kickoff_and_live_fields():
    raw = "https://www.espn.com/soccer/match?gameId=401882859/barcelona-sevilla"
    game = _board(_event(raw, "Sevilla", "Barcelona", date="2026-09-19T19:00Z"))[0]
    assert game.url == "https://www.espn.com/soccer/match/_/gameId/401882859"
    assert game.kickoff == datetime(2026, 9, 19, 19, 0, tzinfo=timezone.utc)
    assert "Sevilla" in game.home_labels

    nba = _board(
        _event(
            "https://www.espn.com/nba/game/_/gameId/401902644/heat-raptors",
            "Toronto Raptors",
            "Miami Heat",
            home_score="88",
            away_score="91",
            venue={"fullName": "Scotiabank Arena"},
            status={"type": {"detail": "Q3 - 4:12", "state": "in"}},
            leaders=[
                {
                    "displayName": "Points",
                    "leaders": [
                        {
                            "displayValue": "28",
                            "athlete": {"displayName": "Jimmy Butler"},
                        }
                    ],
                }
            ],
        )
    )[0]
    assert nba.venue == "Scotiabank Arena"
    assert nba.detail == "Q3 - 4:12"
    assert nba.state == "in"
    assert nba.home_score == "88"
    assert nba.away_score == "91"
    assert "Jimmy Butler" in nba.leaders[0]
    assert parse_espn_scoreboard({}) == []
    assert parse_espn_scoreboard({"events": [{"name": "x"}, "nope"]}) == []


def case_summary_notes_split_goals_and_period_boxes():
    barca = "https://www.espn.com/soccer/match/_/gameId/401882888/barcelona-valencia"
    assert _notes(
        _event(barca, "Valencia", "Barcelona"),
        plays=(
            _goal(
                "Lamine Yamal (Barcelona) Goal at 12'",
                team="Barcelona",
                home_away="home",
                clock="12'",
            ),
            _goal(
                "Hugo Duro (Valencia) Goal at 40'",
                team="Valencia",
                home_away="home",
                clock="40'",
            ),
        ),
    ) == ("Home", "Hugo Duro 40'", "Away", "Lamine Yamal 12'")
    assert _notes(
        _event(barca, "Valencia", "Barcelona"),
        plays=(_goal("Hugo Duro (Valencia) Goal at 40'", team="Valencia", clock="40'"),),
    ) == ("Home", "Hugo Duro 40'", "Away", "-")
    assert _notes(
        _header(
            "https://www.espn.com/soccer/match/_/gameId/401882871",
            "Barcelona",
            "Racing Santander",
            shorts=("Barcelona", "Racing"),
        ),
        events=(
            _goal(short="João Cancelo Goal", team="Barcelona", clock="8'", scoring=True),
            _goal(
                short="Maguette Gueye Goal",
                team="Racing Santander",
                clock="30'",
                scoring=True,
            ),
        ),
    ) == ("Home", "João Cancelo 8'", "Away", "Maguette Gueye 30'")
    assert _notes(
        _header(
            "https://www.espn.com/nba/game/_/gameId/401902644/heat-raptors",
            "Los Angeles Lakers",
            "Warriors",
            league="nba",
            shorts=("Lakers", "Warriors"),
            scores=("88", "92"),
            lines=([19, 23, 24, 22], [21, 21, 21, 29]),
        ),
        plays=(_goal("LeBron James makes two point shot"),),
    ) == ("Lakers quarters: 19 23 24 22; Warriors quarters: 21 21 21 29",)
    assert _notes(
        _header(
            "https://www.espn.com/nhl/game/_/gameId/401559001",
            "Rangers",
            "Bruins",
            league="nhl",
            shorts=("Rangers", "Bruins"),
            lines=([1, 2, 1, 1], [0, 2, 2, 0]),
        )
    ) == ("Rangers periods: 1 2 1 1; Bruins periods: 0 2 2 0",)
    assert _notes(
        _header(
            "https://www.espn.com/mlb/game/_/gameId/401696001",
            "Yankees",
            "Red Sox",
            league="mlb",
            shorts=("Yankees", "Red Sox"),
            lines=(
                [1, 0, 2, 0, 0, 1, 0, 3, 0],
                [0, 1, 0, 0, 2, 0, 1, 0, 0],
            ),
        )
    ) == (
        "Yankees innings: 1 0 2 0 0 1 0 3 0; Red Sox innings: 0 1 0 0 2 0 1 0 0",
    )
    assert _notes(
        _header(
            "https://www.espn.com/tennis/match/_/gameId/1",
            "Alcaraz",
            "Sinner",
            league="atp",
            shorts=("Alcaraz", "Sinner"),
            lines=([6, 4, 6], [3, 6, 4]),
        )
    ) == ("Alcaraz sets: 6 4 6; Sinner sets: 3 6 4",)
    soccer_lines = " ".join(
        _notes(
            _header(
                "https://www.espn.com/soccer/match/_/gameId/1",
                "Arsenal",
                "Chelsea",
                league="eng.1",
                sport="Soccer",
                shorts=("Arsenal", "Chelsea"),
                lines=([1, 0, 0, 1], [0, 1, 0, 0]),
            ),
            plays=(_goal("Bukayo Saka Goal", short="Saka Goal", scoring=True),),
        )
    ).casefold()
    assert "quarters" not in soccer_lines
    assert "periods" not in soccer_lines
    assert "Mahomes" in _notes(
        plays=(_goal("Patrick Mahomes 6 Yd Rush (Harrison Butker Kick)"),)
    )[0]


def case_summary_headline_venue_and_empty_payloads():
    assert parse_espn_summary(None) is None
    assert parse_espn_summary({}) is None
    headline = parse_espn_summary({"article": {"headline": "Arsenal win"}})
    assert headline is not None
    assert headline.home == "" and headline.url == ""
    assert headline.notes == ("Arsenal win",)
    bare = _event(
        "https://www.espn.com/soccer/match/_/gameId/9/arsenal-chelsea",
        "Arsenal",
        "Chelsea",
    )
    summary = parse_espn_summary(
        {
            "header": bare,
            "gameInfo": {"venue": {"displayName": "Emirates Stadium"}},
            "boxscore": {
                "leaders": [
                    {
                        "name": "Goals",
                        "leaders": [{"displayValue": "1", "athlete": {"shortName": "Saka"}}],
                    }
                ]
            },
        }
    )
    assert summary is not None
    assert summary.venue == "Emirates Stadium"
    assert summary.leaders == ("Saka 1 Goals",)
