from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from fetch import FetchError
from news.feeds import BOOKMARK_SEEDED, Fetcher
from news.source.match.thesportsdb import (
    ambiguous_tokens,
    parse_account,
    pick_check,
    pick_preview,
    previewable,
    uses_ai,
)
from news.source.match.thesportsdb.matchstate import cup_scan_days, day_scan_labels, scan_days
from news.source.match.thesportsdb.clubs import sportsdb_url
from news.source.match.thesportsdb.espn import espn_scoreboard_url, espn_summary_url
from news.source.post.weibo.mblog import WEIBO_INDEX_URL
from tests.helpers import (
    FakeHttp,
    espn_board,
    espn_summary,
    fetched_item,
    sportsdb_bodies,
    sportsdb_event,
    sportsdb_team,
    utc,
)


async def test_thesportsdb_source(tmp_path):
    case_parse_account()
    cards = tmp_path / "cards"
    cards.mkdir()
    await case_thesportsdb_fetch_cards_espn_and_name_seed(cards)
    links = tmp_path / "links"
    links.mkdir()
    await case_thesportsdb_espn_query_game_id_links_and_lists_goals(links)
    picks = tmp_path / "picks"
    picks.mkdir()
    await case_thesportsdb_espn_picks_followed_club(picks)
    holds = tmp_path / "holds"
    holds.mkdir()
    await case_thesportsdb_holds_and_scans_schedule(holds)


async def test_posted_card_looks_up_a_match_the_free_lists_dropped(tmp_path):
    await case_posted_card_looks_up_a_match_the_free_lists_dropped(tmp_path)


async def test_stale_last_result_still_finds_the_newer_match(tmp_path):
    await case_stale_last_result_still_finds_the_newer_match(tmp_path)


async def test_near_cup_day_fills_a_game_the_next_list_skipped(tmp_path):
    await case_near_cup_day_fills_a_game_the_next_list_skipped(tmp_path)



def case_parse_account():
    assert parse_account("Arsenal") is None
    assert parse_account("Arsenal", explicit=True) == "133604"
    assert parse_account("") is None
    assert parse_account("133604") == "133604"
    assert parse_account("https://www.thesportsdb.com/team/133604-arsenal") == "133604"
    assert parse_account("https://www.thesportsdb.com/league/4328-premier-league") is None
    assert parse_account("Unknown Club", explicit=True) == "Unknown Club"
    match = fetched_item(platform="thesportsdb", tags=("match", "start"))
    post = fetched_item(tags=("post",))
    assert uses_ai(match) is False and previewable(match) is True
    assert uses_ai(post) is True and previewable(post) is False
    assert ambiguous_tokens(
        FetchError("Name matches more than one team. Use thesportsdb:1, thesportsdb:2")
    ) == ["1", "2"]
    assert ambiguous_tokens(FetchError("down")) == []


NOW = utc(2026, 9, 5, 16)
NOON = utc(2026, 9, 5, 12)
SCAN = utc(2026, 9, 10, 12)
ARSENAL = sportsdb_team("133604", "Arsenal")
BARCA = sportsdb_team("133739", "Barcelona")
MADRID = sportsdb_team(
    "133738", "Real Madrid", league="Spanish La Liga", league_id="4335"
)
BAYERN = sportsdb_team(
    "133664", "Bayern Munich", league="German Bundesliga", league_id="4331"
)
CITY = sportsdb_team(
    "133613", "Manchester City", league="English Premier League", league_id="4328"
)
ZZZFC = sportsdb_team("133999", "Zzzfc United")
LA_LIGA = dict(league="Spanish La Liga", league_id="4335")
EPL = dict(league="English Premier League", league_id="4328")
UCL = dict(league="UEFA Champions League", league_id="4480")
BUNDESLIGA = dict(league="German Bundesliga", league_id="4331")

def _weibo_call(payloads: dict[str, dict]):
    async def call(_method, url, **kwargs):
        if url != WEIBO_INDEX_URL:
            raise AssertionError(url)
        uid = (kwargs.get("params") or {}).get("containerid", "").removeprefix("230413")
        return payloads.get(uid, {"ok": 1, "data": {"cards": []}})

    return call


def _fetcher(tmp_path: Path, bodies: dict[str, str], **kwargs) -> Fetcher:
    kwargs.setdefault("bili_cookie", "buvid3=guest")
    kwargs.setdefault("weibo_call", _weibo_call({}))
    if "bookmarks" not in kwargs:
        kwargs.setdefault("bookmarks_path", tmp_path / "bookmarks.json")
    now = kwargs.get("now")
    if now is not None and not callable(now):
        kwargs["now"] = lambda: now
    return Fetcher(get_text=FakeHttp(bodies), **kwargs)

