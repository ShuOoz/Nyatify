from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone

import pytest

import news.source.post.bilibili.dynamics as dynamics
import news.source.post.bilibili.round as bili_round
from config import settings as live_settings
from fetch import FetchError, format_fetched_item
from news.follows import FEATURE_GAME, FEATURE_OTHER
from news.source.post.bilibili.app import (
    bind_proxy,
    current_proxy,
    grpc_frame,
    grpc_message,
    reset_proxy,
)
from news.source.post.bilibili.browser import (
    BILI_NAV_URL,
    clear_wbi_key,
    space_xhr_headers,
)
from news.source.post.bilibili import shows_post
from news.source.post.bilibili.dynamics import (
    LIST,
    QUIET,
    WATCH,
    SpaceStats,
    fetch_dynamics,
    for_model,
    is_paragraph,
    note_list_post,
    space_stats,
)
from news.source.post.bilibili.protobuf import decode_message, encode_message
from news.source.post.bilibili.web import (
    BILI_CARD_URL,
    BILI_OPUS_URL,
    BILI_SPACE_URL,
    BILI_SPI_URL,
    opus_item,
)
from tests.helpers import fetched_item
from tests.news.support import reset_saved

UID = "1195230310"
IMG = "0123456789abcdef0123456789abcdef"
SUB = "fedcba9876543210fedcba9876543210"


@pytest.fixture(autouse=True)
def _reset_wbi():
    clear_wbi_key()
    yield
    clear_wbi_key()

SPI_BUVID = "SPI3infoc"


def _spi(buvid3: str = SPI_BUVID) -> dict:
    return {"code": 0, "data": {"b_3": buvid3, "b_4": "ignored"}}


def _nav(img: str = IMG, sub: str = SUB) -> dict:
    return {
        "code": 0,
        "data": {
            "wbi_img": {
                "img_url": f"https://i0.hdslb.com/bfs/wbi/{img}.png",
                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{sub}.png",
            }
        },
    }


def _author(name: str = "Studio", ts: int = 1_700_000_000) -> dict:
    return {
        "module_author": {
            "name": name,
            "pub_ts": ts,
            "face": "//i0.hdslb.com/bfs/face/s.jpg",
        }
    }


def _row(
    item_id: str,
    *,
    kind: str = "DYNAMIC_TYPE_WORD",
    desc: str = "",
    major: dict | None = None,
    orig: dict | None = None,
    name: str = "Studio",
) -> dict:
    dynamic: dict = {}
    if desc:
        dynamic["desc"] = {"text": desc}
    if major is not None:
        dynamic["major"] = major
    row: dict = {
        "id_str": item_id,
        "type": kind,
        "modules": {**_author(name), "module_dynamic": dynamic},
    }
    if orig is not None:
        row["orig"] = orig
    return row


def _app_word(item_id: str, text: str, *, name: str = "Studio") -> dict:
    return {
        3: [
            {2: {3: {2: name}}},
            {4: {3: text}},
        ],
        4: {1: item_id},
    }


def _app_frame(cards: list[dict], *, offset: str = "", has_more: bool = False) -> bytes:
    body: dict = {1: cards} if cards else {}
    if offset:
        body[2] = offset
    if has_more:
        body[3] = 1
    return grpc_frame(encode_message("DynSpaceRsp", body))


def case_for_model_drops_list_and_watch_marks():
    item = fetched_item(tags=(LIST, f"{WATCH}2026-10-6-19:30", "keep"))
    shown = format_fetched_item(for_model(item))
    assert "keep" in shown
    assert LIST not in shown and WATCH not in shown
    plain = fetched_item(tags=("keep",))
    assert for_model(plain) is plain


def case_note_list_post_uses_the_usual_bookmark():
    item = fetched_item("1002", tags=(LIST,))
    audience = [(1, FEATURE_GAME), (2, FEATURE_OTHER)]
    game_ref = f"1:{FEATURE_GAME}"
    assert note_list_post(item, UID, audience, []) is False
    assert note_list_post(item, UID, audience, [game_ref]) is False
    plain = fetched_item(tags=("keep",))
    assert note_list_post(plain, UID, audience, [game_ref]) is False


