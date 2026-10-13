from __future__ import annotations

import json

import pytest

from config import settings as live_settings
from fetch import FetchError
from news.source.post.weibo.http import (
    WEIBO_CONFIG_URL,
    WEIBO_HOME,
    WeiboHttp,
    _lane,
    _live_request,
    bound_proxy,
    cookies_from_response,
    guests,
    identity_cookies,
    jar_cookies,
    lane_guest,
    _lanes,
    remember_guest,
    session_identity,
    warmup_weibo,
    weibo_json,
    weibo_pause,
)

INDEX = "https://m.weibo.cn/api/container/getIndex"
DUMP = "SUB=login; _T_WM=stale; XSRF-TOKEN=old"


@pytest.fixture(autouse=True)
def _reset_weibo():
    _lanes.clear()
    yield
    _lanes.clear()


class _Morsel:
    def __init__(self, value: str) -> None:
        self.value = value


def _response(cookies: dict[str, str], history: tuple = ()):
    return type(
        "Final",
        (),
        {
            "cookies": {name: _Morsel(value) for name, value in cookies.items()},
            "history": history,
        },
    )()


def _hop(cookies: dict[str, str]):
    return type(
        "Hop",
        (),
        {"cookies": {name: _Morsel(value) for name, value in cookies.items()}},
    )()


async def test_weibo_http(monkeypatch):
    with monkeypatch.context() as mp:
        await case_weibo_http_session(mp)
    await case_homepage_5xx_keeps_the_saved_guest()


async def case_weibo_http_session(monkeypatch):
    case_identity_cookies_and_redirect_hops()
    for step in (
        case_warmup_then_json_uses_jar_not_dumped_fingerprints,
        case_json_set_cookie_stays_in_jar_for_next_get,
        case_html_set_cookie_used_on_json_retry,
        case_deleted_set_cookie_drops_name_from_jar,
        case_cookie_status_rewarm_strips_visit_cookies,
        case_cookie_status_rewarm_visitor_without_sub,
        case_json_432_fails_fast_without_rewarm,
        case_weibo_json_raises_on_http_and_bad_json,
        case_weibo_json_retries_server_error_and_bad_json,
    ):
        _lanes.clear()
        await step()
    _lanes.clear()
    await case_live_request_timeout_is_a_fetch_error(monkeypatch)
    await case_live_request_uses_proxy_only_when_set(monkeypatch)
    case_proxies_do_not_share_a_jar()
    await case_weibo_pause_only_sleeps_when_live(monkeypatch)

def case_identity_cookies_and_redirect_hops():
    assert identity_cookies(DUMP) == {"SUB": "login"}
    assert identity_cookies("") == {}
    assert identity_cookies("_T_WM=stale; XSRF-TOKEN=old") == {}
    assert identity_cookies("SUB=_2AkGuest; SUBP=p; _T_WM=stale") == {
        "SUB": "_2AkGuest",
        "SUBP": "p",
    }
    assert cookies_from_response(
        _response({"XSRF-TOKEN": "final"}, (_hop({"_T_WM": "from-302"}),))
    ) == {"_T_WM": "from-302", "XSRF-TOKEN": "final"}
    assert cookies_from_response(
        _response({"_T_WM": "new"}, (_hop({"_T_WM": "old"}),))
    ) == {"_T_WM": "new"}
    assert cookies_from_response(
        _response({"SUB": "deleted"}, (_hop({"SUB": "guest"}),))
    ) == {"SUB": ""}


async def case_warmup_then_json_uses_jar_not_dumped_fingerprints():
    calls = []

    async def request(method, url, **kwargs):
        cookies = dict(kwargs.get("cookies") or {})
        calls.append((method, url, cookies))
        if url == WEIBO_HOME:
            assert cookies == {"SUB": "login"}
            return WeiboHttp(200, "<html></html>", cookies={"_T_WM": "fresh"})
        if url == INDEX:
            return WeiboHttp(200, '{"ok":1,"data":{"cards":[]}}')
        raise AssertionError(url)

    payload = await weibo_json("GET", INDEX, cookie=DUMP, request=request)
    assert payload["ok"] == 1
    assert [url for _method, url, _cookies in calls] == [WEIBO_HOME, INDEX]
    assert calls[1][2] == {"SUB": "login", "_T_WM": "fresh"}
    assert "XSRF-TOKEN" not in calls[1][2]
    assert calls[1][2]["_T_WM"] != "stale"
    assert jar_cookies()["_T_WM"] == "fresh"
    assert session_identity() == "SUB=login"

    await weibo_json("GET", INDEX, cookie=DUMP, request=request)
    assert [url for _method, url, _cookies in calls] == [WEIBO_HOME, INDEX, INDEX]