async def _new(fetcher: Fetcher, platform: str, account: str):
    return (await fetcher.pull(platform, account)).new

def _ids(items) -> list[str]:
    return [item.item_id for item in items]

async def _cards(fetcher: Fetcher, account: str, **kwargs):
    loaded = await fetcher.load("thesportsdb", account, **kwargs)
    return loaded.items

def _card(items, item_id: str):
    return next(item for item in items if item.item_id == item_id)

def _urls(fetcher: Fetcher, needle: str) -> list[str]:
    return [url for url in fetcher._get_text.urls if needle in url]

def _espn(bodies, sport, league, stamp, href, home, away, *, game_id=None, note=None):
    bodies[espn_scoreboard_url(sport, league, stamp)] = espn_board(href, home, away)
    if game_id and note:
        bodies[espn_summary_url(sport, league, game_id)] = espn_summary(
            href, home, away, "2", "1", note
        )

async def _end_card(
    tmp_path,
    team,
    account,
    event_id,
    home,
    away,
    href,
    *,
    league_slug,
    league,
    stamp,
    score=None,
    game_id=None,
    note=None,
    espn_home=None,
    espn_away=None,
):
    kwargs = {"status": "Match Finished", "stamp": stamp, **league}
    if score is not None:
        kwargs["score"] = score
    bodies = sportsdb_bodies(
        team, results=[sportsdb_event(event_id, home, away, **kwargs)]
    )
    _espn(
        bodies,
        "soccer",
        league_slug,
        stamp,
        href,
        espn_home or home,
        espn_away or away,
        game_id=game_id,
        note=note,
    )
    return _card(
        await _cards(
            _fetcher(tmp_path, bodies, now=NOW), account, include_hard_skip=True
        ),
        f"{event_id}:end",
    )

def _city_feed(*, result_stamp: str, extra_day: str, extra):
    return sportsdb_bodies(
        CITY,
        events=[
            sportsdb_event(
                "2494030",
                "Manchester United",
                "Manchester City",
                stamp="2026-09-13T15:30:00+00:00",
                **EPL,
            )
        ],
        results=[
            sportsdb_event(
                "2494024",
                "Manchester City",
                "Coventry City",
                status="Match Finished",
                stamp=result_stamp,
                score=("1", "0"),
                **EPL,
            )
        ],
        now=SCAN,
        day_events={(extra_day, "4480"): [extra]},
    )

