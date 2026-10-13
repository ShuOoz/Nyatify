from __future__ import annotations

import base64
import hashlib
import json
import random
from urllib.parse import urlencode

import pytest
from aiohttp import web

import news.source.post.bilibili.round as bili_round
from config import settings as live_settings
from fetch import FetchError
from news.source.post.bilibili.app import bind_proxy, current_proxy, reset_proxy
from news.source.post.bilibili.browser import (
    BILI_BROWSER_HEADERS,
    BILI_CONFIRM_URL,
    BILI_COOKIE_INFO_URL,
    BILI_NAV_URL,
    BILI_REFRESH_URL,
    MIXIN_INDEX,
    WEB_LOCATION,
    WEBGL_RENDERER,
    WEBGL_VENDOR,
    WEBGL_VERSION,
    BiliHttp,
    BiliRefresh,
    bili_correspond_path,
    bili_login_effective,
    bili_request,
    clear_wbi_key,
    close_bili_http,
    current_device,
    dm_params,
    fetch_wbi_key,
    mixin_from_nav,
    refresh_bili_login_cookie,
    sign_wbi,
    space_xhr_headers,
)

UID = "1195230310"
IMG = "0123456789abcdef0123456789abcdef"
SUB = "fedcba9876543210fedcba9876543210"


@pytest.fixture(autouse=True)
def _reset_wbi():
    clear_wbi_key()
    yield
    clear_wbi_key()


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


async def test_bili_login_http(monkeypatch):
    case_bili_correspond_path_is_rsa_hex()
    await case_bili_login_effective_reads_passport()
    await case_refresh_bili_login_cookie_updates_or_skips()
    await case_a_login_refresh_stays_on_the_backup_proxy(monkeypatch)
    await case_a_failed_exit_skips_the_login_refresh(monkeypatch)
    await case_bili_request_drops_login_cookies_before_the_next_call()
    await case_guest_ids_never_share_a_cookie_jar()
    await case_headers_match_the_calling_page()
    await case_bili_request_uses_proxy_only_when_set(monkeypatch)
    await case_real_login_check_closes_the_session(monkeypatch)

def case_bili_correspond_path_is_rsa_hex():
    pytest.importorskip("cryptography")
    path = bili_correspond_path(1_700_000_000_000)
    assert len(path) == 256
    int(path, 16)
    assert bili_correspond_path(1) != path


async def case_bili_login_effective_reads_passport():
    async def ok(method, url, **kwargs):
        assert method == "GET" and url == BILI_COOKIE_INFO_URL
        assert kwargs["cookies"]["SESSDATA"] == "secret"
        return BiliHttp(200, '{"code":0,"data":{"refresh":false}}')

    assert await bili_login_effective("SESSDATA=secret; bili_jct=jct", request=ok) is True

    async def logged_out(_method, _url, **_kwargs):
        return BiliHttp(200, '{"code":-101,"message":"账号未登录"}')

    assert await bili_login_effective("SESSDATA=secret; bili_jct=jct", request=logged_out) is False

    async def down(_method, _url, **_kwargs):
        return BiliHttp(500, "down")

    assert await bili_login_effective("SESSDATA=secret; bili_jct=jct", request=down) is None

    async def boom(_method, _url, **_kwargs):
        raise FetchError("Could not fetch")

    assert await bili_login_effective("SESSDATA=secret; bili_jct=jct", request=boom) is None
    assert await bili_login_effective("buvid3=guest") is False


async def case_real_login_check_closes_the_session(monkeypatch):
    closed = []

    async def close():
        closed.append("closed")

    async def request(method, url, **kwargs):
        assert method == "GET" and url == BILI_COOKIE_INFO_URL
        return BiliHttp(200, '{"code":0}')

    monkeypatch.setattr("news.source.post.bilibili.browser.close_bili_connections", close)
    monkeypatch.setattr("news.source.post.bilibili.browser.bili_request", request)
    assert await bili_login_effective("SESSDATA=s; bili_jct=j") is True
    assert closed == ["closed"]
    closed.clear()
    assert await bili_login_effective("SESSDATA=s; bili_jct=j", request=request) is True
    assert closed == []


