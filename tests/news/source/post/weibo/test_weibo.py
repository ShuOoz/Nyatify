from __future__ import annotations

import pytest

from config import settings as live
from config import settings as live_settings
from fetch import FetchedItem, FetchError
from news.feeds import Fetcher
from news.source.post import weibo as source
from news.source.post.weibo import (
    apply_cookie,
    cookie_effective,
    display_name,
    feed_fault,
    fetch_items,
    parse_account,
    should_skip,
    weibo_uid,
)
from news.source.post.weibo.http import (
    WEIBO_CONFIG_URL,
    WEIBO_HOME,
    WeiboHttp,
    bound_proxy,
    current_proxy,
    _lanes,
    lane_guest,
    remember_guest,
)

UID = "1195230310"


def _mblog(item_id: str, *, text: str = "") -> dict:
    return {
        "id": item_id,
        "text": text,
        "created_at": "Tue Sep 19 08:00:00 +0800 2026",
        "user": {
            "screen_name": "Studio",
            "profile_image_url": "//tvax1.sinaimg.cn/crop/s.jpg",
        },
    }


def _card(mblog: dict) -> dict:
    return {"card_type": 9, "mblog": mblog}


def _post(**overrides) -> FetchedItem:
    data = dict(
        item_id="1",
        title="hi",
        text="hello",
        url="https://weibo.com/1",
        image_url=None,
        platform="weibo",
        account=UID,
    )
    data.update(overrides)
    return FetchedItem(**data)


async def test_weibo_source(monkeypatch):
    case_accounts_names_and_skips()
    await case_fetch_items_uses_cookie_or_empty_visitor(monkeypatch)
    await case_fetch_items_mints_visitor_when_empty(monkeypatch)
    await case_each_proxy_keeps_its_own_guest(monkeypatch)
    await case_next_pull_uses_the_reminted_weibo_cookie(monkeypatch)
    await case_fetch_items_does_not_remint_a_live_block(monkeypatch)
    case_a_held_login_is_not_sent_or_replaced(monkeypatch)


async def test_weibo_cookie(monkeypatch):
    await case_apply_cookie_saves_sub_not_visit_fingerprints(monkeypatch)
    _lanes.clear()
    await case_apply_cookie_does_not_mint_a_guest(monkeypatch)
    _lanes.clear()
    await case_weibo_cookie_effective()


def case_accounts_names_and_skips():
    assert weibo_uid("https://weibo.com/u/1195230310") == UID
    assert weibo_uid("https://m.weibo.cn/profile/1195230310") == UID
    assert weibo_uid("weibo:1195230310") == UID
    assert weibo_uid("wb:1195230310") == UID
    assert parse_account(UID, explicit=True) == UID
    assert parse_account(UID) is None
    assert weibo_uid("") is None
    assert weibo_uid("weibo:studio") is None
    assert weibo_uid("https://weibo.com/n/Studio") is None
    assert weibo_uid("https://weibo.com/home") is None
    assert weibo_uid("https://weibo.com/99") is None
    assert weibo_uid("https://example.test/u/1195230310") is None
    assert display_name("Studio的微博") == "Studio"
    assert display_name(" Studio ") == "Studio"
    assert should_skip(_post(platform="bilibili", text="转发微博")) is None
    assert should_skip(_post(text="转发微博")) is True
    assert should_skip(_post(text="hello")) is None
    fault = feed_fault(FetchError("HTTP 403 from api", status=403))
    assert fault is not None and fault.cookie


async def case_fetch_items_uses_cookie_or_empty_visitor(monkeypatch):
    monkeypatch.setattr(live, "weibo_cookie", None)
    calls = []

    async def call(_method, _url, **kwargs):
        calls.append(kwargs.get("cookie"))
        return {"ok": 1, "data": {"cards": [_card(_mblog("11", text="hello"))]}}

    name, items, *_ = await fetch_items(UID, cookie="SUB=login; _T_WM=stale", call=call)
    assert name == "Studio"
    assert items[0].item_id == "11"
    assert calls == ["SUB=login; _T_WM=stale"]

    calls.clear()
    await fetch_items(UID, cookie="", call=call)
    assert calls == [""]


async def case_fetch_items_mints_visitor_when_empty(monkeypatch):
    monkeypatch.setattr(live, "weibo_cookie", None)
    minted = []
    cookies = []
    persisted = {}

    async def mint(*, request=None):
        minted.append(True)
        return "SUB=_2AkGuest; SUBP=p"

    async def fetch(_account, *, cookie="", call=None):
        cookies.append(cookie)
        return "Studio", [], "", ""

    monkeypatch.setattr(source, "mint_weibo_visitor_cookie", mint)
    monkeypatch.setattr(source, "fetch_mblogs", fetch)
    monkeypatch.setattr(
        source, "upsert_config_values", lambda _path, values: persisted.update(values)
    )
    name, items, *_ = await source.fetch_items(UID)
    assert name == "Studio"
    assert items == []
    assert minted == [True]
    assert cookies == ["SUB=_2AkGuest; SUBP=p"]
    assert persisted == {"WEIBO_COOKIES": "SUB=_2AkGuest; SUBP=p"}
    assert live.weibo_cookie == "SUB=_2AkGuest; SUBP=p"


