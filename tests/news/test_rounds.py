"""One account ask in a poll: fetch, refresh a cookie, park a failure, deliver."""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from config import settings
from fetch import FetchError
from guild import GuildConfig
from news.feeds import Fetcher, PullResult
from news.follows import (
    FEATURE_GAME,
    FEATURE_MATCH,
    FEATURE_TICKET,
    TOPIC_UNASSIGNED,
    add_news_follow,
)
from news.rounds import (
    catch_up_items,
    deliver,
    fetch_failure,
    hold_tickets,
    is_due,
    pause_between,
    poll_plan,
    poll_source,
)
from news.source.registry import FLOOD_CAP
from tests.helpers import COOKIE, GUILD_ID, ScriptedFetcher, fetched_item

SOURCE = ("bilibili", "10")
LOGIN = "SESSDATA=s; bili_jct=j"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _ask(rig, source=SOURCE) -> int:
    return await poll_source(rig.poll, source, [(GUILD_ID, FEATURE_GAME)], _now())


def _login(monkeypatch, *, cookie=LOGIN, ac_time="ac", applied=None):
    """A saved Bilibili login whose refresh is faked and counted."""
    monkeypatch.setattr(settings, "bili_cookie", cookie)
    monkeypatch.setattr(settings, "bili_ac_time", ac_time)

    async def fake_apply(*_args, **kwargs):
        if applied is not None:
            applied.append(kwargs)
        return {"line": "Refreshed login cookie.", "config": {}, "settings_update": {}, "changed": True}

    monkeypatch.setattr("news.rounds.apply_news_cookie", fake_apply)


async def test_the_small_round_rules(monkeypatch, scene):
    case_fetch_failure_includes_cause_and_body()
    case_a_source_is_due_when_asked_for_and_not_parked(scene)
    case_hold_tickets_keeps_ticket_accounts_this_round_did_not_ask(scene)
    with monkeypatch.context() as mp:
        await case_a_pause_sleeps_a_jittered_gap_and_none_without_one(mp)


def case_fetch_failure_includes_cause_and_body():
    try:
        try:
            raise TimeoutError("timed out talking to https://x.test/a?key=secret")
        except TimeoutError as cause:
            raise FetchError("Could not fetch https://x.test/a", body="rate limit") from cause
    except FetchError as exc:
        text = fetch_failure(exc)
    assert "timed out" in text
    assert "rate limit" in text
    assert "key=secret" not in text

    blank = FetchError(
        "Bilibili dynamics returned no posts from https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        body=(
            '{"code":0,"message":"0","data":{"update_num":"0","update_baseline":"",'
            '"offset":"","has_more":false,"total":"0","items":[]},"ttl":1}'
        ),
    )
    blank_text = fetch_failure(blank)
    assert "returned no posts" in blank_text
    assert '"items":[]' in blank_text
    assert "NoneType" not in blank_text

    blocked = FetchError(
        "HTTP 412 from https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        status=412,
        body='{"code":-352,"message":"风控"}',
    )
    blocked_text = fetch_failure(blocked)
    assert "HTTP 412" in blocked_text
    assert "-352" in blocked_text
    assert "NoneType" not in blocked_text


def case_hold_tickets_keeps_ticket_accounts_this_round_did_not_ask(scene):
    rig = scene()
    plain, missed, left_out = "plain", "missed", "left_out"
    hold_tickets(
        rig.poll, "weibo", [plain, missed, left_out], {missed, left_out}, {("weibo", missed)}
    )
    assert rig.poll.held_tickets == {("weibo", missed)}
    rig.poll.skip_until[("weibo", left_out)] = time.monotonic() + 60
    hold_tickets(rig.poll, "weibo", [left_out], {left_out}, None)
    assert ("weibo", left_out) not in rig.poll.held_tickets


