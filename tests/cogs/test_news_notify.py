from __future__ import annotations

import logging
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from announce import notice_text, quote_text
from cogs.news_notify import (
    DAMAI_FETCH_GAP_SECONDS,
    DAMAI_POLL_SECONDS,
    MATCH_FETCH_GAP_SECONDS,
    MATCH_POLL_JITTER_SECONDS,
    MATCH_POLL_SECONDS,
    POST_CHANNEL_KEEP,
    POST_FETCH_GAP_SECONDS,
    POST_FETCH_JITTER_SECONDS,
    POST_POLL_JITTER_SECONDS,
    POST_POLL_SECONDS,
    TICKET_FETCH_GAP_SECONDS,
    TICKET_POLL_SECONDS,
    NewsNotify,
)
from config import Settings
from config import settings as live_settings
from fetch import BookmarkStore, FetchError
from guild import canonical_announce_feature
from news.drafts import FILTER_PEEK_CHARS
from news.feeds import Fetcher, PullResult
from news.follows import (
    FEATURE_GAME,
    FEATURE_MATCH,
    FEATURE_OTHER,
    FEATURE_TICKET,
    TOPIC_UNASSIGNED,
    list_news_follows,
    set_news_follow_topic,
)
from news.source.match.common import (
    START_LATE,
    START_LEAD,
    START_POLL_JITTER_SECONDS,
    START_POLL_SECONDS,
)
from news.source.post.bilibili.dynamics import LIST
from news.source.post.common import CardPicture, weibo_rest
from news.source.post.weibo.ticket import (
    DAMAI_WEIBO,
    ArtistName,
    ArtistRead,
    TicketDraft,
    TicketShow,
)
from news.source.registry import FLOOD_CAP, auth_expired_message
from tests.helpers import (
    ACCOUNT,
    FILTER_DROP,
    FILTER_KEEP,
    GUILD_ID,
    OTHER,
    WRITE_JSON,
    ScriptedAI,
    ScriptedFetcher,
    announce_guild,
    bot_guilds,
    fake_bot,
    fake_context,
    fake_guild,
    fake_text_channel,
    fetched_item,
    map_channels,
    poll_news,
    scratch,
    script_fetcher,
    set_follows,
    soccer_match,
)
from tests.news.support import (
    arm_cards as _arm_cards,
    arm_damai as _arm_damai,
    fill_club as _fill_club,
    fresh_post,
    match_round as _match_round,
    one_card as _one_card,
    post_batches as _post_batches,
    record_sleeps as _record_sleeps,
    stage as _stage,
    warn_cookie as _warn,
)

COOKIE_HINT = notice_text(auth_expired_message("bilibili"))


@asynccontextmanager
async def _loaded(notify):
    await notify.cog_load()
    try:
        yield notify
    finally:
        await notify.cog_unload()
        notify.notices.stop(force=True)


def _notify(store, path, **kwargs):
    return NewsNotify(fake_bot(), guild_config=store, notices_path=path, **kwargs)


def _settings(tmp_path, monkeypatch, name, text=""):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(
        "cogs.news_notify.settings",
        Settings(environ={}, config_path=path),
    )


async def test_roster_and_match_clocks(
    news_store, tmp_path, monkeypatch, news_cog, news_guild, news_channel
):
    with monkeypatch.context() as patched:
        await case_load_and_poll_roster(news_store, tmp_path, patched, news_cog, news_guild)
    fresh_post(news_cog, news_store, news_guild, news_channel)
    with monkeypatch.context() as patched:
        await case_matches_wait_out_their_gap_and_skip_ticket_pauses(
            news_cog, news_store, news_guild, patched
        )
    case_start_lead_outlasts_the_slow_poll()
    fresh_post(news_cog, news_store, news_guild, news_channel)
    with monkeypatch.context() as patched:
        await case_pending_kickoff_polls_quickly_and_skips_other_clubs(
            news_cog, news_store, news_guild, patched
        )
    fresh_post(news_cog, news_store, news_guild, news_channel)
    with monkeypatch.context() as patched:
        await case_started_match_returns_to_the_long_gap(
            news_cog, news_store, news_guild, patched
        )
    fresh_post(news_cog, news_store, news_guild, news_channel)
    with monkeypatch.context() as patched:
        await case_a_failed_kickoff_card_keeps_the_fast_poll(
            news_cog, news_store, news_guild, news_channel, patched
        )
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_match_check_keeps_a_weibo_stop(news_cog, news_store, news_guild)


