"""Shared announcement delivery. Not tied to voice notify."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from announce import (
    HINT_TTL_SECONDS,
    NotifyCog,
    TemporaryNotices,
    _pending_deletes,
    notices_for,
    send_announcement,
)
from guild_config import GuildConfig, canonical_announce_feature, register_announce_feature
from tests.helpers import FakeClock, fake_bot, fake_guild, fake_text_channel

OTHER_FEATURE = "game"
NEWS = "news"


@pytest.fixture(autouse=True)
def _register_news_feature():
    register_announce_feature(NEWS)


@pytest.fixture(autouse=True)
async def _cancel_pending_deletes():
    yield
    tasks = list(_pending_deletes)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _pending_deletes.clear()


async def test_any_function_uses_the_default_channel(tmp_path):
    guild = fake_guild(1)
    channel = fake_text_channel(99, guild=guild)
    guild.get_channel.return_value = channel
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_announce_channel_id(1, channel.id)

    sent = await send_announcement(
        guild,
        store,
        OTHER_FEATURE,
        content="game news",
    )

    assert sent is channel.send.return_value
    channel.send.assert_awaited_once()
    assert channel.send.await_args.kwargs["content"] == "game news"


async def test_missing_channel_hints_once_for_any_function(tmp_path):
    guild = fake_guild(1)
    hint = fake_text_channel(3, guild=guild)
    guild.system_channel = hint
    store = GuildConfig(tmp_path / "empty.json")

    first = await send_announcement(guild, store, OTHER_FEATURE, content="a")
    second = await send_announcement(guild, store, NEWS, content="b")

    assert first is None
    assert second is None
    hint.send.assert_awaited_once()
    assert "announce" in hint.send.await_args.args[0]
    assert hint.send.await_args.kwargs["delete_after"] == HINT_TTL_SECONDS


async def test_deleted_function_channel_falls_back_to_default(tmp_path):
    guild = fake_guild(1)
    default = fake_text_channel(11, guild=guild)
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_announce_channel_id(1, default.id)
    store.set_feature_announce_channel_id(1, NEWS, 22)
    guild.get_channel.side_effect = lambda channel_id: {11: default}.get(channel_id)

    sent = await send_announcement(guild, store, NEWS, content="news ping")

    assert sent is default.send.return_value
    assert store.get_feature_overrides(1) == {}
    assert default.send.await_count == 2
    hint_call = default.send.await_args_list[0]
    assert NEWS in hint_call.args[0]
    assert hint_call.kwargs["delete_after"] == HINT_TTL_SECONDS
    assert default.send.await_args_list[1].kwargs["content"] == "news ping"


async def test_send_retries_default_when_function_channel_returns_not_found(tmp_path):
    guild = fake_guild(1)
    default = fake_text_channel(11, guild=guild)
    gone = fake_text_channel(22, guild=guild)
    gone.send.side_effect = discord.NotFound(MagicMock(), {"message": "Unknown Channel"})
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_announce_channel_id(1, default.id)
    store.set_feature_announce_channel_id(1, NEWS, gone.id)
    guild.get_channel.side_effect = lambda channel_id: {
        default.id: default,
        gone.id: gone,
    }.get(channel_id)

    sent = await send_announcement(guild, store, NEWS, content="news ping")

    assert sent is default.send.return_value
    assert store.get_feature_overrides(1) == {}
    gone.send.assert_awaited_once()
    assert default.send.await_count == 2
    assert default.send.await_args_list[0].kwargs["delete_after"] == HINT_TTL_SECONDS
    assert default.send.await_args_list[1].kwargs["content"] == "news ping"


async def test_uncached_function_channel_is_fetched_and_kept(tmp_path):
    guild = fake_guild(1)
    default = fake_text_channel(11, guild=guild)
    override = fake_text_channel(22, guild=guild)
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_announce_channel_id(1, default.id)
    store.set_feature_announce_channel_id(1, NEWS, override.id)
    guild.get_channel.side_effect = lambda channel_id: {11: default}.get(channel_id)
    guild.fetch_channel = AsyncMock(return_value=override)

    sent = await send_announcement(guild, store, NEWS, content="news ping")

    assert sent is override.send.return_value
    assert store.get_feature_overrides(1) == {NEWS: override.id}
    override.send.assert_awaited_once()
    default.send.assert_not_awaited()


async def test_forbidden_send_does_not_clear_the_function_channel(tmp_path):
    guild = fake_guild(1)
    default = fake_text_channel(11, guild=guild)
    override = fake_text_channel(22, guild=guild)
    override.send.side_effect = discord.Forbidden(
        MagicMock(status=403), {"message": "Missing Permissions"}
    )
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_announce_channel_id(1, default.id)
    store.set_feature_announce_channel_id(1, NEWS, override.id)
    guild.get_channel.side_effect = lambda channel_id: {
        default.id: default,
        override.id: override,
    }.get(channel_id)

    sent = await send_announcement(guild, store, NEWS, content="news ping")

    assert sent is None
    assert store.get_feature_overrides(1) == {NEWS: override.id}
    override.send.assert_awaited_once()
    default.send.assert_not_awaited()


async def test_fetch_error_does_not_clear_or_fall_back(tmp_path):
    guild = fake_guild(1)
    default = fake_text_channel(11, guild=guild)
    hint = fake_text_channel(3, guild=guild)
    guild.system_channel = hint
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_announce_channel_id(1, default.id)
    store.set_feature_announce_channel_id(1, NEWS, 22)
    guild.get_channel.side_effect = lambda channel_id: {11: default}.get(channel_id)
    guild.fetch_channel = AsyncMock(
        side_effect=discord.HTTPException(
            MagicMock(status=500), {"message": "upstream"}
        )
    )

    sent = await send_announcement(guild, store, NEWS, content="news ping")

    assert sent is None
    assert store.get_feature_overrides(1) == {NEWS: 22}
    default.send.assert_not_awaited()
    hint.send.assert_not_awaited()


def _store_and_channel(tmp_path, guild_id: int = 1):
    guild = fake_guild(guild_id)
    channel = fake_text_channel(99, guild=guild)
    guild.get_channel.return_value = channel
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_announce_channel_id(guild_id, channel.id)
    return guild, channel, store


async def test_any_function_can_self_delete_after_ttl(tmp_path):
    clock = FakeClock()
    guild, channel, store = _store_and_channel(tmp_path)
    notices = TemporaryNotices(
        fake_bot(get_channel_result=channel),
        path=tmp_path / "notices.json",
        clock=clock,
    )

    sent = await send_announcement(
        guild,
        store,
        OTHER_FEATURE,
        content="game news",
        delete_after=60,
        notice_key="patch-1",
        notices=notices,
    )

    sent.delete.assert_not_awaited()
    assert notices.live(OTHER_FEATURE, guild.id, "patch-1") is not None

    clock.advance(60)
    await notices.delete_expired()

    sent.delete.assert_awaited_once()
    assert notices.get(OTHER_FEATURE, guild.id, "patch-1") is None


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


async def test_content_plus_embed_deletes_ping_and_card_after_ttl(tmp_path):
    clock = FakeClock()
    guild, channel, store = _store_and_channel(tmp_path)
    ping, card = _split_channel_posts(channel)
    notices = TemporaryNotices(
        fake_bot(get_channel_result=channel),
        path=tmp_path / "notices.json",
        clock=clock,
    )
    embed = discord.Embed(title="card")

    sent = await send_announcement(
        guild,
        store,
        OTHER_FEATURE,
        content="@everyone hi",
        embed=embed,
        delete_after=60,
        notice_key="patch-1",
        notices=notices,
    )

    assert sent is card
    ping_call, card_call = channel.send.await_args_list
    assert ping_call.kwargs["content"] == "@everyone hi"
    assert ping_call.kwargs.get("embed") is None
    assert card_call.kwargs["embed"] is embed
    assert card_call.kwargs.get("silent") is True
    notice = notices.live(OTHER_FEATURE, guild.id, "patch-1")
    assert notice is not None
    assert notice.extra["ping_message_id"] == ping.id

    clock.advance(60)
    await notices.delete_expired()

    card.delete.assert_awaited_once()
    ping.delete.assert_awaited_once()
    assert notices.get(OTHER_FEATURE, guild.id, "patch-1") is None


async def test_failed_card_send_deletes_the_ping(tmp_path):
    guild, channel, store = _store_and_channel(tmp_path)
    ping = MagicMock()
    ping.id = 11
    ping.delete = AsyncMock()
    channel.send = AsyncMock(
        side_effect=[
            ping,
            discord.HTTPException(MagicMock(status=500), {"message": "fail"}),
        ]
    )

    sent = await send_announcement(
        guild,
        store,
        OTHER_FEATURE,
        content="@everyone hi",
        embed=discord.Embed(title="card"),
    )

    assert sent is None
    ping.delete.assert_awaited_once()


async def test_delete_expired_continues_after_a_delete_error(tmp_path):
    clock = FakeClock()
    channel = fake_text_channel(99)
    notices = TemporaryNotices(
        fake_bot(),
        path=tmp_path / "notices.json",
        clock=clock,
    )
    first = MagicMock()
    first.id = 1
    first.channel = channel
    first.delete = AsyncMock(side_effect=RuntimeError("discord hiccup"))
    second = MagicMock()
    second.id = 2
    second.channel = channel
    second.delete = AsyncMock()
    notices.track(NEWS, 1, "a", first, ttl=60)
    notices.track(NEWS, 1, "b", second, ttl=60)

    clock.advance(60)
    await notices.delete_expired()

    first.delete.assert_awaited_once()
    second.delete.assert_awaited_once()
    assert notices.get(NEWS, 1, "a") is not None
    assert notices.get(NEWS, 1, "b") is None


async def test_two_functions_track_notices_separately(tmp_path):
    clock = FakeClock()
    guild, channel, store = _store_and_channel(tmp_path)
    notices = TemporaryNotices(
        fake_bot(),
        path=tmp_path / "notices.json",
        clock=clock,
    )

    await send_announcement(
        guild,
        store,
        OTHER_FEATURE,
        content="game",
        delete_after=60,
        notice_key="same",
        notices=notices,
    )
    await send_announcement(
        guild,
        store,
        NEWS,
        content="news",
        delete_after=60,
        notice_key="same",
        notices=notices,
    )

    assert len(notices) == 2
    assert notices.live(OTHER_FEATURE, guild.id, "same") is not None
    assert notices.live(NEWS, guild.id, "same") is not None
    assert channel.send.await_count == 2


async def test_self_delete_survives_restart(tmp_path):
    clock = FakeClock()
    guild, channel, store = _store_and_channel(tmp_path)
    path = tmp_path / "notices.json"
    first = TemporaryNotices(fake_bot(), path=path, clock=clock)
    sent = await send_announcement(
        guild,
        store,
        OTHER_FEATURE,
        content="news",
        delete_after=60,
        notice_key="item",
        notices=first,
    )
    clock.advance(60)

    restarted = TemporaryNotices(
        fake_bot(get_channel_result=channel),
        path=path,
        clock=clock,
    )
    await restarted.delete_expired()

    sent.delete.assert_awaited_once()
    assert not restarted
    assert json.loads(path.read_text()) == {"notices": []}


async def test_forget_channel_drops_notices_from_that_channel(tmp_path):
    clock = FakeClock()
    guild, channel, store = _store_and_channel(tmp_path)
    notices = TemporaryNotices(
        fake_bot(),
        path=tmp_path / "notices.json",
        clock=clock,
    )
    await send_announcement(
        guild,
        store,
        OTHER_FEATURE,
        content="news",
        delete_after=60,
        notice_key="item",
        notices=notices,
    )
    assert notices.live(OTHER_FEATURE, guild.id, "item") is not None

    notices.forget_channel(channel.id)

    assert notices.live(OTHER_FEATURE, guild.id, "item") is None
    assert json.loads((tmp_path / "notices.json").read_text()) == {"notices": []}


async def test_delete_now_drops_that_notice(tmp_path):
    clock = FakeClock()
    channel = fake_text_channel(99)
    notices = TemporaryNotices(
        fake_bot(),
        path=tmp_path / "notices.json",
        clock=clock,
    )
    first = MagicMock()
    first.id = 1
    first.channel = channel
    first.delete = AsyncMock()
    second = MagicMock()
    second.id = 2
    second.channel = channel
    second.delete = AsyncMock()
    notices.track(NEWS, 1, "a", first, ttl=60)
    notices.track(NEWS, 1, "b", second, ttl=60)

    assert await notices.delete_now(notices.get(NEWS, 1, "a")) is True

    first.delete.assert_awaited_once()
    second.delete.assert_not_awaited()
    assert notices.get(NEWS, 1, "a") is None
    assert notices.live(NEWS, 1, "b") is not None


def test_notices_for_reuses_one_store_per_bot(tmp_path):
    bot = fake_bot()
    first = notices_for(bot, path=tmp_path / "a.json")
    second = notices_for(bot, path=tmp_path / "b.json")
    assert first is second


async def test_purge_loop_start_is_shared(tmp_path):
    notices = TemporaryNotices(fake_bot(), path=tmp_path / "n.json")
    notices.start()
    notices.start()
    assert notices._loop_users == 2
    assert notices._purge.is_running()
    notices.stop()
    assert notices._loop_users == 1
    assert notices._purge.is_running()
    notices.stop()
    assert notices._loop_users == 0
    await asyncio.sleep(0)
    assert not notices._purge.is_running()


def test_drop_unavailable_drops_gone_guilds_and_keeps_the_rest(tmp_path):
    guild = fake_guild(2)
    channel = fake_text_channel(99, guild=guild)
    guild.get_channel.return_value = channel
    bot = fake_bot()
    bot.guilds = [guild]
    notices = TemporaryNotices(bot, path=tmp_path / "notices.json")
    posted = channel.send.return_value
    notices.track(NEWS, 1, "gone", posted, ttl=60)
    notices.track(NEWS, 2, "keep", posted, ttl=60)

    notices.drop_unavailable(bot)

    assert notices.live(NEWS, 1, "gone") is None
    assert notices.live(NEWS, 2, "keep") is not None


class _SampleNotify(NotifyCog):
    feature = "sample"
    feature_aliases = ("sample_alias",)


async def test_notify_cog_registers_feature_only_while_loaded(tmp_path):
    cog = _SampleNotify(fake_bot(), notices_path=tmp_path / "n.json")
    await cog.cog_load()
    assert canonical_announce_feature("sample_alias") == "sample"
    await cog.cog_unload()
    await asyncio.sleep(0)
    assert canonical_announce_feature("sample") is None
    assert canonical_announce_feature("sample_alias") is None


async def test_notify_cog_forgets_notices_when_guild_leaves(tmp_path):
    bot = fake_bot()
    cog = NotifyCog(bot, notices_path=tmp_path / "n.json")
    posted = fake_text_channel(99).send.return_value
    cog.notices.track(NEWS, 1, "item", posted, ttl=60)

    await cog.on_guild_remove(fake_guild(1))

    assert cog.notices.live(NEWS, 1, "item") is None


async def test_notify_cog_forgets_notices_when_channel_is_deleted(tmp_path):
    bot = fake_bot()
    cog = NotifyCog(bot, notices_path=tmp_path / "n.json")
    channel = fake_text_channel(99)
    cog.notices.track(NEWS, 1, "item", channel.send.return_value, ttl=60)

    await cog.on_guild_channel_delete(channel)

    assert cog.notices.live(NEWS, 1, "item") is None


async def test_notify_cog_ready_drops_missing_channels(tmp_path):
    bot = fake_bot()
    bot.guilds = []
    cog = NotifyCog(bot, notices_path=tmp_path / "n.json")
    posted = fake_text_channel(99).send.return_value
    cog.notices.track(NEWS, 1, "item", posted, ttl=60)

    await cog.on_ready()

    assert not cog.notices