async def test_bilibili_dynamics(monkeypatch):
    case_for_model_drops_list_and_watch_marks()
    case_note_list_post_uses_the_usual_bookmark()
    reset_saved()
    case_reserve_notice_reads_the_show_day()
    case_the_skip_rules()
    clear_wbi_key()
    await case_a_blank_app_is_an_empty_result(monkeypatch)
    clear_wbi_key()
    await case_a_dropped_app_raises(monkeypatch)
    clear_wbi_key()
    await case_the_app_page_returns_the_ids_it_got(monkeypatch)
    clear_wbi_key()
    await case_the_visitor_list_tags_a_paragraph_and_a_short_row(monkeypatch)
    clear_wbi_key()
    await case_the_visitor_list_fills_a_missing_author(monkeypatch)
    clear_wbi_key()
    await case_the_visitor_list_stays_on_its_proxy(monkeypatch)
    clear_wbi_key()
    await case_a_mint_retries_one_ordinary_failure(monkeypatch)
    clear_wbi_key()
    await case_the_login_page_returns_every_row(monkeypatch)
    clear_wbi_key()
    await case_a_failed_login_page_is_empty(monkeypatch)
    clear_wbi_key()
    await case_a_blocked_login_rests_the_first_proxy(monkeypatch)
    clear_wbi_key()
    await case_a_dead_login_raises(monkeypatch)
    clear_wbi_key()
    await case_a_failed_exit_skips_the_login(monkeypatch)


async def test_first_try_and_blank_asks(monkeypatch, caplog):
    with monkeypatch.context() as mp:
        await case_first_try_stats(mp, caplog)


async def case_first_try_stats(monkeypatch, caplog):
    caplog.clear()
    clear_wbi_key()
    await case_fetch_dynamics_records_the_first_reply_and_the_rescue(monkeypatch)
    case_space_stats_line_counts_slots_and_resets()
    case_space_stats_line_names_each_account(caplog)


async def case_fetch_dynamics_records_the_first_reply_and_the_rescue(monkeypatch):
    def feed(kind: str):
        async def post(_body, _headers):
            if kind in ("blank", "empty"):
                return _app_frame([])
            return _app_frame([_app_word("11", "hello")])

        async def call(_method, url, **_kwargs):
            if url == BILI_SPI_URL:
                return _spi()
            if url == BILI_NAV_URL:
                return _nav()
            if url == BILI_OPUS_URL and kind == "empty":
                return _BLANK
            if url == BILI_OPUS_URL:
                return {"code": 0, "data": {"items": [_opus_row("150", "Photo")]}}
            if url == BILI_CARD_URL:
                return {"code": 0, "data": {"card": {}}}
            raise AssertionError(url)

        return post, call

    with space_stats.round() as stats:
        post, call = feed("posts")
        await fetch_dynamics("1", post=post, call=call)
        post, call = feed("blank")
        await fetch_dynamics("2", post=post, call=call)
        post, call = feed("empty")
        assert await fetch_dynamics("3", post=post, call=call, seen={"100"}) == (None, [])
        space_stats.repull("4")
        post, call = feed("blank")
        await fetch_dynamics("4", post=post, call=call)
    assert stats.flush() == (
        "Bilibili first try: 1/4 got posts (slot 0 1/4); "
        "rescued by repull 1; failed 0; "
        "first misses: blank 3"
    )
    assert stats.flush() is None
    with space_stats.round() as empty:
        pass
    assert empty.flush() is None


def case_space_stats_line_counts_slots_and_resets():
    stats = SpaceStats()
    for account, slot in (("a", 0), ("b", 0), ("c", 1), ("d", 2), ("e", 2)):
        stats.begin(account, slot)
    stats.first("a", "posts")
    stats.done("a", "first")
    stats.first("b", "blank")
    stats.done("b", "fallback")
    stats.first("c", "posts")
    stats.done("c", "first")
    stats.first("d", "blank")
    stats.repull("d")
    stats.first("d", "posts")
    stats.done("d", "first")
    stats.first("e", "http 412")
    stats.done("e", "failed")
    assert stats.flush() == (
        "Bilibili first try: 2/5 got posts (slot 0 1/2, slot 1 1/1, slot 2 0/2); "
        "rescued by repull 1; failed 1; "
        "first misses: blank 2, http 412 1"
    )
    assert stats.flush() is None

    stats.begin("timeout", 0)
    stats.begin("gone", 1)
    stats.done("gone", "failed")
    assert stats.flush() == (
        "Bilibili first try: 0/2 got posts (slot 0 0/1, slot 1 0/1); "
        "rescued by repull 0; failed 2; first misses: error 2"
    )