async def case_thesportsdb_fetch_cards_espn_and_name_seed(tmp_path):
    kickoff = utc(2026, 9, 5, 21)
    href = "https://www.espn.com/soccer/match/_/gameId/401882888/barcelona-valencia"
    bodies = sportsdb_bodies(
        BARCA,
        events=[
            sportsdb_event(
                "11", "Valencia", "Barcelona", status="Not Started", stamp=kickoff, **LA_LIGA
            ),
            sportsdb_event("10", "Arsenal", "Chelsea", status="1H", stamp="2026-09-05T15:00:00+00:00"),
        ],
        results=[
            sportsdb_event(
                "9",
                "Arsenal",
                "City",
                status="Match Finished",
                stamp="2026-09-05T12:00:00+00:00",
                score=("2", "1"),
            )
        ],
    )
    _espn(bodies, "soccer", "esp.1", kickoff, href, "Valencia", "Barcelona")
    items = await _cards(_fetcher(tmp_path, bodies, now=NOW), "133739", include_hard_skip=True)
    assert "10:start" in _ids(items) and "9:end" in _ids(items)
    preview = _card(items, "11:next")
    assert preview.url.endswith("/barcelona-valencia")
    assert preview.source_name == "Barcelona"

    last = NOW - timedelta(hours=2)
    done = await _end_card(
        tmp_path,
        sportsdb_team("133604", "Arsenal", **EPL),
        "133604",
        "9",
        "Arsenal",
        "Chelsea",
        "https://www.espn.com/soccer/match/_/gameId/401882903/arsenal-chelsea",
        league_slug="eng.1",
        league=EPL,
        stamp=last,
        game_id="401882903",
        note="Saka (Arsenal) Goal at 12'",
    )
    assert done.title == "Arsenal 2–1 Chelsea"
    assert "Saka" in done.text

    kickoff = utc(2026, 9, 7, 15)
    seed = sportsdb_bodies(
        ZZZFC,
        events=[
            sportsdb_event(
                "11", "Zzzfc United", "Chelsea", status="Not Started", stamp=kickoff
            )
        ],
    )
    seed[sportsdb_url("searchteams.php", {"t": "Zzzfc United"})] = json.dumps(
        {"teams": [ZZZFC]}
    )
    seeded = _fetcher(tmp_path, seed, now=NOON)
    assert await _new(seeded, "thesportsdb", "Zzzfc United") == []
    assert not seeded.bookmarks.is_seeded("thesportsdb", "Zzzfc United")
    assert BOOKMARK_SEEDED in seeded.bookmarks.seen_ids("thesportsdb", "133999")
    later = _fetcher(tmp_path, seed, bookmarks=seeded.bookmarks, now=utc(2026, 9, 7, 10))
    assert _ids(await _new(later, "thesportsdb", "133999")) == ["11:next"]

    last = NOW - timedelta(hours=2)
    href = "https://www.espn.com/soccer/match/_/gameId/401900001/real-madrid-bayern"
    far = [
        sportsdb_event(
            str(200 + index),
            "Real Madrid",
            f"Club {index}",
            status="Not Started",
            stamp=NOW + timedelta(days=2 + index),
            **LA_LIGA,
        )
        for index in range(7)
    ]
    board = sportsdb_bodies(
        MADRID,
        events=far,
        results=[
            sportsdb_event(
                "50",
                "Real Madrid",
                "Bayern Munich",
                status="Match Finished",
                stamp=last,
                score=("2", "1"),
                **UCL,
            )
        ],
    )
    _espn(board, "soccer", "uefa.champions", last, href, "Real Madrid", "Bayern Munich")
    fetcher = _fetcher(tmp_path, board, now=NOW)
    done = _card(await _cards(fetcher, "133738", limit=20), "50:end")
    assert done.url.startswith("https://www.espn.com/")
    assert _urls(fetcher, "/scoreboard") == [
        espn_scoreboard_url("soccer", "uefa.champions", last)
    ]

async def case_thesportsdb_espn_query_game_id_links_and_lists_goals(tmp_path):
    last = NOW - timedelta(hours=2)
    done = await _end_card(
        tmp_path,
        sportsdb_team("133739", "Barcelona", **LA_LIGA),
        "133739",
        "2506233",
        "Sevilla",
        "Barcelona",
        "https://www.espn.com/soccer/match?gameId=401882859/barcelona-sevilla",
        league_slug="esp.1",
        league=LA_LIGA,
        stamp=last,
        score=("1", "3"),
        game_id="401882859",
        note="Raphinha (Barcelona) Goal at 22'",
    )
    assert done.url == "https://www.espn.com/soccer/team-stats/_/gameId/401882859"
    assert "Raphinha" in done.text

async def case_thesportsdb_espn_picks_followed_club(tmp_path):
    last = NOW - timedelta(hours=2)
    done = await _end_card(
        tmp_path,
        BAYERN,
        "133664",
        "2508360",
        "Bayern Munich",
        "Union Berlin",
        "https://www.espn.com/soccer/match/_/gameId/401884790/1-fc-union-berlin-bayern-munich",
        league_slug="ger.1",
        league=BUNDESLIGA,
        stamp=last,
        score=("7", "0"),
        espn_home="Bayern Munich",
        espn_away="1. FC Union Berlin",
    )
    assert "401884790" in done.url
    assert done.title == "Bayern Munich 7–0 Union Berlin"