async def case_json_set_cookie_stays_in_jar_for_next_get():
    calls = []

    async def request(_method, url, **kwargs):
        cookies = dict(kwargs.get("cookies") or {})
        calls.append((url, cookies))
        if url == WEIBO_HOME:
            return WeiboHttp(200, "", cookies={"_T_WM": "guest"})
        if len(calls) == 2:
            return WeiboHttp(200, '{"ok":1}', cookies={"MLOGIN": "1"})
        return WeiboHttp(200, '{"ok":1}')

    await weibo_json("GET", INDEX, request=request)
    assert calls[0] == (WEIBO_HOME, {})
    assert jar_cookies()["_T_WM"] == "guest"
    await weibo_json("GET", INDEX, request=request)
    assert calls[1][1] == {"_T_WM": "guest"}
    assert calls[2][1] == {"_T_WM": "guest", "MLOGIN": "1"}
    assert session_identity() == ""


async def case_html_set_cookie_used_on_json_retry():
    indexes = []

    async def request(_method, url, **kwargs):
        cookies = dict(kwargs.get("cookies") or {})
        if url == WEIBO_HOME:
            return WeiboHttp(200, "")
        indexes.append(cookies)
        if len(indexes) == 1:
            return WeiboHttp(200, "<html>nope</html>", cookies={"_T_WM": "after"})
        return WeiboHttp(200, '{"ok":1}')

    assert await weibo_json("GET", INDEX, request=request) == {"ok": 1}
    assert indexes[0] == {}
    assert indexes[1] == {"_T_WM": "after"}


async def case_deleted_set_cookie_drops_name_from_jar():
    calls = []

    async def request(_method, url, **kwargs):
        cookies = dict(kwargs.get("cookies") or {})
        calls.append((url, cookies))
        if url == WEIBO_HOME:
            return WeiboHttp(200, "", cookies={"SUB": "guest", "_T_WM": "wm"})
        if len(calls) == 2:
            return WeiboHttp(200, '{"ok":1}', cookies={"SUB": "deleted"})
        return WeiboHttp(200, '{"ok":1}')

    await weibo_json("GET", INDEX, request=request)
    await weibo_json("GET", INDEX, request=request)
    assert calls[1][1] == {"SUB": "guest", "_T_WM": "wm"}
    assert "SUB" not in calls[2][1]
    assert calls[2][1]["_T_WM"] == "wm"


async def case_cookie_status_rewarm_strips_visit_cookies():
    homes = []
    indexes = []

    async def request(_method, url, **kwargs):
        cookies = dict(kwargs.get("cookies") or {})
        if url == WEIBO_HOME:
            homes.append(cookies)
            return WeiboHttp(200, "", cookies={"_T_WM": f"v{len(homes)}"})
        if url == INDEX:
            indexes.append(cookies)
            if len(indexes) == 1:
                return WeiboHttp(403, "expired")
            return WeiboHttp(200, '{"ok":1}')
        raise AssertionError(url)

    payload = await weibo_json("GET", INDEX, cookie=DUMP, request=request)
    assert payload == {"ok": 1}
    assert homes == [{"SUB": "login"}, {"SUB": "login"}]
    assert indexes[0]["_T_WM"] == "v1"
    assert indexes[1]["_T_WM"] == "v2"
    assert "XSRF-TOKEN" not in indexes[1]


async def case_cookie_status_rewarm_visitor_without_sub():
    homes = []

    async def request(_method, url, **kwargs):
        cookies = dict(kwargs.get("cookies") or {})
        if url == WEIBO_HOME:
            homes.append(cookies)
            return WeiboHttp(200, "", cookies={"_T_WM": f"v{len(homes)}"})
        if len(homes) == 1:
            return WeiboHttp(403, "expired")
        return WeiboHttp(200, '{"ok":1}')

    payload = await weibo_json("GET", INDEX, request=request)
    assert payload == {"ok": 1}
    assert homes == [{}, {}]


async def case_json_432_fails_fast_without_rewarm():
    homes = []
    indexes = []

    async def request(_method, url, **_kwargs):
        if url == WEIBO_HOME:
            homes.append(url)
            return WeiboHttp(200, "", cookies={"_T_WM": "guest"})
        indexes.append(url)
        return WeiboHttp(432, "cooling")

    with pytest.raises(FetchError, match="HTTP 432") as exc:
        await weibo_json("GET", INDEX, request=request)
    assert exc.value.status == 432
    assert len(homes) == 1
    assert len(indexes) == 1


async def case_weibo_json_raises_on_http_and_bad_json():
    indexes = []

    async def forbidden(_method, url, **_kwargs):
        if url == WEIBO_HOME:
            return WeiboHttp(200, "")
        indexes.append(url)
        return WeiboHttp(403, "no")

    with pytest.raises(FetchError, match="HTTP 403"):
        await weibo_json("GET", INDEX, request=forbidden)
    assert len(indexes) == 2

    _lanes.clear()

    async def homepage_down(_method, _url, **_kwargs):
        return WeiboHttp(500, "down")

    with pytest.raises(FetchError, match="HTTP 500"):
        await warmup_weibo(request=homepage_down)

    _lanes.clear()

    bodies = []

    async def not_json(_method, url, **_kwargs):
        if url == WEIBO_HOME:
            return WeiboHttp(200, "")
        bodies.append(url)
        return WeiboHttp(200, "<html>nope</html>")

    with pytest.raises(FetchError, match="JSON"):
        await weibo_json("GET", INDEX, request=not_json)
    assert len(bodies) == 3


