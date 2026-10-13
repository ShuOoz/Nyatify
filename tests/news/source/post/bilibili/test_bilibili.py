from __future__ import annotations

import pytest

from config import settings as live
from fetch import FetchError
from news.source.post.bilibili import (
    VISITOR_COOKIE_UID,
    BiliCookies,
    bili_cookie_uid,
    bili_uid,
    fetch_items,
    is_bili_login_cookie,
    parse_account,
    recover_bili_cookie,
    split_bili_ac_time,
)
from news.source.post.bilibili.app import grpc_frame
from news.source.post.bilibili.browser import BILI_NAV_URL, clear_wbi_key, fetch_wbi_key
from news.source.post.bilibili.protobuf import encode_message
from news.source.post.common import COOKIE_SKIP_SECONDS

_LOGIN = "SESSDATA=s; bili_jct=j; DedeUserID=9"


async def test_bilibili_cookie(tmp_path, monkeypatch):
    case_bili_cookie_helpers()
    await case_fetch_items_mints_a_guest_without_saving_it(tmp_path, monkeypatch)
    await case_fetch_items_closes_connections(monkeypatch)
    await case_fetch_items_sends_a_passed_guest_cookie()
    await case_recover_refreshes_a_login_cookie_only()
    await case_guest_poll_keeps_one_buvid(monkeypatch)
    await case_round_end_drops_the_signing_key()


def case_bili_cookie_helpers():
    assert bili_cookie_uid("buvid3=guest") == VISITOR_COOKIE_UID
    assert bili_cookie_uid("SESSDATA=a; DedeUserID=99") == "99"
    cookie, ac_time = split_bili_ac_time(
        "SESSDATA=s; bili_jct=j; DedeUserID=99; ac_time_value=tok"
    )
    assert ac_time == "tok"
    assert "ac_time_value" not in cookie
    assert is_bili_login_cookie(cookie)
    assert not is_bili_login_cookie("buvid3=guest; b_nut=1")
    assert bili_uid("https://space.bilibili.com/99/dynamic") == "99"
    assert parse_account("UID:99") == "99"
    assert bili_uid("studio") is None


async def case_fetch_items_mints_a_guest_without_saving_it(tmp_path, monkeypatch):
    clear_wbi_key()
    config = tmp_path / "config.txt"
    monkeypatch.setattr(live, "bili_cookie", None)
    monkeypatch.setattr(live, "bili_cookie_uid", None)
    monkeypatch.setattr(live, "bili_ac_time", None)

    async def call(_method, url, **_kwargs):
        raise AssertionError(url)

    async def post(_body, _headers):
        return grpc_frame(
            encode_message(
                "DynSpaceRsp",
                {1: [{3: [{2: {3: {2: "Studio"}}}, {4: {3: "hi"}}], 4: {1: "11"}}]},
            )
        )

    name, items, *_ = await fetch_items("99", cookie="", call=call, post=post)
    assert name == "Studio"
    assert items[0].item_id == "11"
    assert not config.exists()
    assert live.bili_cookie is None
    clear_wbi_key()


async def case_fetch_items_closes_connections(monkeypatch):
    closed: list[str] = []

    async def close():
        closed.append("closed")

    monkeypatch.setattr("news.source.post.bilibili.close_bili_connections", close)

    async def call(_method, url, **_kwargs):
        raise AssertionError(url)

    async def post(_body, _headers):
        return grpc_frame(
            encode_message(
                "DynSpaceRsp",
                {1: [{3: [{2: {3: {2: "Studio"}}}, {4: {3: "hi"}}], 4: {1: "11"}}]},
            )
        )

    await fetch_items("99", cookie="", call=call, post=post)
    assert closed == ["closed"]

    async def fail(_body, _headers):
        raise FetchError("HTTP 412 from api", status=412)

    with pytest.raises(FetchError, match="412"):
        await fetch_items("99", cookie="", call=call, post=fail)
    assert closed == ["closed", "closed"]

    fetcher = type("Fetcher", (), {"bili_cookie": None, "bili_buvid": "XUFAKE"})()
    await BiliCookies().end(fetcher)
    assert closed == ["closed", "closed", "closed"]
    assert fetcher.bili_buvid is None


async def case_fetch_items_sends_a_passed_guest_cookie():
    clear_wbi_key()
    calls = []
    posts = []

    async def call(_method, url, **_kwargs):
        calls.append(url)
        raise AssertionError(url)

    async def post(_body, _headers):
        posts.append(1)
        return grpc_frame(
            encode_message(
                "DynSpaceRsp",
                {1: [{3: [{2: {3: {2: "Studio"}}}, {4: {3: "hi"}}], 4: {1: "11"}}]},
            )
        )

    cookie = "buvid3=AAAAinfoc; buvid4=BBBB; _uuid=u; b_nut=1"
    await fetch_items("99", cookie=cookie, call=call, post=post)
    await fetch_items("100", cookie=cookie, call=call, post=post)
    assert calls == []
    assert len(posts) == 2
    clear_wbi_key()