async def case_thesportsdb_holds_and_scans_schedule(tmp_path):
    kickoff = utc(2026, 9, 7, 15)
    upcoming = sportsdb_bodies(
        ARSENAL,
        events=[
            sportsdb_event("11", "Arsenal", "Chelsea", status="Not Started", stamp=kickoff)
        ],
    )
    fetcher = _fetcher(tmp_path, upcoming, now=NOON)
    first = await fetcher.pull("thesportsdb", "133604")
    assert first.new == []
    assert _ids(first.items) == ["11:next"]
    seen = fetcher.bookmarks.seen_ids("thesportsdb", "133604")
    assert "11:next" not in seen
    assert BOOKMARK_SEEDED in seen
    later = _fetcher(
        tmp_path, upcoming, bookmarks=fetcher.bookmarks, now=utc(2026, 9, 7, 10)
    )
    assert _ids(await _new(later, "thesportsdb", "133604")) == ["11:next"]

    live_kickoff = utc(2026, 9, 5, 15)
    live_bodies = sportsdb_bodies(
        ARSENAL,
        events=[sportsdb_event("10", "Arsenal", "Chelsea", status="1H", stamp=live_kickoff)],
        results=[
            sportsdb_event(
                "9",
                "Arsenal",
                "City",
                status="Match Finished",
                stamp=NOW - timedelta(hours=2),
                score=("2", "1"),
            ),
            sportsdb_event(
                "1",
                "Arsenal",
                "Everton",
                status="Match Finished",
                stamp=NOW - timedelta(days=3),
                score=("3", "0"),
            ),
        ],
    )
    live = _fetcher(tmp_path, live_bodies, now=NOW, bookmarks_path=tmp_path / "live.json")
    assert await _new(live, "thesportsdb", "133604") == []
    held = live.bookmarks.seen_ids("thesportsdb", "133604")
    assert "10:start" not in held and "9:end" not in held
    assert "1:end" in held
    got = await _fetcher(
        tmp_path, live_bodies, bookmarks=live.bookmarks, now=NOW
    ).pull("thesportsdb", "133604")
    assert {item.item_id for item in got.new} == {"10:start", "9:end"}
    assert got.kickoffs == (NOW - timedelta(hours=2), live_kickoff)

    porto = sportsdb_event(
        "2594591",
        "Porto",
        "Manchester City",
        status="Match Finished",
        stamp="2026-09-08T19:00:00+00:00",
        score=("0", "2"),
        **UCL,
    )
    napoli = sportsdb_event(
        "2594700",
        "Napoli",
        "Manchester City",
        stamp="2026-09-11T19:00:00+00:00",
        **UCL,
    )
    scanned = _fetcher(
        tmp_path,
        _city_feed(result_stamp="2026-09-05T14:00:00+00:00", extra_day="2026-09-08", extra=porto),
        now=SCAN,
    )
    city_items = await _cards(scanned, "133613", include_hard_skip=True)
    assert "2594591:end" in _ids(city_items)
    assert pick_preview(city_items).item_id == "2494030:next"
    assert len(_urls(scanned, "eventsday.php")) == (
        len(scan_days(SCAN)) * len(day_scan_labels("4328")) + len(cup_scan_days(SCAN))
    )
    assert any(
        "d=2026-09-06" in url and "l=4480" in url for url in _urls(scanned, "eventsday.php")
    )

    upcoming = _fetcher(
        tmp_path,
        _city_feed(result_stamp="2026-09-08T14:00:00+00:00", extra_day="2026-09-11", extra=napoli),
        now=SCAN,
    )
    upcoming_items = await _cards(upcoming, "133613", include_hard_skip=True)
    assert "2594700:next" in _ids(upcoming_items)
    assert pick_preview(upcoming_items).item_id == "2594700:next"
    holes = _urls(upcoming, "eventsday.php")
    assert not any("d=2026-09-08" in url for url in holes)
    assert any("d=2026-09-11" in url for url in holes)

    shared = _fetcher(tmp_path, sportsdb_bodies(CITY, now=SCAN), now=SCAN)
    await shared.pull("thesportsdb", "133613")
    client = shared.sportsdb
    await shared.pull("thesportsdb", "133613")
    assert shared.sportsdb is client


async def case_posted_card_looks_up_a_match_the_free_lists_dropped(tmp_path: Path):
    """Columbus vs Inter Miami: free lists drop it at kickoff; the posted card does not."""
    now = utc(2026, 9, 27, 23, 2)
    kickoff = utc(2026, 9, 27, 23)
    team = sportsdb_team("137032", "Inter Miami", league="Club friendlies", league_id="9999")
    event_id = "2200001"
    played = sportsdb_event(
        event_id,
        "Columbus Crew",
        "Inter Miami",
        status="FT",
        stamp=kickoff,
        score=("2", "1"),
        league="Club friendlies",
        league_id="9999",
    )
    later = sportsdb_event(
        "2200099",
        "Inter Miami",
        "Chicago Fire",
        stamp="2026-10-10T23:30:00+00:00",
        league="Club friendlies",
        league_id="9999",
    )
    bodies = sportsdb_bodies(team, events=[later], now=now)
    bodies[sportsdb_url("lookupevent.php", {"id": event_id})] = json.dumps(
        {"events": [played]}
    )

    missed = _fetcher(tmp_path, bodies, now=now, bookmarks_path=tmp_path / "missed.json")
    missed_got = await missed.pull("thesportsdb", "137032")
    assert f"{event_id}:end" not in _ids(missed_got.items)
    assert _urls(missed, "lookupevent.php") == []

    held = _fetcher(tmp_path, bodies, now=now, bookmarks_path=tmp_path / "held.json")
    held.mark_delivered("thesportsdb", "137032", f"{event_id}:next", ["1:match"])
    got = await held.pull("thesportsdb", "137032")
    assert f"{event_id}:end" in _ids(got.items)
    assert "2–1" in _card(got.items, f"{event_id}:end").title
    assert kickoff in got.kickoffs
    assert _urls(held, "lookupevent.php")

    done = _fetcher(tmp_path, bodies, now=now, bookmarks_path=tmp_path / "done.json")
    done.mark_delivered("thesportsdb", "137032", f"{event_id}:next", ["1:match"])
    done.mark_delivered("thesportsdb", "137032", f"{event_id}:end", ["1:match"])
    await done.pull("thesportsdb", "137032")
    assert _urls(done, "lookupevent.php") == []


