from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from ai import AIError
from news.drafts import AiCache
from news.follows import FEATURE_TICKET
from news.source.post.weibo.ticket import (
    ARTIST_MAX_TOKENS,
    DAMAI_WEIBO,
    ArtistName,
    DamaiWatch,
    TicketDraft,
    TicketShow,
    WatchedArtist,
    artist_read,
    one_artist,
    parse_ticket_draft,
    sale_seen,
    send_sale,
    simplified,
    weibo_sales,
)
from tests.helpers import ScriptedAI, ScriptedFetcher, fetched_item

POST = (
    "周杰伦 演唱会 9月20日 北京 鸟巢 9月21日 上海 梅赛德斯奔驰文化中心 "
    "票价 880/1280"
)

TWO_SHOWS = {
    "title": "周杰伦 | 演唱会",
    "about": "亚洲巡演两站。",
    "window": "",
    "shows": [
        {
            "performer": "周杰伦",
            "date": "9月20日",
            "place": "北京",
            "price": "",
        },
        {
            "performer": "周杰伦",
            "date": "9月21日",
            "place": "上海",
            "price": "",
        },
    ],
    "price": "",
}

def _draft(payload: dict, post: str = POST):
    return parse_ticket_draft(json.dumps(payload, ensure_ascii=False), post)


async def test_ticket_drafts_artists_and_sales():
    case_plan_keeps_both_places()
    case_plan_drops_seen_show()
    case_empty_place_stays_and_is_keyless()
    case_place_not_in_post_is_omitted()
    case_empty_about_has_no_heading()
    case_sale_and_buy_are_their_own_lines()
    case_english_post_keeps_english_labels()
    case_shared_price_one_line_after_shows()
    case_differing_prices_follow_each_show()
    case_draft_price_overrides_show_prices()
    case_second_sale_is_a_new_key()
    await case_send_sale_posts_on_the_ticket_channel()
    await case_send_sale_is_false_when_the_channel_is_missing()
    await case_one_artist_asks_without_thinking()
    await case_one_artist_keeps_the_model_string()
    await case_username_runs_when_there_is_no_bio()
    case_traditional_name_simplifies_for_search()
    case_same_sale_is_quiet_and_a_second_sale_posts()
    case_prune_keeps_a_followed_artist_and_drops_the_rest()
    case_other_artist_is_skipped()
    await case_a_new_artist_reads_the_past_week_once()

def case_plan_keeps_both_places():
    draft = _draft(TWO_SHOWS)
    assert draft is not None
    plan = draft.plan(set())
    assert plan is not None
    assert "北京" in plan.summary
    assert "上海" in plan.summary
    assert "天津" not in plan.summary
    assert draft.shows[0].remember_key == "musicshow:周杰伦\n9月20日\n北京"
    assert plan.remember == (
        draft.shows[0].remember_key,
        draft.shows[1].remember_key,
    )
    packed_shows = [
        {
            "performer": "汪峰",
            "date": f"1月{day}日",
            "place": f"场馆{day}",
            "price": "",
        }
        for day in range(1, 40)
    ]
    packed_post = "汪峰 多城演出。 " + " ".join(
        f"1月{day}日 场馆{day}" for day in range(1, 40)
    )
    packed = _draft(
        {
            "title": "汪峰 | 巡演",
            "about": "多城演出。",
            "window": "",
            "shows": packed_shows,
            "price": "",
        },
        packed_post,
    )
    assert packed is not None
    packed_plan = packed.plan(set())
    assert packed_plan is not None
    assert packed_plan.summary.endswith("\n…")
    assert "场馆" in packed_plan.summary.split("\n…")[0]