async def case_each_proxy_keeps_its_own_guest(monkeypatch):
    _lanes.clear()
    monkeypatch.setattr(live, "weibo_proxies", ("http://a", "http://b"))
    monkeypatch.setattr(live, "weibo_proxy", "http://a")
    monkeypatch.setattr(live, "weibo_cookie", None)
    minted: list[str] = []
    cookies: list[str] = []
    persisted: dict[str, str] = {}

    async def mint(*, request=None):
        door = current_proxy()
        minted.append(door)
        return f"SUB=_2Ak{door[-1]}; SUBP=p"

    async def fetch(_account, *, cookie="", call=None):
        cookies.append(cookie)
        return "Studio", [], "", ""

    monkeypatch.setattr(source, "mint_weibo_visitor_cookie", mint)
    monkeypatch.setattr(source, "fetch_mblogs", fetch)
    monkeypatch.setattr(
        source, "upsert_config_values", lambda _path, values: persisted.update(values)
    )
    with bound_proxy("http://a"):
        await source.fetch_items(UID)
    with bound_proxy("http://b"):
        await source.fetch_items(UID)
    with bound_proxy("http://a"):
        await source.fetch_items(UID)
    assert minted == ["http://a", "http://b"]
    assert cookies == [
        "SUB=_2Aka; SUBP=p",
        "SUB=_2Akb; SUBP=p",
        "SUB=_2Aka; SUBP=p",
    ]
    assert persisted == {}
    assert live.weibo_cookie is None
    monkeypatch.setattr(live, "weibo_cookie", "SUB=login")
    assert source.poll_proxies() == ("http://a",)
    monkeypatch.setattr(live, "weibo_proxies", ())
    monkeypatch.setattr(live, "weibo_proxy", None)
    monkeypatch.setattr(live, "weibo_cookie", None)


async def case_next_pull_uses_the_reminted_weibo_cookie(monkeypatch):
    stale = "SUB=_2AkStale; SUBP=old"
    fresh = "SUB=_2AkFresh; SUBP=new"
    monkeypatch.setattr(live, "weibo_cookie", stale)
    sent: list[str] = []

    async def mint(*, request=None):
        return fresh

    async def fetch(_account, *, cookie="", call=None):
        sent.append(cookie)
        if cookie.startswith("SUB=_2AkStale"):
            raise FetchError("Weibo cookie expired", status=403)
        return "Studio", [], "", ""

    monkeypatch.setattr(source, "mint_weibo_visitor_cookie", mint)
    monkeypatch.setattr(source, "fetch_mblogs", fetch)
    monkeypatch.setattr(source, "upsert_config_values", lambda *_args, **_kwargs: None)
    fetcher = Fetcher(weibo_cookie=stale)
    with pytest.raises(FetchError, match="Weibo guest rejected") as rejected:
        await fetcher.pull("weibo", UID)
    assert rejected.value.status is None
    assert live.weibo_cookie is None
    await fetcher.pull("weibo", UID)
    assert sent == [stale, fresh]
    assert live.weibo_cookie == fresh


async def case_fetch_items_does_not_remint_a_live_block(monkeypatch):
    monkeypatch.setattr(live, "weibo_cookie", "SUB=_2AkGuest; SUBP=p")

    async def mint(*, request=None):
        raise AssertionError("a live block must not remint")

    async def blocked(status: int, message: str):
        async def fetch(_account, *, cookie="", call=None):
            raise FetchError(message, status=status)

        monkeypatch.setattr(source, "fetch_mblogs", fetch)
        with pytest.raises(FetchError, match=message) as exc:
            await source.fetch_items(UID)
        assert exc.value.status == status

    monkeypatch.setattr(source, "mint_weibo_visitor_cookie", mint)
    await blocked(432, "HTTP 432 from getIndex")
    await blocked(403, "HTTP 403 from getIndex")


def case_a_held_login_is_not_sent_or_replaced(monkeypatch):
    monkeypatch.setattr(live, "weibo_proxies", ("http://p0", "http://p1"))
    monkeypatch.setattr(live, "weibo_proxy", "http://p0")
    monkeypatch.setattr(live, "weibo_cookie", "SUB=login")
    written: list[dict[str, str]] = []
    monkeypatch.setattr(
        source, "upsert_config_values", lambda _path, values: written.append(dict(values))
    )
    source.set_login_held(True)
    try:
        assert source._fetch_cookie("SUB=login") == ""
        with bound_proxy("http://p1"):
            source._store_guest("SUB=_2AkGuest; SUBP=p")
            assert lane_guest() == "SUB=_2AkGuest; SUBP=p"
            assert source._fetch_cookie("SUB=login") == "SUB=_2AkGuest; SUBP=p"
        assert live.weibo_cookie == "SUB=login"
        assert written == []
    finally:
        source.set_login_held(False)
    assert source._fetch_cookie("SUB=login") == "SUB=login"