def case_space_stats_line_names_each_account(caplog):

    caplog.set_level(logging.INFO, logger="news.source.post.bilibili.dynamics")
    stats = SpaceStats()
    secret = bind_proxy("http://user:secret@127.0.0.1:7911")
    try:
        stats.begin("3546", 0)
    finally:
        reset_proxy(secret)
    stats.first("3546", "posts")
    stats.done("3546", "first")
    home = bind_proxy("http://127.0.0.1:7920")
    try:
        stats.begin("88", 1)
    finally:
        reset_proxy(home)
    stats.first("88", "blank")
    stats.done("88", "fallback")
    direct = bind_proxy("")
    try:
        stats.begin("9", 0)
    finally:
        reset_proxy(direct)
    stats.done("9", "failed")
    assert stats.flush() == (
        "Bilibili first try: 1/3 got posts (slot 0 1/2, slot 1 0/1); "
        "rescued by repull 0; failed 1; "
        "first misses: blank 1, error 1"
    )
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Bilibili per account:")
    ]
    assert lines == [
        "Bilibili per account: 3546 127.0.0.1:7911 first=posts via=first",
        "Bilibili per account: 88 127.0.0.1:7920 first=blank via=fallback",
        "Bilibili per account: 9 direct first=error via=failed",
    ]
    assert "secret" not in " ".join(lines)
    assert stats.flush() is None


def case_reserve_notice_reads_the_show_day():
    posted = datetime(2026, 9, 30, 3, tzinfo=timezone.utc)
    same = dynamics.reserve_notice("节目 10月6日 19:30", "10-06 19:30 直播", posted)
    assert same == ("节目 10月6日 19:30", "watch:2026-10-6-19:30")
    dashed = dynamics.reserve_notice("节目 10-06 19:30", "", posted)
    assert dashed == ("节目 10-06 19:30 10月6日 19:30", "watch:2026-10-6-19:30")
    counted = dynamics.reserve_notice("直播预约", "10.6万人预约", posted)
    assert counted == ("直播预约", "")
    december = datetime(2026, 12, 20, 3, tzinfo=timezone.utc)
    new_year = dynamics.reserve_notice("跨年", "1月2日 19:00", december)
    assert new_year == ("跨年 1月2日 19:00", "watch:2027-1-2-19:00")
    named = dynamics.reserve_notice("跨年", "2026年1月2日 19:00", december)
    assert named[1] == "watch:2026-1-2-19:00"
    assert dynamics.watch_quote((named[1],)) == "2026年1月2日 19:00"
    assert dynamics.watch_quote(("watch:2026-10-6-19:30",)) == "2026年10月6日 19:30"
    assert dynamics.watch_quote(()) == ""


def _post(text: str, *, title: str = "", **kwargs):
    post = dynamics.post(item_id="1", account=UID, title=title, text=text, **kwargs)
    assert post is not None
    return post


def case_the_skip_rules():
    assert dynamics.should_skip(_post("wow\n转发了Other的动态\nOld clip")) is True
    assert dynamics.should_skip(_post("hello")) is None
    assert dynamics.should_skip(_post("unlocked body", charge=True)) is True
    assert dynamics.should_skip(replace(_post("hello"), platform="weibo")) is None

    live = _post("https://live.bilibili.com/99", title="正在直播")
    assert dynamics.should_skip(live) is None
    assert dynamics.is_live_room(live) is True
    assert dynamics.is_live_room(_post("hello")) is False

    for lock in (
        "充电专属动态，加入即可解锁观看",
        "为TA充电专属动态\n加入当前UP主的6元档包月充电即可解锁观看",
        "加入当前UP主的「6元档包月充电」即可解锁观看",
    ):
        assert dynamics.should_skip(_post(lock)) is True
    for talk in (
        "感谢大家的充电",
        "感谢充电",
        "今天充电专属直播见",
        "充电专属「今晚八点开播」",
        "充电专属 6元充电",
    ):
        assert dynamics.should_skip(_post(talk)) is None