async def case_refresh_bili_login_cookie_updates_or_skips():
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("data")))
        if url == BILI_COOKIE_INFO_URL:
            return BiliHttp(
                200, '{"code":0,"data":{"refresh":true,"timestamp":1}}'
            )
        if "/correspond/" in url:
            assert url.endswith("/abc")
            return BiliHttp(200, '<html><div id="1-name">csrf-html</div></html>')
        if url == BILI_REFRESH_URL:
            assert kwargs["data"]["refresh_csrf"] == "csrf-html"
            assert kwargs["data"]["refresh_token"] == "old-ac"
            return BiliHttp(
                200,
                '{"code":0,"data":{"refresh_token":"new-ac"}}',
                cookies={"SESSDATA": "new-sess", "bili_jct": "new-jct", "DedeUserID": "99"},
            )
        if url == BILI_CONFIRM_URL:
            assert kwargs["data"]["refresh_token"] == "old-ac"
            assert kwargs["data"]["csrf"] == "new-jct"
            return BiliHttp(200, '{"code":0}')
        raise AssertionError(url)

    result = await refresh_bili_login_cookie(
        "SESSDATA=old; bili_jct=old-jct; DedeUserID=99; buvid3=x",
        "old-ac",
        request=request,
        correspond_path=lambda _ts: "abc",
    )
    assert result == BiliRefresh(
        "SESSDATA=new-sess; bili_jct=new-jct; DedeUserID=99; buvid3=x",
        "new-ac",
        True,
    )
    assert [item[0] for item in calls] == ["GET", "GET", "POST", "POST"]

    async def skip_request(_method, _url, **_kwargs):
        return BiliHttp(200, '{"code":0,"data":{"refresh":false,"timestamp":1}}')

    skipped = await refresh_bili_login_cookie(
        "SESSDATA=s; bili_jct=j",
        "ac",
        request=skip_request,
        correspond_path=lambda _ts: "abc",
    )
    assert skipped.changed is False
    assert skipped.cookie == "SESSDATA=s; bili_jct=j"
    assert skipped.ac_time == "ac"