async def case_load_and_poll_roster(news_store, tmp_path, monkeypatch, news_cog, news_guild):
    _settings(tmp_path, monkeypatch, "cfg.txt")
    async with _loaded(_notify(news_store, tmp_path / "off.json")) as notify:
        assert not notify.poll_feeds.is_running()
        assert not notify.poll_matches.is_running()
        assert canonical_announce_feature("game") == "game"
    assert canonical_announce_feature("game") is None

    _settings(tmp_path, monkeypatch, "match.txt", "NEWS_POLL_MATCH=1\n")
    match_only = _notify(news_store, tmp_path / "match.json")
    assert (match_only._poll_match, match_only._poll_post) == (True, False)
    async with _loaded(match_only):
        assert match_only.poll_matches.is_running()
        assert not match_only.poll_feeds.is_running()

    for poll_match in (False, True):
        news_cog._rng = random.Random(0)
        news_cog._poll_match = poll_match
        assert POST_POLL_SECONDS <= news_cog._poll_interval() <= POST_POLL_SECONDS + POST_POLL_JITTER_SECONDS

    news_cog._ticket_due_at = news_cog._damai_due_at = 0
    assert news_cog._scheduled(100.0) == (True, True, True)
    assert (news_cog._ticket_due_at, news_cog._damai_due_at) == (0, 0)
    news_cog._ticket_due_at = 100.0 + TICKET_POLL_SECONDS
    assert news_cog._scheduled(160.0) == (True, False, True)
    news_cog._damai_due_at = 160.0 + DAMAI_POLL_SECONDS
    assert news_cog._scheduled(160.0)[2] is False

    bot_guilds(news_cog, news_guild)
    club, post, weibo = ("thesportsdb", "133604", FEATURE_MATCH), ("bilibili", "1", FEATURE_GAME), ("weibo", "1195230310", FEATURE_TICKET)

    async def fetched(**options):
        news_cog.fetcher.fetches.clear()
        await news_cog.poll_once(**options)
        return list(news_cog.fetcher.fetches)

    set_follows(news_store, GUILD_ID, club, post)
    assert await fetched(poll_match=False) == [post[:2]]
    assert await fetched(poll_post=False) == [club[:2]]
    set_follows(news_store, GUILD_ID, club, post, weibo)
    got = await fetched(poll_post=False, poll_match=True, poll_ticket=True)
    assert weibo[:2] in got
    assert post[:2] not in got

    now = datetime.now(timezone.utc)
    set_follows(news_store, GUILD_ID, club, post)
    _fill_club(news_store, "133604", now, now + timedelta(days=2))
    announce_guild(news_store, 2, 100)
    set_follows(news_store, 2, club)
    _fill_club(news_store, "133604", now - timedelta(days=3), now - timedelta(hours=2), guild_id=2)
    assert await fetched() == [post[:2]]

    clubs = [("thesportsdb", uid, FEATURE_MATCH) for uid in ("133604", "2", "3")]
    posts = [("bilibili", uid, FEATURE_GAME) for uid in ("1", "10", "11")]
    set_follows(news_store, GUILD_ID, *clubs, *posts)
    _fill_club(news_store, "133604", now, now + timedelta(hours=2))
    got = await fetched()
    assert sorted(got) == sorted(row[:2] for row in [*clubs, *posts])

    slept = _record_sleeps(monkeypatch)
    set_follows(
        news_store,
        GUILD_ID,
        club,
        ("thesportsdb", "133610", FEATURE_MATCH),
        ("bilibili", "1", FEATURE_GAME),
        ("bilibili", "2", FEATURE_GAME),
        weibo,
        ("weibo", "1630856882", FEATURE_TICKET),
    )
    news_cog._post_fetch_gap = POST_FETCH_GAP_SECONDS
    news_cog._match_fetch_gap = MATCH_FETCH_GAP_SECONDS
    news_cog._ticket_fetch_gap = TICKET_FETCH_GAP_SECONDS
    got = await fetched()
    assert slept == [MATCH_FETCH_GAP_SECONDS, TICKET_FETCH_GAP_SECONDS]
    assert got[-2:] == [("weibo", "1195230310"), ("weibo", "1630856882")]

    monkeypatch.setattr(live_settings, "bili_proxies", ("http://p0", "http://p1"))
    set_follows(news_store, GUILD_ID, *[("bilibili", uid, FEATURE_GAME) for uid in "1234"])
    news_cog._rng = random.Random(1)
    slept.clear()
    await fetched()
    assert len(slept) == 2
    assert all(POST_FETCH_GAP_SECONDS <= gap <= POST_FETCH_GAP_SECONDS + POST_FETCH_JITTER_SECONDS for gap in slept)

    news_cog.bot.get_guild = MagicMock(return_value=None)
    assert await poll_news(news_cog, new=[fetched_item()]) == 0
    assert news_cog.fetcher.fetches == []


async def test_posted_cards(
    monkeypatch, caplog, news_cog, news_store, news_guild, news_channel, tmp_path
):
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_each_post_kind_reaches_its_feature(
        monkeypatch, news_cog, news_store, news_guild, news_channel
    )
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_a_post_is_claimed_before_discord_and_logged(
        caplog, news_cog, news_store, news_guild, news_channel
    )
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_post_channel_keeps_fifteen(news_cog, news_store, news_guild, news_channel)
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_missing_channel_topic_change_and_uncapped_matches(
        news_cog, news_store, news_guild, news_channel
    )
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_a_guild_and_its_cookie_warning(
        news_cog, news_store, news_guild, news_channel, tmp_path
    )


async def case_each_post_kind_reaches_its_feature(monkeypatch, news_cog, news_store, news_guild, news_channel):
    await case_poll_live_room_posts_only_to_other(news_cog, news_store, news_guild, news_channel)
    await case_list_post_stays_out_of_bookmarks_until_space_returns_it(news_cog, news_store, news_guild, news_channel)
    with monkeypatch.context() as mp:
        await case_weibo_face_and_cover_are_attached(news_cog, news_store, news_guild, news_channel, mp)


async def case_poll_live_room_posts_only_to_other(news_cog, news_store, news_guild, news_channel):
    live = fetched_item(title="今晚开播", text="https://live.bilibili.com/99")
    other_guild, other_channel = announce_guild(news_store, 2, 56)
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    set_follows(news_store, 2, ("bilibili", ACCOUNT, FEATURE_OTHER))
    assert await poll_news(news_cog, news_guild, other_guild, new=[live]) == 1
    assert news_channel.send.await_count == 0
    assert other_channel.send.await_count == 1
    assert news_cog.fetcher.seen_calls == [("bilibili", ACCOUNT, ["guid-1"])]


async def case_list_post_stays_out_of_bookmarks_until_space_returns_it(news_cog, news_store, news_guild, news_channel):
    other_guild, other_channel = announce_guild(news_store, 2, 56)
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    set_follows(news_store, 2, ("bilibili", ACCOUNT, FEATURE_OTHER))
    bot_guilds(news_cog, news_guild, other_guild)
    fetcher = script_fetcher(news_cog, new=[fetched_item("1002", tags=(LIST,))])
    assert await news_cog.poll_once() == 1
    assert news_channel.send.await_count == 1
    assert other_channel.send.await_count == 0
    assert fetcher.seen_calls == []

    news_channel.send.reset_mock()
    fetcher.new = [fetched_item("1002")]
    assert await news_cog.poll_once() == 1
    assert news_channel.send.await_count == 0
    assert other_channel.send.await_count == 1
    assert fetcher.seen_calls == [("bilibili", ACCOUNT, ["1002"])]


async def test_match_cards(news_cog, news_store, news_guild, news_channel):
    await case_poll_match_card(news_cog, news_store, news_guild, news_channel)
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_other_club_cannot_rewind_match(news_cog, news_store, news_guild, news_channel)