_BLANK = {"code": 0, "message": "0", "data": {"items": []}}



_PARAGRAPH = "分享专属邀请码，邀约同服回归漂泊者参与。需联觉等级达到8级。心狐灵签可兑换星声480、贝币300000。"
_LOGIN = "SESSDATA=saved; bili_jct=jct; DedeUserID=99"


def _clear_cap() -> None:
    bili_round.cap._out = set()
    bili_round.cap._save()


def _opus_row(
    opus_id: str,
    content: str,
    *,
    name: str | None = None,
    face: str | None = None,
) -> dict:
    author = None
    if name or face:
        author = {"mid": int(UID)}
        if name:
            author["name"] = name
        if face:
            author["face"] = face
    return {
        "opus_id": opus_id,
        "content": content,
        "jump_url": f"//www.bilibili.com/opus/{opus_id}",
        "cover": {"url": "//i0.hdslb.com/bfs/new_dyn/c.jpg", "width": 720, "height": 1640},
        "author": author,
    }


async def case_a_blank_app_is_an_empty_result(monkeypatch):
    del monkeypatch
    calls: list[str] = []
    app_calls: list[str] = []

    async def post(_body, headers):
        app_calls.append(headers["buvid"])
        return _app_frame([])

    async def call(_method, url, **_kwargs):
        calls.append(url)
        raise AssertionError(url)

    name, items = await fetch_dynamics(UID, call=call, post=post, buvid="XUSHARED", seen={"100"})
    assert (name, items) == (None, [])
    assert app_calls == ["XUSHARED"]
    assert calls == []

    proxy = "http://phone"
    _clear_cap()
    with bili_round.polling(proxy, slot=0, mode="app") as ask:
        name, items = await fetch_dynamics(UID, call=call, post=post, seen={"100"})
    assert ask.blank is True
    assert ask.posts is False
    assert (name, items) == (None, [])
    assert calls == []
    assert bili_round.cap.sits_out(proxy) is False

    async def blocked(_body, _headers):
        app_calls.append("blocked")
        raise FetchError("HTTP 412 from api", status=412)

    app_calls.clear()
    with pytest.raises(FetchError, match="412") as caught:
        with bili_round.polling(proxy, slot=0, mode="app"):
            await fetch_dynamics(UID, call=call, post=blocked)
    assert caught.value.status == 412
    assert app_calls == ["blocked"]
    assert calls == []
    assert bili_round.cap.sits_out(proxy) is False


async def case_a_dropped_app_raises(monkeypatch):
    del monkeypatch
    calls: list[str] = []

    async def post(_body, _headers):
        raise FetchError("Could not fetch https://grpc.biliapi.net/dyn")

    async def call(_method, url, **_kwargs):
        calls.append(url)
        raise AssertionError(url)

    with pytest.raises(FetchError, match="Could not fetch"):
        await fetch_dynamics(UID, call=call, post=post, seen={"100"})
    assert calls == []


async def case_the_app_page_returns_the_ids_it_got(monkeypatch):
    del monkeypatch
    calls: list[object] = []

    async def post(body, _headers):
        asked = decode_message("DynSpaceReq", grpc_message(body))
        calls.append(asked.get(5))
        return _app_frame(
            [_app_word("200", "new"), _app_word("100", "mid")],
            offset="p2",
            has_more=True,
        )

    _name, items = await fetch_dynamics(UID, post=post, buvid="XU" + "B" * 35)
    assert [item.item_id for item in items] == ["200", "100"]
    assert calls == [1]


