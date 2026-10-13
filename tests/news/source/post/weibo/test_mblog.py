from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fetch import FetchError
from news.source.post.weibo.http import WEIBO_HOME, WeiboHttp, _lanes
from news.source.post.weibo.mblog import (
    WEIBO_INDEX_URL,
    fetch_mblogs,
    items_from_index,
    mblog_item,
    profile_fields,
)

UID = "1195230310"


def _mblog(
    item_id: str,
    *,
    text: str = "",
    name: str = "Studio",
    pics: list | None = None,
    orig: dict | None = None,
    created_at: str = "Tue Sep 19 08:00:00 +0800 2026",
    extra: dict | None = None,
) -> dict:
    row: dict = {
        "id": item_id,
        "text": text,
        "created_at": created_at,
        "user": {
            "screen_name": name,
            "profile_image_url": "//tvax1.sinaimg.cn/crop/s.jpg",
        },
    }
    if pics is not None:
        row["pics"] = pics
    if orig is not None:
        row["retweeted_status"] = orig
    if extra:
        row.update(extra)
    return row


def _card(mblog: dict | None, *, card_type: int = 9, group: list | None = None) -> dict:
    card: dict = {"card_type": card_type}
    if mblog is not None:
        card["mblog"] = mblog
    if group is not None:
        card["card_group"] = group
    return card



async def test_weibo_mblogs(monkeypatch):
    case_profile_fields_reads_certification_and_bio()
    case_items_from_index_maps_text_and_unwraps_group()
    case_mblog_item_uses_desktop_status_url()
    case_mblog_item_keeps_video_cover_url()
    case_mblog_item_marks_forward_for_skip()
    case_items_from_index_keeps_pin_after_regular_timeline()
    await case_fetch_mblogs_signs_container_and_retries_empty_data(monkeypatch)
    await case_fetch_mblogs_sends_the_account_through_weibo_json(monkeypatch)
    await case_fetch_mblogs_raises_when_data_never_arrives()
    await case_fetch_mblogs_raises_cookie_expired_on_ok_minus_100()

def case_profile_fields_reads_certification_and_bio():
    certification, bio = profile_fields(
        {
            "data": {
                "userInfo": {
                    "screen_name": "一样音乐工作室",
                    "verified_reason": "北京一样音乐文化传播有限公司",
                    "description": "李荣浩工作室官方微博",
                }
            }
        }
    )
    assert certification == "北京一样音乐文化传播有限公司"
    assert bio == "李荣浩工作室官方微博"
    certification, bio = profile_fields(
        {
            "data": {
                "cards": [
                    _card(
                        _mblog(
                            "11",
                            text="开票",
                            extra={
                                "user": {
                                    "screen_name": "一样音乐工作室",
                                    "description": "李荣浩工作室官方微博",
                                    "verified_reason": "北京一样音乐文化传播有限公司",
                                }
                            },
                        )
                    )
                ]
            }
        }
    )
    assert certification == "北京一样音乐文化传播有限公司"
    assert bio == "李荣浩工作室官方微博"


def case_items_from_index_maps_text_and_unwraps_group():
    name, items = items_from_index(
        {
            "ok": 1,
            "data": {
                "userInfo": {"screen_name": "Studio"},
                "cards": [
                    _card(_mblog("", text="no id")),
                    _card(
                        _mblog(
                            "11",
                            text="hello<br/>from <a href='/n/x'>space</a>",
                        )
                    ),
                    _card(
                        _mblog(
                            "22",
                            text="cats",
                            pics=[
                                {
                                    "type": "video",
                                    "large": {"url": "https://example.test/skip.mp4"},
                                },
                                {
                                    "large": {
                                        "url": "//wx1.sinaimg.cn/large/pic.jpg"
                                    }
                                },
                            ],
                        )
                    ),
                    _card(
                        None,
                        card_type=11,
                        group=[
                            _card(_mblog("33", text="nested")),
                        ],
                    ),
                    {"card_type": 8, "desc": "not an mblog"},
                ],
            },
        },
        account=UID,
    )
    assert name == "Studio"
    assert [item.item_id for item in items] == ["11", "22", "33"]
    word, photo, nested = items
    assert word.title == "hello"
    assert word.text == "hello\nfrom space"
    assert word.url == f"https://weibo.com/{UID}/b"
    assert word.account == UID
    assert word.source_name == "Studio"
    assert word.author_icon == "https://tvax1.sinaimg.cn/crop/s.jpg"
    assert word.occurred_at == datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc)
    assert photo.image_url == "https://wx1.sinaimg.cn/large/pic.jpg"
    assert photo.image_width is None
    tall = mblog_item(
        _card(
            _mblog(
                "44",
                text="scroll",
                pics=[
                    {
                        "large": {
                            "url": "https://wx1.sinaimg.cn/large/tall.jpg",
                            "geo": {"width": "1080", "height": "4320"},
                        }
                    }
                ],
            )
        ),
        account=UID,
    )
    assert tall is not None
    assert tall.image_url == "https://wx1.sinaimg.cn/large/tall.jpg"
    assert tall.image_width == 1080
    assert tall.image_height == 4320
    assert nested.text == "nested"
    assert items_from_index({"data": {"cards": []}}, account=UID) == (None, [])


