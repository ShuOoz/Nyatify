from __future__ import annotations

from datetime import datetime, timedelta, timezone

from news.source.match.thesportsdb.matchstate import (
    _kickoff_dates,
    classify_phase,
    cup_scan_days,
    day_scan_labels,
    parse_kickoff,
    parse_sportsdb_event,
    parse_sportsdb_events,
    period_label,
    prefer_match_state,
    result_gap_days,
    scan_days,
)
from tests.helpers import match_state


def test_match_watch():
    case_parse_kickoff_and_day_scan()
    case_classify_phase_and_prefer_the_later_stage()
    case_period_label_and_event_rows()

def case_parse_kickoff_and_day_scan():
    assert parse_kickoff({"strTimestamp": "2026-09-05T15:00:00+00:00"}) == datetime(
        2026, 9, 5, 15, 0, tzinfo=timezone.utc
    )
    assert parse_kickoff({"dateEvent": "2026-09-05", "strTime": "15:00:00"}) == datetime(
        2026, 9, 5, 15, 0, tzinfo=timezone.utc
    )
    for raw in (
        {"dateEvent": "2026-09-05"},
        {"dateEvent": "2026-09-05", "strTime": ""},
        {"dateEvent": "2026-09-05", "strTime": "00:00:00"},
    ):
        assert parse_kickoff(raw) is None
    midnight = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
    assert parse_kickoff({"strTimestamp": "2026-09-05T00:00:00+00:00"}) == midnight
    assert parse_kickoff({"strTimestamp": "2026-09-05T00:00:00Z"}) == midnight
    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    days = scan_days(now)
    assert days[0] == "2026-09-06"
    assert days[-1] == "2026-09-12"
    assert "2026-09-13" not in days
    assert day_scan_labels("4328") == ("4480", "4481", "4328")
    assert day_scan_labels("Spanish La Liga") == ("4480", "4481")
    today = datetime(2026, 9, 26, 14, tzinfo=timezone.utc)
    assert cup_scan_days(today) == [
        "2026-09-26",
        "2026-09-27",
        "2026-09-28",
        "2026-09-25",
    ]
    covered = _kickoff_dates(
        [
            match_state(kickoff=datetime(2026, 9, 9, 19, 0, tzinfo=timezone.utc)),
            match_state(kickoff=datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc)),
        ]
    )
    holes = [day for day in days if day not in covered]
    assert "2026-09-09" not in holes
    assert "2026-09-11" in holes
    late = datetime(2026, 9, 26, 14, tzinfo=timezone.utc)
    stale = datetime(2026, 9, 16, 19, 30, tzinfo=timezone.utc)
    assert result_gap_days(late, [stale, late + timedelta(days=14)]) == [
        "2026-09-21",
        "2026-09-20",
        "2026-09-19",
        "2026-09-18",
        "2026-09-17",
    ]
    assert result_gap_days(late, [datetime(2026, 9, 24, 19, tzinfo=timezone.utc)]) == []
    assert result_gap_days(late, [None]) == []


def case_classify_phase_and_prefer_the_later_stage():
    phases = {
        "Match Finished": "finished",
        "After Penalties": "finished",
        "Pen": "live",
        "Q1 Complete": "live",
        "12'": "live",
        "Halftime": "live",
        "Postponed": "skip",
        "Not Started": "scheduled",
    }
    for status, phase in phases.items():
        assert classify_phase(status) == phase
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    fixture = match_state("99", kickoff=now)
    result = match_state(
        "99",
        phase="finished",
        home_score="2",
        away_score="1",
        kickoff=now,
    )
    called_off = match_state("99", phase="skip", status="Postponed", kickoff=now)
    assert prefer_match_state(None, result) is result
    kept = prefer_match_state(fixture, result)
    assert kept.phase == "finished"
    assert kept.home_score == "2"
    assert prefer_match_state(fixture, called_off).phase == "skip"
    assert prefer_match_state(called_off, fixture).phase == "skip"
    assert prefer_match_state(called_off, result).phase == "finished"
    nil = match_state("99", phase="live", home_score="0", away_score="0", kickoff=now)
    scored = match_state("99", phase="live", home_score="1", away_score="0", kickoff=now)
    assert prefer_match_state(nil, scored).home_score == "1"


def case_period_label_and_event_rows():
    assert period_label("") == ""
    assert period_label("2H") == "2nd half"
    assert period_label("Q3 - 4:12") == "3rd quarter"
    assert period_label("HT") == "Halftime"
    assert period_label("12'") == ""
    assert period_label("Extra time - 90+2") == "Extra time"
    raw = {
        "idEvent": "9",
        "strHomeTeam": "Arsenal",
        "strAwayTeam": "Chelsea",
        "strSport": "Soccer",
        "strStatus": "Match Finished",
        "intHomeScore": "2",
        "intAwayScore": "1",
        "strTimestamp": "2026-09-05T15:00:00+00:00",
        "strLeague": "Premier League",
        "idLeague": "4328",
        "strThumb": "https://example.test/t.jpg",
        "strResult": "Saka 2",
    }
    state = parse_sportsdb_event(raw)
    assert state is not None
    assert state.match_id == "9"
    assert state.phase == "finished"
    assert state.home_score == "2"
    assert state.kickoff == datetime(2026, 9, 5, 15, tzinfo=timezone.utc)
    assert state.facts == "Saka 2"
    assert state.image_url == "https://example.test/t.jpg"
    assert parse_sportsdb_event({"idEvent": "1"}) is None
    assert parse_sportsdb_event({"strHomeTeam": "Arsenal"}) is None
    tba = dict(raw, strTimestamp="2026-09-05T00:00:00Z", strSport="null")
    undated = parse_sportsdb_event(tba)
    assert undated is not None
    assert undated.kickoff == datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
    assert undated.sport == "sport"
    rows = parse_sportsdb_events(
        {"events": [raw, raw, {"idEvent": "x"}], "results": [{"idEvent": "2", "strAwayTeam": "City"}]}
    )
    assert [item.match_id for item in rows] == ["9"]
    assert parse_sportsdb_events({"results": [raw]})[0].match_id == "9"
    assert parse_sportsdb_events(None) == []
