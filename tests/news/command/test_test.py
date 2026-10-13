from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import commands

import news.source.post.weibo.http as wbhttp
from config import settings as live_settings
from fetch import FetchError
from news.command.test import _later_than
from news.feeds import FeedSnapshot
from news.follows import (
    FEATURE_GAME,
    FEATURE_MATCH,
    FEATURE_OTHER,
    FEATURE_TICKET,
    TOPIC_UNASSIGNED,
    list_news_follows,
)
from news.source.post.bilibili.browser import BiliRefresh
from news.source.post.bilibili.dynamics import LIST
from news.source.post.common import CardPicture
from news.source.post.weibo.http import identity_cookies
from news.source.post.weibo.ticket import (
    DAMAI_WEIBO,
    ArtistName,
    ArtistRead,
    TicketDraft,
    TicketShow,
)
from tests.helpers import (
    ACCOUNT,
    COOKIE,
    FILTER_DROP,
    FILTER_KEEP,
    GUILD_ID,
    OTHER,
    WRITE_JSON,
    ScriptedAI,
    bot_guilds,
    fake_context,
    fetched_item,
    reply_text,
    scratch,
    script_fetcher,
    set_follows,
    soccer_match,
)

WEIBO = "1195230310"
VISITOR = "buvid3=minted-infoc; buvid4=xx; _uuid=u; b_nut=1"


async def test_check_autocomplete_and_errors(news_cog, news_store, news_guild):
    await case_check_autocomplete_lists_followed_accounts(news_cog, news_store, news_guild)
    await case_news_command_errors(news_cog, news_guild)


async def case_check_autocomplete_lists_followed_accounts(news_cog, news_store, news_guild):
    set_follows(
        news_store,
        GUILD_ID,
        ("bilibili", ACCOUNT, FEATURE_GAME, "studio"),
        ("bilibili", OTHER, TOPIC_UNASSIGNED, "waiting"),
    )
    interaction = MagicMock()
    interaction.guild = news_guild
    names = [choice.name for choice in news_cog._followed_account_choices(interaction, "")]
    assert [choice.name for choice in await news_cog.news_account_autocomplete(interaction, "")] == names


def _cookie_rows(embed) -> list[tuple[str, str, str]]:
    rows = []
    fields = [field for field in embed.fields if field.name != "\u200b"]
    for index in range(0, len(fields), 2):
        kind, status = fields[index], fields[index + 1]
        assert kind.inline is True and status.inline is True
        assert status.name == "Status"
        rows.append((kind.name, kind.value, status.value))
    return rows

def _cookie_env(guild, tmp_path, monkeypatch):
    config_path = tmp_path / "config.txt"
    monkeypatch.setattr("news.command.test.CONFIG_PATH", config_path)
    monkeypatch.setattr(live_settings, "bili_cookie", None)
    monkeypatch.setattr(live_settings, "weibo_cookie", None)
    return fake_context(guild), config_path

