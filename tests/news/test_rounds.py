"""One account ask in a poll: fetch, refresh a cookie, park a failure, deliver."""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone

from config import settings
from fetch import FetchError
from news.follows import FEATURE_GAME
from news.rounds import fetch_failure, hold_tickets, is_due, pause_between, poll_source
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
