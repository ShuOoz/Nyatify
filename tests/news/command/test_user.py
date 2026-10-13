from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from announce import COMMAND_REPLY_SECONDS
from fetch import BookmarkStore, FetchError
from news.command.user import (
    NEWS_SHOW_SECONDS,
    add_account_choices,
    add_topic_choices,
    chosen_platform,
)
from news.feeds import Fetcher
from news.follows import (
    FEATURE_GAME,
    FEATURE_MATCH,
    FEATURE_OTHER,
    FEATURE_TICKET,
    FOLLOWS_KEY,
    TOPIC_UNASSIGNED,
    list_news_follows,
    news_follow_ref,
)
from tests.helpers import (
    ACCOUNT,
    GUILD_ID,
    OTHER,
    ScriptedAI,
    bot_guilds,
    embed_fields,
    fake_context,
    fetched_item,
    reply_text,
    script_fetcher,
    set_follows,
    soccer_match,
)

WEIBO = "1195230310"
POST_TOPICS = (FEATURE_GAME, FEATURE_TICKET, FEATURE_OTHER)


def _arsenal(cog, kickoff):
    script_fetcher(
        cog,
        source_name="Arsenal",
        loaded=[soccer_match("next", occurred_at=kickoff, source_name="Arsenal")],
        kickoffs=kickoff,
    )

def _slash(guild, command, focused, **values):
    interaction = MagicMock()
    interaction.guild = guild
    interaction.namespace.platform = values.get("platform")
    interaction.namespace.account = values.get("account")
    options = [{"name": name, "type": 3, "value": value} for name, value in values.items()]
    if not any(item["name"] == focused for item in options):
        options.append({"name": focused, "type": 3, "focused": True, "value": ""})
    else:
        for item in options:
            if item["name"] == focused:
                item["focused"] = True
    interaction.data = {
        "name": "news",
        "options": [{"name": command, "type": 1, "options": options}],
    }
    return interaction