async def case_the_visitor_list_tags_a_paragraph_and_a_short_row(monkeypatch):
    del monkeypatch
    title = "3.7版本回归邀约开启"
    assert is_paragraph(title) is False
    assert is_paragraph(_PARAGRAPH) is True
    sent: dict[str, list] = {}

    async def call(_method, url, **kwargs):
        if url == BILI_SPI_URL:
            return _spi()
        if url == BILI_NAV_URL:
            return _nav()
        sent.setdefault(url, []).append(kwargs)
        if url == BILI_OPUS_URL:
            return {
                "code": 0,
                "data": {
                    "items": [
                        _opus_row(
                            "150",
                            _PARAGRAPH,
                            name="鸣潮",
                            face="//i0.hdslb.com/bfs/face/s.jpg",
                        ),
                        _opus_row("140", title),
                    ]
                },
            }
        raise AssertionError(url)

    with bili_round.polling("http://phone", slot=0, mode="visitor") as ask:
        name, items = await fetch_dynamics(UID, call=call, seen={"100"})
    assert ask.posts is True
    assert ask.blank is False
    assert name == "鸣潮"
    assert [item.item_id for item in items] == ["150", "140"]
    assert LIST in items[0].tags
    assert QUIET in items[1].tags
    assert items[0].text == _PARAGRAPH
    assert shows_post(items[0], FEATURE_GAME) is True
    assert shows_post(items[0], FEATURE_OTHER) is False
    assert BILI_SPACE_URL not in sent
    opus_call, = sent[BILI_OPUS_URL]
    assert opus_call["params"]["host_mid"] == UID
    assert opus_call["params"]["web_location"] == "333.1387"
    assert opus_call["params"]["w_rid"]
    assert opus_call["cookies"] == {"buvid3": SPI_BUVID, "opus-goback": "1"}
    assert "SESSDATA" not in opus_call["cookies"]
    assert opus_call["headers"] == space_xhr_headers(f"https://space.bilibili.com/{UID}/dynamic")


async def case_the_visitor_list_fills_a_missing_author(monkeypatch):
    del monkeypatch
    sent: dict[str, list] = {}

    async def call(_method, url, **kwargs):
        if url == BILI_SPI_URL:
            return _spi()
        if url == BILI_NAV_URL:
            return _nav()
        sent.setdefault(url, []).append(kwargs)
        if url == BILI_OPUS_URL:
            return {"code": 0, "data": {"items": [_opus_row("150", _PARAGRAPH)]}}
        if url == BILI_CARD_URL:
            return {
                "code": 0,
                "data": {
                    "card": {
                        "name": "宫叶Miyaba",
                        "face": "https://i1.hdslb.com/bfs/face/c.jpg",
                    }
                },
            }
        raise AssertionError(url)

    with bili_round.polling("http://phone", slot=0, mode="visitor"):
        name, items = await fetch_dynamics(UID, cookie="SESSDATA=saved; bili_jct=jct", call=call)
    marked = opus_item(
        _opus_row("150", _PARAGRAPH),
        account=UID,
        source_name="宫叶Miyaba",
        author_icon="https://i1.hdslb.com/bfs/face/c.jpg",
    )
    assert name == "宫叶Miyaba"
    assert items == [replace(marked, tags=(*marked.tags, LIST))]
    card_call, = sent[BILI_CARD_URL]
    assert card_call["params"] == {"mid": UID}
    assert card_call["cookies"]["buvid3"] == SPI_BUVID
    assert "SESSDATA" not in card_call["cookies"]

    async def broken(_method, url, **_kwargs):
        if url == BILI_SPI_URL:
            return _spi()
        if url == BILI_NAV_URL:
            return _nav()
        if url == BILI_OPUS_URL:
            return {"code": 0, "data": {"items": [_opus_row("160", _PARAGRAPH)]}}
        if url == BILI_CARD_URL:
            raise FetchError("HTTP 412 from api", status=412)
        raise AssertionError(url)

    with bili_round.polling("http://phone", slot=0, mode="visitor"):
        missed_name, missed = await fetch_dynamics(UID, call=broken)
    assert missed_name is None
    assert [item.item_id for item in missed] == ["160"]
    assert LIST in missed[0].tags


async def case_the_visitor_list_stays_on_its_proxy(monkeypatch):
    monkeypatch.setattr(live_settings, "bili_proxies", ("http://fixed", "http://other"))
    seen: list[str | None] = []

    async def call(_method, url, **_kwargs):
        if url == BILI_SPI_URL:
            return _spi()
        if url == BILI_NAV_URL:
            return _nav()
        if url == BILI_OPUS_URL:
            seen.append(current_proxy())
            return {"code": 0, "data": {"items": [_opus_row("150", "Photo", name="Studio")]}}
        raise AssertionError(url)

    with bili_round.polling("http://other", slot=0, mode="visitor"):
        name, items = await fetch_dynamics(UID, call=call, seen={"100"})
    assert name == "Studio"
    assert [item.item_id for item in items] == ["150"]
    assert QUIET in items[0].tags
    assert seen == ["http://other"]