async def test_check_previews(news_cog, news_store, news_guild, monkeypatch):
    cog, store, guild = news_cog, news_store, news_guild
    ctx = fake_context(guild)

    async def check(account, **kwargs):
        if kwargs:
            script_fetcher(cog, **kwargs)
        await cog.news_check.callback(cog, ctx, account)

    await check("missing")
    assert "not in the follow list" in reply_text(ctx)
    set_follows(store, GUILD_ID, ("bilibili", ACCOUNT, TOPIC_UNASSIGNED))
    await check(ACCOUNT)
    assert "feature" in reply_text(ctx).lower()

    set_follows(store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    await check(ACCOUNT, latest=[fetched_item(), fetched_item("guid-0")])
    embed = ctx.send.await_args.kwargs["embed"]
    assert embed.title == "Patch notes"
    assert embed.footer.text == "Game Post"
    assert "News Preview" in reply_text(ctx)
    await check(
        ACCOUNT,
        latest=[fetched_item("lotto", title="互动抽奖", text="官方抽奖", image_url=None)],
    )
    assert "skip" in reply_text(ctx).lower()
    live = fetched_item(
        "live", title="今晚开播", text="https://live.bilibili.com/99", image_url=None
    )
    await check(ACCOUNT, latest=[live])
    assert "skip" in reply_text(ctx).lower()
    set_follows(store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_OTHER))
    await check(ACCOUNT, latest=[live])
    assert ctx.send.await_args.kwargs["embed"].title == "今晚开播"
    assert "News Preview" in reply_text(ctx)
    set_follows(store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    listed = fetched_item("1002", tags=(LIST,), title="List drop")
    await check(ACCOUNT, latest=[listed])
    assert ctx.send.await_args.kwargs["embed"].title == "List drop"
    set_follows(store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_OTHER))
    await check(ACCOUNT, latest=[listed])
    assert "skip" in reply_text(ctx).lower()
    set_follows(store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))

    upcoming = soccer_match(
        "next", item_id="11:next", title="Arsenal vs Liverpool", text="Kickoff."
    )
    done = soccer_match("end", item_id="9:end", title="Arsenal 2–1 City", notes="Saka 12'")
    set_follows(
        store,
        GUILD_ID,
        ("bilibili", ACCOUNT, FEATURE_GAME),
        ("thesportsdb", "133604", FEATURE_MATCH),
    )
    await check("133604", latest=[upcoming, done])
    assert ctx.send.await_args.kwargs["embed"].title == "Arsenal vs Liverpool"
    club = next(item for item in list_news_follows(store, GUILD_ID) if item["account"] == "133604")
    assert not club.get("fetched_at")

    cog._skip_until[("bilibili", ACCOUNT)] = 10**12
    await check(ACCOUNT, latest=[])
    assert ("bilibili", ACCOUNT) in cog.fetcher.fetches
    cog._skip_until.clear()

    cog._ai_on_poll = False
    cog.ai = ScriptedAI(replies=[FILTER_KEEP, WRITE_JSON])
    await check(ACCOUNT, latest=[fetched_item()])
    written = ctx.send.await_args.kwargs["embed"]
    assert written.title == "AI title"
    assert "AI body" in (written.description or "")
    assert "No account name" in cog.ai.calls[1][0]["content"]

    dropped = fetched_item("guid-new", title="Free item", text="A gift code", image_url=None)
    cog.ai = ScriptedAI(
        replies=[FILTER_DROP, FILTER_KEEP, '{"title": "Patch", "summary": "It shipped."}']
    )
    await check(ACCOUNT, latest=[dropped, fetched_item("guid-keep")])
    assert ctx.send.await_args.kwargs["embed"].title == "Patch"

    cog.ai = ScriptedAI(replies=[FILTER_DROP, WRITE_JSON])
    await check(ACCOUNT, latest=[fetched_item()])
    assert "Dropped" in reply_text(ctx)
    assert "AI_ON_POLL" not in reply_text(ctx)
    dropped_card = ctx.send.await_args.kwargs["embed"]
    assert dropped_card.title == "Patch notes"
    assert "Hello world" in (dropped_card.description or "")

    cog.ai = ScriptedAI(replies=['{"title":"","summary":"Saka 12\'"}'])
    await check("133604", latest=[done])
    recap = ctx.send.await_args.kwargs["embed"]
    assert recap.title == "Arsenal 2–1 City"
    assert "Saka 12'" in (recap.description or "")
    assert "Keep title empty" in cog.ai.calls[0][0]["content"]

    cog.ai = ScriptedAI(available=False)
    await check(ACCOUNT, latest=[fetched_item(title="Raw title")])
    assert ctx.send.await_args.kwargs["embed"].title == "Raw title"
    cached = fetched_item("guid-cached", title="Raw title")
    cog._filter_cache[(cached.platform, cached.account, cached.item_id, FEATURE_GAME)] = False
    await check(ACCOUNT, latest=[cached])
    assert "Dropped" not in reply_text(ctx)
    assert "News Preview" in reply_text(ctx)
    await check(
        ACCOUNT,
        fetch_error=FetchError(
            "code -352 from https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?key=secret"
        ),
    )
    assert "Fetch failed" in reply_text(ctx)
    assert "/news cookie" in reply_text(ctx)
    assert "secret" not in reply_text(ctx)
    await check(ACCOUNT, fetch_error=FetchError("HTTP 412 from api"))
    assert "/news cookie" not in reply_text(ctx)


    set_follows(store, GUILD_ID, ("weibo", WEIBO, FEATURE_GAME))
    cover = CardPicture(b"jpeg-bytes", "cover.jpg")

    async def fake_cover(item):
        assert item.image_url == "https://wx4.sinaimg.cn/orj480/cover.jpg"
        return cover

    monkeypatch.setattr("news.source.post.common.card_picture", fake_cover)
    cog.ai = ScriptedAI(replies=[FILTER_KEEP, WRITE_JSON])
    await check(
        WEIBO,
        latest=[
            fetched_item(
                platform="weibo",
                account=WEIBO,
                image_url="https://wx4.sinaimg.cn/orj480/cover.jpg",
                title="3.7版本 前瞻通讯",
            )
        ],
    )
    sent = ctx.send.await_args.kwargs
    assert sent["embed"].image.url == "attachment://cover.jpg"
    assert sent["file"].filename == "cover.jpg"
    assert sent["file"].fp.read() == b"jpeg-bytes"