def case_plan_drops_seen_show():
    draft = _draft({**TWO_SHOWS, "about": "北京和上海两场，详情见官宣。"})
    assert draft is not None
    first_key = draft.shows[0].remember_key
    second_key = draft.shows[1].remember_key
    assert first_key is not None and second_key is not None
    plan = draft.plan({first_key})
    assert plan is not None
    assert plan.summary.startswith("**简介**\n北京和上海两场，详情见官宣。")
    assert "上海" in plan.summary
    assert "北京" not in plan.summary.split("简介", 1)[1].split("日期：", 1)[1]
    assert plan.remember == (second_key,)
    assert draft.plan({first_key, second_key}) is None

def case_empty_place_stays_and_is_keyless():
    payload = {
        "title": "周杰伦 | 演唱会",
        "about": "一站待定。",
        "window": "",
        "shows": [
            {
                "performer": "周杰伦",
                "date": "9月20日",
                "place": "北京",
                "price": "",
            },
            {
                "performer": "周杰伦",
                "date": "9月21日",
                "place": "",
                "price": "",
            },
        ],
        "price": "",
    }
    draft = _draft(payload)
    assert draft is not None
    keyless = draft.shows[1]
    assert keyless.remember_key is None
    assert "日期：9月21日" in draft.plan(set()).summary
    assert "地点：" not in draft.plan(set()).summary.split("日期：9月21日", 1)[1]
    other_key = draft.shows[0].remember_key
    assert other_key is not None
    later = draft.plan({other_key})
    assert later is not None
    assert "日期：9月21日" in later.summary
    assert "北京" not in later.summary
    assert later.remember == ()

def case_place_not_in_post_is_omitted():
    payload = {
        **TWO_SHOWS,
        "shows": [
            {
                "performer": "周杰伦",
                "date": "9月20日",
                "place": "北京",
                "price": "",
            },
            {
                "performer": "周杰伦",
                "date": "9月21日",
                "place": "广州",
                "price": "",
            },
        ],
    }
    draft = _draft(payload)
    assert draft is not None
    assert len(draft.shows) == 2
    assert draft.shows[0].place == "北京"
    assert draft.shows[1].date == "9月21日"
    assert draft.shows[1].place == ""
    summary = draft.plan(set()).summary
    assert "广州" not in summary
    assert "日期：9月21日" in summary
    assert "地点：" not in summary.split("日期：9月21日", 1)[1]

def case_empty_about_has_no_heading():
    payload = {**TWO_SHOWS, "about": ""}
    draft = _draft(payload)
    assert draft is not None
    summary = draft.plan(set()).summary
    assert summary.startswith("**日程**\n")
    assert "简介" not in summary
    assert "售票" not in summary
    assert "票价" not in summary

def case_sale_and_buy_are_their_own_lines():
    payload = {
        **TWO_SHOWS,
        "window": "2026/9/29（二）13:00",
        "where": "大麦",
        "shows": [TWO_SHOWS["shows"][0]],
    }
    draft = _draft(payload, "周杰伦 演唱会 9月20日 北京 鸟巢 2026/9/29 13:00 大麦")
    assert draft is not None
    summary = draft.plan(set()).summary
    assert summary == (
        "**简介**\n亚洲巡演两站。\n\n"
        "**日程**\n"
        "售票：2026/9/29（二）13:00\n"
        "购买：大麦\n"
        "日期：9月20日\n"
        "地点：北京"
    )

def case_english_post_keeps_english_labels():
    payload = {
        "title": "Jay Chou | Carnival",
        "about": "Two stops.",
        "window": "Sep 29, 13:00",
        "where": "Damai",
        "shows": [
            {
                "performer": "Jay Chou",
                "date": "Nov 7",
                "place": "Beijing",
                "price": "",
            }
        ],
        "price": "",
    }
    draft = _draft(payload, "Jay Chou Carnival Nov 7 Beijing Sep 29 13:00 Damai")
    assert draft is not None
    assert draft.plan(set()).summary == (
        "**About**\nTwo stops.\n\n"
        "**Schedule**\n"
        "Sale: Sep 29, 13:00\n"
        "Buy: Damai\n"
        "Date: Nov 7\n"
        "Venue: Beijing"
    )

