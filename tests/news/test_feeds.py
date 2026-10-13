from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fetch import BookmarkStore, FetchError
from news.feeds import (
    Fetcher,
    PullResult,
    _split_items,
    catch_up_items,
    deliver,
    pull_locked,
)
from news.follows import (
    FEATURE_GAME,
    FEATURE_TICKET,
)
from news.source.post.bilibili.app import _DYN_EPOCH, grpc_frame, grpc_message
from news.source.post.bilibili.browser import BILI_NAV_URL, clear_wbi_key
import news.source.post.bilibili.dynamics as dynamics
from news.source.post.bilibili.dynamics import QUIET
from news.source.post.bilibili.protobuf import decode_message, encode_message
from news.source.post.bilibili.web import BILI_CARD_URL, BILI_OPUS_URL, BILI_SPI_URL
from news.source.post.weibo.mblog import WEIBO_INDEX_URL
from news.source.registry import FLOOD_CAP
from tests.helpers import (
    FakeHttp,
    ScriptedFetcher,
    fetched_item,
    scratch,
    utc,
)
from tests.news.support import reset_saved

WEIBO = "1195230310"
BILI = "99"
PATCH = ("Patch notes", "https://example.test/patch", "guid-1", "Hello world")
OLDER = ("Older post", "https://example.test/old", "guid-0", "Yesterday")
LOTTO = ("Lottery", "https://example.test/lotto", "guid-2", "官方抽奖 关注并转发")
_NAV = {
    "code": 0,
    "data": {
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/0123456789abcdef0123456789abcdef.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/fedcba9876543210fedcba9876543210.png",
        }
    },
}


@pytest.fixture(autouse=True)
def _reset_wbi():
    clear_wbi_key()
    yield
    clear_wbi_key()


def _dyn_id(when: datetime) -> str:
    """A DynSpace id whose snowflake time is `when`."""
    return str((int(when.timestamp()) - _DYN_EPOCH) << 32)


def _paragraph(text: str) -> dict:
    return {3: {1: [{2: text}]}}


def _space_item(
    item_id: str,
    text: str,
    *,
    name: str = "Studio",
    title: str = "",
):
    heading = title or text.split("\n", 1)[0]
    return {
        3: [
            {2: {3: {2: name}}},
            {30: {1: _paragraph(heading), 2: _paragraph(text)}},
        ],
        4: {1: str(item_id)},
    }


def _space(*rows, name: str = "Studio"):
    items = []
    for row in rows:
        title, _link, guid, text, *_extra = row
        items.append(_space_item(guid, text, name=name, title=title))
    return items


def _dyn_frame(cards: list) -> bytes:
    return grpc_frame(encode_message("DynSpaceRsp", {1: cards} if cards else {}))


def _mint_reply(url: str) -> dict | None:
    """SPI buvid3 for an opus fallback."""
    if url == BILI_SPI_URL:
        return {"code": 0, "data": {"b_3": "SLOTinfoc", "b_4": "B4"}}
    return None


def _bili_post(payloads: dict[str, list]):
    async def post(body, _headers):
        req = decode_message("DynSpaceReq", grpc_message(body))
        uid = str(req.get(1))
        if uid not in payloads:
            raise AssertionError(uid)
        return _dyn_frame(payloads[uid])

    return post


def _mblog_card(item_id: str, text: str, *, name: str = "Studio") -> dict:
    return {
        "card_type": 9,
        "mblog": {
            "id": item_id,
            "text": text,
            "user": {"screen_name": name},
        },
    }


def _index(*rows, name: str = "Studio"):
    return {
        "ok": 1,
        "data": {
            "userInfo": {"screen_name": name},
            "cards": [
                _mblog_card(guid, title or text, name=name)
                for title, _link, guid, text in rows
            ],
        },
    }


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


async def test_pull_names_seeds_and_delivery(monkeypatch, tmp_path):
    case_first_pull_backfill_posts_the_last_week()
    await case_index_uses_the_author_name(scratch(tmp_path, "author"))
    reset_saved()
    await case_ticket_follow_gets_the_last_week_beside_a_game_follow(
        scratch(tmp_path, "ticket_week")
    )
    reset_saved()
    await case_pull_seeds_names_skips_and_dupes(scratch(tmp_path, "seeds"))
    reset_saved()
    with monkeypatch.context() as patched:
        await case_pull_faults_catch_up_and_flood(scratch(tmp_path, "faults"), patched)
    await case_ticket_backlog_stops_at_the_flood_cap(scratch(tmp_path, "backlog"))


