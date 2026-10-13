"""Setup shared by news tests. Production code does not import this."""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord

from ai import AI
from news.follows import (
    FEATURE_GAME,
    FEATURE_MATCH,
    FOLLOWS_KEY,
    NEWS_FEATURES,
    fill_news_follow,
)
from news.source.post.bilibili.browser import clear_wbi_key
from news.source.post.bilibili.round import BiliState
from news.source.post.common import weibo_rest
from news.source.post.weibo.ticket import ArtistName, DamaiWatch, WatchedArtist
from news.source.registry import FLOOD_CAP
from tests.helpers import (
    ACCOUNT,
    COOKIE,
    GUILD_ID,
    ScriptedAI,
    bot_guilds,
    fetched_item,
    poll_news,
    script_fetcher,
    set_follows,
    soccer_match,
)


def reset_saved() -> None:
    """Drop a Weibo sit-out and a cached WBI key between cases."""
    clear_wbi_key()
    weibo_rest._out = set()
    weibo_rest._save()


def reset_channel(channel) -> None:
    posted = MagicMock()
    posted.id = channel.id * 1000
    posted.channel = channel
    posted.edit = AsyncMock()
    posted.delete = AsyncMock()
    posted.fetch = AsyncMock(return_value=posted)
    channel.send = AsyncMock(return_value=posted)
    channel.get_partial_message = MagicMock(return_value=posted)
    channel._known_cards = {}


def fresh_post(cog, store, guild, channel) -> None:
    """One clean post environment on a cog the next case reuses."""
    for guild_id in (GUILD_ID, 2, 3):
        store.set_guild_list(guild_id, FOLLOWS_KEY, [])
        for name in NEWS_FEATURES:
            store.clear_feature_announce_channel_id(guild_id, name)
    store.set_announce_channel_id(GUILD_ID, channel.id)
    cog.notices._notices.clear()
    cog.notices._posts.clear()
    cog.notices.save()
    cog._sending.clear()
    cog._filter_cache.clear()
    cog._write_cache.clear()
    cog._seen_live.clear()
    cog._skip_until.clear()
    cog._bili_stopped.clear()
    cog._weibo_stopped.clear()
    cog._weibo_quiet.clear()
    cog._ticket_due_at = cog._damai_due_at = 0.0
    cog._ticket_held = False
    cog._match_full_at = 0.0
    cog._poll_match = True
    cog._poll_post = True
    cog._start_polling = False
    cog._ai_on_poll = False
    cog.ai = AI()
    cog._damai = DamaiWatch()
    cog._bili_state = BiliState()
    cog._post_fetch_gap = 0
    cog._ticket_fetch_gap = 0
    cog._damai_fetch_gap = 0
    cog._match_fetch_gap = 0
    script_fetcher(cog)
    reset_saved()
    reset_channel(channel)
    guild.get_channel.side_effect = None
    guild.get_channel.return_value = channel
    guild.fetch_channel = AsyncMock(
        side_effect=discord.NotFound(MagicMock(), {"message": "Unknown Channel"})
    )
    cog.bot.get_guild = MagicMock(return_value=None)
    cog.bot.get_channel = MagicMock(return_value=None)


def stage(cog):
    return cog.notices.live(FEATURE_MATCH, GUILD_ID, "10")


def fill_club(store, account, fetched_at, kickoff, guild_id=GUILD_ID):
    fill_news_follow(
        store,
        guild_id,
        "thesportsdb",
        account,
        fetched_at=fetched_at,
        next_kickoff=kickoff,
    )


def record_sleeps(monkeypatch, where: str = "news.rounds") -> list[float]:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(f"{where}.asyncio.sleep", sleep)
    return slept


async def warn_cookie(cog, seconds: float = 300.0) -> None:
    await cog._warn_cookie(("bilibili", ACCOUNT), [(GUILD_ID, FEATURE_GAME)], COOKIE, seconds)


def arm_cards(channel, ids) -> list:
    known = getattr(channel, "_known_cards", {})
    messages = []
    for message_id in ids:
        message = MagicMock()
        message.id = message_id
        message.channel = channel
        message.delete = AsyncMock()
        message.edit = AsyncMock()
        known[message_id] = message
        messages.append(message)
    channel._known_cards = known
    channel.send = AsyncMock(side_effect=messages)
    channel.get_partial_message = MagicMock(side_effect=known.get)
    return messages


def one_card(channel, message_id: int):
    message = arm_cards(channel, [message_id])[0]
    channel.send = AsyncMock(return_value=message)
    return message


async def post_batches(cog, guild, item_ids, *, platform: str = "bilibili") -> None:
    posted = 0
    while posted < len(item_ids):
        batch = item_ids[posted : posted + FLOOD_CAP]
        items = [fetched_item(item_id, platform=platform) for item_id in batch]
        assert await poll_news(cog, guild, new=items) == len(batch)
        posted += len(batch)


async def match_round(cog, store, guild, monkeypatch, club, stage_name, *idle):
    """One match round for a club whose kickoff is now. Returns the sleeps it took."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    platform, account = club
    set_follows(store, GUILD_ID, (platform, account, FEATURE_MATCH), *idle)
    fill_news_follow(store, GUILD_ID, platform, account, fetched_at=now, next_kickoff=now)
    bot_guilds(cog, guild)
    script_fetcher(
        cog,
        new=[soccer_match(stage_name, occurred_at=now, platform=platform, account=account)],
        kickoffs=(now,),
    )
    cog._rng = random.Random(0)
    cog._match_full_at = time.monotonic() + 3600
    slept = record_sleeps(monkeypatch, "cogs.news_notify")
    await cog.poll_matches()
    assert cog.fetcher.fetches == [club]
    return slept


def arm_damai(cog) -> None:
    cog._ai_on_poll = True
    cog.ai = ScriptedAI()
    cog._damai_due_at = 0
    cog._damai._watched.append(
        WatchedArtist(GUILD_ID, "bilibili", ACCOUNT, ArtistName("周杰伦", "周杰伦"))
    )