async def case_stale_last_result_still_finds_the_newer_match(tmp_path: Path):
    now = utc(2026, 9, 26, 14)
    sevilla = sportsdb_event(
        "401882859",
        "Sevilla",
        "Barcelona",
        status="FT",
        stamp="2026-09-19T19:00:00+00:00",
        score=("1", "3"),
        **LA_LIGA,
    )
    bodies = sportsdb_bodies(
        sportsdb_team(
            "133739", "Barcelona", league="Spanish La Liga", league_id="4335"
        ),
        events=[
            sportsdb_event(
                "2",
                "Barcelona",
                "Getafe",
                stamp="2026-10-10T16:30:00+00:00",
                **LA_LIGA,
            )
        ],
        results=[
            sportsdb_event(
                "1",
                "Barcelona",
                "Racing de Santander",
                status="FT",
                stamp="2026-09-16T19:30:00+00:00",
                score=("7", "2"),
                **LA_LIGA,
            )
        ],
        now=now,
    )
    for day, events in (
        ("2026-09-21", []),
        ("2026-09-20", []),
        ("2026-09-19", [sevilla]),
    ):
        bodies[sportsdb_url("eventsday.php", {"d": day, "l": "4335"})] = json.dumps(
            {"events": events}
        )
    fetcher = _fetcher(tmp_path, bodies, now=now)
    loaded = await fetcher.load("thesportsdb", "133739", include_hard_skip=True)
    picked = pick_check(loaded.items, now=now)
    assert loaded.schedule is True
    found = next(item for item in loaded.items if item.item_id == "401882859:end")
    assert found.title == "Sevilla 1–3 Barcelona"
    assert picked is not None and picked.item_id == "2:next"
    urls = _urls(fetcher, "eventsday.php")
    assert any("d=2026-09-19" in url and "l=4335" in url for url in urls)
    assert not any("d=2026-09-18" in url or "d=2026-09-17" in url for url in urls)


async def case_near_cup_day_fills_a_game_the_next_list_skipped(tmp_path: Path):
    now = utc(2026, 9, 26, 14)
    cup = sportsdb_event(
        "88",
        "Barcelona",
        "Celta Vigo",
        stamp="2026-09-27T19:00:00+00:00",
        league="Copa del Rey",
        league_id="4483",
    )
    bodies = sportsdb_bodies(
        sportsdb_team(
            "133739", "Barcelona", league="Spanish La Liga", league_id="4335"
        ),
        events=[
            sportsdb_event(
                "2",
                "Barcelona",
                "Getafe",
                stamp="2026-10-10T16:30:00+00:00",
                **LA_LIGA,
            )
        ],
        results=[
            sportsdb_event(
                "1",
                "Sevilla",
                "Barcelona",
                status="FT",
                stamp="2026-09-25T19:00:00+00:00",
                score=("1", "3"),
                **LA_LIGA,
            )
        ],
        now=now,
        day_events={("2026-09-27", "4483"): [cup]},
    )
    fetcher = _fetcher(tmp_path, bodies, now=now)
    loaded = await fetcher.load("thesportsdb", "133739", include_hard_skip=True)
    assert loaded.schedule is True
    assert "88:next" in _ids(loaded.items)
    urls = _urls(fetcher, "eventsday.php")
    assert any("d=2026-09-27" in url and "l=4483" in url for url in urls)
    assert not any("d=2026-09-22" in url and "l=4483" in url for url in urls)