async def case_poll_match_card(news_cog, news_store, news_guild, news_channel):
    kickoff = datetime.now(timezone.utc)
    set_follows(
        news_store,
        GUILD_ID,
        ("thesportsdb", "133604", FEATURE_MATCH),
        ("thesportsdb", "133610", FEATURE_MATCH),
    )
    news_cog._ai_on_poll = True
    news_cog.ai = ScriptedAI(replies=[])
    fetcher = script_fetcher(
        news_cog, new=[soccer_match("next", occurred_at=kickoff)], kickoffs=kickoff
    )
    assert await poll_news(news_cog, news_guild) == 2
    assert _stage(news_cog).extra["stage"] == "next"

    live = soccer_match("start", occurred_at=kickoff)
    fetcher.new = [live]
    fetcher.kickoffs = kickoff
    assert await news_cog.poll_once() == 2
    posted = news_channel.send.return_value
    posted.edit.assert_awaited()
    assert _stage(news_cog).extra["stage"] == "start"
    fetcher.new = [live]
    assert await news_cog.poll_once() == 0

    news_cog.ai = ScriptedAI(replies=['{"title":"","summary":"Saka scored early."}'])
    fetcher.new = [soccer_match("end")]
    assert await news_cog.poll_once() == 2
    notice = _stage(news_cog)
    assert notice.extra["stage"] == "end"
    assert notice.extra["summary"] == "Saka scored early."
    assert news_cog.ai.calls[0][1]["content"] == "Saka 12'"
    assert "Game Status" not in news_cog.ai.calls[0][1]["content"]
    expires = notice.expires_at
    fetcher.new = [soccer_match("end")]
    await news_cog.poll_once()
    assert _stage(news_cog).expires_at == expires

    notice.message = None
    news_cog.bot.get_channel = MagicMock(return_value=news_channel)
    later = soccer_match(
        "end",
        title="Arsenal 3–1 Chelsea",
        notes="Saka 12'\nMartinelli 95'",
        occurred_at=kickoff - timedelta(hours=2),
    )
    news_cog.ai = ScriptedAI(replies=['{"title":"","summary":"Saka 12\'\\nMartinelli 95\'"}'])
    fetcher.new = [later]
    assert await news_cog.poll_once() == 0
    notice = _stage(news_cog)
    assert notice.extra["notes"] == "Saka 12'\nMartinelli 95'"
    notice.message = None
    sent = news_channel.send.await_count
    posted.fetch = AsyncMock(
        side_effect=[
            discord.NotFound(MagicMock(), {"message": "Unknown Message"}),
            posted,
        ]
    )
    fetcher.new = [later]
    assert await news_cog.poll_once() == 1
    assert news_channel.send.await_count == sent + 1
    replaced = news_channel.send.await_count
    _stage(news_cog).message = None
    news_channel.send.return_value.fetch = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), {"message": "fail"})
    )
    fetcher.new = [later]
    assert await news_cog.poll_once() == 0
    assert news_channel.send.await_count == replaced


async def case_a_post_is_claimed_before_discord_and_logged(
    caplog, news_cog, news_store, news_guild, news_channel
):
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    item = fetched_item()
    during: dict[str, object] = {}

    ref = f"{GUILD_ID}:{FEATURE_GAME}"

    async def send(*_args, **_kwargs):
        during["refs"] = news_cog.fetcher.delivered_refs("bilibili", ACCOUNT, item.item_id)
        during["claimed"] = ("bilibili", ACCOUNT, item.item_id, ref) in news_cog._sending
        during["second"] = await news_cog._announce_item(
            "bilibili", ACCOUNT, [(GUILD_ID, FEATURE_GAME)], item
        )
        during["seen"] = list(news_cog.fetcher.seen_calls)
        message = MagicMock()
        message.id = 4242
        message.channel = news_channel
        message.delete = AsyncMock()
        return message

    news_channel.send.side_effect = send
    with caplog.at_level(logging.INFO, logger="cogs.news_notify"):
        assert await poll_news(news_cog, news_guild, new=[item]) == 1
    assert during["refs"] == set()
    assert during["claimed"] is True
    assert during["seen"] == []
    assert during["second"] == 0
    assert "Sent guid-1 message 4242" in caplog.text
    assert news_cog.fetcher.delivered_refs("bilibili", ACCOUNT, item.item_id) == {ref}
    assert news_cog.fetcher.seen_calls == [("bilibili", ACCOUNT, ["guid-1"])]
    assert news_cog._sending == set()

    news_channel.send.side_effect = discord.HTTPException(
        MagicMock(status=500), {"message": "fail"}
    )
    assert await poll_news(news_cog, news_guild, new=[fetched_item("guid-miss")]) == 0
    assert news_cog.fetcher.delivered_refs("bilibili", ACCOUNT, "guid-miss") == set()
    assert news_cog.fetcher.seen_calls == []


async def case_other_club_cannot_rewind_match(news_cog, news_store, news_guild, news_channel):
    kickoff = datetime.now(timezone.utc)
    set_follows(
        news_store,
        GUILD_ID,
        ("thesportsdb", "133604", FEATURE_MATCH),
        ("thesportsdb", "133610", FEATURE_MATCH),
    )
    versions = {
        "133604": soccer_match(
            "start",
            title="Arsenal 2–0 Chelsea",
            account="133604",
            source_name="Arsenal",
            occurred_at=kickoff,
        ),
        "133610": soccer_match(
            "start",
            title="Arsenal 1–0 Chelsea",
            account="133610",
            source_name="Chelsea",
            occurred_at=kickoff,
        ),
    }
    fetcher = script_fetcher(news_cog, kickoffs=kickoff)

    async def pull(_platform, account, **_kwargs):
        fetcher.fetches.append(("thesportsdb", account))
        item = versions[account]
        return PullResult(item.source_name, [item], [item], kickoffs=(kickoff,), items=[item])

    fetcher.pull = pull
    assert await poll_news(news_cog, news_guild) == 2
    assert news_channel.send.await_count == 1
    assert news_channel.send.await_args.kwargs["embed"].title == "Arsenal 2–0 Chelsea"
    posted = news_channel.send.return_value
    posted.edit.assert_not_awaited()
    assert _stage(news_cog).extra["stage"] == "start"
    assert _stage(news_cog).extra["account"] == "133604"

    versions["133604"] = soccer_match(
        "start",
        title="Arsenal 3–0 Chelsea",
        account="133604",
        source_name="Arsenal",
        occurred_at=kickoff,
    )
    assert await news_cog.poll_once() == 0
    assert posted.edit.await_args.kwargs["embed"].title == "Arsenal 3–0 Chelsea"

    versions["133610"] = soccer_match(
        "end",
        title="Arsenal 3–0 Chelsea",
        account="133610",
        source_name="Chelsea",
        occurred_at=kickoff,
    )
    posted.edit.reset_mock()
    assert await news_cog.poll_once() == 1
    assert _stage(news_cog).extra["stage"] == "end"
    assert _stage(news_cog).extra["account"] == "133610"

    versions["133604"] = soccer_match(
        "start",
        title="Arsenal 0–0 Chelsea",
        account="133604",
        source_name="Arsenal",
        occurred_at=kickoff,
    )
    posted.edit.reset_mock()
    assert await news_cog.poll_once() == 0
    posted.edit.assert_not_awaited()
    assert _stage(news_cog).extra["stage"] == "end"