def test_a_moved_weibo_exit_drops_the_guest_and_keeps_a_login(monkeypatch):
    monkeypatch.setattr(live_settings, "weibo_proxies", ("http://p0", "http://p1"))
    monkeypatch.setattr(live_settings, "weibo_proxy", "http://p0")
    monkeypatch.setattr(live_settings, "weibo_cookie", "SUB=_2AkGuest")
    written: list[dict[str, str]] = []
    monkeypatch.setattr(
        source, "upsert_config_values", lambda _path, values: written.append(dict(values))
    )
    with bound_proxy("http://p0"):
        remember_guest("SUB=_2AkGuest")
    with bound_proxy("http://p1"):
        remember_guest("SUB=_2AkOther")
    source.fresh_identity("http://p1")
    assert live_settings.weibo_cookie == "SUB=_2AkGuest"
    assert written == []
    with bound_proxy("http://p1"):
        assert lane_guest() == ""
    with bound_proxy("http://p0"):
        assert lane_guest() == "SUB=_2AkGuest"
    source.fresh_identity("http://p0")
    assert live_settings.weibo_cookie is None
    assert written == [{"WEIBO_COOKIES": ""}]
    with bound_proxy("http://p0"):
        assert lane_guest() == ""

    monkeypatch.setattr(live_settings, "weibo_cookie", "SUB=login")
    with bound_proxy("http://p0"):
        remember_guest("SUB=login")
    source.fresh_identity("http://p0")
    assert live_settings.weibo_cookie == "SUB=login"
    assert written == [{"WEIBO_COOKIES": ""}]


async def case_apply_cookie_saves_sub_not_visit_fingerprints(monkeypatch):
    monkeypatch.setattr(live, "weibo_cookie", None)
    homes = []

    async def request(_method, _url, **kwargs):
        cookies = dict(kwargs.get("cookies") or {})
        homes.append(cookies)
        assert cookies == {"SUB": "login"}
        return WeiboHttp(200, "", cookies={"_T_WM": "fresh", "XSRF-TOKEN": "new"})

    result = await apply_cookie(
        cookie="SUB=login; _T_WM=stale; XSRF-TOKEN=old",
        request=request,
    )
    assert homes == [{"SUB": "login"}]
    assert result["config"] == {"WEIBO_COOKIES": "SUB=login"}
    assert result["settings_update"] == {"weibo_cookie": "SUB=login"}
    assert "Wrote Weibo SUB" in str(result["line"])

    monkeypatch.setattr(live, "weibo_cookie", "SUB=login")
    again = await apply_cookie(
        cookie=None,
        fallback_cookie="SUB=login; XSRF-TOKEN=old",
        request=request,
    )
    assert again["config"] == {}
    assert "Warmed Weibo cookie" in str(again["line"])

    with pytest.raises(ValueError, match="m.weibo.cn"):
        await apply_cookie(
                cookie="_T_WM=stale; XSRF-TOKEN=old",
            request=request,
        )


async def case_apply_cookie_does_not_mint_a_guest(monkeypatch):
    monkeypatch.setattr(live, "weibo_cookie", None)
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url, dict(kwargs.get("cookies") or {})))
        raise AssertionError(url)

    with pytest.raises(ValueError, match="Paste a Weibo Cookie"):
        await apply_cookie(cookie=None, request=request)
    assert calls == []

    with pytest.raises(ValueError, match="guest cookie"):
        await apply_cookie(
                cookie="SUB=_2AkGuest",
            request=request,
        )
    with pytest.raises(ValueError, match="Paste a Weibo Cookie"):
        await apply_cookie(cookie=None, request=request)
    assert calls == []


def _config_request(body: str, *, status: int = 200):
    async def request(_method, url, **_kwargs):
        if url == WEIBO_HOME:
            return WeiboHttp(200, "", cookies={"_T_WM": "v"})
        assert url == WEIBO_CONFIG_URL
        return WeiboHttp(status, body)

    return request


async def case_weibo_cookie_effective():
    assert (
        await cookie_effective(
            "SUB=_2AkGuest",
            request=_config_request('{"ok":1,"data":{"login":false}}'),
        )
        is True
    )
    _lanes.clear()
    assert (
        await cookie_effective(
            "SUB=login",
            request=_config_request('{"ok":1,"data":{"login":true}}'),
        )
        is True
    )
    _lanes.clear()
    assert (
        await cookie_effective(
            "SUB=login",
            request=_config_request('{"ok":1,"data":{"login":false}}'),
        )
        is False
    )
    _lanes.clear()
    assert (
        await cookie_effective(
            "SUB=login",
            request=_config_request('{"ok":-100}'),
        )
        is False
    )
    _lanes.clear()
    assert await cookie_effective("SUB=login", request=_config_request("cooling", status=432)) is None
    assert await cookie_effective("") is None