async def test_the_catch_up_line_is_one_bookmark(tmp_path):
    await case_a_blank_app_does_not_read_the_visitor_list(scratch(tmp_path, "blank"))
    reset_saved()
    await case_the_catch_up_line_leaves_that_page_as_history(scratch(tmp_path, "line"))
    reset_saved()
    await case_unfollowing_drops_the_line_so_a_readd_starts_clean(scratch(tmp_path, "readd"))


async def case_index_uses_the_author_name(tmp_path):
    weibo = _fetcher(
        tmp_path,
        {},
        weibo_call=_weibo_call({WEIBO: _index(PATCH, OLDER, name="Studio")}),
    )
    bili = _fetcher(
        tmp_path,
        {},
        bili_post=_bili_post({BILI: _space(PATCH, OLDER, name="Studio")}),
    )
    for fetcher, platform, account in (
        (weibo, "wb", WEIBO),
        (bili, "bili", BILI),
    ):
        items = (await fetcher.load(platform, account)).items
        assert items[0].source_name == "Studio"
        assert items[0].account == account
        assert items[0].item_id == "guid-1"
        assert items[0].title == "Patch notes"


def case_first_pull_backfill_posts_the_last_week():
    now = utc(2026, 9, 25, 12)
    recent = fetched_item("recent", occurred_at=utc(2026, 9, 22, 12))
    ahead = fetched_item("ahead", occurred_at=utc(2026, 9, 25, 13))
    old = fetched_item("old", occurred_at=utc(2026, 9, 1, 12))
    undated = fetched_item("undated")
    new, _visible, _skip, seed = _split_items(
        [recent, ahead, old, undated],
        now,
        None,
        backfill=timedelta(days=7),
    )
    assert [item.item_id for item in new] == ["recent", "ahead"]
    assert seed == ["old", "undated"]
    again, _, _, _ = _split_items(
        [recent, old],
        now,
        {"old", "undated"},
    )
    assert [item.item_id for item in again] == ["recent"]


async def case_ticket_follow_gets_the_last_week_beside_a_game_follow(tmp_path):
    now = utc(2026, 9, 25, 12)
    recent_at = now - timedelta(days=2)
    old_at = now - timedelta(days=30)
    recent_id = _dyn_id(recent_at)
    old_id = _dyn_id(old_at)
    fetcher = _fetcher(
        tmp_path,
        {},
        bili_post=_bili_post(
            {
                BILI: _space(
                    ("On sale", "https://example.test/sale", recent_id, "Tickets"),
                    ("Ancient", "https://example.test/old", old_id, "Long ago"),
                )
            }
        ),
        now=now,
    )
    got = await fetcher.pull("bilibili", BILI)
    assert _ids(got.new) == []
    assert fetcher.bookmarks.is_seeded("bilibili", BILI)

    sent: list[tuple[list[tuple[int, str]], str]] = []

    async def missed(_platform, _account, live, item):
        sent.append((list(live), item.item_id))
        return 0

    game = [(1, FEATURE_GAME)]
    live = [(1, FEATURE_GAME), (2, FEATURE_TICKET)]
    assert await deliver(fetcher, "bilibili", BILI, game, got, missed) == 0
    assert sent == []
    assert await deliver(fetcher, "bilibili", BILI, live, got, missed) == 0
    assert sent == [([(2, FEATURE_TICKET)], recent_id)]

    async def posted(_platform, _account, live, item):
        sent.append((list(live), item.item_id))
        fetcher.mark_delivered(
            "bilibili", BILI, item.item_id, [f"{guild_id}:{topic}" for guild_id, topic in live]
        )
        return 1

    assert await deliver(fetcher, "bilibili", BILI, live, got, posted) == 1
    assert fetcher.delivered_refs("bilibili", BILI, recent_id) == {"2:ticket"}
    again = await fetcher.pull("bilibili", BILI)
    assert recent_id in _ids(again.visible)
    assert catch_up_items(fetcher, "bilibili", BILI, live, again, set(), 5, now=now) == []






