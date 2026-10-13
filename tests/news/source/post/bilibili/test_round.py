"""Account groups, the rotating first ask, the proxy window, and one Bilibili round."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

import news.source.post.bilibili.app as bili_app
import news.source.post.bilibili.dynamics as dynamics
import news.source.post.bilibili.round as bili_round
import news.source.post.common as common
from config import settings
from fetch import FetchError
from news.feeds import PullResult
from news.source.post.bilibili.round import (
    BlankPlan,
    Phone,
    Rotation,
    _Run,
    backup_proxy,
    plan,
    roster_key,
)
from news.source.post.common import ProxyRest
from tests.helpers import RoundRig, scratch


def _saved_phone(proxy: str) -> str | None:
    book = bili_app.buvid_store
    found = book._memory.get(proxy, {}).get(0) or book._load().get(proxy, {}).get(0)
    return found.buvid if found else None


def test_the_window_and_what_it_saves(caplog, tmp_path):
    case_roster_covers_with_two_then_three_and_slides()
    case_roster_uses_the_configured_phone_and_account_counts()
    case_rotation_saves_and_resets_on_a_new_roster(scratch(tmp_path, "rotation_saves_and_resets_on_a_new_roster"), caplog)
    case_a_412_rest_sits_out_once(scratch(tmp_path, "a_412_rest_sits_out_once"))
    case_a_blank_plan_remembers_the_account(scratch(tmp_path, "a_blank_plan_remembers_the_account"))


def case_roster_covers_with_two_then_three_and_slides():
    proxies = ("p0", "p1", "p2", "p3", "p4")
    accounts = ("a", "b", "c", "d", "e", "f")
    phones, spent = plan(accounts, proxies, 0, ())
    assert spent == ()
    assert [(phone.proxy, phone.accounts) for phone in phones] == [
        ("p0", ("a", "b")),
        ("p1", ("c", "d")),
        ("p2", ("e", "f")),
    ]
    phones, _spent = plan(accounts, proxies, 1, ())
    assert [(phone.proxy, phone.accounts) for phone in phones] == [
        ("p1", ("b", "a")),
        ("p2", ("d", "c")),
        ("p3", ("f", "e")),
    ]
    phones, _spent = plan(accounts, proxies, 2, ())
    assert [phone.proxy for phone in phones] == ["p2", "p3", "p4"]

    tight = ("p0", "p1")
    phones, _spent = plan(accounts, tight, 0, ())
    assert [(phone.proxy, phone.accounts) for phone in phones] == [
        ("p0", ("a", "b", "c")),
        ("p1", ("d", "e", "f")),
    ]
    phones, _spent = plan(("a", "b", "c"), ("p0", "p1"), 0, ())
    assert [(phone.proxy, phone.accounts) for phone in phones] == [
        ("p0", ("a", "b")),
        ("p1", ("c",)),
    ]
    phones, _spent = plan(("a", "b", "c", "d", "e", "f", "g"), tight, 0, ())
    assert [(phone.proxy, phone.accounts) for phone in phones] == [
        ("p0", ("a", "b", "c", "d")),
        ("p1", ("e", "f", "g")),
    ]
    phones, _spent = plan(("b", "a", "c"), ("p0", "p1"), 0, ())
    assert [phone.accounts for phone in phones] == [("b", "a"), ("c",)]

    phones, spent = plan(accounts, proxies, 0, ("p0",))
    assert spent == ("p0",)
    assert [phone.proxy for phone in phones] == ["p1", "p2", "p3"]
    phones, spent = plan(accounts[:2], proxies, 0, ("p4",))
    assert spent == ()
    assert [phone.proxy for phone in phones] == ["p0"]
    phones, spent = plan(("a", "b", "c"), ("p0",), 1, ())
    assert phones[0].accounts == ("b", "c", "a")
    phones, spent = plan(("a", "b", "c"), ("p0",), 2, ())
    assert phones[0].accounts == ("c", "a", "b")


def case_roster_uses_the_configured_phone_and_account_counts():
    proxies = ("p0", "p1")
    accounts = ("a", "b", "c", "d", "e", "f")
    phones, spent = plan(
        accounts, proxies, 0, (), phones_per_proxy=2, accounts_per_phone=2
    )
    assert spent == ()
    assert [(phone.proxy, phone.index, phone.accounts) for phone in phones] == [
        ("p0", 0, ("a", "b")),
        ("p1", 0, ("c", "d")),
        ("p0", 1, ("e", "f")),
    ]
    phones, _spent = plan(
        accounts, proxies, 1, (), phones_per_proxy=2, accounts_per_phone=2
    )
    assert [(phone.proxy, phone.index, phone.accounts) for phone in phones] == [
        ("p1", 0, ("b", "a")),
        ("p0", 1, ("d", "c")),
        ("p1", 1, ("f", "e")),
    ]
    phones, spent = plan(
        accounts, proxies, 0, ("p0",), phones_per_proxy=2, accounts_per_phone=2
    )
    assert spent == ("p0",)
    assert [(phone.proxy, phone.index, phone.accounts) for phone in phones] == [
        ("p1", 0, ("a", "b", "e", "f")),
        ("p1", 1, ("c", "d")),
    ]

    phones, _spent = plan(
        accounts, ("p0", "p1", "p2"), 0, (), phones_per_proxy=1, accounts_per_phone=4
    )
    assert [(phone.proxy, phone.accounts) for phone in phones] == [
        ("p0", ("a", "b", "c", "d")),
        ("p1", ("e", "f")),
    ]
    phones, _spent = plan(
        ("a", "b"), ("p0", "p1"), 2, (), phones_per_proxy=2, accounts_per_phone=2
    )
    assert [(phone.proxy, phone.index, phone.accounts) for phone in phones] == [
        ("p0", 0, ("a", "b")),
    ]
    phones, spent = plan(
        accounts, ("p0", "p1", "p2"), 0, ("p0",), phones_per_proxy=2, accounts_per_phone=2
    )
    assert spent == ("p0",)
    assert [(phone.proxy, phone.index) for phone in phones] == [
        ("p1", 0),
        ("p2", 0),
        ("p1", 1),
    ]
    phones, _spent = plan(
        ("a", "b", "c"), ("p0",), 0, (), phones_per_proxy=3, accounts_per_phone=1
    )
    assert [(phone.index, phone.accounts) for phone in phones] == [
        (0, ("a",)),
        (1, ("b",)),
        (2, ("c",)),
    ]


def case_rotation_saves_and_resets_on_a_new_roster(tmp_path, caplog):
    caplog.clear()

    path = tmp_path / "bili_rotation.json"
    store = Rotation(path)
    key = roster_key((("1", "2"), ("http://p0",), 1, 2))
    store.save(7, key)
    assert store.load(key) == 7
    assert "http" not in path.read_text(encoding="utf-8")
    assert "p0" not in path.read_text(encoding="utf-8")
    assert store.load(roster_key((("1",), ("http://p0",), 1, 2))) == 0

    path.write_text("{", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="news.source.post.bilibili.round")
    assert store.load(key) == 0
    assert any("Could not read" in record.getMessage() for record in caplog.records)


def case_a_412_rest_sits_out_once(tmp_path):
    path = tmp_path / "cap.json"
    fresh = ProxyRest(path)
    fresh.rest("http://fresh")
    assert json.loads(path.read_text(encoding="utf-8"))["http://fresh"] == {"sit_out": True}
    loaded = ProxyRest(path)
    assert loaded.sits_out("http://fresh")
    assert loaded.take("http://fresh")
    assert json.loads(path.read_text(encoding="utf-8")) == {}

    path.write_text(
        json.dumps(
            {
                "http://blank": {"blanks": 3, "sit_out": True},
                "http://count": {"blanks": 1, "sit_out": False},
                "http://blocked": {"blocked": True, "sit_out": True},
            }
        ),
        encoding="utf-8",
    )
    kept = ProxyRest(path)
    assert kept.sits_out("http://blank")
    assert kept.sits_out("http://count") is False
    assert kept.sits_out("http://blocked")
    assert kept.take("http://blank")
    again = ProxyRest(path)
    assert again.sits_out("http://blocked")
    assert again.sits_out("http://blank") is False


def case_a_blank_plan_remembers_the_account(tmp_path):
    path = tmp_path / "blank.json"
    plan_store = BlankPlan(path)
    assert plan_store.due("1") is False
    plan_store.note("1")
    plan_store.note("1")
    assert json.loads(path.read_text(encoding="utf-8")) == {"accounts": ["1"]}
    loaded = BlankPlan(path)
    assert loaded.due("1")
    loaded.clear("1")
    assert loaded.due("1") is False
    assert json.loads(path.read_text(encoding="utf-8")) == {"accounts": []}


def _nodes(count: int) -> tuple[str, ...]:
    return tuple(f"http://p{n}" for n in range(count))


@pytest.fixture
def minted(monkeypatch):
    """Phone ids the poll mints, in order."""
    ids: list[str] = []

    def fake_buvid(seed: str = "") -> str:
        ids.append(seed)
        return f"XUFAKE{len(ids)}"

    monkeypatch.setattr("news.source.post.bilibili.app_buvid", fake_buvid)
    return ids


def _exit_lookup(table):
    async def lookup(proxy: str):
        return table[proxy]

    return lookup


def _asked(rig, mode: str):
    return [
        (account, proxy)
        for account, proxy, how in rig.fetcher.asks
        if how == mode
    ]


async def case_a_crash_during_the_ask_keeps_the_turn(monkeypatch, scene):
    rig = scene()
    monkeypatch.setattr(settings, "bili_proxies", _nodes(2))
    seen: list[tuple[str, str]] = []

    async def crash(platform, account, **_kwargs):
        seen.append((account, bili_app.proxy_key()))
        raise RuntimeError("crash")

    rig.fetcher.pull = crash
    with pytest.raises(RuntimeError, match="crash"):
        await rig.bili("1", "2", "3", "4")
    assert rig.bili_state.turn == 0
    assert seen[0][1] == "http://p0"

    async def pull(platform, account, **_kwargs):
        seen.append((account, bili_app.proxy_key()))
        rig.fetcher.fetches.append((platform, account))
        return PullResult(None, [])

    rig.fetcher.pull = pull
    await rig.bili("1", "2", "3", "4")
    assert seen[1][1] == "http://p0"
    assert rig.bili_state.turn == 1


async def test_phones_follow_the_window_and_the_exit(monkeypatch, scene, minted, news_store, caplog):
    with monkeypatch.context() as mp:
        await case_a_phone_clears_before_the_proxy_gap(scene, mp, minted)
    with monkeypatch.context() as mp:
        await case_the_saved_rotation_continues_after_a_restart(scene, news_store, mp)
    with monkeypatch.context() as mp:
        await case_a_moved_exit_mints_a_new_phone(scene, mp, caplog)


async def case_a_phone_clears_before_the_proxy_gap(scene, monkeypatch, minted):
    rig = scene()
    monkeypatch.setattr(settings, "bili_proxies", _nodes(2))
    held: list[str | None] = []
    during_gap: list[str | None] = []

    async def pull(platform, account, **_kwargs):
        held.append(rig.fetcher.bili_buvid)
        rig.fetcher.fetches.append((platform, account))
        return PullResult(None, [])

    rig.fetcher.pull = pull

    async def fake_sleep(_seconds: float) -> None:
        during_gap.append(rig.fetcher.bili_buvid)

    monkeypatch.setattr("news.rounds.asyncio.sleep", fake_sleep)
    await rig.bili("1", "2", "3", "4", gap=30.0)
    assert held == ["XUFAKE1", "XUFAKE2", "XUFAKE1", "XUFAKE2"]
    assert during_gap == [None, None]
    assert rig.fetcher.bili_buvid is None
























async def case_the_saved_rotation_continues_after_a_restart(scene, news_store, monkeypatch):
    rig = scene()
    monkeypatch.setattr(settings, "bili_proxies", _nodes(2))
    await rig.bili("1", "2", "3", "4")
    assert rig.fetcher.asks[0][1] == "http://p0"

    restarted = RoundRig(news_store)
    await restarted.bili("1", "2", "3", "4")
    assert restarted.fetcher.asks[0][1] == "http://p1"

    await restarted.bili("1", "2", "3", "4", "5")
    assert restarted.fetcher.asks[-5][1] == "http://p0"




async def test_a_412_or_a_failed_proxy_closes_the_login(monkeypatch, scene):
    with monkeypatch.context() as mp:
        await case_a_crash_during_the_ask_keeps_the_turn(mp, scene)
    await case_a_412_without_proxies_stops_the_round(scene)
    with monkeypatch.context() as mp:
        await case_a_412_stops_the_round_with_a_login_cookie_too(scene, mp)
    with monkeypatch.context() as mp:
        await case_a_failed_first_proxy_stays_closed_to_the_login(scene, mp)


async def case_a_412_without_proxies_stops_the_round(scene):
    rig = scene()
    roster = ("7", "9", "10", "11", "12")
    rig.script(pull_error=FetchError("HTTP 412 from api"), pull_error_for="10")
    await rig.bili(*roster)
    assert rig.poll.skip_until == {}
    assert rig.fetcher.fetches[-1] == ("bilibili", "10")
    assert {account for _platform, account in rig.fetcher.fetches} <= set(roster)
    assert bili_round.cap.sits_out("")

    rig.script()
    await rig.bili(*roster)
    assert rig.fetcher.fetches == []
    assert bili_round.cap.sits_out("") is False

    await rig.bili(*roster)
    assert sorted(account for _platform, account in rig.fetcher.fetches) == sorted(roster)


async def case_a_412_stops_the_round_with_a_login_cookie_too(scene, monkeypatch):
    rig = scene()
    monkeypatch.setattr(settings, "bili_cookie", "SESSDATA=s; bili_jct=j")
    monkeypatch.setattr(settings, "bili_ac_time", "tok")
    blocked = FetchError("HTTP 412 from api", status=412, body='{"code":-352}')
    rig.script(pull_error=blocked, pull_error_for="10")
    await rig.bili("2", "3", "1", "10")
    assert rig.fetcher.fetches[-1] == ("bilibili", "10")
    assert rig.poll.skip_until == {}






async def case_a_failed_first_proxy_stays_closed_to_the_login(scene, monkeypatch):
    rig = scene()
    monkeypatch.setattr(settings, "ip_check", True)
    monkeypatch.setattr(settings, "bili_cookie", "SESSDATA=s; bili_jct=j")
    monkeypatch.setattr(settings, "bili_proxies", _nodes(6))
    monkeypatch.setattr(
        "news.source.post.common.lookup_exit",
        _exit_lookup({"http://p0": None, "http://p1": "203.0.113.1", "http://p2": "203.0.113.2"}),
    )
    await rig.bili("1", "2", "3", "4", tickets=("1", "2", "3", "4"))
    assert rig.fetcher.asks == []
    assert backup_proxy() == "http://p0"
    assert rig.poll.held_tickets == {
        ("bilibili", "1"),
        ("bilibili", "2"),
        ("bilibili", "3"),
        ("bilibili", "4"),
    }




async def case_a_moved_exit_mints_a_new_phone(scene, monkeypatch, caplog):
    rig = scene()
    caplog.clear()
    caplog.set_level(logging.INFO, logger="news.source.post.common")
    monkeypatch.setattr(settings, "ip_check", True)
    monkeypatch.setattr(settings, "bili_proxies", _nodes(2))
    old = bili_app.app_buvid("old")
    kept = bili_app.app_buvid("kept")
    bili_app.buvid_store.get("http://p0", mint=lambda seed="": old)
    bili_app.buvid_store.get("http://p1", mint=lambda seed="": kept)
    common.exits.remember("bilibili", "http://p0", "203.0.113.1")
    monkeypatch.setattr(
        "news.source.post.common.lookup_exit",
        _exit_lookup({"http://p0": "203.0.113.9", "http://p1": "203.0.113.2"}),
    )
    await rig.bili("1", "2", "3", "4")
    assert _saved_phone("http://p0") not in {None, old}
    assert _saved_phone("http://p1") == kept
    assert any("exit changed; fresh phone ids" in record.getMessage() for record in caplog.records)




class _Probe(_Run):
    """A round whose asks are recorded, not made. `blank` accounts come back empty."""

    asked: list
    pauses: list
    blank: set
    stop: dict

    async def fetch(self, account, proxy, slot, phone, *, mode):
        self.asked.append((account, proxy, slot, phone, mode))
        if self.stop.get(account):
            self.poll.bili_stopped.add(proxy)
        empty = account in self.blank
        return dynamics.PollAsk(blank=empty, posts=not empty)

    async def pause(self, due):
        self.pauses.append(due)


def _probe(rig, *, blank=(), stop=None):
    run = _Probe(
        rig.poll, rig.bili_state, [], {}, datetime(2026, 10, 5, tzinfo=timezone.utc),
        None, set(), 0.0, 0.0, rig.rng,
    )
    run.asked, run.pauses, run.blank, run.stop = [], [], set(blank), stop or {}
    return run


async def case_the_first_try_line_is_logged_once(caplog, scene):
    """This poll logs one line. A nested poll with no accounts keeps that line."""
    caplog.set_level(logging.INFO, logger="news.source.post.bilibili.round")
    rig = scene()

    def lines():
        return [record.getMessage() for record in caplog.records if "Bilibili first try" in record.getMessage()]

    await rig.bili()
    assert lines() == []
    dynamics.space_stats.begin("manual", 0)
    dynamics.space_stats.first("manual", "posts")
    dynamics.space_stats.done("manual", "first")
    fetcher = rig.script()
    scripted = fetcher.pull
    nested = []

    async def pull(platform, account, **kwargs):
        dynamics.space_stats.begin(account, 0)
        dynamics.space_stats.first(account, "blank")
        nested.append(await bili_round.poll_bili(
            rig.poll, rig.bili_state, [], {}, datetime.now(timezone.utc), None,
            backfill_for=set(), gap=0, jitter=0, rng=rig.rng,
        ))
        dynamics.space_stats.done(account, "fallback")
        return await scripted(platform, account, **kwargs)

    fetcher.pull = pull
    await rig.bili("10")
    assert nested == [(0, False)]
    assert lines() == [
        "Bilibili first try: 0/1 got posts (slot 0 0/1); "
        "rescued by repull 0; failed 0; first misses: blank 1"
    ]
    rig.script()
    await rig.bili("10")
    assert len(lines()) == 1


async def test_the_passes_of_one_round(monkeypatch, scene, caplog):
    await case_the_first_try_line_is_logged_once(caplog, scene)
    await case_the_app_passes_ask_a_slot_at_a_time_and_note_a_blank(scene)
    await case_the_app_passes_hold_a_ticket_on_a_stopped_proxy_and_skip_one_not_due(scene)
    await case_a_planned_blank_reads_the_visitor_list_and_clears(scene)
    await case_a_resting_proxy_keeps_the_blank_plan(scene)
    with monkeypatch.context() as mp:
        await case_a_login_reads_only_the_first_proxy(scene, mp)
    await case_a_failed_exit_skips_the_login_and_holds_the_tickets(scene, monkeypatch)


async def case_the_app_passes_ask_a_slot_at_a_time_and_note_a_blank(scene):
    rig = scene()
    phones = [Phone("p0", ("a", "b"), 0), Phone("p1", ("c", "d"), 1)]
    run = _probe(rig, blank={"b", "c"})
    await bili_round._app_passes(run, phones)
    assert [(ask[0], ask[1], ask[2], ask[3], ask[4]) for ask in run.asked] == [
        ("a", "p0", 0, 0, "app"),
        ("c", "p1", 0, 1, "app"),
        ("b", "p0", 1, 0, "app"),
        ("d", "p1", 1, 1, "app"),
    ]
    assert run.pauses == [False, True, False, True]
    assert bili_round.blanks.due("b")
    assert bili_round.blanks.due("c")
    assert bili_round.blanks.due("a") is False


async def case_the_app_passes_hold_a_ticket_on_a_stopped_proxy_and_skip_one_not_due(scene):
    rig = scene()
    phones = [Phone("p0", ("a", "b"), 0), Phone("p1", ("c", "d"), 1)]
    rig.poll.skip_until[("bilibili", "d")] = 1e18
    run = _probe(rig, blank={"a"}, stop={"a": True})
    run.backfill_for = {"b", "d"}
    await bili_round._app_passes(run, phones)
    assert [ask[0] for ask in run.asked] == ["a", "c"]
    assert bili_round.blanks.due("a") is False
    assert rig.poll.held_tickets == {("bilibili", "b")}


async def case_a_planned_blank_reads_the_visitor_list_and_clears(scene):
    rig = scene()
    bili_round.blanks.note("a")
    bili_round.blanks.note("c")
    phones = [Phone("p0", ("a", "b"), 0), Phone("p1", ("c",), 1)]
    run = _probe(rig, blank={"a"})
    await bili_round._app_passes(run, phones)
    assert [(ask[0], ask[4]) for ask in run.asked] == [
        ("a", "visitor"), ("c", "visitor"), ("b", "app"),
    ]
    assert bili_round.blanks.due("a") is False
    assert bili_round.blanks.due("c") is False
    assert bili_round.blanks.due("b") is False


async def case_a_resting_proxy_keeps_the_blank_plan(scene):
    rig = scene()
    bili_round.blanks.note("a")
    rig.poll.bili_stopped.add("p0")
    phones = [Phone("p0", ("a",), 0), Phone("p1", ("b",), 1)]
    run = _probe(rig)
    run.backfill_for = {"a"}
    await bili_round._app_passes(run, phones)
    assert [ask[0] for ask in run.asked] == ["b"]
    assert bili_round.blanks.due("a")
    assert rig.poll.held_tickets == {("bilibili", "a")}


async def case_a_login_reads_only_the_first_proxy(scene, monkeypatch):
    rig = scene()
    monkeypatch.setattr(settings, "bili_cookie", "SESSDATA=s; bili_jct=j")
    monkeypatch.setattr(settings, "bili_proxies", ("http://fixed", "http://other"))
    phones = [Phone("http://other", ("a", "b"), 0)]
    run = _probe(rig, blank={"a"})
    await bili_round._login_passes(run, phones)
    assert [(ask[0], ask[1], ask[4]) for ask in run.asked] == [
        ("a", "http://fixed", "login"),
        ("b", "http://fixed", "login"),
    ]
    assert bili_round.blanks.due("a") is False


async def case_a_failed_exit_skips_the_login_and_holds_the_tickets(scene, monkeypatch):
    rig = scene()
    monkeypatch.setattr(settings, "bili_proxies", ("http://fixed",))
    phones = [Phone("http://fixed", ("a", "b"), 0)]
    run = _probe(rig)
    run.backfill_for = {"a", "b"}
    with bili_round.failed_exits(("http://fixed",)):
        await bili_round._login_passes(run, phones)
    assert run.asked == []
    assert rig.poll.held_tickets == {("bilibili", "a"), ("bilibili", "b")}