async def case_weibo_json_retries_server_error_and_bad_json():
    statuses = []

    async def flaky_500(_method, url, **_kwargs):
        if url == WEIBO_HOME:
            return WeiboHttp(200, "")
        statuses.append(url)
        if len(statuses) < 3:
            return WeiboHttp(502, "busy")
        return WeiboHttp(200, '{"ok":1}')

    assert await weibo_json("GET", INDEX, request=flaky_500) == {"ok": 1}
    assert len(statuses) == 3

    _lanes.clear()
    bodies = []

    async def flaky_json(_method, url, **_kwargs):
        if url == WEIBO_HOME:
            return WeiboHttp(200, "")
        bodies.append(url)
        if len(bodies) == 1:
            return WeiboHttp(200, "<html>nope</html>")
        return WeiboHttp(200, '{"ok":1}')

    assert await weibo_json("GET", INDEX, request=flaky_json) == {"ok": 1}
    assert len(bodies) == 2


async def case_live_request_timeout_is_a_fetch_error(monkeypatch):
    class _TimedOut:
        async def __aenter__(self):
            raise TimeoutError()

        async def __aexit__(self, *_args):
            return False

    class _Session:
        def request(self, *_args, **_kwargs):
            return _TimedOut()

    async def session():
        return _Session()

    monkeypatch.setattr("news.source.post.weibo.http._weibo_session", session)
    with pytest.raises(FetchError, match="Could not fetch"):
        await _live_request("GET", INDEX, cookies={})


async def case_live_request_uses_proxy_only_when_set(monkeypatch):
    sent = []

    class _Response:
        status = 200
        cookies = {}
        history = ()

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Session:
        def request(self, method, url, **kwargs):
            sent.append(kwargs)
            return _Response()

    async def session():
        return _Session()

    monkeypatch.setattr("news.source.post.weibo.http._weibo_session", session)
    monkeypatch.setattr(live_settings, "weibo_proxy", None)
    await _live_request("GET", INDEX, cookies={})
    monkeypatch.setattr(live_settings, "weibo_proxy", "http://127.0.0.1:7890")
    await _live_request("GET", INDEX, cookies={})
    assert "proxy" not in sent[0]
    assert sent[1]["proxy"] == "http://127.0.0.1:7890"
    with bound_proxy("http://10.0.0.8:8080"):
        await _live_request("GET", INDEX, cookies={})
    assert sent[2]["proxy"] == "http://10.0.0.8:8080"
    with bound_proxy(""):
        await _live_request("GET", INDEX, cookies={})
    assert "proxy" not in sent[3]


def case_proxies_do_not_share_a_jar():
    with bound_proxy("http://a"):
        _lane().jar["SUB"] = "from-a"
    with bound_proxy("http://b"):
        assert jar_cookies() == {}
        _lane().jar["SUB"] = "from-b"
    with bound_proxy("http://a"):
        assert jar_cookies()["SUB"] == "from-a"
    with bound_proxy("http://b"):
        assert jar_cookies()["SUB"] == "from-b"


async def case_weibo_pause_only_sleeps_when_live(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("news.source.post.weibo.http.asyncio.sleep", fake_sleep)
    await weibo_pause(1.0, live=False)
    await weibo_pause(0, live=True)
    await weibo_pause(2.0, live=True)
    assert slept == [2.0]


def _config_request(body: str, *, status: int = 200):
    async def request(_method, url, **_kwargs):
        if url == WEIBO_HOME:
            return WeiboHttp(200, "", cookies={"_T_WM": "v"})
        assert url == WEIBO_CONFIG_URL
        return WeiboHttp(status, body)

    return request


async def case_homepage_5xx_keeps_the_saved_guest():

    async def homepage_down(_method, _url, **_kwargs):
        return WeiboHttp(500, "down")

    with bound_proxy("http://p0"):
        remember_guest("SUB=_2AkStay")
        with pytest.raises(FetchError, match="HTTP 500"):
            await warmup_weibo("SUB=_2AkStay", request=homepage_down)
        assert lane_guest() == "SUB=_2AkStay"
        assert jar_cookies() == {}
    assert json.loads(guests.path.read_text(encoding="utf-8")) == {"http://p0": "SUB=_2AkStay"}
    _lanes.clear()
    guests._rows = None
    with bound_proxy("http://p0"):
        assert lane_guest() == "SUB=_2AkStay"
    with bound_proxy("http://p1"):
        assert lane_guest() == ""