async def test_show_and_autocomplete(news_cog, news_store, news_guild):
    cog, store, guild = news_cog, news_store, news_guild
    ctx = fake_context(guild)
    await cog.news_show.callback(cog, ctx)
    assert "No follows yet" in reply_text(ctx)
    assert "/news add" in reply_text(ctx)
    assert "/news check" in reply_text(ctx)

    set_follows(
        store,
        GUILD_ID,
        ("weibo", WEIBO, FEATURE_GAME, "weibo-lab"),
        ("bilibili", ACCOUNT, FEATURE_GAME, "studio"),
        ("bilibili", OTHER, FEATURE_OTHER, "other"),
        ("thesportsdb", "Philadelphia 76ers", FEATURE_MATCH),
        ("thesportsdb", "Barcelona", FEATURE_MATCH),
        ("thesportsdb", "Los Angeles Lakers", FEATURE_MATCH),
        ("thesportsdb", "Real Madrid", FEATURE_MATCH),
        ("pandascore", "paper-rex-valorant", FEATURE_MATCH, "Paper Rex"),
        ("pandascore", "t1", FEATURE_MATCH, "T1"),
        ("pandascore", "vitality-cs-go", FEATURE_MATCH, "Vitality"),
    )
    follows = list_news_follows(store, GUILD_ID)
    stamps = {
        ("weibo", WEIBO): "2026-09-05T10:00:00+00:00",
        ("bilibili", ACCOUNT): "2026-09-05T12:00:00+00:00",
    }
    for follow in follows:
        stamp = stamps.get((follow["platform"], follow["account"]))
        if stamp:
            follow["added_at"] = stamp
        if follow["platform"] == "thesportsdb":
            follow.pop("added_at", None)
        if follow["platform"] == "pandascore":
            follow["sport"] = {
                "paper-rex-valorant": "Valorant",
                "t1": "LoL",
                "vitality-cs-go": "Counter-Strike",
            }[follow["account"]]
    store.set_guild_list(GUILD_ID, FOLLOWS_KEY, follows)
    await cog.news_show.callback(cog, ctx)
    fields = embed_fields(ctx.send.await_args.kwargs["embed"])
    assert list(fields) == ["Unassigned", "Game", "Match", "Ticket", "Other"]
    assert fields["Game"] == "bil - studio\nwb - weibo-lab"
    assert "bilibili ·" not in fields["Game"]
    assert fields["Ticket"] == "none"
    assert fields["Other"] == "bil - other"
    match = fields["Match"]
    assert "sdb - Philadelphia 76ers" in match
    assert "pds - Paper Rex · Valorant" in match
    assert "pds - T1 · LoL" in match
    assert "pds - Vitality · Counter-Strike" in match
    assert "thesportsdb" not in match.lower()
    assert "Basketball ·" not in match
    assert match.index("Philadelphia 76ers") < match.index("Los Angeles Lakers") < match.index(
        "Paper Rex"
    ) < match.index("T1") < match.index("Vitality") < match.index("Barcelona") < match.index(
        "Real Madrid"
    )
    assert ctx.send.await_args.kwargs["delete_after"] == NEWS_SHOW_SECONDS

    interaction = MagicMock()
    interaction.guild = guild
    names = [choice.name for choice in cog._followed_account_choices(interaction, "")]
    assert "Philadelphia 76ers" in names and "studio" in names
    assert "Paper Rex · Valorant" in names
    assert "T1 · LoL" in names
    assert "Vitality · Counter-Strike" in names

    assert add_account_choices(None, "99") == []
    assert [choice.value for choice in add_account_choices("bilibili", "1265652806")] == [
        "1265652806"
    ]
    clubs = add_account_choices("thesportsdb", "gunners")
    assert any("Arsenal" in choice.name for choice in clubs)
    fuzzy = add_account_choices("thesportsdb", "los angelos")
    assert any("Lakers" in choice.name and "Basketball" in choice.name for choice in fuzzy)
    assert any("Kings" in choice.name and "Ice Hockey" in choice.name for choice in fuzzy)
    assert [choice.value for choice in add_topic_choices("thesportsdb", "game")] == [FEATURE_MATCH]
    for platform in ("bilibili", "weibo"):
        assert [choice.value for choice in add_topic_choices(platform, "")] == list(POST_TOPICS)
        assert FEATURE_MATCH not in [choice.value for choice in add_topic_choices(platform, "match")]

    interaction = _slash(guild, "add", "topic", platform="thesportsdb")
    assert chosen_platform(interaction) == "thesportsdb"
    interaction = _slash(guild, "add", "account")
    assert await cog.news_add_account_autocomplete(interaction, "99") == []
    interaction = _slash(guild, "add", "account", platform="bilibili")
    assert [
        choice.value for choice in await cog.news_add_account_autocomplete(interaction, "99")
    ] == ["99"]
    interaction = _slash(guild, "add", "account", platform="thesportsdb")
    assert any(
        "Arsenal" in choice.name
        for choice in await cog.news_add_account_autocomplete(interaction, "gunners")
    )
    assert list(cog.news_add._params) == ["platform", "account"]
    interaction = _slash(guild, "feature", "topic")
    assert await cog.news_feature_topic_autocomplete(interaction, "") == []
    studio = next(item for item in list_news_follows(store, GUILD_ID) if item["account"] == ACCOUNT)
    interaction = _slash(guild, "feature", "topic", account=news_follow_ref(studio))
    assert [
        choice.value for choice in await cog.news_feature_topic_autocomplete(interaction, "")
    ] == list(POST_TOPICS)
    set_follows(
        store,
        GUILD_ID,
        ("bilibili", ACCOUNT, FEATURE_GAME, "studio"),
        ("bilibili", OTHER, TOPIC_UNASSIGNED, "waiting"),
    )
    names = [choice.name for choice in cog._followed_account_choices(interaction, "")]
    assert names.index("waiting") < names.index("studio")

async def test_ticket_follow_edges(news_cog, news_store, news_guild):
    await case_ticket_studio_search_keeps_the_account_name(news_cog, news_store, news_guild)
    await case_feature_ticket_again_drops_a_follow_with_no_artist(news_cog, news_store, news_guild)


