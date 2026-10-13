from __future__ import annotations

import pytest

from fetch import FetchError
from news.source.post.weibo.http import WeiboHttp, _lanes
from news.source.post.weibo.visitor import (
    WEIBO_VISITOR_URL,
    is_weibo_visitor_cookie,
    mint_weibo_visitor_cookie,
    parse_visitor_payload,
    visitor_cookie_header,
)

JSONP = (
    'window.visitor_gray_callback && visitor_gray_callback('
    '{"retcode":20000000,"msg":"succ","data":{"sub":"_2AkGuest","subp":"p"}});'
)


@pytest.fixture(autouse=True)
def _reset_weibo():
    _lanes.clear()
    yield
    _lanes.clear()



async def test_weibo_visitor_cookie():
    case_visitor_cookie_helpers()
    await case_mint_visitor_cookie()
    await case_mint_weibo_visitor_cookie_raises_on_http_error()

def case_visitor_cookie_helpers():
    assert is_weibo_visitor_cookie("SUB=_2AkGuest; SUBP=p")
    assert not is_weibo_visitor_cookie("SUB=login")
    assert not is_weibo_visitor_cookie("")
    assert visitor_cookie_header("_2AkGuest", "p") == "SUB=_2AkGuest; SUBP=p"
    assert parse_visitor_payload(JSONP) == {"SUB": "_2AkGuest", "SUBP": "p"}
    with pytest.raises(FetchError, match="visitor mint failed"):
        parse_visitor_payload(
            'visitor_gray_callback({"retcode":50000000,"data":{"sub":"x"}});'
        )
    with pytest.raises(FetchError, match="no SUB"):
        parse_visitor_payload(
            'visitor_gray_callback({"retcode":20000000,"data":{}});'
        )
    with pytest.raises(FetchError, match="JSON"):
        parse_visitor_payload("not jsonp")


async def case_mint_visitor_cookie():
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("data"), kwargs.get("cookies")))
        assert url == WEIBO_VISITOR_URL
        assert method == "POST"
        assert kwargs["data"]["cb"] == "visitor_gray_callback"
        assert kwargs["cookies"] == {}
        return WeiboHttp(200, JSONP)

    cookie = await mint_weibo_visitor_cookie(request=request)
    assert cookie == "SUB=_2AkGuest; SUBP=p"
    assert len(calls) == 1


async def case_mint_weibo_visitor_cookie_raises_on_http_error():
    async def request(_method, _url, **_kwargs):
        return WeiboHttp(403, "no")

    with pytest.raises(FetchError, match="HTTP 403"):
        await mint_weibo_visitor_cookie(request=request)