async def case_recover_refreshes_a_login_cookie_only():
    applied = []
    saved = []

    async def apply(*_args, **kwargs):
        applied.append(kwargs)
        return {"line": "Refreshed login cookie.", "changed": True}

    async def save_result(result):
        saved.append(result)

    guest, guest_until = await recover_bili_cookie(
        "bilibili",
        FetchError("code -352"),
        cookie="buvid3=guest; b_nut=1",
        ac_time=None,
        refreshed_until=0,
        now=10,
        apply_cookie=apply,
        save_result=save_result,
    )
    assert (guest, guest_until) == (False, 0)
    assert applied == []

    empty = FetchError("Bilibili dynamics returned no posts from api")
    blank = await recover_bili_cookie(
        "bilibili",
        empty,
        cookie="buvid3=old; b_nut=1",
        ac_time=None,
        refreshed_until=0,
        now=10,
        apply_cookie=apply,
        save_result=save_result,
    )
    assert blank == (False, 0)

    blocked = await recover_bili_cookie(
        "bilibili",
        FetchError("HTTP 412 from api", status=412),
        cookie=_LOGIN,
        ac_time="tok",
        refreshed_until=0,
        now=10,
        apply_cookie=apply,
        save_result=save_result,
    )
    assert blocked == (False, 0)
    assert applied == []

    recovered, until = await recover_bili_cookie(
        "bilibili",
        FetchError("code -352"),
        cookie=_LOGIN,
        ac_time="tok",
        refreshed_until=0,
        now=10,
        apply_cookie=apply,
        save_result=save_result,
    )
    assert recovered is True
    assert until == 10 + COOKIE_SKIP_SECONDS
    assert saved

    again, held = await recover_bili_cookie(
        "bilibili",
        FetchError("code -352"),
        cookie=_LOGIN,
        ac_time="tok",
        refreshed_until=until,
        now=11,
        apply_cookie=apply,
        save_result=save_result,
    )
    assert again is None
    assert held == until
    assert len(applied) == 1

    async def unchanged(*_args, **_kwargs):
        return {"line": "Login cookie still valid.", "changed": False}

    skipped, skipped_until = await recover_bili_cookie(
        "bilibili",
        FetchError("code -352"),
        cookie=_LOGIN,
        ac_time="tok",
        refreshed_until=0,
        now=10,
        apply_cookie=unchanged,
        save_result=save_result,
    )
    assert skipped is None
    assert skipped_until == 0


async def case_guest_poll_keeps_one_buvid(monkeypatch):
    from news.source.post.bilibili.app import buvid_store

    buvid_store._rows, buvid_store._memory = {}, {}
    if buvid_store.path.exists():
        buvid_store.path.unlink()
    minted = []

    def fake_buvid(seed: str = "") -> str:
        minted.append(seed)
        return f"XUFAKE{len(minted)}"

    monkeypatch.setattr("news.source.post.bilibili.app_buvid", fake_buvid)
    monkeypatch.setattr(live, "bili_cookie", None)
    cookies = BiliCookies()
    fetcher = type("Fetcher", (), {"bili_cookie": None, "bili_call": None, "bili_buvid": None})()

    await cookies.prepare(fetcher)
    await cookies.prepare(fetcher)
    assert minted == [""]
    assert fetcher.bili_buvid == "XUFAKE1"
    assert fetcher.bili_cookie is None
    await cookies.end(fetcher)
    assert fetcher.bili_cookie is None
    assert fetcher.bili_buvid is None
    await cookies.prepare(fetcher)
    assert minted == [""]
    assert fetcher.bili_buvid == "XUFAKE1"

    monkeypatch.setattr(live, "bili_cookie", _LOGIN)
    login = BiliCookies()
    await login.prepare(fetcher)
    assert fetcher.bili_cookie == _LOGIN
    assert fetcher.bili_buvid == "XUFAKE1"
    assert minted == [""]


async def case_round_end_drops_the_signing_key():
    calls = []

    async def nav(_method, url, **_kwargs):
        calls.append(url)
        return {
            "data": {
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/0123456789abcdef0123456789abcdef.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/fedcba9876543210fedcba9876543210.png",
                }
            }
        }

    cookies = BiliCookies()
    fetcher = type("Fetcher", (), {"bili_cookie": None, "bili_buvid": "XUFAKE"})()
    try:
        await fetch_wbi_key(call=nav)
        await fetch_wbi_key(call=nav)
        assert calls == [BILI_NAV_URL]
        await cookies.end(fetcher)
        await fetch_wbi_key(call=nav)
        assert calls == [BILI_NAV_URL, BILI_NAV_URL]
        assert fetcher.bili_buvid is None
    finally:
        clear_wbi_key()