async def _serve(handler):
    app = web.Application()
    app.router.add_get("/x", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/x"


_LOGIN = "SESSDATA=saved; bili_jct=jct; DedeUserID=99"


async def case_a_login_refresh_stays_on_the_backup_proxy(monkeypatch):
    monkeypatch.setattr(live_settings, "bili_proxies", ("http://fixed", "http://other"))
    seen: list[str | None] = []

    async def request(_method, _url, **_kwargs):
        seen.append(current_proxy())
        return BiliHttp(200, '{"code":0,"data":{"refresh":false,"timestamp":1}}')

    token = bind_proxy("http://other")
    try:
        result = await refresh_bili_login_cookie(_LOGIN, "ac", request=request)
        restored = current_proxy()
    finally:
        reset_proxy(token)
    assert result.changed is False
    assert seen == ["http://fixed"]
    assert restored == "http://other"


async def case_a_failed_exit_skips_the_login_refresh(monkeypatch):
    monkeypatch.setattr(live_settings, "bili_proxies", ("http://fixed", "http://other"))
    refreshed: list[str | None] = []

    async def request(_method, _url, **_kwargs):
        refreshed.append(current_proxy())
        return BiliHttp(200, '{"code":0,"data":{"refresh":false,"timestamp":1}}')

    with bili_round.failed_exits(("http://fixed",)):
        result = await refresh_bili_login_cookie(_LOGIN, "ac", request=request)
    assert result.changed is False
    assert result.cookie == _LOGIN
    assert refreshed == []


async def case_bili_request_drops_login_cookies_before_the_next_call():
    seen: list[str] = []

    async def handler(request):
        seen.append(request.headers.get("Cookie", ""))
        resp = web.Response(text="ok")
        if request.query.get("set") == "1":
            resp.set_cookie("SESSDATA", "from-set-cookie")
            resp.set_cookie("buvid3", "device")
        return resp

    runner, url = await _serve(handler)
    await close_bili_http()
    try:
        await bili_request(
            "GET", url, cookies={"SESSDATA": "login-secret", "bili_jct": "jct"}
        )
        await bili_request("GET", url, cookies={"buvid3": "guest"})
        await bili_request("GET", f"{url}?set=1", cookies={"SESSDATA": "login-secret"})
        await bili_request("GET", url)
    finally:
        await close_bili_http()
        await runner.cleanup()

    assert "SESSDATA=login-secret" in seen[0]
    assert "bili_jct=jct" in seen[0]
    assert "SESSDATA" not in seen[1]
    assert "bili_jct" not in seen[1]
    assert "buvid3=guest" in seen[1]
    assert "SESSDATA" not in seen[3]
    assert "buvid3=device" in seen[3]


async def case_guest_ids_never_share_a_cookie_jar():
    seen: list[str] = []

    async def handler(request):
        seen.append(request.headers.get("Cookie", ""))
        resp = web.Response(text="ok")
        owner = request.query.get("set")
        if owner:
            resp.set_cookie("b_lsid", f"from-{owner}")
            resp.set_cookie("SESSDATA", "leaked-login")
        return resp

    runner, url = await _serve(handler)
    await close_bili_http()
    try:
        await bili_request("GET", f"{url}?set=a", cookies={"buvid3": "guest-a"})
        await bili_request("GET", url, cookies={"buvid3": "guest-b"})
        await bili_request("GET", url, cookies={"buvid3": "guest-a"})
        await bili_request("GET", url)
    finally:
        await close_bili_http()
        await runner.cleanup()

    slot_b, slot_a, bare = seen[1:]
    assert "buvid3=guest-b" in slot_b
    assert "from-a" not in slot_b and "guest-a" not in slot_b
    assert "b_lsid=from-a" in slot_a
    assert slot_a.count("buvid3") == 1
    assert "SESSDATA" not in slot_a
    assert "from-a" not in bare and "guest" not in bare


async def case_headers_match_the_calling_page():
    seen: list[dict[str, str]] = []

    async def handler(request):
        seen.append(dict(request.headers))
        return web.Response(text="ok")

    runner, url = await _serve(handler)
    await close_bili_http()
    page = "https://space.bilibili.com/2/dynamic"
    try:
        await bili_request("GET", url, headers=space_xhr_headers(page))
        await bili_request("GET", url, headers={"Content-Type": "application/json"})
    finally:
        await close_bili_http()
        await runner.cleanup()

    xhr, default = seen
    for sent in seen:
        assert sent["User-Agent"] == BILI_BROWSER_HEADERS["User-Agent"]
        assert "Chrome/150" in sent["User-Agent"] and "Macintosh" in sent["User-Agent"]
        assert '"Google Chrome";v="150"' in sent["sec-ch-ua"]
        assert sent["sec-ch-ua-mobile"] == "?0"
        assert sent["sec-ch-ua-platform"] == '"macOS"'
        assert sent["Accept-Language"].startswith("en-US")
    assert xhr["Origin"] == "https://space.bilibili.com"
    assert xhr["Referer"] == page
    assert xhr["Accept"] == "application/json, text/plain, */*"
    assert (xhr["Sec-Fetch-Site"], xhr["Sec-Fetch-Mode"], xhr["Sec-Fetch-Dest"]) == (
        "same-site",
        "cors",
        "empty",
    )
    assert default["Origin"] == "https://www.bilibili.com"
    assert default["Referer"] == "https://www.bilibili.com/"
    assert "Sec-Fetch-Mode" not in default


async def case_bili_request_uses_proxy_only_when_set(monkeypatch):
    sent = []

    class _Response:
        status_code = 200
        text = "{}"
        cookies = {}

    class _Cookies:
        def delete(self, _name):
            pass

    class _Session:
        cookies = _Cookies()

        async def request(self, method, url, **kwargs):
            sent.append(kwargs)
            return _Response()

    async def session(_guest=""):
        return _Session()

    monkeypatch.setattr("news.source.post.bilibili.browser._bili_session", session)
    monkeypatch.setattr(live_settings, "bili_proxy", None)
    await bili_request("GET", "https://api.bilibili.com/x")
    monkeypatch.setattr(live_settings, "bili_proxy", "http://127.0.0.1:7890")
    await bili_request("GET", "https://api.bilibili.com/x")
    assert "proxy" not in sent[0]
    assert sent[1]["proxy"] == "http://127.0.0.1:7890"


    token = bind_proxy("http://10.0.0.8:8080")
    try:
        await bili_request("GET", "https://api.bilibili.com/x")
    finally:
        reset_proxy(token)
    assert sent[2]["proxy"] == "http://10.0.0.8:8080"
    direct = bind_proxy("")
    try:
        await bili_request("GET", "https://api.bilibili.com/x")
    finally:
        reset_proxy(direct)
    assert "proxy" not in sent[3]


async def test_wbi_sign():
    case_mixin_from_nav_shuffles_img_and_sub()
    case_sign_wbi_adds_wts_and_rid()
    await case_fetch_wbi_key_caches_until_cleared()
    case_dm_params_describe_the_window()


def case_dm_params_describe_the_window():
    first = dm_params({"mid": UID}, rng=random.Random(1))
    assert dm_params({"mid": UID}, rng=random.Random(1)) == first
    other = dm_params({"mid": UID}, rng=random.Random(2))
    assert other["dm_img_inter"] != first["dm_img_inter"]
    assert first["mid"] == UID
    assert first["dm_img_str"] == base64.b64encode(WEBGL_VERSION.encode()).decode()[:-2]
    cover = base64.b64encode(f"{WEBGL_RENDERER}{WEBGL_VENDOR}".encode()).decode()[:-2]
    assert first["dm_cover_img_str"] == cover
    assert (other["dm_img_str"], other["dm_cover_img_str"]) == (
        first["dm_img_str"],
        first["dm_cover_img_str"],
    )

    device = current_device()
    width, height = device.window_width, device.window_height
    point, = json.loads(first["dm_img_list"])
    assert (point["z"], point["type"]) == (0, 0)
    assert point["timestamp"] >= 0
    x, rest_x = divmod(5 * point["x"] + 2 * point["y"], 23)
    y, rest_y = divmod(4 * point["x"] - 3 * point["y"], 23)
    assert rest_x == rest_y == 0
    assert 0 <= x < width and 0 <= y < height

    inter = json.loads(first["dm_img_inter"])
    seed = inter["wh"][2]
    assert 0 <= seed < 114
    assert inter["wh"] == [
        2 * width + 2 * height + 3 * seed,
        4 * width - height + seed,
        seed,
    ]
    assert min(inter["wh"][:2]) > 0
    scroll = inter["of"][2]
    assert inter["of"] == [scroll, 2 * scroll, scroll] and 0 <= scroll < 514
    search, wrapper = inter["ds"]
    assert search["t"] == wrapper["t"] == 2
    assert search["c"] == base64.b64encode(b"clearfix g-search search-container").decode()[:-2]
    assert all(len(box["p"]) == len(box["s"]) == 3 for box in inter["ds"])


def case_mixin_from_nav_shuffles_img_and_sub():
    mixed = IMG + SUB
    expected = "".join(mixed[i] for i in MIXIN_INDEX if i < len(mixed))[:32]
    assert mixin_from_nav(_nav()) == expected
    with pytest.raises(FetchError, match="WBI"):
        mixin_from_nav({"data": {}})


def case_sign_wbi_adds_wts_and_rid():
    mixin = mixin_from_nav(_nav())
    signed = sign_wbi({"host_mid": UID}, mixin, now=1_700_000_000)
    assert signed["wts"] == 1_700_000_000
    assert signed["web_location"] == WEB_LOCATION
    query = urlencode(
        sorted((key, value) for key, value in signed.items() if key != "w_rid")
    )
    assert signed["w_rid"] == hashlib.md5((query + mixin).encode()).hexdigest()
    assert sign_wbi({"host_mid": UID, "w_rid": "old"}, mixin, now=1)["w_rid"] != "old"


async def case_fetch_wbi_key_caches_until_cleared():
    calls = []

    async def call(_method, url, **_kwargs):
        calls.append(url)
        return _nav()

    first = await fetch_wbi_key(call=call)
    second = await fetch_wbi_key(call=call)
    assert first == second == mixin_from_nav(_nav())
    assert calls == [BILI_NAV_URL]
    clear_wbi_key()
    await fetch_wbi_key(call=call)
    assert calls == [BILI_NAV_URL, BILI_NAV_URL]