async def test_news_cookie_commands(monkeypatch, news_cog, news_guild, news_store, news_channel, tmp_path):
    with monkeypatch.context() as mp:
        await case_news_cookie(news_cog, news_guild, scratch(tmp_path, "news_cookie"), mp)
    await case_a_written_cookie_unparks_the_site_and_takes_down_its_warning(news_cog, news_store, news_guild, news_channel)


async def case_news_cookie(news_cog, news_guild, tmp_path, monkeypatch):
    ctx, config_path = _cookie_env(news_guild, tmp_path, monkeypatch)

    async def cookie(**kwargs):
        await news_cog.news_cookie.callback(news_cog, ctx, **kwargs)

    async def fake_weibo_warmup(cookie="", **_kwargs):
        identity = identity_cookies(cookie)
        lane = wbhttp._lane()
        lane.jar.clear()
        lane.jar.update(identity)
        lane.identity_sub = identity.get("SUB", "")
        lane.warmed = True
        return dict(identity)

    monkeypatch.setattr("news.source.post.weibo.warmup_weibo", fake_weibo_warmup)

    def saved() -> str:
        return config_path.read_text(encoding="utf-8") if config_path.exists() else ""

    await cookie(platform="bilibili", login="SESSDATA=secret-cookie; bili_jct=jct; DedeUserID=123")
    assert news_cog.fetcher.bili_cookie == (
        "SESSDATA=secret-cookie; bili_jct=jct; DedeUserID=123"
    )
    assert "BILI_COOKIE=SESSDATA=secret-cookie; bili_jct=jct; DedeUserID=123" in saved()
    assert "Wrote `123`" in reply_text(ctx)
    assert "secret-cookie" not in reply_text(ctx)
    assert "BILI_COOKIE_UID=123" in saved()
    assert ctx.send.await_args.kwargs["ephemeral"] is True

    await cookie(platform="weibo", login="SUB=secret-weibo; XSRF-TOKEN=xyz")
    assert news_cog.fetcher.weibo_cookie == "SUB=secret-weibo"
    assert "WEIBO_COOKIES=SUB=secret-weibo" in saved()
    assert "XSRF-TOKEN" not in saved()
    assert "Wrote Weibo SUB" in reply_text(ctx)
    assert "secret-weibo" not in reply_text(ctx)

    await cookie(platform="bilibili", login="SESSDATA=secret; bili_jct=jct\nextra")
    assert "single line" in reply_text(ctx)
    assert "jct\nextra" not in saved()

    await cookie(platform="bilibili")
    assert "Paste a login cookie" in reply_text(ctx)
    await cookie(platform="x")
    assert "Pick Bilibili or Weibo" in reply_text(ctx)
    await cookie(login="SESSDATA=x; bili_jct=y")
    assert "Pick Bilibili or Weibo" in reply_text(ctx)

    news_cog._skip_until[("bilibili", ACCOUNT)] = 10**12
    news_cog._skip_until[("weibo", WEIBO)] = 10**12
    news_cog.bili.refreshed_until = 10**12
    await cookie(
        platform="bilibili",
        login="SESSDATA=old-secret; bili_jct=jct; DedeUserID=99; ac_time_value=old-ac",
    )
    assert "BILI_COOKIE=SESSDATA=old-secret; bili_jct=jct; DedeUserID=99" in saved()
    assert "ac_time_value" not in saved()
    assert "BILI_AC_TIME=old-ac" in saved()
    assert "old-secret" not in reply_text(ctx)
    assert ("bilibili", ACCOUNT) not in news_cog._skip_until
    assert news_cog._skip_until[("weibo", WEIBO)] == 10**12
    assert news_cog.bili.refreshed_until == 0.0

    async def fake_refresh(cookie_header, ac_time, **_kwargs):
        assert "SESSDATA=old-secret" in cookie_header and ac_time == "old-ac"
        return BiliRefresh("SESSDATA=new-secret; bili_jct=new; DedeUserID=99", "new-ac", True)

    monkeypatch.setattr("news.source.post.bilibili.refresh_bili_login_cookie", fake_refresh)
    monkeypatch.setattr(
        live_settings, "bili_cookie", "SESSDATA=old-secret; bili_jct=jct; DedeUserID=99"
    )
    monkeypatch.setattr(live_settings, "bili_ac_time", "old-ac")
    await cookie(platform="bilibili", login="buvid3=guest; b_nut=1")
    assert "SESSDATA" in reply_text(ctx)
    await cookie(platform="bilibili", login="")
    assert "SESSDATA=new-secret" in saved()
    assert "Refreshed login cookie" in reply_text(ctx)
    assert "new-secret" not in reply_text(ctx)
    assert "BILI_AC_TIME=new-ac" in saved()

    monkeypatch.setattr(live_settings, "bili_cookie", None)
    monkeypatch.setattr(live_settings, "bili_ac_time", None)
    monkeypatch.setattr(live_settings, "weibo_cookie", None)
    await cookie(platform="bilibili", login="")
    assert "Paste a login Cookie" in reply_text(ctx)

    monkeypatch.setattr(live_settings, "bili_cookie_uid", None)
    effect = {"bili": True, "weibo": True}

    async def login_effect(cookie: str, **_kwargs):
        assert "secret" in cookie
        return effect["bili"]

    async def weibo_effect(_cookie: str, **_kwargs):
        return effect["weibo"]

    monkeypatch.setattr("news.source.post.bilibili.bili_login_effective", login_effect)
    monkeypatch.setattr("news.command.test.weibo_cookie_effective", weibo_effect)

    from news.source.post.bilibili.app import app_buvid, buvid_store

    assert not buvid_store.path.exists()
    await news_cog.news_cookie.callback(news_cog, ctx)
    embed = ctx.send.await_args.kwargs["embed"]
    assert embed.title == "Cookie status"
    assert _cookie_rows(embed) == [
        ("Bilibili", "not set\nPhone id", "not set\nmissing"),
        ("Weibo", "not set", "not set"),
    ]
    assert not buvid_store.path.exists()
    assert ctx.send.await_args.kwargs["ephemeral"] is True

    phone = app_buvid("status")
    buvid_store.path.write_text(phone + "\n", encoding="utf-8")
    monkeypatch.setattr(
        live_settings, "bili_cookie", "SESSDATA=secret; bili_jct=jct; DedeUserID=99"
    )
    monkeypatch.setattr(live_settings, "bili_cookie_uid", "99")
    monkeypatch.setattr(live_settings, "bili_ac_time", "token")
    monkeypatch.setattr(live_settings, "weibo_cookie", "SUB=_2AkGuest; SUBP=p")
    await news_cog.news_cookie.callback(news_cog, ctx)
    embed = ctx.send.await_args.kwargs["embed"]
    assert _cookie_rows(embed) == [
        ("Bilibili", "Login · `99`\nPhone id", "effective\nsaved"),
        ("Weibo", "Guest", "effective"),
    ]
    shown = "\n".join(field.value for field in embed.fields)
    assert "secret" not in shown
    assert "_2AkGuest" not in shown
    assert phone not in shown

    effect["weibo"] = False
    monkeypatch.setattr(live_settings, "bili_ac_time", None)
    monkeypatch.setattr(live_settings, "bili_cookie", VISITOR)
    monkeypatch.setattr(live_settings, "weibo_cookie", "SUB=login-secret")
    await news_cog.news_cookie.callback(news_cog, ctx)
    embed = ctx.send.await_args.kwargs["embed"]
    assert _cookie_rows(embed) == [
        ("Bilibili", "not a login\nPhone id", "not used\nsaved"),
        ("Weibo", "Login", "not effective"),
    ]
    shown = "\n".join(field.value for field in embed.fields)
    assert "login-secret" not in shown
    assert "minted-infoc" not in shown

    effect["bili"] = None
    monkeypatch.setattr(
        live_settings, "bili_cookie", "SESSDATA=secret; bili_jct=jct; DedeUserID=99"
    )
    monkeypatch.setattr(live_settings, "bili_cookie_uid", "99")
    await news_cog.news_cookie.callback(news_cog, ctx)
    assert _cookie_rows(ctx.send.await_args.kwargs["embed"])[0] == (
        "Bilibili",
        "Login · `99`\nno ac_time\nPhone id",
        "unknown\n\u200b\nsaved",
    )


