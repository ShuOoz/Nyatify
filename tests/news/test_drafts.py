from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone

from fetch import FetchedItem
from news.cards import LINE_WIDTH, TITLE_CHARS
from news.drafts import (
    FILTER_CACHE_CAP,
    AiCache,
    parse_filter_job,
    parse_title_summary,
    stream_fingerprint,
    stream_when,
)
from news.follows import FEATURE_GAME, FEATURE_TICKET


def line_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1 for ch in text)


def parse_filter(text: str, caption: str = "", topic: str = "") -> bool | None:
    job = parse_filter_job(text, caption, topic)
    return None if job is None else job.keep


def test_filter_and_cache():
    case_parse_filter()
    case_parse_title_summary()
    case_stream_fingerprint_is_the_show_day()
    case_a_january_stream_keeps_next_years_clock()
    case_a_reserve_clock_stamps_when_the_quote_leaves_it_out()
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

def case_parse_title_summary():
    assert parse_title_summary('{"title": "Patch", "summary": "It shipped."}') == (
        "Patch",
        "It shipped.",
    )
    assert parse_title_summary(
        '```json\n{"title": "Patch", "summary": "It shipped."}\n```'
    ) == ("Patch", "It shipped.")
    assert parse_title_summary(
        '{"title": "预下载", "summary": "**雪凇幽梦** 已开\\n9月2日更新。"}'
    ) == ("预下载", "**雪凇幽梦** 已开\n9月2日更新。")
    assert parse_title_summary("not json") is None
    assert parse_title_summary('{"title": "", "summary": "x"}') == ("", "x")
    assert parse_title_summary('{"title": "T", "summary": ""}') is None
    game_card = parse_title_summary(
        '{"title": "崩坏星穹铁道 | 4.6前瞻", "time": "2026年9月20日19:30",'
        ' "about": "前瞻直播", "detail": "官方直播间公布新角色。", "reward": "兑换码"}'
    )
    assert game_card == (
        "崩坏星穹铁道 | 4.6前瞻",
        "**Time**\n2026年9月20日19:30\n\n"
        "**About**\n前瞻直播\n官方直播间公布新角色。\n\n"
        "**Reward**\n兑换码",
    )
    grant = parse_title_summary(
        '{"title": "维护", "about": "停服维护", "detail": "更新后重新登录。",'
        ' "reward": "星琼\\n活动页领取，10月3日前。"}'
    )
    assert grant == (
        "维护",
        "**About**\n停服维护\n更新后重新登录。\n\n**Reward**\n星琼\n活动页领取，10月3日前。",
    )
    label_only = parse_title_summary(
        '{"title": "维护", "about": "停服维护", "detail": "", "reward": "星琼\\n"}'
    )
    assert label_only == ("维护", "**About**\n停服维护\n\n**Reward**\n星琼")
    no_grant = parse_title_summary(
        '{"title": "明日方舟 | 闪断更新", "time": "16:00 ~ 16:10",'
        ' "about": "闪断更新", "reward": ""}'
    )
    assert no_grant is not None
    assert "**Reward**" not in no_grant[1]
    assert "**About**" in no_grant[1]
    flavor = (
        "「蠢动于幽暗的兽再度开口，索求一位神明作为祭品。"
        "在月亮重新升起前，唯有智者的血方能平息它的饥渴......」"
    )
    wide = parse_title_summary(
        '{"title": "x", "about": "前瞻特别节目", "detail": "' + flavor + '",'
        ' "reward": "兑换码福利"}'
    )
    assert wide is not None
    about_line = wide[1].split("**About**\n", 1)[1].split("\n", 1)[0]
    assert about_line == "前瞻特别节目"
    assert "蠢动" in wide[1]
    assert "**Reward**\n兑换码福利" in wide[1]
    assert line_width(about_line) <= LINE_WIDTH
    kept = parse_title_summary(
        '{"title": "活动", "about": "限时活动",'
        ' "detail": "' + ("签到活动开启；" * 8) + '", "reward": ""}'
    )
    assert kept is not None
    assert "签到活动开启；" * 8 in kept[1]
    repeated = parse_title_summary(
        '{"title": "前瞻", "about": "前瞻特别节目", "detail": "节目已结束。",'
        ' "reward": "兑换码\\n兑换码将于2026年9月21日23:59:59失效。"}'
    )
    assert repeated is not None
    assert "**Reward**\n兑换码\n将于2026年9月21日23:59:59失效。" in repeated[1]
    long = "t" * (TITLE_CHARS + 10)
    parsed = parse_title_summary('{"title": "' + long + '", "summary": "ok"}')
    assert parsed is not None
    assert len(parsed[0]) == TITLE_CHARS
    assert parsed[0].endswith("…")