async def case_a_mint_retries_one_ordinary_failure(monkeypatch):
    from news.source.post.bilibili.web import _MINT_PAUSE, _spi_buvid3

    slept: list[float] = []
    closed: list[bool] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async def close() -> None:
        closed.append(True)

    monkeypatch.setattr("news.source.post.bilibili.web.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("news.source.post.bilibili.browser.close_bili_connections", close)

    replies: list[object] = []

    async def do(_method, url, **_kwargs):
        if url != BILI_SPI_URL:
            raise AssertionError(url)
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    replies[:] = [FetchError("Could not fetch spi"), _spi()]
    assert await _spi_buvid3(do, live=False) == SPI_BUVID
    assert slept == []
    assert closed == [True]

    replies[:] = [FetchError("Could not fetch spi"), _spi()]
    slept.clear()
    closed.clear()
    assert await _spi_buvid3(do, live=True) == SPI_BUVID
    assert slept == [_MINT_PAUSE]
    assert _MINT_PAUSE == 15.0

    replies[:] = [FetchError("HTTP 412 from api", status=412), _spi()]
    slept.clear()
    closed.clear()
    with pytest.raises(FetchError, match="412") as blocked:
        await _spi_buvid3(do, live=True)
    assert blocked.value.status == 412
    assert slept == []
    assert closed == []
    assert replies == [_spi()]

    replies[:] = [{"code": -352, "message": "风控"}, _spi()]
    with pytest.raises(FetchError, match="Could not mint") as refused:
        await _spi_buvid3(do, live=True)
    assert "-352" not in str(refused.value)
    assert replies == [_spi()]

    spi_calls = {"n": 0}

    async def flaky(_method, url, **_kwargs):
        if url == BILI_SPI_URL:
            spi_calls["n"] += 1
            if spi_calls["n"] == 1:
                raise FetchError("Could not fetch spi")
            return _spi()
        if url == BILI_NAV_URL:
            return _nav()
        if url == BILI_OPUS_URL:
            return {
                "code": 0,
                "data": {
                    "items": [
                        _opus_row(
                            "150", _PARAGRAPH, name="Studio",
                            face="//i0.hdslb.com/bfs/face/s.jpg",
                        )
                    ]
                },
            }
        raise AssertionError(url)

    with bili_round.polling("http://phone", slot=0, mode="visitor"):
        name, items = await fetch_dynamics(UID, call=flaky)
    assert name == "Studio"
    assert [item.item_id for item in items] == ["150"]
    assert spi_calls["n"] == 2


async def case_the_login_page_returns_every_row(monkeypatch):
    monkeypatch.setattr(live_settings, "bili_proxies", ("http://fixed", "http://other"))
    sent: dict[str, list] = {}

    async def call(_method, url, **kwargs):
        if url == BILI_SPI_URL:
            return _spi()
        if url == BILI_NAV_URL:
            return _nav()
        sent.setdefault(url, []).append((current_proxy(), kwargs))
        if url == BILI_SPACE_URL:
            return {
                "code": 0,
                "data": {
                    "items": [
                        _row(
                            "150",
                            kind="DYNAMIC_TYPE_AV",
                            major={
                                "archive": {
                                    "title": "Trailer",
                                    "cover": "https://i0.hdslb.com/bfs/archive/v.jpg",
                                }
                            },
                        ),
                        _row("160", desc="Short title"),
                        _row("90", desc="Old line"),
                    ]
                },
            }
        raise AssertionError(url)

    with bili_round.polling("http://other", slot=0, mode="login"):
        name, items = await fetch_dynamics(UID, cookie=_LOGIN, call=call, seen={"100", "160"})
    assert name == "Studio"
    assert [item.item_id for item in items] == ["150", "160", "90"]
    assert items[0].text == "Trailer"
    assert items[0].image_url == "https://i0.hdslb.com/bfs/archive/v.jpg"
    assert all(LIST not in item.tags for item in items)
    assert BILI_OPUS_URL not in sent
    proxy, space_call = sent[BILI_SPACE_URL][0]
    assert proxy == "http://fixed"
    assert space_call["params"]["host_mid"] == UID
    assert space_call["params"]["dm_img_list"]
    assert space_call["cookies"]["SESSDATA"] == "saved"
    assert space_call["cookies"]["bili_jct"] == "jct"
    assert space_call["cookies"]["DedeUserID"] == "99"
    assert space_call["cookies"]["buvid3"] == SPI_BUVID
    assert "buvid4" not in space_call["cookies"]


async def case_a_failed_login_page_is_empty(monkeypatch):
    monkeypatch.setattr(live_settings, "bili_proxies", ("http://fixed",))
    asked: list[str] = []

    async def call(_method, url, **_kwargs):
        if url == BILI_SPI_URL:
            return _spi()
        if url == BILI_NAV_URL:
            return _nav()
        asked.append(url)
        if url == BILI_SPACE_URL:
            raise FetchError("Could not fetch space")
        raise AssertionError(url)

    with bili_round.polling("http://fixed", slot=0, mode="login") as ask:
        assert await fetch_dynamics(UID, cookie=_LOGIN, call=call, seen={"100"}) == (None, [])
    assert ask.blank is True
    assert asked == [BILI_SPACE_URL]


async def case_a_blocked_login_rests_the_first_proxy(monkeypatch):
    monkeypatch.setattr(live_settings, "bili_proxies", ("http://fixed", "http://other"))
    _clear_cap()
    asked: list[str] = []

    async def call(_method, url, **_kwargs):
        if url == BILI_SPI_URL:
            return _spi()
        if url == BILI_NAV_URL:
            return _nav()
        asked.append(url)
        if url == BILI_SPACE_URL:
            raise FetchError("HTTP 412 from api", status=412)
        raise AssertionError(url)

    with bili_round.polling("http://fixed", slot=0, mode="login"):
        with pytest.raises(FetchError, match="412"):
            await fetch_dynamics(UID, cookie=_LOGIN, call=call, seen={"100"})
    assert asked == [BILI_SPACE_URL]
    assert bili_round.cap.sits_out("http://fixed")
    assert bili_round.cap.sits_out("http://other") is False

    _clear_cap()
    spi_calls = {"n": 0}

    async def spi_blocked(_method, url, **_kwargs):
        if url == BILI_SPI_URL:
            spi_calls["n"] += 1
            raise FetchError("HTTP 412 from api", status=412)
        raise AssertionError(url)

    with bili_round.polling("http://fixed", slot=0, mode="login"):
        with pytest.raises(FetchError, match="412"):
            await fetch_dynamics(UID, cookie=_LOGIN, call=spi_blocked, seen={"100"})
    assert spi_calls["n"] == 1
    assert bili_round.cap.sits_out("http://fixed")


async def case_a_dead_login_raises(monkeypatch):
    monkeypatch.setattr(live_settings, "bili_proxies", ("http://fixed",))
    _clear_cap()
    asked: list[str] = []

    async def call(_method, url, **_kwargs):
        if url == BILI_SPI_URL:
            return _spi()
        if url == BILI_NAV_URL:
            return _nav()
        asked.append(url)
        if url == BILI_SPACE_URL:
            return {"code": -352, "message": "风控"}
        raise AssertionError(url)

    with bili_round.polling("http://fixed", slot=0, mode="login"):
        with pytest.raises(FetchError, match="-352"):
            await fetch_dynamics(UID, cookie=_LOGIN, call=call, seen={"100"})
    assert asked == [BILI_SPACE_URL]
    assert bili_round.cap.sits_out("http://fixed") is False


async def case_a_failed_exit_skips_the_login(monkeypatch):
    monkeypatch.setattr(live_settings, "bili_proxies", ("http://fixed", "http://other"))
    seen: list[str] = []

    async def call(_method, url, **_kwargs):
        seen.append(url)
        raise AssertionError(url)

    with bili_round.failed_exits(("http://fixed",)):
        with bili_round.polling("http://other", slot=0, mode="login") as ask:
            assert await fetch_dynamics(UID, cookie=_LOGIN, call=call, seen={"100"}) == (None, [])
    assert ask.blank is True
    assert seen == []