async def case_a_guild_and_its_cookie_warning(news_cog, news_store, news_guild, news_channel, tmp_path):
    await case_a_cookie_warning_posts_once_per_guild(news_cog, news_store, news_guild, news_channel)
    await case_leaving_a_guild_drops_bookmarks_only_it_followed(news_cog, news_store, news_guild, scratch(tmp_path, "leaving_a_guild_drops_bookmarks_only_it_followed"))


async def case_a_cookie_warning_posts_once_per_guild(news_cog, news_store, news_guild, news_channel):
    bot_guilds(news_cog, news_guild)
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    await _warn(news_cog)
    assert news_channel.send.await_count == 1
    assert news_channel.send.await_args.kwargs["content"] == quote_text(COOKIE_HINT)
    await _warn(news_cog)
    assert news_channel.send.await_count == 1
    assert news_cog.notices.live(FEATURE_GAME, GUILD_ID, f"cookie:bilibili:{ACCOUNT}") is not None


async def case_leaving_a_guild_drops_bookmarks_only_it_followed(news_cog, news_store, news_guild, tmp_path):
    set_follows(news_store, GUILD_ID, ("bilibili", "10", FEATURE_GAME))
    set_follows(news_store, 2, ("bilibili", OTHER, FEATURE_GAME))
    bookmarks = BookmarkStore(tmp_path / "bookmarks.json")
    bookmarks.remember("bilibili", "10", ["guid-1"])
    bookmarks.remember("bilibili", OTHER, ["guid-2"])

    async def unused_get(_url, _headers):
        raise AssertionError("guild remove should not fetch")

    news_cog.fetcher = Fetcher(get_text=unused_get, bookmarks=bookmarks)
    await news_cog.on_guild_remove(news_guild)
    assert not bookmarks.is_seeded("bilibili", "10")
    assert bookmarks.seen_ids("bilibili", OTHER) == {"guid-2"}


async def case_post_channel_keeps_fifteen(news_cog, news_store, news_guild, news_channel):
    game_ids = list(range(1, POST_CHANNEL_KEEP + 1))
    messages = _arm_cards(news_channel, [*game_ids, POST_CHANNEL_KEEP + 1])
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    await _post_batches(news_cog, news_guild, [f"g-{i}" for i in game_ids])
    for message in messages:
        message.delete.assert_not_awaited()
    assert news_cog.notices.post_ids(news_channel.id) == tuple(game_ids)

    set_follows(news_store, GUILD_ID, ("weibo", "1195230310", FEATURE_TICKET))
    await _post_batches(news_cog, news_guild, ["w-shared"], platform="weibo")
    messages[0].delete.assert_awaited_once()
    kept = tuple(range(2, POST_CHANNEL_KEEP + 2))
    assert news_cog.notices.post_ids(news_channel.id) == kept

    ticket = fake_text_channel(55, guild=news_guild)
    news_store.set_feature_announce_channel_id(GUILD_ID, FEATURE_TICKET, ticket.id)
    map_channels(news_guild, news_channel, ticket)
    ticket_ids = list(range(101, 101 + POST_CHANNEL_KEEP + 1))
    ticket_messages = _arm_cards(ticket, ticket_ids)
    await _post_batches(
        news_cog, news_guild, [f"s-{i}" for i in ticket_ids], platform="weibo"
    )
    ticket_messages[0].delete.assert_awaited_once()
    assert all(message.delete.await_count == 0 for message in messages[1:])
    assert news_cog.notices.post_ids(news_channel.id) == kept
    assert news_cog.notices.post_ids(ticket.id) == tuple(ticket_ids[1:])

    set_follows(news_store, GUILD_ID, ("thesportsdb", "133604", FEATURE_MATCH))
    match_card = _one_card(news_channel, 200)
    assert await poll_news(news_cog, news_guild, new=[soccer_match("end")]) == 1
    match_card.delete.assert_not_awaited()
    assert news_cog.notices.post_ids(news_channel.id) == kept

    restarted = _notify(
        news_store, news_cog.notices.path, fetcher=ScriptedFetcher(), ai=news_cog.ai, ai_on_poll=False, start_polling=False
    )
    restarted.bot.get_channel = {news_channel.id: news_channel, ticket.id: ticket}.get
    restarted._post_fetch_gap = restarted._match_fetch_gap = 0
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    nxt = _one_card(news_channel, 17)
    assert await poll_news(restarted, news_guild, new=[fetched_item("g-restart")]) == 1
    messages[1].delete.assert_awaited_once()
    nxt.delete.assert_not_awaited()
    assert restarted.notices.post_ids(news_channel.id) == tuple(range(3, 18))

    messages[2].delete.side_effect = RuntimeError("no")
    held = _one_card(news_channel, 18)
    assert await poll_news(restarted, news_guild, new=[fetched_item("g-held")]) == 1
    messages[2].delete.assert_awaited()
    messages[3].delete.assert_not_awaited()
    held.delete.assert_not_awaited()
    assert restarted.notices.post_ids(news_channel.id)[0] == 3
    assert restarted.notices.post_ids(news_channel.id)[-1] == 18
    restarted.notices.stop(force=True)


async def case_match_check_keeps_a_weibo_stop(news_cog, news_store, news_guild):
    set_follows(news_store, GUILD_ID, ("thesportsdb", "133604", FEATURE_MATCH))
    bot_guilds(news_cog, news_guild)
    script_fetcher(news_cog)
    news_cog._weibo_stopped.add("http://bad")
    news_cog._weibo_quiet.add("http://bad")
    await news_cog.poll_once(poll_post=False, poll_match=True, poll_ticket=False, poll_damai=False)
    assert news_cog._weibo_stopped == {"http://bad"}
    assert news_cog._weibo_quiet == {"http://bad"}

    set_follows(news_store, GUILD_ID, ("weibo", "1195230310", FEATURE_GAME))
    await news_cog.poll_once(poll_post=True, poll_match=False, poll_ticket=False, poll_damai=False)
    assert news_cog._weibo_stopped == set()
    assert news_cog._weibo_quiet == set()