async def case_news_command_errors(news_cog, news_guild):
    cases = (
        (commands.NoPrivateMessage(), None, ("server",)),
        (commands.NotOwner(), news_guild, ("bot owner", "cookie")),
        (
            discord.app_commands.MissingPermissions(["manage_guild"]),
            news_guild,
            ("Manage Server", "news"),
        ),
    )
    for error, guild, snippets in cases:
        ctx = fake_context(guild)
        await news_cog.cog_app_command_error(ctx, error)
        message = reply_text(ctx)
        for snippet in snippets:
            assert snippet in message
        assert ctx.send.await_args.kwargs["ephemeral"] is True

async def _ticket_check(
    news_cog,
    news_store,
    news_guild,
    monkeypatch,
    *,
    artist,
    keep,
    fields,
    own,
    damai,
    studio,
    bio,
    quiet_before_damai=False,
):
    news_cog.ai = ScriptedAI()
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_TICKET))
    monkeypatch.setattr(
        "news.source.post.common.card_picture", AsyncMock(return_value=None)
    )

    async def one_artist(*_args):
        return ArtistRead(name=ArtistName(artist, artist), answered=True)

    monkeypatch.setattr("news.command.test.one_artist", one_artist)
    monkeypatch.setattr("news.command.test.keep_post", keep)
    monkeypatch.setattr("news.command.test.card_fields", fields)
    fetcher = script_fetcher(news_cog)
    ctx = fake_context(news_guild)
    fetches = []

    async def load(platform, account, **_kwargs):
        fetches.append((platform, account))
        if account == DAMAI_WEIBO:
            if quiet_before_damai:
                assert ctx.send.await_count == 0
            return FeedSnapshot("大麦官博", damai)
        return FeedSnapshot(studio, own, bio=bio)

    fetcher.load = load
    await news_cog.news_check.callback(news_cog, ctx, ACCOUNT)
    return ctx, fetcher, fetches