def case_stream_fingerprint_is_the_show_day():
    pv = (
        "《重返未来：1999》银幕楔引PV：尘埃寓言 现已发布！"
        "特别前瞻节目将于9月12日19:00开播"
    )
    job = parse_filter_job(
        '{"type":"stream","facts":{"start":"特别前瞻节目将于9月12日19:00开播"}}',
        pv,
        FEATURE_GAME,
    )
    assert job is not None
    cn = timezone(timedelta(hours=8))
    teaser = FetchedItem(
        item_id="pv",
        title="银幕楔引PV 现已发布",
        text=pv,
        url="https://example.test/pv",
        image_url=None,
        platform="bilibili",
        account="1",
        occurred_at=datetime(2026, 9, 11, 12, tzinfo=cn),
    )
    day = FetchedItem(
        item_id="live",
        title="特别前瞻节目",
        text=pv,
        url="https://example.test/live",
        image_url=None,
        platform="bilibili",
        account="1",
        occurred_at=datetime(2026, 9, 12, 10, tzinfo=cn),
    )
    assert stream_fingerprint(
        job.quotes, teaser.occurred_at, keep=job.keep, kind=job.kind
    ) == "stream:2026-9-12-19:00"
    assert stream_fingerprint(
        job.quotes,
        datetime(2027, 9, 11, 12, tzinfo=cn),
        keep=job.keep,
        kind=job.kind,
    ) == "stream:2027-9-12-19:00"
    assert stream_fingerprint(
        job.quotes, day.occurred_at, keep=job.keep, kind=job.kind
    ) is None
    assert stream_fingerprint(
        job.quotes, teaser.occurred_at, keep=True, kind="ops"
    ) is None


def case_a_january_stream_keeps_next_years_clock():
    cn = timezone(timedelta(hours=8))
    december = datetime(2026, 12, 20, 12, tzinfo=cn)
    quote = {"start": "跨年直播将于1月2日19:00开播"}
    assert stream_when(quote["start"], december) == datetime(2027, 1, 2, 19, 0, tzinfo=cn)
    assert stream_fingerprint(
        quote, december, keep=True, kind="stream"
    ) == "stream:2027-1-2-19:00"
    assert stream_when("2026年1月2日19:00", december) == datetime(2026, 1, 2, 19, 0, tzinfo=cn)
    assert stream_fingerprint(
        quote, datetime(2027, 1, 2, 18, tzinfo=cn), keep=True, kind="stream"
    ) is None


def case_a_reserve_clock_stamps_when_the_quote_leaves_it_out():
    cn = timezone(timedelta(hours=8))
    posted = datetime(2026, 9, 30, 11, tzinfo=cn)
    quote = "直播预约：「丹青渡」版本前瞻特别节目"
    watch = "2026年10月6日 19:30"
    assert stream_fingerprint(
        {"start": quote}, posted, keep=True, kind="stream", watch=watch
    ) == "stream:2026-10-6-19:30"
    assert stream_fingerprint(
        {"start": "维护到9月1日 12:00"},
        posted,
        keep=True,
        kind="stream",
        watch=watch,
    ) == "stream:2026-10-6-19:30"
    assert stream_fingerprint(
        {"start": quote},
        datetime(2026, 10, 6, 12, tzinfo=cn),
        keep=True,
        kind="stream",
        watch=watch,
    ) is None