async def case_ticket_backlog_stops_at_the_flood_cap(tmp_path):
    now = utc(2026, 9, 25, 12)
    items = [
        fetched_item(f"guid-{n}", occurred_at=now - timedelta(hours=n))
        for n in range(FLOOD_CAP + 1)
    ]
    got = PullResult(None, [], items=items)
    seen: list[str] = []

    async def announce(_platform, _account, _live, item):
        seen.append(item.item_id)
        return 1

    posted = await deliver(
        _fetcher(tmp_path, {}, now=now), "bilibili", BILI, [(2, FEATURE_TICKET)], got, announce
    )
    assert posted == FLOOD_CAP
    assert seen == [f"guid-{n}" for n in range(FLOOD_CAP, 0, -1)]


async def case_pull_seeds_names_skips_and_dupes(tmp_path):
    named = _fetcher(
        tmp_path,
        {},
        bili_post=_bili_post(
            {
                "1": _space(PATCH, OLDER, name="Studio"),
                "2": _space(PATCH, name="Other"),
            }
        ),
    )
    assert (await named.pull("bilibili", "1")).name == "Studio"
    assert (await named.pull("bilibili", "2")).name == "Other"

    weibo = _fetcher(tmp_path, {}, weibo_call=_weibo_call({WEIBO: {"ok": 1, "data": {"cards": []}}}))
    assert await _new(weibo, "weibo", WEIBO) == []
    assert not weibo.bookmarks.is_seeded("weibo", WEIBO)
    weibo = _fetcher(
        tmp_path,
        {},
        bookmarks=weibo.bookmarks,
        weibo_call=_weibo_call({WEIBO: _index(PATCH, OLDER)}),
    )
    assert await _new(weibo, "weibo", WEIBO) == []
    assert weibo.bookmarks.is_seeded("weibo", WEIBO)

    payloads = {BILI: _space(LOTTO, PATCH, OLDER)}
    bookmarks = BookmarkStore(tmp_path / "seen.json")
    bookmarks.remember("bilibili", BILI, ["guid-0"])
    skipper = _fetcher(tmp_path, {}, bookmarks=bookmarks, bili_post=_bili_post(payloads))
    items = await _new(skipper, "bilibili", BILI)
    assert _ids(items) == ["guid-1"]
    assert "guid-2" not in bookmarks.seen_ids("bilibili", BILI)
    skipper.mark_seen("bilibili", BILI, ["guid-1"])
    payloads[BILI] = _space(
        ("Patch notes", LOTTO[1], "guid-2", "Hello world"), PATCH, OLDER
    )
    later = await _new(skipper, "bilibili", BILI)
    assert _ids(later) == ["guid-2"]
    assert later[0].title == "Patch notes"


def _lists_call(state: dict):
    """Opus list and its nav/card calls read from `state` on every call."""

    async def call(_method, url, **_kwargs):
        if (minted := _mint_reply(url)) is not None:
            return minted
        if url == BILI_NAV_URL:
            return _NAV
        if url == BILI_OPUS_URL:
            return {"code": 0, "data": {"items": state["opus"]}}
        if url == BILI_CARD_URL:
            return {"code": 0, "data": {"card": {}}}
        raise AssertionError(url)

    return call


def _lists_post(state: dict):
    async def post(_body, _headers):
        return _dyn_frame(state["space"])

    return post




async def case_a_blank_app_does_not_read_the_visitor_list(tmp_path):
    body = "分享专属邀请码，邀约同服回归漂泊者参与。需联觉等级达到8级。心狐灵签可兑换星声480、贝币300000。"
    state: dict = {
        "space": [],
        "opus": [{"opus_id": "1002", "content": body}],
    }
    asked: list[str] = []
    inner = _lists_call(state)

    async def call(method, url, **kwargs):
        asked.append(url)
        return await inner(method, url, **kwargs)

    fetcher = _fetcher(tmp_path, {}, bili_call=call, bili_post=_lists_post(state))
    assert _ids((await fetcher.pull("bilibili", BILI)).new) == []
    assert BILI_OPUS_URL not in asked
    assert not fetcher.bookmarks.is_seeded("bilibili", BILI)