def case_shared_price_one_line_after_shows():
    payload = {
        **TWO_SHOWS,
        "shows": [
            {
                "performer": "周杰伦",
                "date": "9月20日",
                "place": "北京",
                "price": "880/1280",
            },
            {
                "performer": "周杰伦",
                "date": "9月21日",
                "place": "上海",
                "price": "880/1280",
            },
        ],
    }
    draft = _draft(payload)
    assert draft is not None
    summary = draft.plan(set()).summary
    assert summary == (
        "**简介**\n亚洲巡演两站。\n\n"
        "**日程**\n"
        "日期：9月20日\n"
        "地点：北京\n"
        "日期：9月21日\n"
        "地点：上海\n"
        "票价：880/1280"
    )

def case_differing_prices_follow_each_show():
    payload = {
        **TWO_SHOWS,
        "shows": [
            {
                "performer": "周杰伦",
                "date": "9月20日",
                "place": "北京",
                "price": "880/1280",
            },
            {
                "performer": "周杰伦",
                "date": "9月21日",
                "place": "上海",
                "price": "980/1580",
            },
        ],
    }
    draft = _draft(payload)
    assert draft is not None
    summary = draft.plan(set()).summary
    assert summary == (
        "**简介**\n亚洲巡演两站。\n\n"
        "**日程**\n"
        "日期：9月20日\n"
        "地点：北京\n"
        "票价：880/1280\n"
        "日期：9月21日\n"
        "地点：上海\n"
        "票价：980/1580"
    )
    assert "日期：9月20日 地点" not in summary
    assert "880/1280" not in summary.split("地点：北京", 1)[0]

def case_draft_price_overrides_show_prices():
    payload = {
        **TWO_SHOWS,
        "price": "统一定价 880",
        "shows": [
            {
                "performer": "周杰伦",
                "date": "9月20日",
                "place": "北京",
                "price": "880/1280",
            },
            {
                "performer": "周杰伦",
                "date": "9月21日",
                "place": "上海",
                "price": "980/1580",
            },
        ],
    }
    draft = _draft(payload)
    assert draft is not None
    summary = draft.plan(set()).summary
    assert summary == (
        "**简介**\n亚洲巡演两站。\n\n"
        "**日程**\n"
        "日期：9月20日\n"
        "地点：北京\n"
        "日期：9月21日\n"
        "地点：上海\n"
        "票价：统一定价 880"
    )

def case_second_sale_is_a_new_key():
    first = TicketShow("周杰伦", "9月20日", "北京", window="8月28日 18:00正式开票")
    same = TicketShow("周杰伦", "9月20日", "北京", window="8月28日18:00开票")
    second = TicketShow("周杰伦", "9月20日", "北京", window="9月10日 20:00二开")
    assert first.remember_key == same.remember_key
    assert first.remember_key != second.remember_key
    assert first.remember_key is not None and second.remember_key is not None
    stored = {first.remember_key}
    assert sale_seen(same.remember_key, stored)
    assert not sale_seen(second.remember_key, stored)
    legacy = "musicshow:周杰伦\n9月20日\n北京"
    assert sale_seen(second.remember_key, {legacy})
    assert sale_seen(legacy, {second.remember_key})
    timed = TicketShow("周杰伦", "9月20日", "K-Arena", window="8月28日 18:00开票")
    plain = TicketShow("周杰伦", "9月20日", "K-Arena")
    again = TicketShow("周杰伦", "9月20日", "K-Arena", window="9月10日 20:00二开")
    benz = TicketShow("周杰伦", "9月20日", "Mercedes-Benz Arena")
    benz_timed = TicketShow(
        "周杰伦", "9月20日", "Mercedes-Benz Arena", window="8月28日 18:00开票"
    )
    assert timed.remember_key and plain.remember_key and again.remember_key
    assert benz.remember_key and benz_timed.remember_key
    assert sale_seen(plain.remember_key, {timed.remember_key})
    assert sale_seen(timed.remember_key, {plain.remember_key})
    assert sale_seen(benz.remember_key, {benz_timed.remember_key})
    assert not sale_seen(again.remember_key, {timed.remember_key})