def case_mblog_item_uses_desktop_status_url():
    given = mblog_item(
        _card(
            _mblog(
                "4451977893666437",
                text="pc",
                extra={"mblogid": "IlObjfnO5"},
            )
        ),
        account=UID,
    )
    assert given is not None
    assert given.url == f"https://weibo.com/{UID}/IlObjfnO5"
    encoded = mblog_item(
        _card(_mblog("4451977893666437", text="pc")),
        account=UID,
    )
    assert encoded is not None
    assert encoded.url == f"https://weibo.com/{UID}/IlObjfnO5"
    from_bid = mblog_item(
        _card(_mblog("11", text="hi", extra={"bid": "z8ElgBLeQ"})),
        account=UID,
    )
    assert from_bid is not None
    assert from_bid.url == f"https://weibo.com/{UID}/z8ElgBLeQ"
    junk = mblog_item(
        _card(
            _mblog(
                "3520617028999724",
                text="x",
                extra={"mblogid": "not a bid"},
            )
        ),
        account=UID,
    )
    assert junk is not None
    assert junk.url == f"https://weibo.com/{UID}/z8ElgBLeQ"


def case_mblog_item_keeps_video_cover_url():
    clip = mblog_item(
        _card(
            _mblog(
                "77",
                text="clip",
                extra={
                    "page_info": {
                        "type": "video",
                        "page_pic": {"url": "//wx4.sinaimg.cn/orj480/cover.jpg"},
                    }
                },
            )
        ),
        account=UID,
    )
    assert clip is not None
    assert clip.image_url == "https://wx4.sinaimg.cn/orj480/cover.jpg"


def case_mblog_item_marks_forward_for_skip():
    forward = mblog_item(
        _card(
            _mblog(
                "44",
                text="wow",
                orig={
                    "id": "43",
                    "text": "Old clip",
                    "user": {"screen_name": "Other"},
                },
            )
        ),
        account=UID,
    )
    assert forward is not None
    assert "转发了Other的微博" in forward.text
    assert "Old clip" in forward.text
    assert forward.image_url is None


def case_items_from_index_keeps_pin_after_regular_timeline():
    pin = _card(_mblog("55", text="Pinned lore", extra={"isTop": 1}))
    titled = {
        **_card(_mblog("56", text="Titled pin")),
        "title": {"text": "置顶", "base_color": 1},
    }
    items = items_from_index(
        {
            "data": {
                "cards": [
                    pin,
                    _card(_mblog("11", text="Latest patch")),
                    titled,
                    _card(_mblog("55", text="Pinned lore again", extra={"isTop": 0})),
                ]
            }
        },
        account=UID,
    )[1]
    assert [item.item_id for item in items] == ["11", "55", "56"]


async def case_fetch_mblogs_signs_container_and_retries_empty_data(monkeypatch):
    calls = []
    pauses = []

    async def pause(seconds, *, live):
        pauses.append((seconds, live))

    monkeypatch.setattr("news.source.post.weibo.mblog.weibo_pause", pause)

    async def call(_method, url, **kwargs):
        calls.append((url, kwargs.get("params"), kwargs.get("cookies")))
        assert url == WEIBO_INDEX_URL
        assert kwargs["params"]["containerid"] == f"230413{UID}"
        assert kwargs["cookie"] == "SUB=login"
        if len(calls) == 1:
            return {"ok": 0, "msg": "请求失败"}
        return {
            "ok": 1,
            "data": {"cards": [_card(_mblog("11", text="hello"))]},
        }

    name, items, certification, bio = await fetch_mblogs(UID, cookie="SUB=login", call=call)
    assert name == "Studio"
    assert certification == ""
    assert bio == ""
    assert items[0].item_id == "11"
    assert len(calls) == 2
    assert calls[0][2] is None
    assert pauses == [(1.0, False)]


async def case_fetch_mblogs_sends_the_account_through_weibo_json(monkeypatch):
    seen = []

    async def fake_do(_request, _method, url, *, cookies, data=None):
        seen.append(url)
        if url == WEIBO_HOME:
            return WeiboHttp(200, "")
        return WeiboHttp(
            200,
            '{"ok":1,"data":{"cards":[{"card_type":9,"mblog":{"id":"11","text":"hello",'
            '"created_at":"Tue Sep 19 08:00:00 +0800 2026","user":{"screen_name":"Studio"}}}]}}',
        )

    monkeypatch.setattr("news.source.post.weibo.http._do", fake_do)
    _lanes.clear()
    try:
        name, items, certification, bio = await fetch_mblogs(UID, cookie="SUB=login")
    finally:
        _lanes.clear()
    assert name == "Studio"
    assert certification == ""
    assert bio == ""
    assert items[0].item_id == "11"
    assert seen == [
        WEIBO_HOME,
        f"{WEIBO_INDEX_URL}?containerid=230413{UID}&page=1",
    ]


async def case_fetch_mblogs_raises_when_data_never_arrives():
    async def call(_method, _url, **_kwargs):
        return {"ok": 0}

    with pytest.raises(FetchError, match="no data"):
        await fetch_mblogs(UID, call=call)


async def case_fetch_mblogs_raises_cookie_expired_on_ok_minus_100():
    async def call(_method, _url, **_kwargs):
        return {"ok": -100, "url": "https://passport.weibo.com/sso/signin"}

    with pytest.raises(FetchError, match="cookie expired") as exc:
        await fetch_mblogs(UID, call=call)
    assert exc.value.status == 403