async def case_the_catch_up_line_leaves_that_page_as_history(tmp_path):
    when = utc(2026, 10, 4, 12)
    older = _dyn_id(utc(2026, 9, 1, 12))
    recent = _dyn_id(utc(2026, 10, 2, 12))
    newer = _dyn_id(utc(2026, 10, 4, 13))
    state: dict = {"space": [], "opus": []}
    fetcher = _fetcher(
        tmp_path,
        {},
        bili_call=_lists_call(state),
        bili_post=_lists_post(state),
        now=when,
    )
    state["opus"] = [
        {"opus_id": "1001", "content": "Hello"},
        {"opus_id": "1000", "content": "Older"},
    ]
    token = dynamics._mode.set("visitor")
    try:
        opened = await fetcher.pull("bilibili", BILI)
    finally:
        dynamics._mode.reset(token)
    assert _ids(opened.new) == []
    assert {QUIET} <= {tag for item in opened.items for tag in item.tags}
    assert fetcher.bookmarks.catch_line("bilibili", BILI) == (1001, when)
    assert fetcher.bookmarks.seen_ids("bilibili", BILI) == {"1001", "1000"}

    state["space"] = [
        _space_item(newer, "New clip"),
        _space_item("1002", "Title"),
        _space_item(recent, "Last clip"),
        _space_item(older, "Old clip"),
        _space_item("999", "Old text"),
    ]
    state["opus"] = []
    posted = await fetcher.pull("bilibili", BILI)
    assert _ids(posted.new) == [newer, "1002"]
    assert {recent, older, "999"} <= fetcher.bookmarks.seen_ids("bilibili", BILI)
    assert newer not in fetcher.bookmarks.seen_ids("bilibili", BILI)
    assert fetcher.bookmarks.catch_line("bilibili", BILI) == (1001, when)

    sent: list[str] = []

    async def announce(_platform, _account, _live, item):
        sent.append(item.item_id)
        return 1

    await deliver(fetcher, "bilibili", BILI, [(2, FEATURE_TICKET)], posted, announce)
    assert recent not in sent

    state["space"] = []
    state["opus"] = [{"opus_id": "1003", "content": "Later title"}]
    token = dynamics._mode.set("visitor")
    try:
        quiet = await fetcher.pull("bilibili", BILI)
    finally:
        dynamics._mode.reset(token)
    assert _ids(quiet.new) == []
    assert "1003" not in fetcher.bookmarks.seen_ids("bilibili", BILI)

    state["space"] = [_space_item("1003", "Later title")]
    state["opus"] = []
    assert _ids((await fetcher.pull("bilibili", BILI)).new) == ["1003"]


async def case_unfollowing_drops_the_line_so_a_readd_starts_clean(tmp_path):
    when = utc(2026, 10, 4, 12)
    state: dict = {"space": [_space_item("200", "Hello"), _space_item("150", "Older")], "opus": []}
    fetcher = _fetcher(
        tmp_path, {}, bili_call=_lists_call(state), bili_post=_lists_post(state), now=when,
    )
    fetcher.bookmarks.remember("bilibili", "77", ["5"])
    fetcher.bookmarks.remember_line("bilibili", "77", "5", when)
    first = await fetcher.pull("bilibili", BILI)
    assert _ids(first.new) == []
    assert fetcher.bookmarks.catch_line("bilibili", BILI) == (200, when)

    assert fetcher.prune_unused([("bilibili", "77")]) == 1
    assert fetcher.bookmarks.catch_line("bilibili", BILI) == (None, None)
    assert fetcher.bookmarks.is_seeded("bilibili", BILI) is False
    assert fetcher.bookmarks.catch_line("bilibili", "77") == (5, when)

    again = await fetcher.pull("bilibili", BILI)
    assert _ids(again.new) == []
    assert fetcher.bookmarks.seen_ids("bilibili", BILI) == {"200", "150"}
    assert fetcher.bookmarks.catch_line("bilibili", BILI) == (200, when)

    state["space"] = [_space_item("201", "New"), _space_item("200", "Hello")]
    posted = await fetcher.pull("bilibili", BILI)
    assert _ids(posted.new) == ["201"]