async def test_ticket_check_orders_shows_and_the_damai_backup(
    news_cog, news_store, news_guild, monkeypatch
):
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    own = fetched_item("own", title="开票", text="北京", occurred_at=now - timedelta(days=1))
    sale = fetched_item(
        "damai-sale", title="开票", text="周杰伦 北京", platform="weibo", occurred_at=now
    )
    jay = TicketDraft(
        "周杰伦 | 嘉年华",
        "",
        "8月28日 18:00开票",
        (TicketShow("周杰伦", "9月20日", "北京", window="8月28日 18:00开票"),),
    )

    async def jay_fields(_ai, _filt, _write, _lock, item, _topic, **_kwargs):
        if item.item_id == "damai-sale":
            return jay
        return ("自己的开票", "北京站")

    async def keep_all(*_args, **_kwargs):
        return True

    ctx, _, fetches = await _ticket_check(
        news_cog, news_store, news_guild, monkeypatch,
        artist="周杰伦", keep=keep_all, fields=jay_fields,
        own=[own], damai=[sale], studio="周杰伦工作室", bio="周杰伦",
        quiet_before_damai=True,
    )
    assert fetches == [("bilibili", ACCOUNT), ("weibo", DAMAI_WEIBO)]
    assert ctx.send.await_count == 1
    assert ctx.send.return_value.edit.await_count == 0
    assert "Damai backup" in ctx.send.await_args.args[0]
    assert ctx.send.await_args.kwargs["embed"].title == "周杰伦 | 嘉年华"

    assert _later_than(sale, own)
    assert not _later_than(own, sale)
    assert list_news_follows(news_store, GUILD_ID, FEATURE_TICKET)[0]["artist"] == "周杰伦"

    award = fetched_item("award", title="恭喜获奖", text="恭喜汪峰荣获年度行业成就奖", occurred_at=now)
    older_sale = fetched_item(
        "own-sale", title="开票", text="汪峰 10月1日 北京 开票", occurred_at=now - timedelta(days=2)
    )
    mention = fetched_item(
        "damai-thanks", title="感谢", text="汪峰 谢谢", platform="weibo", occurred_at=now - timedelta(days=1)
    )
    wang = TicketDraft("汪峰 | 演唱会", "开票。", "", (TicketShow("汪峰", "10月1日", "北京"),))

    async def only_sale(_ai, _cache, _lock, item, _topic, **_kwargs):
        return item.item_id == "own-sale"

    async def sale_fields(_ai, _filt, _write, _lock, item, _topic, **_kwargs):
        assert item.item_id == "own-sale"
        return wang

    ctx, _, _ = await _ticket_check(
        news_cog, news_store, news_guild, monkeypatch,
        artist="汪峰", keep=only_sale, fields=sale_fields,
        own=[award, older_sale], damai=[mention], studio="汪峰工作室", bio="汪峰",
    )
    assert "News Preview" in ctx.send.await_args.args[0]
    assert "Dropped" not in ctx.send.await_args.args[0]
    assert ctx.send.await_args.kwargs["embed"].title == "汪峰 | 演唱会"
    assert ctx.send.await_args.kwargs["embed"].description != award.text

    older = fetched_item("older-sale", title="旧开票", text="汪峰 9月1日 上海 开票", occurred_at=now - timedelta(days=3))
    newer = fetched_item("newer-sale", title="新开票", text="汪峰 10月1日 北京 开票", occurred_at=now - timedelta(days=1))
    newer_draft = TicketDraft("汪峰 | 演唱会", "", "", (TicketShow("汪峰", "10月1日", "北京"),))

    async def newer_fields(_ai, _filt, _write, _lock, item, _topic, **_kwargs):
        assert item.item_id == "newer-sale"
        return newer_draft

    ctx, _, _ = await _ticket_check(
        news_cog, news_store, news_guild, monkeypatch,
        artist="汪峰", keep=keep_all, fields=newer_fields,
        own=[older, newer], damai=[], studio="汪峰工作室", bio="汪峰",
    )
    assert ctx.send.await_args.kwargs["embed"].title == "汪峰 | 演唱会"

    async def drop_all(*_args, **_kwargs):
        return False

    async def no_fields(*_args, **_kwargs):
        raise AssertionError("a dropped post is not rewritten")

    ctx, _, _ = await _ticket_check(
        news_cog, news_store, news_guild, monkeypatch,
        artist="汪峰", keep=drop_all, fields=no_fields,
        own=[award], damai=[mention], studio="汪峰工作室", bio="汪峰",
    )
    assert "Dropped" in ctx.send.await_args.args[0]
    assert ctx.send.await_args.kwargs.get("embed") is None


async def case_a_written_cookie_unparks_the_site_and_takes_down_its_warning(news_cog, news_store, news_guild, news_channel):
    bot_guilds(news_cog, news_guild)
    set_follows(news_store, GUILD_ID, ("bilibili", ACCOUNT, FEATURE_GAME))
    await news_cog._warn_cookie(("bilibili", ACCOUNT), [(GUILD_ID, FEATURE_GAME)], COOKIE, 300.0)
    parked = time.monotonic() + 300
    news_cog._skip_until.update({("bilibili", ACCOUNT): parked, ("weibo", WEIBO): parked})
    news_cog.bili.refreshed_until = 99.0
    warning = news_channel.send.return_value
    subject = f"cookie:bilibili:{ACCOUNT}"
    assert news_cog.notices.live(FEATURE_GAME, GUILD_ID, subject) is not None

    await news_cog._resume_cookie_feed("bilibili")
    warning.delete.assert_awaited_with()
    assert news_cog.notices.live(FEATURE_GAME, GUILD_ID, subject) is None
    assert news_cog._skip_until == {("weibo", WEIBO): parked}
    assert news_cog.bili.refreshed_until == 0.0
