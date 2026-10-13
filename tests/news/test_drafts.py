from __future__ import annotations

from news.drafts import (
    FILTER_CACHE_CAP,
    AiCache,
    parse_filter_job,
)
from news.follows import FEATURE_GAME, FEATURE_TICKET


def parse_filter(text: str, caption: str = "", topic: str = "") -> bool | None:
    job = parse_filter_job(text, caption, topic)
    return None if job is None else job.keep


def test_filter_and_cache():
    case_parse_filter()
    case_ai_cache_evicts_oldest()

def case_parse_filter():
    assert parse_filter("KEEP") is None
    assert parse_filter('```json\n{"type":"drop","facts":{}}\n```') is False
    assert parse_filter("{}") is False
    assert parse_filter('{"type":"ops","facts":"down"}') is None
    assert parse_filter(
        '{"type":"ops","facts":{"how":true}}',
        "downtime 06:00, 5 hours, predownload",
    ) is False
    assert parse_filter(
        '{"type":"ops","facts":{"down":"5"}}',
        "downtime 5 hours",
    ) is False
    assert parse_filter(
        '{"type":"ops","facts":{"down":"5 hours"}}',
        "unrelated caption",
    ) is False
    assert parse_filter(
        '{"type":"banner_new","facts":{"who":"提弗洛斯"}}',
        "冬猎 提弗洛斯",
    ) is False
    assert parse_filter(
        '{"type":"drop","facts":{"down":"5 hours"}}',
        "downtime 5 hours",
    ) is False
    assert parse_filter(
        '{"type":"ops","facts":{"down":"5 hours","how":"predownload"}}',
        "4.5 downtime 06:00, 5 hours, 300 gems, predownload in the launcher",
    ) is True
    assert parse_filter(
        '{"type":"stream","facts":{"start":"8月21日19:30"}}',
        "前瞻 8月21日19:30",
    ) is True
    assert parse_filter(
        '{"type":"collab_grant","facts":{"grant":"头像框","how":"游戏内入口"}}',
        "高校认证 头像框 游戏内入口",
    ) is True
    assert parse_filter(
        '{"type":"notice","facts":{"until":"10月3日"}}',
        "活动奖励头像 9月20日16:00 - 10月3日03:59",
    ) is True
    assert parse_filter(
        '{"type":"release","facts":{}}',
        "新专辑现已上线",
    ) is True
    assert parse_filter(
        '{"type":"notice","facts":{"until":"10月3日"}}',
        "活动奖励头像 9月20日16:00 - 10月3日03:59",
        FEATURE_GAME,
    ) is False
    footer = (
        "「弹球勇者」活动介绍\n"
        "—— 欢迎来到罗斯凯利法！——\n"
        "《绝区零》3.2版本现已上线！\n"
        "bilibili安卓端下载地址：网页链接"
    )
    assert parse_filter(
        '{"type":"ops","facts":{"until":"现已上线",'
        '"how":"bilibili安卓端下载地址：网页链接"}}',
        footer,
    ) is False
    assert parse_filter(
        '{"type":"show","facts":{}}',
        "Title: 开票\nAccount: 大麦官博\nText: 薛之谦「天外来物」巡演…",
        FEATURE_TICKET,
    ) is True
    assert parse_filter(
        '{"type":"ops","facts":{"down":"正式开票","start":"3/27 南京"}}',
        "正式开票 3/27 南京 1701 LIVEHOUSE",
        FEATURE_TICKET,
    ) is False
    pv = (
        "《重返未来：1999》银幕楔引PV：尘埃寓言 现已发布！"
        "特别前瞻节目将于9月12日19:00开播"
    )
    job = parse_filter_job(
        '{"type":"stream","facts":{"start":"特别前瞻节目将于9月12日19:00开播"}}',
        pv,
        FEATURE_GAME,
    )
    assert job is not None and job.keep is True
    assert job.kind == "stream"


def case_ai_cache_evicts_oldest():
    cache = AiCache()
    written = ("title", "body")
    for index in range(FILTER_CACHE_CAP + 5):
        cache[("bilibili", f"id-{index}")] = written
    assert len(cache) == FILTER_CACHE_CAP
    assert ("bilibili", "id-0") not in cache
    assert ("bilibili", f"id-{FILTER_CACHE_CAP + 4}") in cache
    kept = AiCache()
    kept[("bilibili", "1", "post", "game")] = written
    kept[("weibo", "2", "post", "ticket")] = written
    kept.drop_sources({("bilibili", "1")})
    assert ("bilibili", "1", "post", "game") in kept
    assert ("weibo", "2", "post", "ticket") not in kept
