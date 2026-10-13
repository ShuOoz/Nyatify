"""Saved announcements: timed notices and the recent-post queue."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from guild import GuildConfig
from notice import TemporaryNotice, TemporaryNotices, _pending_deletes, notices_for
from tests.helpers import FakeClock, fake_bot, fake_guild, fake_text_channel, scratch

OTHER_FEATURE = "game"
NEWS = "news"


@pytest.fixture(autouse=True)
async def _cancel_pending_deletes():
    yield
    tasks = list(_pending_deletes)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _pending_deletes.clear()


def _store_and_channel(tmp_path, guild_id: int = 1):
    guild = fake_guild(guild_id)
    channel = fake_text_channel(99, guild=guild)
    guild.get_channel.return_value = channel
    store = GuildConfig(tmp_path / "guild.json")
    store.set_announce_channel_id(guild_id, channel.id)
    return guild, channel, store


def _notices(tmp_path, clock=None, bot=None) -> TemporaryNotices:
    kwargs = {"clock": clock} if clock is not None else {}
    return TemporaryNotices(
        bot or fake_bot(), path=tmp_path / "notices.json", **kwargs
    )


def _tracked_message(channel, message_id: int, delete_side_effect=None) -> MagicMock:
    message = MagicMock()
    message.id = message_id
    message.channel = channel
    message.delete = AsyncMock(side_effect=delete_side_effect)
    return message


def _split_channel_posts(channel):
    ping = MagicMock()
    ping.id = 11
    ping.delete = AsyncMock()
    card = MagicMock()
    card.id = 22
    card.channel = channel
    card.edit = AsyncMock()
    card.delete = AsyncMock()

    def partial(message_id):
        return {ping.id: ping, card.id: card}[message_id]

    channel.send = AsyncMock(side_effect=[ping, card])
    channel.get_partial_message = MagicMock(side_effect=partial)
    ping.channel = channel
    return ping, card


async def test_notices_persist_and_replace(tmp_path):
    await case_notices_track_survive_restart_and_drop_unavailable(scratch(tmp_path, "notices_track_survive_restart_and_drop_unavailable"))
    await case_track_replace_and_resolve_message_errors(scratch(tmp_path, "track_replace_and_resolve_message_errors"))
    await case_channel_queue_drops_oldest_and_survives_restart(scratch(tmp_path, "channel_queue_drops_oldest_and_survives_restart"))


async def case_notices_track_survive_restart_and_drop_unavailable(tmp_path):
    clock = FakeClock()
    guild, channel, store = _store_and_channel(tmp_path)
    path = tmp_path / "notices.json"
    first = _notices(tmp_path, clock)
    posted = channel.send.return_value
    first.track(OTHER_FEATURE, guild.id, "same", posted, ttl=60)
    first.track(NEWS, guild.id, "same", posted, ttl=60)
    assert len(first) == 2
    assert first.live(OTHER_FEATURE, guild.id, "same") is not None
    assert first.live(NEWS, guild.id, "same") is not None
    clock.advance(60)
    restarted = _notices(tmp_path, clock, fake_bot(get_channel_result=channel))
    await restarted.delete_expired()
    channel.send.return_value.delete.assert_awaited()
    assert not restarted
    assert json.loads(path.read_text()) == {"notices": []}

    live = _notices(tmp_path, clock)
    live.track(OTHER_FEATURE, guild.id, "item", posted, ttl=60)
    live.forget_channel(channel.id)
    assert live.live(OTHER_FEATURE, guild.id, "item") is None
    assert json.loads(path.read_text()) == {"notices": []}

    first_msg = _tracked_message(channel, 1)
    second_msg = _tracked_message(channel, 2)
    live.track(NEWS, 1, "a", first_msg, ttl=60)
    live.track(NEWS, 1, "b", second_msg, ttl=60)
    assert await live.delete_now(live.get(NEWS, 1, "a")) is True
    first_msg.delete.assert_awaited_once()
    second_msg.delete.assert_not_awaited()
    assert live.get(NEWS, 1, "a") is None
    assert live.live(NEWS, 1, "b") is not None
    live.forget(NEWS, 1, "b")

    boom = _tracked_message(channel, 3, RuntimeError("discord hiccup"))
    later = _tracked_message(channel, 4)
    live.track(NEWS, 1, "c", boom, ttl=60)
    live.track(NEWS, 1, "d", later, ttl=60)
    clock.advance(60)
    await live.delete_expired()
    boom.delete.assert_awaited_once()
    later.delete.assert_awaited_once()
    assert live.get(NEWS, 1, "c") is not None
    assert live.get(NEWS, 1, "d") is None

    bot = fake_bot()
    keep = fake_guild(2)
    keep_channel = fake_text_channel(99, guild=keep)
    keep.get_channel.return_value = keep_channel
    bot.guilds = [keep]
    notices = _notices(tmp_path, bot=bot)
    kept = keep_channel.send.return_value
    notices.track(NEWS, 1, "gone", kept, ttl=60)
    notices.track(NEWS, 2, "keep", kept, ttl=60)
    notices.drop_unavailable(bot)
    assert notices.live(NEWS, 1, "gone") is None
    assert notices.live(NEWS, 2, "keep") is not None
    offline = fake_guild(3)
    offline.unavailable = True
    offline.get_channel.return_value = None
    bot.guilds = [keep, offline]
    notices.track(NEWS, 3, "outage", kept, ttl=60)
    notices.drop_unavailable(bot)
    assert notices.live(NEWS, 3, "outage") is not None

async def test_the_purge_loop_and_failed_deletes(tmp_path):
    await case_notices_store_and_purge_loop_are_shared(scratch(tmp_path, "notices_store_and_purge_loop_are_shared"))
    await case_failed_card_delete_still_deletes_ping(scratch(tmp_path, "failed_card_delete_still_deletes_ping"))


async def case_notices_store_and_purge_loop_are_shared(tmp_path):
    bot = fake_bot()
    assert notices_for(bot, path=tmp_path / "a.json") is notices_for(
        bot, path=tmp_path / "b.json"
    )
    notices = _notices(tmp_path)
    notices.start()
    notices.start()
    assert notices._cogs == 2
    notices.stop()
    assert notices._purge.is_running()
    notices.stop()
    await asyncio.sleep(0)
    assert not notices._purge.is_running()

async def case_track_replace_and_resolve_message_errors(tmp_path):
    clock = FakeClock()
    channel = fake_text_channel(99)
    bot = fake_bot(get_channel_result=channel)
    notices = TemporaryNotices(bot, path=tmp_path / "n.json", clock=clock)
    first = _tracked_message(channel, 1)
    second = _tracked_message(channel, 2)
    notices.track(NEWS, 1, "a", first, ttl=60)
    notices.track(NEWS, 1, "a", second, ttl=60)
    await asyncio.sleep(0)
    first.delete.assert_awaited()
    live = notices.get(NEWS, 1, "a")
    assert live is not None
    assert await notices.resolve_message(live) is second
    channel.get_partial_message = MagicMock(return_value="partial")
    assert await notices.resolve_message(live, message_id=11) == "partial"
    channel.get_partial_message.assert_called_with(11)
    clock.advance(60)
    assert notices.live(NEWS, 1, "a") is None
    assert notices.get(NEWS, 1, "a") is live

    stale = TemporaryNotice(
        feature=NEWS,
        guild_id=1,
        subject="gone",
        channel_id=99,
        message_id=8,
        expires_at=clock() + 60,
    )
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), {"message": "up"})
    )
    with pytest.raises(discord.HTTPException):
        await notices.resolve_message(stale)
    bot.fetch_channel = AsyncMock(
        side_effect=discord.NotFound(MagicMock(), {"message": "Unknown Channel"})
    )
    assert await notices.resolve_message(stale) is None

    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "notices": [
                    {"guild_id": 1},
                    {
                        "channel_id": 99,
                        "expires_at": clock() + 60,
                        "feature": NEWS,
                        "guild_id": 1,
                        "message_id": 3,
                        "subject": "ok",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = TemporaryNotices(fake_bot(), path=path, clock=clock)
    assert loaded.get(NEWS, 1, "ok") is not None
    assert len(loaded) == 1

async def case_channel_queue_drops_oldest_and_survives_restart(tmp_path):
    channel = fake_text_channel(99)
    other = fake_text_channel(50)
    notices = _notices(tmp_path, bot=fake_bot(get_channel_result=channel))
    messages = [_tracked_message(channel, i) for i in range(1, 5)]
    for message in messages[:3]:
        await notices.keep_recent(1, message, 3)
    assert notices.post_ids(99) == (1, 2, 3)
    await notices.keep_recent(1, messages[3], 3)
    messages[0].delete.assert_awaited_once()
    assert notices.post_ids(99) == (2, 3, 4)
    await notices.keep_recent(1, messages[3], 3)
    assert notices.post_ids(99) == (2, 3, 4)

    messages[1].delete = AsyncMock(
        side_effect=discord.NotFound(MagicMock(), {"message": "Unknown Message"})
    )
    fifth = _tracked_message(channel, 5)
    await notices.keep_recent(1, fifth, 3)
    assert notices.post_ids(99) == (3, 4, 5)
    fifth.delete.assert_not_awaited()

    messages[2].delete = AsyncMock(side_effect=RuntimeError("no"))
    sixth = _tracked_message(channel, 6)
    await notices.keep_recent(1, sixth, 3)
    assert notices.post_ids(99) == (3, 4, 5, 6)
    messages[3].delete.assert_not_awaited()
    sixth.delete.assert_not_awaited()

    path = tmp_path / "notices.json"
    restarted = TemporaryNotices(fake_bot(get_channel_result=channel), path=path)
    assert restarted.post_ids(99) == (3, 4, 5, 6)
    assert json.loads(path.read_text())["post_queue"]
    restarted.forget_channel(99)
    assert restarted.post_ids(99) == ()
    assert "post_queue" not in json.loads(path.read_text())

    await restarted.keep_recent(1, _tracked_message(channel, 7), 3)
    await restarted.keep_recent(2, _tracked_message(other, 8), 3)
    restarted.forget_guild(1)
    assert restarted.post_ids(99) == ()
    assert restarted.post_ids(50) == (8,)
    bot = fake_bot()
    bot.guilds = []
    restarted.drop_unavailable(bot)
    assert restarted.post_ids(50) == ()

async def case_failed_card_delete_still_deletes_ping(tmp_path):
    clock = FakeClock()
    channel = fake_text_channel(99)
    notices = _notices(tmp_path, clock, fake_bot(get_channel_result=channel))
    ping, card = _split_channel_posts(channel)
    card.delete = AsyncMock(side_effect=RuntimeError("discord hiccup"))
    notices.track(
        NEWS, 1, "split", card, ttl=60, extra={"ping_message_id": ping.id}
    )
    clock.advance(60)
    await notices.delete_expired()
    card.delete.assert_awaited_once()
    ping.delete.assert_awaited_once()
    assert notices.get(NEWS, 1, "split") is not None