async def case_send_sale_posts_on_the_ticket_channel():
    seen = {}

    async def send(guild_id, topic, *, embed, queue, files):
        seen.update(guild=guild_id, topic=topic, embed=embed, queue=queue, files=files)
        return object()

    assert await send_sale(send, 7, "embed", ["file"]) is True
    assert seen == {
        "guild": 7,
        "topic": FEATURE_TICKET,
        "embed": "embed",
        "queue": True,
        "files": ["file"],
    }

async def case_send_sale_is_false_when_the_channel_is_missing():
    async def send(guild_id, topic, *, embed, queue, files):
        return None

    assert await send_sale(send, 7, "embed", None) is False

class _AI:
    def __init__(self, reply: str | Exception, *, available: bool = True) -> None:
        self.reply = reply
        self._available = available
        self.messages = None
        self.options = None

    def available(self) -> bool:
        return self._available

    async def complete(self, messages, **options) -> str:
        self.messages = messages
        self.options = options
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

async def case_one_artist_asks_without_thinking():
    ai = _AI('{"artist":"李荣浩"}')
    got = await one_artist(
        ai,
        "一样音乐工作室",
        "北京一样音乐文化传播有限公司",
        "李荣浩工作室官方微博",
    )
    assert got.answered
    assert got.name is not None
    assert got.name.written == "李荣浩"
    assert got.name.simplified == "李荣浩"
    assert "Display name: 一样音乐工作室" in ai.messages[1]["content"]
    assert "Certification: 北京一样音乐文化传播有限公司" in ai.messages[1]["content"]
    assert "Bio: 李荣浩工作室官方微博" in ai.messages[1]["content"]
    assert ai.options == {
        "max_tokens": ARTIST_MAX_TOKENS,
        "temperature": 0,
        "thinking": {"type": "disabled"},
    }

async def case_one_artist_keeps_the_model_string():
    studio = _AI('{"artist":"一样音乐工作室"}')
    named = await one_artist(studio, "一样音乐工作室", "", "李荣浩工作室官方微博")
    assert named.name is not None
    assert named.name.written == "一样音乐工作室"
    failed = _AI(AIError("down"))
    missed = await one_artist(failed, "周杰伦", "", "")
    assert missed.name is None and not missed.answered
    off = _AI('{"artist":"李荣浩"}', available=False)
    unread = await one_artist(off, "一样音乐工作室", "", "李荣浩工作室官方微博")
    assert unread.name is None and not unread.answered
    assert off.messages is None

async def case_username_runs_when_there_is_no_bio():
    person = _AI('{"artist":"周杰伦"}')
    got = await one_artist(person, "周杰伦", "", "")
    assert got.name is not None
    assert got.name.written == "周杰伦"
    assert "Display name: 周杰伦" in person.messages[1]["content"]
    assert "Bio:" not in person.messages[1]["content"]

    empty = _AI('{"artist":""}')
    nobody = await one_artist(empty, "周杰伦", "", "")
    assert nobody.answered and nobody.name is None
    missed = artist_read("not json")
    assert not missed.answered and missed.name is None

def case_traditional_name_simplifies_for_search():
    got = artist_read('{"artist":"周杰倫"}').name
    assert got is not None
    assert got.written == "周杰倫"
    assert got.simplified == "周杰伦"
    assert simplified("鄧紫棋") == "邓紫棋"
    assert simplified("陳奕迅") == "陈奕迅"

ARTIST = ArtistName("周杰伦", "周杰伦")

WATCH = WatchedArtist(1, "bilibili", "99", ARTIST)