def test_catch_line_stays_on_the_bookmark(tmp_path):
    path = tmp_path / "old.json"
    when = "2026-10-04T12:00:00+00:00"
    later = utc(2026, 10, 5, 12)
    earlier = utc(2026, 10, 1, 12)
    old = {
        "bilibili:1": {
            "delivered": {"1001": ["1:game"]},
            "ids": ["1001"],
            "aliases": {"av10": "1001"},
            "floor": "1001",
            "since": when,
        }
    }
    path.write_text(json.dumps(old))
    store = BookmarkStore(path)
    assert store.seen_ids("bilibili", "1") == {"1001"}
    assert store.catch_line("bilibili", "1")[0] == 1001
    saved = json.loads(path.read_text())["bilibili:1"]
    assert "aliases" not in saved
    assert saved["floor"] == "1001"

    store.remember_line("bilibili", "1", "1002", later)
    assert store.catch_line("bilibili", "1")[0] == 1001
    store.remember("bilibili", "1", ["1002"])
    assert "1001" in store.seen_ids("bilibili", "1")
    assert store.delivered_refs("bilibili", "1", "1001") == {"1:game"}

    store.remember("bilibili", "9", ["3"])
    store.remember_line("bilibili", "9", "3", later)
    store.move("bilibili", "1", "9")
    assert store.catch_line("bilibili", "1") == (None, None)
    assert store.catch_line("bilibili", "9")[0] == 3

    store.remember("bilibili", "8", ["1"])
    store.remember_line("bilibili", "8", "50", earlier)
    store.remember("bilibili", "7", ["2"])
    store.remember_line("bilibili", "7", "80", later)
    store.move("bilibili", "7", "8")
    assert store.catch_line("bilibili", "8")[0] == 50

    assert store.keep_only([("bilibili", "8")]) == 1
    assert store.catch_line("bilibili", "9") == (None, None)
    assert BookmarkStore(path).catch_line("bilibili", "8")[0] == 50


async def case_pull_faults_catch_up_and_flood(tmp_path, monkeypatch):
    await case_unknown_source_raises(tmp_path)
    await case_bilibili_space_error_raises(tmp_path)
    case_catch_up_posts_only_when_a_guild_missed_a_delivered_item()
    case_stored_shows_lists_a_delivered_ticket(tmp_path)
    await case_deliver_stops_at_the_flood_cap()
    await case_pull_locked_retries_after_a_pause(monkeypatch)

async def case_unknown_source_raises(tmp_path):
    fetcher = _fetcher(tmp_path, {})
    with pytest.raises(FetchError, match="not supported"):
        await fetcher.load("steam", "123")


async def case_bilibili_space_error_raises(tmp_path):
    async def boom(*_args, **_kwargs):
        raise FetchError("Bilibili dynamics failed (-352)")

    fetcher = _fetcher(tmp_path, {}, bili_post=boom)
    with pytest.raises(FetchError, match="-352"):
        await fetcher.load("bilibili", BILI)


def case_catch_up_posts_only_when_a_guild_missed_a_delivered_item():
    item = fetched_item("guid-1")
    got = PullResult(None, [], [item], items=[item])
    empty = ScriptedFetcher()
    assert (
        catch_up_items(empty, "weibo", WEIBO, [(1, "game")], got, set(), 5) == []
    )
    seen = ScriptedFetcher()
    seen.mark_delivered("weibo", WEIBO, "guid-1", ["1:game"])
    found = catch_up_items(
        seen, "weibo", WEIBO, [(1, "game"), (2, "game")], got, set(), 5
    )
    assert _ids(found) == ["guid-1"]


def case_stored_shows_lists_a_delivered_ticket(tmp_path):
    fetcher = Fetcher(bookmarks_path=tmp_path / "bookmarks.json")
    fetcher.mark_delivered("weibo", "1195230310", "post-9", ["7:ticket"])
    assert fetcher.stored_shows("weibo", "1195230310", "7:ticket") == {"post-9"}
    assert fetcher.stored_shows("weibo", "1195230310", "8:ticket") == set()


async def case_pull_locked_retries_after_a_pause(monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("news.feeds.asyncio.sleep", sleep)
    calls = 0

    class Once:
        async def pull(self, platform, account, *, backfill=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise FetchError("again")
            return PullResult(platform, [], team_id=account)

    got = await pull_locked(
        Once(), "bilibili", "1", asyncio.Lock(), tries=2, pause=True, wait=2
    )
    assert waits == [2, 2]
    assert got.team_id == "1"


async def case_deliver_stops_at_the_flood_cap():
    items = [fetched_item(f"guid-{n}") for n in range(6)]
    got = PullResult(None, items)
    seen = []

    async def announce(platform, account, live, item):
        seen.append(item.item_id)
        return 1

    posted = await deliver(
        ScriptedFetcher(), "weibo", WEIBO, [(1, "game")], got, announce
    )
    assert posted == 5
    assert seen == [f"guid-{n}" for n in range(5, 0, -1)]