async def case_matches_wait_out_their_gap_and_skip_ticket_pauses(news_cog, news_store, news_guild, monkeypatch):
    set_follows(
        news_store,
        GUILD_ID,
        ("thesportsdb", "133604", FEATURE_MATCH),
        ("weibo", "1195230310", FEATURE_TICKET),
        ("weibo", "1630856882", FEATURE_TICKET),
    )
    bot_guilds(news_cog, news_guild)
    news_cog._ticket_fetch_gap = TICKET_FETCH_GAP_SECONDS
    news_cog._rng = random.Random(0)
    during_pause: list[tuple[str, str]] = []

    async def sleep(seconds: float) -> None:
        if seconds == TICKET_FETCH_GAP_SECONDS:
            await news_cog.poll_matches()
            during_pause.extend(news_cog.fetcher.fetches)

    monkeypatch.setattr("cogs.news_notify.asyncio.sleep", sleep)
    await news_cog.poll_feeds()
    assert ("thesportsdb", "133604") in during_pause
    assert ("weibo", "1630856882") not in during_pause
    assert [item for item in news_cog.fetcher.fetches if item[0] == "weibo"] == [
        ("weibo", "1195230310"),
        ("weibo", "1630856882"),
    ]


def case_start_lead_outlasts_the_slow_poll():
    assert START_LEAD.total_seconds() > MATCH_POLL_SECONDS + MATCH_POLL_JITTER_SECONDS
    assert START_LATE > START_LEAD


async def case_pending_kickoff_polls_quickly_and_skips_other_clubs(news_cog, news_store, news_guild, monkeypatch):
    slept = await _match_round(news_cog, news_store, news_guild, monkeypatch, ("thesportsdb", "133604"), "next", ("pandascore", "paper-rex-valorant", FEATURE_MATCH))
    assert len(slept) == 1
    assert START_POLL_SECONDS <= slept[0] <= START_POLL_SECONDS + START_POLL_JITTER_SECONDS
    news_cog.fetcher.fetches.clear()
    await news_cog.poll_matches()
    assert news_cog.fetcher.fetches == [("thesportsdb", "133604")]


async def case_started_match_returns_to_the_long_gap(news_cog, news_store, news_guild, monkeypatch):
    slept = await _match_round(news_cog, news_store, news_guild, monkeypatch, ("pandascore", "paper-rex-valorant"), "start")
    assert MATCH_POLL_SECONDS <= slept[-1] <= MATCH_POLL_SECONDS + MATCH_POLL_JITTER_SECONDS


async def case_a_failed_kickoff_card_keeps_the_fast_poll(
    news_cog, news_store, news_guild, news_channel, monkeypatch
):
    news_channel.send.side_effect = discord.HTTPException(
        MagicMock(status=500), {"message": "fail"}
    )
    club = ("pandascore", "paper-rex-valorant")
    slept = await _match_round(news_cog, news_store, news_guild, monkeypatch, club, "start")
    assert news_cog._seen_live == set()
    assert START_POLL_SECONDS <= slept[-1] <= START_POLL_SECONDS + START_POLL_JITTER_SECONDS
    news_cog.fetcher.fetches.clear()
    await news_cog.poll_matches()
    assert news_cog.fetcher.fetches == [club]


async def case_ticket_clock_moves_only_after_the_round_finishes(news_cog, news_store, news_guild):
    news_cog._ticket_due_at = 0
    set_follows(news_store, GUILD_ID, ("weibo", "1195230310", FEATURE_TICKET))
    bot_guilds(news_cog, news_guild)
    _, ticket, _ = news_cog._scheduled(time.monotonic())
    assert ticket is True
    assert news_cog._ticket_due_at == 0

    original = news_cog.poll_once

    async def down(**_kwargs):
        raise ConnectionError("down")

    news_cog.poll_once = down
    try:
        with pytest.raises(ConnectionError):
            await news_cog.poll_feeds()
        assert news_cog._ticket_due_at == 0
        _, ticket, _ = news_cog._scheduled(time.monotonic())
        assert ticket is True
    finally:
        news_cog.poll_once = original
    before = time.monotonic()
    await news_cog.poll_feeds()
    assert news_cog._ticket_due_at >= before + TICKET_POLL_SECONDS
    _, ticket, _ = news_cog._scheduled(news_cog._ticket_due_at - 1)
    assert ticket is False


async def case_a_blocked_weibo_round_retries_missed_tickets(news_cog, news_store, news_guild):
    first, second = "1195230310", "1630856882"
    set_follows(
        news_store,
        GUILD_ID,
        ("weibo", first, FEATURE_TICKET),
        ("weibo", second, FEATURE_TICKET),
    )
    bot_guilds(news_cog, news_guild)
    news_cog._ticket_due_at = 0
    fetches: list[str] = []

    async def blocked(_platform, account, **_kwargs):
        fetches.append(account)
        if account == first:
            raise FetchError("HTTP 432 from api", status=432)
        return PullResult(account, [], [])

    script_fetcher(news_cog).pull = blocked
    try:
        await news_cog.poll_feeds()
        assert fetches == [first]
        assert news_cog._ticket_due_at == 0
        _, ticket, _ = news_cog._scheduled(time.monotonic())
        assert ticket is True
    finally:
        weibo_rest.take("")


async def case_weibo_face_and_cover_are_attached(news_cog, news_store, news_guild, news_channel, monkeypatch):
    face = CardPicture(b"face-bytes", "face.jpg")
    cover = CardPicture(b"cover-bytes", "cover.jpg")

    async def fake_face(_item):
        return face

    async def fake_cover(_item):
        return cover

    monkeypatch.setattr("news.source.post.common.author_face", fake_face)
    monkeypatch.setattr("news.source.post.common.card_picture", fake_cover)
    second_guild, second_channel = announce_guild(news_store, 3, 57)
    set_follows(news_store, GUILD_ID, ("weibo", ACCOUNT, FEATURE_OTHER))
    set_follows(news_store, 3, ("weibo", ACCOUNT, FEATURE_OTHER))
    weibo = fetched_item(
        "guid-wb",
        platform="weibo",
        source_name="Studio",
        author_icon="https://tvax1.sinaimg.cn/crop/s.jpg",
        image_url="https://wx1.sinaimg.cn/large/pic.jpg",
    )
    assert await poll_news(news_cog, news_guild, second_guild, new=[weibo]) == 2
    for channel in (news_channel, second_channel):
        kwargs = channel.send.await_args.kwargs
        sent = kwargs["files"]
        assert [item.filename for item in sent] == ["face.jpg", "cover.jpg"]
        assert sent[0].fp.read() == b"face-bytes"
        assert sent[1].fp.read() == b"cover-bytes"
        assert kwargs["embed"].author.icon_url == "attachment://face.jpg"
        assert kwargs["embed"].image.url == "attachment://cover.jpg"


