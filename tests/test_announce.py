"""Announcement delivery and slash-command replies. Not tied to voice notify."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from announce import (
    COMMAND_REPLY_SECONDS,
    HINT_TTL_SECONDS,
    notice_text,
    quote_text,
    respond,
    send_announcement,
)
from guild import GuildConfig, register_announce_feature
from notice import TemporaryNotices, _pending_deletes
from tests.helpers import FakeClock, fake_bot, fake_guild, fake_text_channel, scratch

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


async def test_where_announcements_go(tmp_path):
    await case_send_uses_default_and_hints_once_when_missing(scratch(tmp_path, "send_uses_default_and_hints_once_when_missing"))
    await case_feature_channel_resolution(scratch(tmp_path, "feature_channel_resolution"))


async def case_send_uses_default_and_hints_once_when_missing(tmp_path):
    guild, channel, store = _store_and_channel(tmp_path)
    sent = await send_announcement(guild, store, OTHER_FEATURE, content="game news")
    assert sent is channel.send.return_value
    assert channel.send.await_args.kwargs["content"] == quote_text("game news")

    empty = fake_guild(2)
    hint = fake_text_channel(3, guild=empty)
    empty.system_channel = hint
    blank = GuildConfig(tmp_path / "empty.json")
    assert await send_announcement(empty, blank, OTHER_FEATURE, content="a") is None
    assert await send_announcement(empty, blank, NEWS, content="b") is None
    hint.send.assert_awaited_once()
    assert "announce" in hint.send.await_args.args[0]
    assert hint.send.await_args.kwargs["delete_after"] == HINT_TTL_SECONDS


async def case_feature_channel_resolution(tmp_path):
    for mode in ("missing", "not_found", "uncached", "forbidden", "fetch_error"):
        root = tmp_path / mode
        root.mkdir()
        await _feature_channel_resolution(root, mode)


async def _feature_channel_resolution(tmp_path, mode):
    guild = fake_guild(1)
    default = fake_text_channel(11, guild=guild)
    override = fake_text_channel(22, guild=guild)
    store = GuildConfig(tmp_path / "guild.json")
    store.set_announce_channel_id(1, default.id)
    store.set_feature_announce_channel_id(1, NEWS, override.id if mode != "missing" else 22)

    if mode == "missing":
        guild.get_channel.side_effect = lambda channel_id: {11: default}.get(channel_id)
        sent = await send_announcement(guild, store, NEWS, content="news ping")
        assert sent is default.send.return_value
        assert store.get_feature_overrides(1) == {}
        hint, post = default.send.await_args_list
        assert "gone" in hint.args[0]
        assert post.kwargs["content"] == quote_text("news ping")
        return

    if mode == "not_found":
        override.send.side_effect = discord.NotFound(
            MagicMock(), {"message": "Unknown Channel"}
        )
        guild.get_channel.side_effect = lambda channel_id: {
            default.id: default,
            override.id: override,
        }.get(channel_id)
        sent = await send_announcement(guild, store, NEWS, content="news ping")
        assert sent is default.send.return_value
        assert store.get_feature_overrides(1) == {}
        override.send.assert_awaited_once()
        hint, post = default.send.await_args_list
        assert "gone" in hint.args[0]
        assert post.kwargs["content"] == quote_text("news ping")
        return

    if mode == "uncached":
        guild.get_channel.side_effect = lambda channel_id: {11: default}.get(channel_id)
        guild.fetch_channel = AsyncMock(return_value=override)
        sent = await send_announcement(guild, store, NEWS, content="news ping")
        assert sent is override.send.return_value
        assert store.get_feature_overrides(1) == {NEWS: override.id}
        default.send.assert_not_awaited()
        return

    if mode == "forbidden":
        override.send.side_effect = discord.Forbidden(
            MagicMock(status=403), {"message": "Missing Permissions"}
        )
        guild.get_channel.side_effect = lambda channel_id: {
            default.id: default,
            override.id: override,
        }.get(channel_id)
        sent = await send_announcement(guild, store, NEWS, content="news ping")
        assert sent is None
        assert store.get_feature_overrides(1) == {NEWS: override.id}
        default.send.assert_not_awaited()
        return

    hint = fake_text_channel(3, guild=guild)
    guild.system_channel = hint
    guild.get_channel.side_effect = lambda channel_id: {11: default}.get(channel_id)
    guild.fetch_channel = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), {"message": "upstream"})
    )
    sent = await send_announcement(guild, store, NEWS, content="news ping")
    assert sent is None
    assert store.get_feature_overrides(1) == {NEWS: 22}
    default.send.assert_not_awaited()
    hint.send.assert_not_awaited()


async def test_split_posts_and_expiry(tmp_path):
    await case_notices_delete_after_ttl_including_split_posts(scratch(tmp_path, "notices_delete_after_ttl_including_split_posts"))
    await case_split_extra_is_copied_and_delete_after_needs_store(scratch(tmp_path, "split_extra_is_copied_and_delete_after_needs_store"))


async def case_notices_delete_after_ttl_including_split_posts(tmp_path):
    clock = FakeClock()
    guild, channel, store = _store_and_channel(tmp_path)
    notices = _notices(tmp_path, clock, fake_bot(get_channel_result=channel))
    ping, card = _split_channel_posts(channel)
    embed = discord.Embed(title="card")
    sent = await send_announcement(
        guild,
        store,
        OTHER_FEATURE,
        content="@everyone hi",
        embed=embed,
        delete_after=60,
        notice_key="split",
        notices=notices,
    )
    assert sent is card
    assert channel.send.await_args_list[-2].kwargs.get("embed") is None
    assert channel.send.await_args_list[-1].kwargs.get("silent") is True
    clock.advance(60)
    await notices.delete_expired()
    card.delete.assert_awaited_once()
    ping.delete.assert_awaited_once()

    fail_ping = MagicMock()
    fail_ping.id = 11
    fail_ping.delete = AsyncMock()
    channel.send = AsyncMock(
        side_effect=[
            fail_ping,
            discord.HTTPException(MagicMock(status=500), {"message": "fail"}),
        ]
    )
    assert (
        await send_announcement(
            guild,
            store,
            OTHER_FEATURE,
            content="@everyone hi",
            embed=discord.Embed(title="card"),
        )
        is None
    )
    fail_ping.delete.assert_awaited_once()


async def case_split_extra_is_copied_and_delete_after_needs_store(tmp_path):
    guild, channel, store = _store_and_channel(tmp_path)
    with pytest.raises(ValueError, match="TemporaryNotices"):
        await send_announcement(
            guild, store, OTHER_FEATURE, content="later", delete_after=1
        )

    ping, card = _split_channel_posts(channel)
    extra = {"gif": "wave"}
    notices = _notices(tmp_path)
    sent = await send_announcement(
        guild,
        store,
        OTHER_FEATURE,
        content="@everyone hi",
        embed=discord.Embed(title="card"),
        delete_after=60,
        notice_key="split",
        extra=extra,
        notices=notices,
    )
    assert sent is card
    assert extra == {"gif": "wave"}
    stored = notices.get(OTHER_FEATURE, guild.id, "split")
    assert stored is not None
    assert stored.extra_id("ping_message_id") == ping.id




async def test_command_replies():
    case_quote_and_notice_text()
    await case_respond_followup_initial_and_ephemeral()


def case_quote_and_notice_text():
    assert quote_text("pong") == "> pong"
    assert quote_text("> pong") == "> pong"
    assert quote_text("a\nb") == "> a\n> b"
    assert quote_text("> a\n> b") == "> a\n> b"
    assert quote_text(">>>\na\nb") == ">>>\na\nb"
    assert notice_text("pong") == "**>**  pong"
    assert notice_text("pong", starter=False) == "pong"
    assert notice_text("> pong") == "**>**  pong"
    assert notice_text("**>** pong") == "**>**  pong"
    assert notice_text("Fetch failed.", "Use `/news cookie`.") == (
        "**>**  Fetch failed. Use `/news cookie`"
    )
    assert quote_text(notice_text("pong", starter=False)) == "> pong"
    assert quote_text(notice_text("All cogs will post in #x.")) == (
        "> **>**  All cogs will post in #x"
    )


def _fake_interaction(*, deferred: bool) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response.is_done.return_value = deferred
    interaction.response.send_message = AsyncMock()
    posted = MagicMock()
    posted.delete = AsyncMock()
    interaction.followup.send = AsyncMock(return_value=posted)
    return interaction


async def case_respond_followup_initial_and_ephemeral():
    followup = _fake_interaction(deferred=True)
    await respond(followup, "Following `bilibili` 原神.")
    send = followup.followup.send
    assert "delete_after" not in send.await_args.kwargs
    send.return_value.delete.assert_awaited_once_with(delay=COMMAND_REPLY_SECONDS)

    initial = _fake_interaction(deferred=False)
    await respond(initial, "pong")
    initial.response.send_message.assert_awaited_once_with(
        content=quote_text("pong"), delete_after=COMMAND_REPLY_SECONDS
    )

    ephemeral = _fake_interaction(deferred=True)
    await respond(ephemeral, "secret", ephemeral=True)
    ephemeral.followup.send.assert_awaited_once_with(content="secret", ephemeral=True)
    ephemeral.followup.send.return_value.delete.assert_not_awaited()