async def case_ticket_studio_search_keeps_the_account_name(news_cog, news_store, news_guild):
    set_follows(
        news_store,
        GUILD_ID,
        ("weibo", WEIBO, FEATURE_TICKET, "汪峰工作室"),
        ("weibo", "1195230311", FEATURE_TICKET, "一样音乐工作室"),
    )
    follows = list_news_follows(news_store, GUILD_ID)
    for follow in follows:
        if follow["label"] == "一样音乐工作室":
            follow["artist"] = "李荣浩"
        if follow["label"] == "汪峰工作室":
            follow["artist"] = "汪峰"
    news_store.set_guild_list(GUILD_ID, FOLLOWS_KEY, follows)
    interaction = MagicMock()
    interaction.guild = news_guild
    found = {choice.name for choice in news_cog._followed_account_choices(interaction, "工作室")}
    assert found == {"汪峰*", "李荣浩*"}
    assert [
        choice.name for choice in news_cog._followed_account_choices(interaction, "李荣浩")
    ] == ["李荣浩*"]

async def test_add_feature_and_delete(news_cog, news_store, news_guild, news_channel, tmp_path):
    cog, store, guild = news_cog, news_store, news_guild
    ctx = fake_context(guild)

    async def add(*args):
        await cog.news_add.callback(cog, ctx, *args)

    async def feature(*args):
        await cog.news_feature.callback(cog, ctx, *args)

    script_fetcher(
        cog,
        new=[fetched_item(platform="bilibili", account="99", source_name="Studio", image_url=None)],
    )
    cog._poll_post = False
    await add("bilibili", "https://space.bilibili.com/99")
    follow = list_news_follows(store, GUILD_ID, TOPIC_UNASSIGNED)[0]
    assert follow["account"] == "99" and follow["label"] == "Studio"
    assert "Poll off" in reply_text(ctx)
    assert "Following Studio" in reply_text(ctx)
    assert "`/news feature`" in reply_text(ctx)
    assert ctx.send.await_args.kwargs["delete_after"] == COMMAND_REPLY_SECONDS
    await cog.news_delete.callback(cog, ctx, "99")
    cog._poll_post = True
    await add("bilibili", "https://space.bilibili.com/99")
    assert "No posts until `/news feature`" in reply_text(ctx)
    assert "Poll off" not in reply_text(ctx)
    await cog.news_delete.callback(cog, ctx, "99")

    await add("bilibili")
    assert "uid" in reply_text(ctx).lower()
    await add("thesportsdb", "https://x.com/example")
    assert "club name" in reply_text(ctx)
    assert list_news_follows(store, GUILD_ID) == []

    bot_guilds(cog, guild)
    _arsenal(cog, datetime.now(timezone.utc) + timedelta(hours=2))
    await add("thesportsdb", "Arsenal")
    follow = list_news_follows(store, GUILD_ID)[0]
    assert follow["topic"] == FEATURE_MATCH
    assert follow["account"] == "133604"
    assert "Bookm" in reply_text(ctx)
    assert ctx.send.await_args.kwargs["delete_after"] == COMMAND_REPLY_SECONDS
    assert news_channel.send.await_count == 1
    await feature("Arsenal", FEATURE_TICKET)
    assert "Match platforms only use Match" in reply_text(ctx)

    await add("bilibili", "99")
    assert list_news_follows(store, GUILD_ID, TOPIC_UNASSIGNED)[0]["account"] == "99"
    await feature("99", FEATURE_MATCH)
    assert "Match is for match platforms" in reply_text(ctx)
    cog.ai = ScriptedAI(replies=['{"artist":""}'])
    await feature("99", FEATURE_TICKET)
    assert "No one artist" in reply_text(ctx)
    assert list_news_follows(store, GUILD_ID, TOPIC_UNASSIGNED)[0]["account"] == "99"
    cog.ai = ScriptedAI(replies=['{"artist":"周杰伦"}'])
    await feature("99", FEATURE_TICKET)
    saved = list_news_follows(store, GUILD_ID, FEATURE_TICKET)[0]
    assert saved["account"] == "99"
    assert saved["artist"] == "周杰伦"

    script_fetcher(
        cog,
        pull_error=FetchError("Name matches more than one team. Use thesportsdb:1, thesportsdb:2"),
    )
    await add("thesportsdb", "Rangers")
    assert not any(item["account"] == "Rangers" for item in list_news_follows(store, GUILD_ID))
    assert "more than one team" in reply_text(ctx)

    set_follows(
        store,
        GUILD_ID,
        ("bilibili", WEIBO, FEATURE_GAME),
        ("weibo", WEIBO, FEATURE_TICKET),
    )
    await feature(WEIBO, FEATURE_OTHER)
    assert "more than one platform" in reply_text(ctx)

    set_follows(store, GUILD_ID)
    cog._poll_match = False
    _arsenal(cog, datetime.now(timezone.utc) + timedelta(hours=2))
    await add("thesportsdb", "Arsenal")
    assert news_channel.send.await_count == 1
    assert "Poll off" in reply_text(ctx)

    set_follows(store, GUILD_ID, ("thesportsdb", "133999", FEATURE_MATCH))
    script_fetcher(cog, source_name="Zzzfc United", team_id="133999")
    await add("thesportsdb", "Zzzfc United")
    assert "Already following that account" in reply_text(ctx)

    set_follows(store, GUILD_ID)
    script_fetcher(cog, pull_error=FetchError("HTTP 500 from api"))
    await add("bilibili", "99")
    assert list_news_follows(store, GUILD_ID)[0]["account"] == "99"
    assert "Following" in reply_text(ctx)

    for platform, account, error, keep, expired in (
        ("thesportsdb", "Zzzfc", FetchError("No team named Zzzfc"), False, None),
        ("bilibili", "99", FetchError("HTTP 412 from api"), True, False),
        ("bilibili", "99", FetchError("code -352 from api"), True, True),
    ):
        set_follows(store, GUILD_ID)
        script_fetcher(cog, pull_error=error)
        await add(platform, account)
        assert bool(list_news_follows(store, GUILD_ID)) is keep
        if expired is not None:
            assert ("Cookie expired" in reply_text(ctx)) is expired

    set_follows(
        store,
        GUILD_ID,
        ("bilibili", ACCOUNT, FEATURE_GAME, "studio"),
        ("bilibili", OTHER, FEATURE_TICKET),
    )
    await cog.news_delete.callback(cog, ctx, ACCOUNT)
    assert [item["account"] for item in list_news_follows(store, GUILD_ID)] == [OTHER]
    set_follows(
        store,
        GUILD_ID,
        ("bilibili", ACCOUNT, FEATURE_GAME, "studio"),
        ("bilibili", OTHER, FEATURE_TICKET),
    )
    bookmarks = BookmarkStore(tmp_path / "bookmarks.json")
    bookmarks.remember("bilibili", ACCOUNT, ["guid-1"])
    bookmarks.remember("bilibili", OTHER, ["guid-2"])

    async def unused_get(_url, _headers):
        raise AssertionError("delete should not fetch")

    cog.fetcher = Fetcher(get_text=unused_get, bookmarks=bookmarks)
    await cog.news_delete.callback(cog, ctx, "studio")
    assert not bookmarks.is_seeded("bilibili", ACCOUNT)
    assert bookmarks.seen_ids("bilibili", OTHER) == {"guid-2"}

async def case_feature_ticket_again_drops_a_follow_with_no_artist(news_cog, news_store, news_guild):
    set_follows(news_store, GUILD_ID, ("bilibili", "99", FEATURE_TICKET, "厂牌"))
    script_fetcher(news_cog, source_name="厂牌")
    news_cog.ai = ScriptedAI(replies=['{"artist":""}'])
    ctx = fake_context(news_guild)
    await news_cog.news_feature.callback(news_cog, ctx, "99", FEATURE_TICKET)
    assert "No one artist" in reply_text(ctx)
    assert list_news_follows(news_store, GUILD_ID, FEATURE_TICKET) == []
    assert list_news_follows(news_store, GUILD_ID, TOPIC_UNASSIGNED)[0]["account"] == "99"