async def test_tickets_and_damai(
    monkeypatch, news_cog, news_store, news_guild, news_channel
):
    await case_ticket_polls(monkeypatch, news_cog, news_store, news_guild, news_channel)
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_ticket_clock_moves_only_after_the_round_finishes(news_cog, news_store, news_guild)
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_a_blocked_weibo_round_retries_missed_tickets(news_cog, news_store, news_guild)
    fresh_post(news_cog, news_store, news_guild, news_channel)
    with monkeypatch.context() as patched:
        await case_damai_retries_until_a_pull_finishes(
            news_cog, news_store, news_guild, patched
        )
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_damai_shares_weibo_cooldown_and_cookie_warning(
        news_cog, news_store, news_guild, news_channel
    )


async def case_ticket_polls(monkeypatch, news_cog, news_store, news_guild, news_channel):
    with monkeypatch.context() as mp:
        await case_poll_ticket_shows_dedup(news_cog, news_store, news_guild, news_channel, mp)
    news_channel.send.reset_mock()
    with monkeypatch.context() as mp:
        await case_poll_reads_damai_weibo_for_a_new_sale(news_cog, news_store, news_guild, news_channel, mp)
    news_channel.send.reset_mock()
    with monkeypatch.context() as mp:
        await case_poll_keeps_a_ticket_follow_when_the_artist_is_blank(news_cog, news_store, news_guild, news_channel, mp)


async def case_poll_ticket_shows_dedup(news_cog, news_store, news_guild, news_channel, monkeypatch):
    news_cog._ai_on_poll = True
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_TICKET))
    show1 = TicketShow("周杰伦", "9月20日", "北京")
    show2 = TicketShow("周杰伦", "9月21日", "上海")
    show3 = TicketShow("周杰伦", "9月22日", "广州")
    draft_a = TicketDraft(
        title="周杰伦 | 演唱会",
        about="两站。",
        window="",
        shows=(show1, show2),
    )
    draft_b = TicketDraft(
        title="周杰伦 | 演唱会",
        about="续宣。",
        window="",
        shows=(show2, show3),
    )
    drafts = [draft_a, draft_b]

    async def fake_fields(*_args, **_kwargs):
        return drafts.pop(0)

    monkeypatch.setattr("news.drafts.card_fields", fake_fields)
    monkeypatch.setattr(
        "news.source.post.common.card_picture", AsyncMock(return_value=None)
    )

    post_a = fetched_item("guid-ticket-a", title="演唱会", text="9月20日北京 9月21日上海")
    post_b = fetched_item("guid-ticket-b", title="演唱会", text="9月21日上海 9月22日广州")
    fetcher = script_fetcher(news_cog, new=[post_a])
    assert await poll_news(news_cog, news_guild) == 1
    assert news_channel.send.await_count == 1

    fetcher.new = [post_b]
    assert await news_cog.poll_once() == 1
    assert news_channel.send.await_count == 2

    fetcher.new = [post_b]
    assert await news_cog.poll_once() == 0
    assert news_channel.send.await_count == 2

    ref = f"{GUILD_ID}:{FEATURE_TICKET}"
    for key in (show2.remember_key, show3.remember_key):
        assert key is not None
        assert ref in fetcher.delivered_refs("bilibili", ACCOUNT, key)
    assert ref in fetcher.delivered_refs("bilibili", ACCOUNT, "guid-ticket-b")

    fetcher._delivered.pop(("bilibili", ACCOUNT, "guid-ticket-b"), None)
    drafts.append(draft_b)
    live = [(GUILD_ID, FEATURE_TICKET)]
    assert await news_cog._announce_item("bilibili", ACCOUNT, live, post_b) == 0
    assert news_channel.send.await_count == 2
    assert ref in fetcher.delivered_refs("bilibili", ACCOUNT, "guid-ticket-b")


async def case_poll_reads_damai_weibo_for_a_new_sale(news_cog, news_store, news_guild, news_channel, monkeypatch):
    news_cog._ai_on_poll = True
    news_cog.ai = ScriptedAI()
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_TICKET))
    monkeypatch.setattr(
        "news.source.post.common.card_picture", AsyncMock(return_value=None)
    )

    async def artist(_ai, _display, _certification, _bio):
        return ArtistRead(name=ArtistName("周杰伦", "周杰伦"), answered=True)

    sale = TicketShow("周杰伦", "9月20日", "北京", window="9月10日 20:00二开")
    draft = TicketDraft("周杰伦 | 嘉年华", "", "9月10日 20:00二开", (sale,))

    async def fields(_ai, _filt, _write, _lock, item, _topic, **_kwargs):
        if item.item_id == "damai-sale":
            return draft
        return None

    monkeypatch.setattr("news.source.post.weibo.ticket.one_artist", artist)
    monkeypatch.setattr("news.drafts.card_fields", fields)
    posted = fetched_item("damai-sale", title="二开", text="周杰伦 北京")

    async def pull(_platform, account, **_kwargs):
        if account == DAMAI_WEIBO:
            return PullResult("大麦官博", [posted], [posted])
        return PullResult("周杰伦", [], [])

    script_fetcher(news_cog).pull = pull
    news_cog._damai_due_at = 0
    bot_guilds(news_cog, news_guild)
    assert await news_cog.poll_once() == 1
    assert news_channel.send.await_count == 1
    assert news_channel.send.await_args.kwargs["embed"].title == "周杰伦 | 嘉年华"


async def case_poll_keeps_a_ticket_follow_when_the_artist_is_blank(news_cog, news_store, news_guild, news_channel, monkeypatch):
    news_cog._ai_on_poll = True
    news_cog.ai = ScriptedAI(replies=[FILTER_KEEP, FILTER_KEEP, WRITE_JSON])
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_TICKET, "厂牌"))
    follows = list_news_follows(news_store, GUILD_ID)
    follows.append(
        {
            "platform": "bilibili",
            "account": ACCOUNT,
            "topic": FEATURE_GAME,
            "label": "厂牌",
        }
    )
    news_store.set_guild_list(GUILD_ID, "news_follows", follows)
    ticket = fake_text_channel(55, guild=news_guild)
    news_store.set_feature_announce_channel_id(GUILD_ID, FEATURE_TICKET, ticket.id)
    map_channels(news_guild, news_channel, ticket)

    async def artist(*_args):
        return ArtistRead(answered=True)

    monkeypatch.setattr("news.source.post.weibo.ticket.one_artist", artist)

    async def pull(_platform, _account, **_kwargs):
        return PullResult("厂牌", [fetched_item("sale", title="开票")], [])

    fetcher = script_fetcher(news_cog)
    fetcher.pull = pull
    bot_guilds(news_cog, news_guild)
    assert await news_cog.poll_once() == 1
    assert news_channel.send.await_count == 1
    assert news_channel.send.await_args.kwargs["embed"].title == "AI title"
    kept_ticket = list_news_follows(news_store, GUILD_ID, FEATURE_TICKET)
    assert [item["account"] for item in kept_ticket] == [ACCOUNT]
    assert "artist" not in kept_ticket[0]
    assert list_news_follows(news_store, GUILD_ID, FEATURE_GAME)[0]["account"] == ACCOUNT
    assert list_news_follows(news_store, GUILD_ID, TOPIC_UNASSIGNED) == []

    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_TICKET, "周杰伦"))
    saved = list_news_follows(news_store, GUILD_ID, FEATURE_TICKET)[0]
    saved["artist"] = "周杰伦"
    news_store.set_guild_list(GUILD_ID, "news_follows", [saved])

    async def quiet(_platform, _account, **_kwargs):
        return PullResult("周杰伦", [], [])

    fetcher.pull = quiet
    assert await news_cog.poll_once() == 0
    kept = list_news_follows(news_store, GUILD_ID, FEATURE_TICKET)[0]
    assert kept["artist"] == "周杰伦"


