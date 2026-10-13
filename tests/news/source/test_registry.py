from __future__ import annotations

from fetch import FetchError
from news.follows import FEATURE_GAME, FEATURE_OTHER
from news.source.post.bilibili.dynamics import LIST, WATCH
from news.source.registry import (
    FLOOD_CAP,
    ambiguous_tokens,
    card_kind,
    cookie_hint,
    feed_display_name,
    feed_fault,
    flood_cap,
    for_model,
    hold_bookmark,
    is_hard_skip,
    is_live_room,
    is_live_start,
    is_match_platform,
    note_list_post,
    shows_post,
)
from tests.helpers import fetched_item


def test_registry():
    case_display_name_hard_skip_and_card_kind()
    case_registry_asks_the_named_source()
    case_shows_post_for_model_and_note_list_post_follow_the_source()

def case_display_name_hard_skip_and_card_kind():
    assert feed_display_name("Studio 的 bilibili 动态", "bilibili") == "Studio"
    assert feed_display_name("The post by Studio", "bilibili") == "Studio"
    assert feed_display_name("Studio的微博", "weibo") == "Studio"
    assert feed_display_name("https://example.test/feed.xml", "weibo") is None
    item = fetched_item
    assert is_hard_skip(item(text="//转发自: @other: hi"))
    assert is_hard_skip(item(title="UP 转发了 other 的动态"))
    assert is_hard_skip(item(title="Lottery", text="官方抽奖 关注并转发"))
    assert is_hard_skip(item(title="", text="", tags=("互动抽奖",)))
    assert not is_hard_skip(
        item(
            text="互动抽奖 完成高校认证，必得专属头像框奖励！"
            "高校认证入口 游戏内：公告"
        )
    )
    assert not is_hard_skip(
        item(text="互动抽奖 预下载开启，停服预计需要5个小时。")
    )
    assert is_hard_skip(item(platform="weibo", text="转发微博 //@other: hi"))
    assert not is_hard_skip(item(platform="weibo", text="RT @other: hi"))
    assert not is_hard_skip(item())
    assert not is_hard_skip(
        item(
            platform="thesportsdb",
            tags=("match", "next", "soccer"),
            title="Arsenal vs Liverpool",
        )
    )
    assert not is_hard_skip(item(title="今晚开播", text="来看"))
    assert not is_hard_skip(item(title="Nya started a live", text="come watch"))
    assert is_live_room(item(title="今晚开播", text="来看"))
    assert is_live_room(item(title="Nya started a live", text="come watch"))
    assert not is_live_room(
        item(title="Version 3.0 is now live", text="Patch notes and a download link.")
    )
    assert not is_hard_skip(
        item(title="Version 3.0 is now live", text="Patch notes and a download link.")
    )
    assert not is_hard_skip(
        item(
            title="1.4 版本更新",
            text="补丁已发布。今晚直播讲解 https://live.bilibili.com/123 以及完整更新内容。",
        )
    )
    assert not is_live_room(
        item(
            title="1.4 版本更新",
            text="补丁已发布。今晚直播讲解 https://live.bilibili.com/123 以及完整更新内容。",
        )
    )
    preview = fetched_item(platform="thesportsdb", tags=("match", "next"))
    live = fetched_item(platform="thesportsdb", tags=("match", "start"))
    done = fetched_item(platform="thesportsdb", tags=("match", "end"))
    post = fetched_item(tags=("match", "next"))
    assert card_kind("weibo") == card_kind("bilibili") == "post"
    assert card_kind("thesportsdb") == card_kind(preview) == "match"
    assert card_kind("pandascore") == "match"
    assert is_match_platform("thesportsdb") and is_match_platform("pandascore")
    assert not is_match_platform("bilibili")
    assert flood_cap("bilibili") == FLOOD_CAP
    assert flood_cap("thesportsdb") is None
    assert flood_cap("pandascore") is None
    assert hold_bookmark(preview) and hold_bookmark(live)
    assert not hold_bookmark(done) and not hold_bookmark(post)
    assert is_live_start(live)
    assert not is_live_start(preview) and not is_live_start(post)


def case_registry_asks_the_named_source():
    exc = FetchError("Name matches more than one team. Use thesportsdb:1, thesportsdb:2")
    assert ambiguous_tokens("thesportsdb", exc) == ["1", "2"]
    assert ambiguous_tokens("weibo", exc) == []
    cookie = FetchError("code -352")
    assert feed_fault("bilibili", cookie).cookie is True
    assert cookie_hint("bilibili", cookie) == "Use `/news cookie`."
    assert cookie_hint("bilibili", FetchError("HTTP 412 from api")) is None
    assert feed_fault("thesportsdb", FetchError("HTTP 403")) is None
    assert cookie_hint("weibo", FetchError("HTTP 401 from api")) == "Use `/news cookie`."


def case_shows_post_for_model_and_note_list_post_follow_the_source():
    listed = fetched_item(tags=(LIST, f"{WATCH}2026-10-6-19:30", "keep"))
    assert shows_post(listed, FEATURE_GAME) is True
    assert shows_post(listed, FEATURE_OTHER) is False
    assert note_list_post(listed, "10", [(1, FEATURE_GAME)], []) is False
    cleaned = for_model(listed)
    assert "keep" in cleaned.tags
    assert LIST not in cleaned.tags
    assert not any(tag.startswith(WATCH) for tag in cleaned.tags)
    weibo = fetched_item(platform="weibo", tags=(LIST,))
    assert shows_post(weibo, FEATURE_OTHER) is True
    assert for_model(weibo) is weibo
    assert note_list_post(weibo, "10", [(1, FEATURE_OTHER)], []) is False