def case_a_source_is_due_when_asked_for_and_not_parked(scene):
    rig = scene()
    other = ("bilibili", "11")
    assert is_due(rig.poll, SOURCE, None)
    assert is_due(rig.poll, SOURCE, {SOURCE})
    assert not is_due(rig.poll, SOURCE, {other})
    rig.poll.skip_until[SOURCE] = time.monotonic() + 60
    assert not is_due(rig.poll, SOURCE, None)
    rig.poll.skip_until[SOURCE] = time.monotonic() - 1
    assert is_due(rig.poll, SOURCE, None)


async def case_a_pause_sleeps_a_jittered_gap_and_none_without_one(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("news.rounds.asyncio.sleep", fake_sleep)
    await pause_between(0, 10.0, random.Random(0))
    assert slept == []
    await pause_between(30.0, 60.0, random.Random(0))
    assert len(slept) == 1 and 30.0 <= slept[0] <= 90.0












async def test_a_failed_pull_parks_or_warns(monkeypatch, scene):
    with monkeypatch.context() as mp:
        await case_a_login_cookie_fault_refreshes_then_parks_the_account_and_warns(scene, mp)
    with monkeypatch.context() as mp:
        await case_a_new_login_cookie_is_read_once(scene, mp)
    with monkeypatch.context() as mp:
        await case_a_cookie_fault_inside_the_refresh_window_pulls_once(scene, mp)
    with monkeypatch.context() as mp:
        await case_a_visitor_cookie_is_not_repaired(scene, mp)
    await case_a_down_source_is_parked_without_a_warning(scene)
    await case_an_empty_bilibili_space_is_not_parked(scene)


async def case_a_login_cookie_fault_refreshes_then_parks_the_account_and_warns(scene, monkeypatch):
    rig = scene()
    applied: list[dict] = []
    _login(monkeypatch, applied=applied)
    rig.script(pull_error=COOKIE)
    assert await _ask(rig) == 0
    assert len(applied) == 1
    assert rig.fetcher.fetches == [SOURCE, SOURCE]
    assert rig.poll.skip_until[SOURCE] > time.monotonic()
    rig.poll.warn_cookie.assert_awaited_once()
    key, live, exc, seconds = rig.poll.warn_cookie.await_args.args
    assert (key, live, exc) == (SOURCE, [(GUILD_ID, FEATURE_GAME)], COOKIE)
    assert seconds > 0


async def case_a_new_login_cookie_is_read_once(scene, monkeypatch):
    rig = scene()
    applied: list[dict] = []
    _login(monkeypatch, applied=applied)
    rig.fetcher = rig.poll.fetcher = ScriptedFetcher(new=[fetched_item()]).fail_then(COOKIE)
    assert await _ask(rig) == 0
    assert len(applied) == 1
    assert rig.fetcher.fetches == [SOURCE, SOURCE]
    assert rig.poll.skip_until == {}
    rig.poll.warn_cookie.assert_not_awaited()


async def case_a_cookie_fault_inside_the_refresh_window_pulls_once(scene, monkeypatch):
    rig = scene()
    applied: list[dict] = []
    _login(monkeypatch, applied=applied)
    rig.poll.bili.refreshed_until = time.monotonic() + 60
    rig.script(pull_error=COOKIE)
    assert await _ask(rig) == 0
    assert applied == []
    assert rig.fetcher.fetches == [SOURCE]
    assert rig.poll.skip_until[SOURCE] > time.monotonic()
    rig.poll.warn_cookie.assert_awaited_once()


async def case_a_visitor_cookie_is_not_repaired(scene, monkeypatch):
    rig = scene()
    applied: list[dict] = []
    _login(monkeypatch, cookie="buvid3=guest; b_nut=1", ac_time=None, applied=applied)
    rig.fetcher = rig.poll.fetcher = ScriptedFetcher(new=[fetched_item()]).fail_then(COOKIE)
    assert await _ask(rig) == 0
    assert applied == []
    assert rig.fetcher.fetches == [SOURCE]
    assert rig.poll.skip_until == {}








async def case_a_down_source_is_parked_without_a_warning(scene):
    rig = scene()
    rig.script(pull_error=FetchError("HTTP 503 from api", status=503))
    assert await _ask(rig) == 0
    assert SOURCE in rig.poll.skip_until
    rig.poll.warn_cookie.assert_not_awaited()


def test_who_gets_asked(tmp_path):
    case_poll_plan_asks_live_topics_and_skips_a_silent_guild(tmp_path)


async def test_what_gets_posted(tmp_path):
    await case_ticket_week_goes_only_to_ticket_guilds(tmp_path)
    await case_posts_stop_at_the_flood_cap(tmp_path)
    case_catch_up_posts_only_when_a_guild_missed_a_delivered_item()
    await case_the_catch_line_keeps_that_page_out_of_the_ticket_week(tmp_path)


def case_poll_plan_asks_live_topics_and_skips_a_silent_guild(tmp_path):
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    store = GuildConfig(tmp_path / "plan.json")
    add_news_follow(store, GUILD_ID, "bilibili", "1", TOPIC_UNASSIGNED)
    add_news_follow(store, GUILD_ID, "bilibili", "2", FEATURE_GAME)
    add_news_follow(store, GUILD_ID, "thesportsdb", "133604", FEATURE_MATCH)
    add_news_follow(store, GUILD_ID, "weibo", "1195230310", FEATURE_TICKET)
    add_news_follow(store, GUILD_ID, "bilibili", "4", FEATURE_GAME)
    keep, live_map, sports, posts, tickets = poll_plan(
        store,
        now=10.0,
        now_wall=now,
        poll_post=True,
        poll_match=True,
        poll_ticket=True,
        skip_until={("bilibili", "2"): 99.0},
        guild_ok=lambda _gid: True,
    )
    assert ("bilibili", "1") in keep
    assert ("bilibili", "1") not in live_map
    assert live_map[("bilibili", "2")] == [(GUILD_ID, FEATURE_GAME)]
    assert posts == [("bilibili", "4")]
    assert tickets == [("weibo", "1195230310")]
    assert sports == [("thesportsdb", "133604")]
    _, _, _, posts_only, tickets_off = poll_plan(
        store,
        now=10.0,
        now_wall=now,
        poll_post=True,
        poll_match=False,
        poll_ticket=False,
        skip_until={},
        guild_ok=lambda _gid: True,
    )
    assert ("weibo", "1195230310") not in tickets_off
    assert ("bilibili", "4") in posts_only

    silent = GuildConfig(tmp_path / "silent.json")
    add_news_follow(silent, GUILD_ID, "bilibili", "7", FEATURE_GAME)
    add_news_follow(silent, 2, "bilibili", "8", FEATURE_TICKET)
    keep, live, sports, posts, tickets = poll_plan(
        silent,
        now=10.0,
        now_wall=now,
        poll_post=True,
        poll_match=True,
        skip_until={},
        guild_ok=lambda _gid: False,
    )
    assert keep == {("bilibili", "7"), ("bilibili", "8")}
    assert live == {}
    assert sports == [] and posts == [] and tickets == []


def _clocked(tmp_path, now: datetime) -> Fetcher:
    return Fetcher(bookmarks_path=tmp_path / "bookmarks.json", now=lambda: now)


async def case_ticket_week_goes_only_to_ticket_guilds(tmp_path):
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    recent = fetched_item("recent", occurred_at=now - timedelta(days=2))
    old = fetched_item("old", occurred_at=now - timedelta(days=30))
    fetcher = _clocked(tmp_path, now)
    got = PullResult(None, [], [recent, old], items=[recent, old], opening=True)
    sent: list[tuple[list[tuple[int, str]], str]] = []

    async def missed(_platform, _account, live, item):
        sent.append((list(live), item.item_id))
        return 0

    game = [(1, FEATURE_GAME)]
    live = [(1, FEATURE_GAME), (2, FEATURE_TICKET)]
    assert await deliver(fetcher, "bilibili", "99", game, got, missed) == 0
    assert sent == []
    assert await deliver(fetcher, "bilibili", "99", live, got, missed) == 0
    assert sent == [([(2, FEATURE_TICKET)], "recent")]

    async def posted(_platform, _account, audience, item):
        sent.append((list(audience), item.item_id))
        fetcher.mark_delivered(
            "bilibili", "99", item.item_id, [f"{guild_id}:{topic}" for guild_id, topic in audience]
        )
        return 1

    assert await deliver(fetcher, "bilibili", "99", live, got, posted) == 1
    assert fetcher.delivered_refs("bilibili", "99", "recent") == {"2:ticket"}
    assert catch_up_items(fetcher, "bilibili", "99", live, got, set(), 5, now=now) == []


async def case_posts_stop_at_the_flood_cap(tmp_path):
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    fresh = [fetched_item(f"guid-{n}") for n in range(6)]
    seen: list[str] = []

    async def announce(_platform, _account, _live, item):
        seen.append(item.item_id)
        return 1

    posted = await deliver(
        ScriptedFetcher(), "weibo", "1195230310", [(1, FEATURE_GAME)], PullResult(None, fresh), announce
    )
    assert posted == FLOOD_CAP
    assert seen == [f"guid-{n}" for n in range(5, 0, -1)]

    backlog = [
        fetched_item(f"week-{n}", occurred_at=now - timedelta(hours=n))
        for n in range(FLOOD_CAP + 1)
    ]
    ticket_seen: list[str] = []

    async def ticket_announce(_platform, _account, _live, item):
        ticket_seen.append(item.item_id)
        return 1

    posted = await deliver(
        _clocked(tmp_path, now),
        "bilibili",
        "99",
        [(2, FEATURE_TICKET)],
        PullResult(None, [], items=backlog),
        ticket_announce,
    )
    assert posted == FLOOD_CAP
    assert ticket_seen == [f"week-{n}" for n in range(FLOOD_CAP, 0, -1)]


def case_catch_up_posts_only_when_a_guild_missed_a_delivered_item():
    item = fetched_item("guid-1")
    got = PullResult(None, [], [item], items=[item])
    empty = ScriptedFetcher()
    assert catch_up_items(empty, "weibo", "1195230310", [(1, "game")], got, set(), 5) == []
    seen = ScriptedFetcher()
    seen.mark_delivered("weibo", "1195230310", "guid-1", ["1:game"])
    found = catch_up_items(
        seen, "weibo", "1195230310", [(1, "game"), (2, "game")], got, set(), 5
    )
    assert [item.item_id for item in found] == ["guid-1"]


async def case_the_catch_line_keeps_that_page_out_of_the_ticket_week(tmp_path):
    now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    fetcher = _clocked(tmp_path, now)
    fetcher.bookmarks.remember_line("bilibili", "99", "1001", now)
    on_the_page = fetched_item("1001", occurred_at=now + timedelta(hours=1))
    older_clock = fetched_item("5000", occurred_at=now - timedelta(hours=2))
    newer = fetched_item("1003", occurred_at=now + timedelta(hours=1))
    got = PullResult(
        None, [], [on_the_page, older_clock, newer], items=[on_the_page, older_clock, newer]
    )
    sent: list[str] = []

    async def announce(_platform, _account, _live, item):
        sent.append(item.item_id)
        return 1

    await deliver(fetcher, "bilibili", "99", [(2, FEATURE_TICKET)], got, announce)
    assert sent == ["1003"]


async def case_an_empty_bilibili_space_is_not_parked(scene):
    rig = scene()
    rig.script(
        pull_error=FetchError(
            "Bilibili dynamics returned no posts from api",
            body='{"code":0,"message":"0","data":{"items":[]}}',
        ),
        pull_error_for="10",
    )
    assert await _ask(rig) == 0
    assert rig.poll.skip_until == {}
