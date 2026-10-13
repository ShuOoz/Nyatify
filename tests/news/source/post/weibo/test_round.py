"""One Weibo poll: each guest on its proxy, then the Damai backup."""

from __future__ import annotations

import time

import pytest

import news.source.post.common as common
from config import settings
from fetch import FetchError
from news.feeds import PullResult
from news.source.post.weibo import _fetch_cookie, login_held
from news.source.post.weibo.http import (
    current_proxy,
)
from news.source.post.weibo.ticket import DAMAI_WEIBO, ArtistName, WatchedArtist
from tests.helpers import ACCOUNT, GUILD_ID, ScriptedAI

FIRST, SECOND = "1111111111", "2222222222"


@pytest.fixture(autouse=True)
def _two_proxies(monkeypatch):
    monkeypatch.setattr(settings, "weibo_proxies", ("http://p0", "http://p1"))
    monkeypatch.setattr(settings, "weibo_proxy", "http://p0")
    monkeypatch.setattr(settings, "weibo_cookie", None)


def _exit_lookup(table):
    async def lookup(proxy: str):
        return table[proxy]

    return lookup


def _lanes(rig):
    """Record `(account, proxy)` for each ask; the cookie and login ride along."""
    seen: list[tuple] = []

    async def pull(_platform, account, **_kwargs):
        seen.append((account, current_proxy(), _fetch_cookie("SUB=login"), login_held()))
        return PullResult(account, [], [])

    rig.fetcher.pull = pull
    return seen


def _arm_damai(rig) -> None:
    rig.poll.ai_on = True
    rig.poll.ai = ScriptedAI()
    rig.poll.damai._watched.append(
        WatchedArtist(GUILD_ID, "bilibili", ACCOUNT, ArtistName("周杰伦", "周杰伦"))
    )


async def test_a_weibo_round_stops_rests_and_keeps_the_login(monkeypatch, scene):
    await case_a_parked_or_resting_account_waits(scene)
    await case_a_432_rests_only_the_proxy_it_came_from(scene)
    with monkeypatch.context() as mp:
        await case_a_saved_login_keeps_to_the_first_proxy(mp, scene)


async def case_a_parked_or_resting_account_waits(scene):
    await case_a_parked_account_does_not_move_the_others(scene)
    await case_a_rest_waits_for_a_weibo_ask(scene)


async def case_a_parked_account_does_not_move_the_others(scene):
    rig = scene()
    rig.poll.skip_until[("weibo", "3333333333")] = time.monotonic() + 3600
    seen = _lanes(rig)
    assert await rig.weibo(FIRST, SECOND, "3333333333") == (0, True)
    assert [(account, proxy) for account, proxy, *_rest in seen] == [
        (FIRST, "http://p0"),
        (SECOND, "http://p0"),
    ]


async def case_a_rest_waits_for_a_weibo_ask(scene):
    rig = scene()
    common.weibo_rest.rest("")
    assert await rig.weibo() == (0, False)
    assert common.weibo_rest.sits_out("")


async def case_a_432_rests_only_the_proxy_it_came_from(scene):
    await case_a_432_stops_only_that_proxy(scene)
    await case_a_432_skips_the_rest_of_that_proxy_and_holds_its_ticket(scene)
    await case_a_432_from_the_damai_backup_rests_the_proxy_it_used(scene)


async def case_a_432_stops_only_that_proxy(scene):
    rig = scene()
    rig.script(pull_error=FetchError("HTTP 432 from api", status=432), pull_error_for=FIRST)
    await rig.weibo(FIRST, SECOND)
    assert rig.fetcher.fetches == [("weibo", FIRST), ("weibo", SECOND)]
    assert rig.poll.skip_until == {}
    assert common.weibo_rest.sits_out("http://p0")
    assert common.weibo_rest.sits_out("http://p1") is False

    rig.fetcher.fetches.clear()
    rig.script()
    await rig.weibo(FIRST, SECOND)
    assert rig.fetcher.fetches == [("weibo", FIRST), ("weibo", SECOND)]
    assert common.weibo_rest.sits_out("http://p0") is False


async def case_a_432_skips_the_rest_of_that_proxy_and_holds_its_ticket(scene):
    rig = scene()
    rig.script(pull_error=FetchError("HTTP 432 from api", status=432), pull_error_for=FIRST)
    await rig.weibo(FIRST, SECOND, "3333333333", "4444444444", tickets=(SECOND,))
    asked = [account for _platform, account in rig.fetcher.fetches]
    assert asked == [FIRST, "3333333333", "4444444444"]
    assert rig.poll.weibo_stopped == {"http://p0"}
    assert rig.poll.held_tickets == {("weibo", SECOND)}


async def case_a_432_from_the_damai_backup_rests_the_proxy_it_used(scene):
    rig = scene()
    _arm_damai(rig)
    common.weibo_rest.rest("http://p0")

    async def pull(_platform, account, **_kwargs):
        if account == DAMAI_WEIBO:
            raise FetchError("HTTP 432 from api", status=432)
        return PullResult(account, [], [])

    rig.fetcher.pull = pull
    assert await rig.damai() == (0, False)
    assert common.weibo_rest.sits_out("http://p1")
    assert common.weibo_rest.sits_out("http://p0") is False




async def case_a_saved_login_keeps_to_the_first_proxy(monkeypatch, scene):
    with monkeypatch.context() as mp:
        await case_a_failed_first_proxy_does_not_skip_a_saved_login(scene, mp)
    with monkeypatch.context() as mp:
        await case_a_saved_login_stays_on_the_first_proxy_when_it_passes(scene, mp)


async def case_a_failed_first_proxy_does_not_skip_a_saved_login(scene, monkeypatch):
    rig = scene()
    monkeypatch.setattr(settings, "ip_check", True)
    monkeypatch.setattr(settings, "weibo_cookie", "SUB=login")
    monkeypatch.setattr(
        "news.source.post.common.lookup_exit",
        _exit_lookup({"http://p0": None, "http://p1": "203.0.113.2"}),
    )
    seen = _lanes(rig)
    await rig.weibo(FIRST, SECOND)
    assert seen == [
        (FIRST, "http://p1", "", True),
        (SECOND, "http://p1", "", True),
    ]
    assert settings.weibo_cookie == "SUB=login"


async def case_a_saved_login_stays_on_the_first_proxy_when_it_passes(scene, monkeypatch):
    rig = scene()
    monkeypatch.setattr(settings, "ip_check", True)
    monkeypatch.setattr(settings, "weibo_cookie", "SUB=login")
    monkeypatch.setattr(
        "news.source.post.common.lookup_exit",
        _exit_lookup({"http://p0": "203.0.113.1", "http://p1": "203.0.113.2"}),
    )
    seen = _lanes(rig)
    await rig.weibo(FIRST, SECOND)
    assert seen == [
        (FIRST, "http://p0", "SUB=login", False),
        (SECOND, "http://p0", "SUB=login", False),
    ]