async def case_damai_retries_until_a_pull_finishes(
    news_cog, news_store, news_guild, monkeypatch
):
    _arm_damai(news_cog)
    news_cog._damai_fetch_gap = DAMAI_FETCH_GAP_SECONDS
    slept = _record_sleeps(monkeypatch, "news.source.post.weibo.ticket")
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_TICKET))
    pulls = {"damai": 0}

    async def pull(_platform, account, **_kwargs):
        if account == DAMAI_WEIBO:
            pulls["damai"] += 1
            if pulls["damai"] == 1:
                raise FetchError("Could not fetch")
        return PullResult("大麦", [], [])

    fetcher = script_fetcher(news_cog)
    fetcher.pull = pull
    bot_guilds(news_cog, news_guild)
    assert await news_cog.poll_once() == 0
    assert news_cog._damai_due_at == 0
    assert pulls["damai"] == 1
    assert slept == [DAMAI_FETCH_GAP_SECONDS]

    before = time.monotonic()
    assert await news_cog.poll_once() == 0
    assert pulls["damai"] == 2
    assert news_cog._damai_due_at >= before + DAMAI_POLL_SECONDS

    news_cog._damai_due_at = 0
    sale = fetched_item("damai-miss", title="开票", text="周杰伦 北京")

    async def miss(_platform, account, **_kwargs):
        if account == DAMAI_WEIBO:
            return PullResult("大麦", [sale], [sale])
        return PullResult("周杰伦", [], [])

    fetcher.pull = miss

    async def no_fields(*_args, **_kwargs):
        return None

    monkeypatch.setattr("news.drafts.card_fields", no_fields)
    assert await news_cog.poll_once() == 0
    assert news_cog._damai_due_at == 0
    assert fetcher.seen_calls == []

    async def unsold(*_args, **_kwargs):
        return False

    monkeypatch.setattr("news.source.post.common.card_picture", AsyncMock(return_value=None))
    monkeypatch.setattr("news.source.post.weibo.ticket.send_sale", unsold)
    show = TicketShow("周杰伦", "9月20日", "北京", window="8月28日 18:00开票")
    draft = TicketDraft("周杰伦 | 嘉年华", "", "8月28日 18:00开票", (show,))

    async def fields(*_args, **_kwargs):
        return draft

    monkeypatch.setattr("news.drafts.card_fields", fields)
    assert await news_cog.poll_once() == 0
    assert news_cog._damai_due_at == 0
    assert fetcher.seen_calls == []


async def case_damai_shares_weibo_cooldown_and_cookie_warning(
    news_cog, news_store, news_guild, news_channel
):
    _arm_damai(news_cog)
    set_follows(news_store, GUILD_ID, ("weibo", "1195230310", FEATURE_TICKET))
    fetches: list[tuple[str, str]] = []

    async def cooled(_platform, account, **_kwargs):
        fetches.append(("weibo", account))
        if account != DAMAI_WEIBO:
            raise FetchError("HTTP 432 from api", status=432)
        return PullResult("大麦", [], [])

    fetcher = script_fetcher(news_cog)
    fetcher.pull = cooled
    bot_guilds(news_cog, news_guild)
    assert await news_cog.poll_once() == 0
    assert fetches == [("weibo", "1195230310")]
    assert ("weibo", DAMAI_WEIBO) not in news_cog._skip_until
    assert news_cog._damai_due_at == 0
    assert weibo_rest.take("")
    news_cog._skip_until.clear()
    _arm_damai(news_cog)
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_TICKET))

    async def expired(_platform, account, **_kwargs):
        if account == DAMAI_WEIBO:
            raise FetchError("HTTP 403 from api", status=403)
        return PullResult("周杰伦", [], [])

    fetcher.pull = expired
    assert await news_cog.poll_once() == 0
    assert news_cog._damai_due_at == 0
    assert ("weibo", DAMAI_WEIBO) in news_cog._skip_until
    hint = quote_text(notice_text(auth_expired_message("weibo")))
    assert news_channel.send.await_args.kwargs["content"] == hint
    assert news_cog.notices.live(FEATURE_TICKET, GUILD_ID, f"cookie:weibo:{DAMAI_WEIBO}") is not None


async def case_missing_channel_topic_change_and_uncapped_matches(
    news_cog, news_store, news_guild, news_channel
):
    other = fake_guild(2)
    other_channel = fake_text_channel(100, guild=other)
    other.get_channel.return_value = None
    other.fetch_channel = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), {"message": "unavailable"})
    )
    news_store.set_announce_channel_id(2, other_channel.id)
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    set_follows(news_store, 2, ("bilibili", ACCOUNT, FEATURE_TICKET))
    assert await poll_news(news_cog, news_guild, other, new=[fetched_item()]) == 1
    assert news_cog.fetcher.seen_calls == []
    other.get_channel.return_value = other_channel
    assert await news_cog.poll_once() == 1
    assert news_cog.fetcher.seen_calls == [("bilibili", ACCOUNT, ["guid-1"])]

    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    set_follows(news_store, 2)
    ticket = fake_text_channel(55, guild=news_guild)
    news_store.set_feature_announce_channel_id(GUILD_ID, FEATURE_TICKET, ticket.id)
    map_channels(news_guild, news_channel, ticket)
    assert await poll_news(news_cog, news_guild, new=[fetched_item("guid-topic")]) == 1
    set_news_follow_topic(
        news_store, GUILD_ID, list_news_follows(news_store, GUILD_ID)[0], FEATURE_TICKET
    )
    news_cog.fetcher.new = []
    news_cog.fetcher.loaded = [fetched_item("guid-topic")]
    assert await news_cog.poll_once() == 0
    assert ticket.send.await_count == 0

    set_follows(news_store, GUILD_ID, ("thesportsdb", "133604", FEATURE_MATCH))
    extra = FLOOD_CAP + 2
    sent = news_channel.send.await_count
    assert (
        await poll_news(
            news_cog,
            new=[
                soccer_match(
                    "end", item_id=f"{i}:end", title=f"Match {i}", text="**Full time** | 1–0"
                )
                for i in range(extra)
            ],
        )
        == extra
    )
    assert news_channel.send.await_count == sent + extra