def _window_draft(window: str) -> TicketDraft:
    show = TicketShow("周杰伦", "9月20日", "北京", window=window)
    return TicketDraft("周杰伦 | 嘉年华", "", window, (show,))

def case_same_sale_is_quiet_and_a_second_sale_posts():
    first = _window_draft("8月28日 18:00开票")
    key = first.shows[0].remember_key
    assert key is not None
    stored = {("bilibili", "99", "1:ticket"): {key}}

    def seen(platform, account, ref):
        return stored.get((platform, account, ref), set())

    assert weibo_sales(first, (WATCH,), seen) == ()
    second = _window_draft("9月10日 20:00二开")
    posts = weibo_sales(second, (WATCH,), seen)
    assert len(posts) == 1
    assert posts[0].guild_id == 1
    assert posts[0].stamps[0][2] == second.shows[0].remember_key

def case_prune_keeps_a_followed_artist_and_drops_the_rest():
    watch = DamaiWatch()
    watch._watched = [WATCH, WatchedArtist(2, "weibo", "1195230310", ARTIST)]
    watch._artists = {
        ("bilibili", "99", "周杰伦", "", ""): ARTIST,
        ("weibo", "1195230310", "周杰伦", "", ""): ARTIST,
    }
    watch.prune({(1, "bilibili", "99")})
    assert watch._watched == [WATCH]
    assert list(watch._artists) == [("bilibili", "99", "周杰伦", "", "")]

def _sale(item_id: str, age: timedelta):
    return fetched_item(
        item_id,
        title="开票",
        text="周杰伦 9月20日 K-Arena 开票",
        platform="weibo",
        account=DAMAI_WEIBO,
        image_url=None,
        occurred_at=datetime.now(timezone.utc) - age,
    )


async def case_a_new_artist_reads_the_past_week_once():
    watch = DamaiWatch()
    lock = asyncio.Lock()
    found = await watch.note(
        "weibo", "99", [1], display="周杰伦", certification="", bio="",
        ai=_AI('{"artist":"周杰伦"}'), lock=lock, ai_on=True,
    )
    assert found is not None and found.name is not None and watch._replay
    await watch.note(
        "weibo", "99", [1], display="周杰伦", certification="", bio="",
        ai=_AI('{"artist":"周杰伦"}'), lock=lock, ai_on=True,
    )
    assert watch._replay

    feed = ScriptedFetcher(loaded=[_sale("recent", timedelta(days=1)), _sale("old", timedelta(days=8))])
    sent: list[int] = []

    async def send(guild_id, _embed, _files):
        sent.append(guild_id)
        return True

    posted, done = await watch.post(
        feed,
        ai=ScriptedAI(replies=[
            '{"type":"show","facts":[]}',
            json.dumps({
                "title": "周杰伦",
                "shows": [{"performer": "周杰伦", "date": "9月20日", "place": "K-Arena"}],
            }, ensure_ascii=False),
        ]),
        filter_cache=AiCache(),
        write_cache=AiCache(),
        lock=lock,
        ai_on=True,
        stored=feed.stored_shows,
        send=send,
    )
    assert (posted, done, sent, feed.seen_calls, watch._replay) == (
        1, True, [1], [("weibo", DAMAI_WEIBO, ["recent"])], False,
    )
    posted, done = await watch.post(
        feed, ai=ScriptedAI(), filter_cache=AiCache(), write_cache=AiCache(),
        lock=lock, ai_on=True, stored=feed.stored_shows, send=send,
    )
    assert (posted, done, sent) == (0, True, [1])


def case_other_artist_is_skipped():
    draft = TicketDraft(
        "别人 | 演唱会",
        "",
        "8月28日 18:00开票",
        (TicketShow("薛之谦", "9月1日", "上海", window="8月28日 18:00开票"),),
    )
    assert weibo_sales(draft, (WATCH,), lambda *_args: set()) == ()