STREAM_KEEP = (
    '{"type":"stream","facts":{"start":"特别前瞻节目将于9月12日19:00开播"}}'
)
WRITE_PATCH = '{"title": "Patch", "summary": "Ships tomorrow"}'


async def test_ai_streams_and_edits(news_cog, news_store, news_guild, news_channel):
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_a_stream_teaser_posts_once(news_cog, news_store, news_guild)
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_an_edited_drop_is_decided_again(news_cog, news_store, news_guild, news_channel)
    fresh_post(news_cog, news_store, news_guild, news_channel)
    await case_ai_filters_rewrites_and_retries(news_cog, news_store, news_guild, news_channel)


async def _one_teaser(cog, store, guild, *, keep: str, body: str, rows) -> None:
    """A teaser posts once. A later teaser for that show does not. The show day does."""
    cog._ai_on_poll = True
    set_follows(store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    cn = timezone(timedelta(hours=8))
    made = [
        fetched_item(item_id, title=title, text=body, occurred_at=datetime(*parts, tzinfo=cn))
        for item_id, title, parts in rows
    ]
    cog.ai = ScriptedAI(replies=[keep, WRITE_JSON])
    fetcher = script_fetcher(cog, new=[made[0]])
    assert await poll_news(cog, guild) == 1
    for item, posted in zip(made[1:], (0, 1), strict=True):
        cog.ai = ScriptedAI(replies=[keep, WRITE_JSON])
        fetcher.new = [item]
        assert await cog.poll_once() == posted


async def case_a_stream_teaser_posts_once(news_cog, news_store, news_guild):
    await _one_teaser(
        news_cog,
        news_store,
        news_guild,
        keep=STREAM_KEEP,
        body="特别前瞻节目将于9月12日19:00开播",
        rows=(
            ("guid-preview", "特别前瞻节目预告", (2026, 9, 10, 12)),
            ("guid-pv", "银幕楔引PV 现已发布", (2026, 9, 11, 12)),
            ("guid-day", "特别前瞻节目", (2026, 9, 12, 18)),
        ),
    )


async def case_an_edited_drop_is_decided_again(
    news_cog, news_store, news_guild, news_channel
):
    news_cog._ai_on_poll = True
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    original = fetched_item("guid-edit", title="Notice", text="nothing to see")
    news_cog.ai = ScriptedAI(replies=[FILTER_DROP])
    fetcher = script_fetcher(news_cog, new=[original])
    assert await poll_news(news_cog, news_guild) == 0
    assert news_channel.send.await_count == 0

    news_cog.ai = ScriptedAI(replies=[FILTER_KEEP, WRITE_JSON])
    fetcher.new = []
    fetcher.loaded = [fetched_item("guid-edit", title="Notice", text="nothing to see")]
    assert await news_cog.poll_once() == 0
    assert news_cog.ai.calls == []

    fetcher.loaded = [fetched_item(
        "guid-edit", title="Notice", text="Predownload is open for 5 hours.",
    )]
    assert await news_cog.poll_once() == 1
    assert news_channel.send.await_args.kwargs["embed"].title == "AI title"


async def case_ai_filters_rewrites_and_retries(news_cog, news_store, news_guild, news_channel):
    news_cog._ai_on_poll = True
    other, other_channel = announce_guild(news_store, 2, 100)
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    set_follows(news_store, 2, ("bilibili", ACCOUNT, FEATURE_TICKET))
    script_fetcher(news_cog, new=[fetched_item(title="Predownload 5 hours", text="A" * 1000)])
    news_cog.ai = ScriptedAI(replies=[FILTER_KEEP, WRITE_PATCH, FILTER_DROP])
    assert await poll_news(news_cog, news_guild, other) == 1
    assert news_channel.send.await_count == 1
    assert other_channel.send.await_count == 0
    assert "A" * FILTER_PEEK_CHARS in news_cog.ai.calls[0][1]["content"]
    assert "about is the kind in a few words" in news_cog.ai.calls[1][0]["content"]

    set_follows(news_store, 2)
    news_cog.ai = ScriptedAI(replies=["maybe", FILTER_KEEP, WRITE_JSON])
    assert await poll_news(news_cog, news_guild, new=[fetched_item("retry")]) == 0
    assert await news_cog.poll_once() == 1

    news_cog.ai = ScriptedAI(replies=[FILTER_KEEP, "not json"])
    sent = news_channel.send.await_count
    assert await poll_news(news_cog, new=[fetched_item("guid-write")]) == 0
    assert news_channel.send.await_count == sent
    news_cog.ai = ScriptedAI(replies=[WRITE_JSON])
    assert await news_cog.poll_once() == 1

    news_cog.ai = ScriptedAI(replies=[FILTER_KEEP, WRITE_JSON])
    ok = news_channel.send.return_value
    news_channel.send.side_effect = [
        discord.HTTPException(MagicMock(status=500), {"message": "fail"}),
        ok,
    ]
    assert await poll_news(news_cog, new=[fetched_item("send")]) == 0
    calls = len(news_cog.ai.calls)
    news_channel.send.side_effect = None
    assert await news_cog.poll_once() == 1
    assert len(news_cog.ai.calls) == calls

    news_cog.ai = ScriptedAI(replies=[FILTER_DROP], available=False)
    assert await poll_news(news_cog, new=[fetched_item("excerpt")]) == 1

    item = fetched_item("guid-check")
    news_cog._filter_cache.clear()
    news_cog._write_cache.clear()
    news_cog.ai = ScriptedAI(replies=[FILTER_DROP])
    script_fetcher(news_cog, latest=[item], new=[item])
    ctx = fake_context(news_guild)
    await news_cog.news_check.callback(news_cog, ctx, ACCOUNT)
    assert "Dropped" in ctx.send.await_args.args[0]
    assert ctx.send.await_args.kwargs["embed"].title == "Patch notes"
    assert news_cog._filter_cache == {}
    news_cog.ai = ScriptedAI(replies=[FILTER_KEEP, WRITE_JSON])
    assert await news_cog.poll_once() == 1
