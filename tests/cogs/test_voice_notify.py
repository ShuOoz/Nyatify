"""Isolated tests for the voice-notify function. No Discord login."""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.voice_notify import (
    FEATURE_VOICE,
    NOTICE_TTL_SECONDS,
    RECONNECT_GRACE_SECONDS,
    VOICE_NOTICE_GIF_URLS,
    VOICE_NOTICE_TITLE,
    VoiceNotify,
)
from guild import GuildConfig
from tests.helpers import (
    GUILD_ID,
    NOTIFY_CHANNEL_ID,
    FakeClock,
    fake_bot,
    fake_guild,
    fake_member,
    fake_text_channel,
    fake_voice_channel,
    fake_voice_state,
    scratch,
)


@pytest.fixture
def guild():
    return fake_guild(GUILD_ID)


@pytest.fixture
def text_channel(guild):
    channel = fake_text_channel(NOTIFY_CHANNEL_ID, guild=guild)
    guild.get_channel.return_value = channel
    return channel


@pytest.fixture
def store(tmp_path):
    config = GuildConfig(tmp_path / "guild.json")
    config.set_announce_channel_id(GUILD_ID, NOTIFY_CHANNEL_ID)
    return config


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def cog(store, clock, tmp_path):
    notify = VoiceNotify(
        fake_bot(),
        guild_config=store,
        grace_seconds=RECONNECT_GRACE_SECONDS,
        ttl_seconds=NOTICE_TTL_SECONDS,
        clock=clock,
        notices_path=tmp_path / "voice_notices.json",
    )
    yield notify
    notify.notices.stop(force=True)


async def _update(cog, member, before, after):
    await cog.on_voice_state_update(
        member,
        fake_voice_state(channel=before),
        fake_voice_state(channel=after),
    )


def _voice_notify(store, clock, path, bot=None) -> VoiceNotify:
    return VoiceNotify(
        bot or fake_bot(), guild_config=store, clock=clock, notices_path=path
    )


_CHANNEL_MENTION = re.compile(r"<#\d+>")


def _capture_posts(text_channel) -> list:
    posts: list = []

    async def send(*_args, **_kwargs):
        posted = MagicMock()
        posted.id = 1000 + len(posts)
        posted.channel = text_channel
        posted.edit = AsyncMock()
        posted.delete = AsyncMock()
        posts.append(posted)
        return posted

    def partial(message_id):
        for posted in posts:
            if posted.id == message_id:
                return posted
        missing = MagicMock()
        missing.id = message_id
        missing.edit = AsyncMock()
        missing.delete = AsyncMock()
        return missing

    text_channel.send = AsyncMock(side_effect=send)
    text_channel.get_partial_message = MagicMock(side_effect=partial)
    return posts


def _assert_voice_posts(text_channel, n: int = 1) -> None:
    assert text_channel.send.await_count == n * 2


def _card_kwargs(text_channel):
    cards = [
        call.kwargs
        for call in text_channel.send.await_args_list
        if call.kwargs.get("embed") is not None
    ]
    return cards[-1]




def _channel_field(embed) -> str:
    match = _CHANNEL_MENTION.search(embed.description or "")
    assert match is not None
    return match.group(0)


def _started_field(started_at: float) -> str:
    return f"<t:{int(started_at)}:R>"


def _assert_compact_notice(embed, member, channel, started_at: float) -> str:
    assert embed.title == VOICE_NOTICE_TITLE
    assert embed.author.name == member.display_name
    assert embed.description == (
        f"**Channel** | {channel.mention} · **Start** | "
        f"{_started_field(started_at)}"
    )
    assert embed.fields == []
    gif_url = embed.image.url
    assert gif_url in VOICE_NOTICE_GIF_URLS
    return gif_url






async def test_grace_and_ttl_hold_back_announcements(cog, text_channel, guild, clock):
    await case_grace_blocks_reconnect_until_it_expires(cog, text_channel, guild, clock)
    await case_ttl_blocks_reannounce_until_they_leave(cog, text_channel, guild, clock)


async def case_grace_blocks_reconnect_until_it_expires(cog, text_channel, guild, clock):
    stale = fake_member("Stale", 8)
    stale_room = fake_voice_channel(channel_id=40, members=[stale], guild=guild)
    await _update(cog, stale, None, stale_room)
    await _update(cog, stale, stale_room, None)
    assert stale_room.id in cog.active_channels
    assert stale_room.id in cog._emptied_at

    human = fake_member()
    bot_user = fake_member("Music", 9, bot=True)
    voice = fake_voice_channel(members=[human], guild=guild)
    await _update(cog, human, None, voice)
    posted = text_channel.send.return_value
    voice.members = [bot_user]
    await _update(cog, human, voice, None)
    posted.delete.assert_not_awaited()
    assert cog.notices.live(FEATURE_VOICE, guild.id, str(human.id)) is not None
    text_channel.send.reset_mock()

    clock.advance(RECONNECT_GRACE_SECONDS - 1)
    voice.members = [bot_user, human]
    await _update(cog, human, None, voice)
    text_channel.send.assert_not_awaited()
    assert voice.id in cog.active_channels

    voice.members = [bot_user]
    await _update(cog, human, voice, None)
    clock.advance(RECONNECT_GRACE_SECONDS)
    voice.members = [human]
    await _update(cog, human, None, voice)
    _assert_voice_posts(text_channel)
    assert voice.id not in cog._emptied_at

    posted.delete = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), {"message": "fail"})
    )
    text_channel.send.reset_mock()
    voice.members = []
    await _update(cog, human, voice, None)
    clock.advance(RECONNECT_GRACE_SECONDS)
    voice.members = [human]
    await _update(cog, human, None, voice)
    _assert_voice_posts(text_channel)
    assert cog.notices.live(FEATURE_VOICE, guild.id, str(human.id)) is not None


async def case_ttl_blocks_reannounce_until_they_leave(cog, text_channel, guild, clock):
    member = fake_member()
    first_room = fake_voice_channel(channel_id=1, members=[member], guild=guild)
    await _update(cog, member, None, first_room)
    posted = text_channel.send.return_value
    text_channel.send.reset_mock()

    clock.advance(NOTICE_TTL_SECONDS - 10)
    first_room.members = []
    second_room = fake_voice_channel(channel_id=2, members=[member], guild=guild)
    await _update(cog, member, first_room, second_room)
    posted.edit.assert_awaited()
    posted.delete.assert_not_awaited()
    text_channel.send.assert_not_awaited()

    clock.advance(10)
    await cog.notices.delete_expired()
    posted.delete.assert_awaited_once()
    second_room.members = []
    third_room = fake_voice_channel(channel_id=3, members=[member], guild=guild)
    await _update(cog, member, second_room, third_room)
    text_channel.send.assert_not_awaited()

    third_room.members = []
    await _update(cog, member, third_room, None)
    clock.advance(RECONNECT_GRACE_SECONDS)
    third_room.members = [member]
    await _update(cog, member, None, third_room)
    _assert_voice_posts(text_channel)

    host = fake_member("Host", 2)
    guest = fake_member("Guest", 3)
    occupied = fake_voice_channel(channel_id=4, members=[host], guild=guild)
    await _update(cog, host, None, occupied)
    occupied.members = [host, guest]
    await _update(cog, guest, None, occupied)
    text_channel.send.reset_mock()
    clock.advance(NOTICE_TTL_SECONDS)
    await cog.notices.delete_expired()
    occupied.members = [host]
    empty = fake_voice_channel(channel_id=5, members=[guest], guild=guild)
    await _update(cog, guest, occupied, empty)
    _assert_voice_posts(text_channel)


async def test_ready_syncs_and_restarts(store, clock, guild, text_channel, cog, tmp_path):
    await case_ready_and_restart_keep_ttl_and_grace(store, clock, scratch(tmp_path, "ready_and_restart_keep_ttl_and_grace"), guild, text_channel)
    await case_ready_syncs_occupancy_and_offline_moves(cog, text_channel, guild, clock)


async def case_ready_and_restart_keep_ttl_and_grace(store, clock, tmp_path, guild, text_channel):
    ready = _voice_notify(store, clock, tmp_path / "ready.json")
    stayer = fake_member("Stayer", 1)
    first_room = fake_voice_channel(channel_id=1, members=[stayer], guild=guild)
    guild.voice_channels = [first_room]
    ready.bot.guilds = [guild]
    await _update(ready, stayer, None, first_room)
    text_channel.send.reset_mock()
    clock.advance(NOTICE_TTL_SECONDS)
    await ready.on_ready()
    first_room.members = []
    second_room = fake_voice_channel(channel_id=2, members=[stayer], guild=guild)
    await _update(ready, stayer, first_room, second_room)
    text_channel.send.assert_not_awaited()

    leaver = fake_member("Leaver", 2)
    grace_room = fake_voice_channel(channel_id=3, members=[leaver], guild=guild)
    guild.voice_channels = [grace_room]
    await _update(ready, leaver, None, grace_room)
    grace_room.members = []
    await _update(ready, leaver, grace_room, None)
    text_channel.send.reset_mock()
    await ready.on_ready()
    assert ready.notices.live(FEATURE_VOICE, guild.id, str(leaver.id)) is not None
    clock.advance(RECONNECT_GRACE_SECONDS - 1)
    grace_room.members = [leaver]
    await _update(ready, leaver, None, grace_room)
    text_channel.send.assert_not_awaited()
    assert grace_room.id in ready.active_channels
    await ready.cog_unload()

    ttl_path = tmp_path / "restart_ttl.json"
    first = _voice_notify(store, clock, ttl_path)
    ttl_member = fake_member("TTL", 3)
    ttl_room = fake_voice_channel(channel_id=10, members=[ttl_member], guild=guild)
    await _update(first, ttl_member, None, ttl_room)
    clock.advance(NOTICE_TTL_SECONDS)
    await first.notices.delete_expired()
    text_channel.send.reset_mock()
    await first.cog_unload()
    restarted = _voice_notify(store, clock, ttl_path, fake_bot())
    ttl_room.members = [ttl_member]
    guild.voice_channels = [ttl_room]
    restarted.bot.guilds = [guild]
    await restarted.on_ready()
    ttl_room.members = []
    ttl_hop = fake_voice_channel(channel_id=11, members=[ttl_member], guild=guild)
    await _update(restarted, ttl_member, ttl_room, ttl_hop)
    text_channel.send.assert_not_awaited()
    await restarted.cog_unload()

    grace_path = tmp_path / "restart_grace.json"
    before = _voice_notify(store, clock, grace_path)
    grace_member = fake_member("Grace", 4)
    persist = fake_voice_channel(channel_id=12, members=[grace_member], guild=guild)
    await _update(before, grace_member, None, persist)
    persist.members = []
    await _update(before, grace_member, persist, None)
    text_channel.send.reset_mock()
    await before.cog_unload()
    after = _voice_notify(store, clock, grace_path, fake_bot())
    guild.voice_channels = [persist]
    after.bot.guilds = [guild]
    await after.on_ready()
    assert after.notices.live(FEATURE_VOICE, guild.id, str(grace_member.id)) is not None
    clock.advance(RECONNECT_GRACE_SECONDS - 1)
    persist.members = [grace_member]
    await _update(after, grace_member, None, persist)
    text_channel.send.assert_not_awaited()
    assert persist.id in after.active_channels
    await after.cog_unload()




async def case_ready_syncs_occupancy_and_offline_moves(cog, text_channel, guild, clock):
    human = fake_member("Human", 1)
    other = fake_member("Other", 2)
    bot_user = fake_member("Music", 9, bot=True)
    occupied = fake_voice_channel(channel_id=10, members=[human], guild=guild)
    empty = fake_voice_channel(channel_id=11, members=[], guild=guild)
    afk = fake_voice_channel(channel_id=77, members=[other], guild=guild)
    bot_only = fake_voice_channel(channel_id=12, members=[bot_user], guild=guild)
    guild.afk_channel = afk
    guild.voice_channels = [occupied, empty, afk, bot_only]
    cog.bot.guilds = [guild]
    await cog.on_ready()
    assert occupied.id in cog.active_channels
    assert empty.id not in cog.active_channels
    assert afk.id not in cog.active_channels
    assert bot_only.id not in cog.active_channels
    await _update(cog, human, None, occupied)
    text_channel.send.assert_not_awaited()
    empty.members = [other]
    await _update(cog, other, None, empty)
    _assert_voice_posts(text_channel)
    cog.active_channels.clear()
    cog._emptied_at.clear()
    cog.bot.is_ready.return_value = True
    await cog.cog_load()
    assert occupied.id in cog.active_channels

    empty.members = []
    await cog.on_ready()
    posts = _capture_posts(text_channel)
    leaver = fake_member("Leaver", 3)
    room = fake_voice_channel(channel_id=13, members=[leaver], guild=guild)
    guild.voice_channels = [room]
    await _update(cog, leaver, None, room)
    ping, card = posts[-2], posts[-1]
    room.members = []
    text_channel.send.reset_mock()
    await cog.on_ready()
    assert (guild.id, leaver.id) not in cog._announced
    assert cog.notices.get(FEATURE_VOICE, guild.id, str(leaver.id)) is None
    ping.delete.assert_awaited_once()
    card.delete.assert_awaited_once()
    room.members = [leaver]
    await _update(cog, leaver, None, room)
    _assert_voice_posts(text_channel)

    mover = fake_member("Mover", 4)
    first_room = fake_voice_channel(channel_id=14, members=[mover], guild=guild)
    await _update(cog, mover, None, first_room)
    card = posts[-1]
    started_at = clock.t
    started_gif = _card_kwargs(text_channel)["embed"].image.url
    text_channel.send.reset_mock()
    first_room.members = []
    second_room = fake_voice_channel(channel_id=15, members=[mover], guild=guild)
    guild.voice_channels = [first_room, second_room]
    await cog.on_ready()
    text_channel.send.assert_not_awaited()
    card.edit.assert_awaited_once()
    embed = card.edit.await_args.kwargs["embed"]
    _assert_compact_notice(embed, mover, second_room, started_at)
    assert embed.image.url == started_gif
    assert cog.notices.get(FEATURE_VOICE, guild.id, str(mover.id)).extra[
        "voice_channel_id"
    ] == second_room.id


async def test_bots_afk_mute_and_empty_moves(cog, text_channel, guild):
    bot_user = fake_member("Music", 9, bot=True)
    human = fake_member()
    bots = fake_voice_channel(channel_id=12, members=[bot_user], guild=guild)
    await _update(cog, bot_user, None, bots)
    text_channel.send.assert_not_awaited()
    assert bots.id not in cog.active_channels
    bots.members = [bot_user, human]
    await _update(cog, human, None, bots)
    _assert_voice_posts(text_channel)
    assert bots.id in cog.active_channels

    await _update(cog, human, bots, bots)
    assert text_channel.send.await_count == 2

    afk = fake_voice_channel(channel_id=77, members=[human], guild=guild)
    guild.afk_channel = afk
    voice = fake_voice_channel(members=[human], guild=guild)
    bots.members = [bot_user]
    await _update(cog, human, bots, afk)
    posted = text_channel.send.return_value
    posted.delete.assert_not_awaited()
    assert cog.notices.live(FEATURE_VOICE, guild.id, str(human.id)) is not None
    text_channel.send.reset_mock()
    await _update(cog, human, None, afk)
    text_channel.send.assert_not_awaited()
    assert afk.id not in cog.active_channels
    await _update(cog, human, afk, voice)
    text_channel.send.assert_not_awaited()
    posted.edit.assert_awaited()
    assert voice.id in cog.active_channels

    mover = fake_member("Mover", 2)
    left = fake_voice_channel(channel_id=1, members=[], guild=guild)
    right = fake_voice_channel(channel_id=2, members=[mover], guild=guild)
    text_channel.send.reset_mock()
    await _update(cog, mover, left, right)
    _assert_voice_posts(text_channel)
    assert right.id in cog.active_channels
    assert left.id not in cog.active_channels
    assert left.id not in cog._emptied_at


async def test_hop_edits_or_reposts_the_same_stint(
    cog, text_channel, guild, clock
):
    member = fake_member()
    first_room = fake_voice_channel(channel_id=1, members=[member], guild=guild)
    await _update(cog, member, None, first_room)
    posted = text_channel.send.return_value
    started_at = clock.t
    posted_gif = text_channel.send.await_args.kwargs["embed"].image.url
    text_channel.send.reset_mock()
    clock.advance(120)
    first_room.members = []
    second_room = fake_voice_channel(channel_id=2, members=[member], guild=guild)
    await _update(cog, member, first_room, second_room)
    text_channel.send.assert_not_awaited()
    posted.edit.assert_awaited_once()
    posted.delete.assert_not_awaited()
    embed = posted.edit.await_args.kwargs["embed"]
    assert "content" not in posted.edit.await_args.kwargs
    assert _assert_compact_notice(embed, member, second_room, started_at) == posted_gif

    posted.edit.reset_mock()
    second_room.members = []
    first_room.members = [member]
    await _update(cog, member, second_room, first_room)
    posted.edit.assert_awaited_once()
    embed = posted.edit.await_args.kwargs["embed"]
    assert _assert_compact_notice(embed, member, first_room, started_at) == posted_gif

    notice = cog.notices.get(FEATURE_VOICE, guild.id, str(member.id))
    assert notice is not None
    notice.message = None
    text_channel.send.reset_mock()
    posted.edit.reset_mock()
    guild.get_channel.return_value = None
    cog.bot.get_channel.return_value = None
    cog.bot.fetch_channel = AsyncMock(return_value=text_channel)
    text_channel.get_partial_message.return_value = posted
    first_room.members = []
    uncached = fake_voice_channel(channel_id=3, members=[member], guild=guild)
    await _update(cog, member, first_room, uncached)
    text_channel.send.assert_not_awaited()
    posted.edit.assert_awaited_once()
    _assert_compact_notice(
        posted.edit.await_args.kwargs["embed"], member, uncached, started_at
    )

    guild.get_channel.return_value = text_channel
    posts = _capture_posts(text_channel)
    host = fake_member("Host", 2)
    guest = fake_member("Guest", 3)
    occupied = fake_voice_channel(channel_id=4, members=[host], guild=guild)
    lobby = fake_voice_channel(channel_id=5, members=[guest], guild=guild)
    await _update(cog, host, None, occupied)
    await _update(cog, guest, None, lobby)
    guest_card = posts[3]
    text_channel.send.reset_mock()
    lobby.members = []
    occupied.members = [host, guest]
    await _update(cog, guest, lobby, occupied)
    text_channel.send.assert_not_awaited()
    guest_card.edit.assert_awaited_once()
    assert _channel_field(guest_card.edit.await_args.kwargs["embed"]) == occupied.mention
    assert guest_card.edit.await_args.kwargs["embed"].author.name == guest.display_name

    failer = fake_member("Failer", 4)
    fail_room = fake_voice_channel(channel_id=6, members=[failer], guild=guild)
    await _update(cog, failer, None, fail_room)
    fail_card = posts[-1]
    fail_card.edit = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), {"message": "fail"})
    )
    text_channel.send.reset_mock()
    fail_room.members = []
    fail_next = fake_voice_channel(channel_id=7, members=[failer], guild=guild)
    await _update(cog, failer, fail_room, fail_next)
    _assert_voice_posts(text_channel)
    assert _channel_field(_card_kwargs(text_channel)["embed"]) == fail_next.mention

    gone = fake_member("Gone", 5)
    gone_room = fake_voice_channel(channel_id=8, members=[gone], guild=guild)
    await _update(cog, gone, None, gone_room)
    ping, card = posts[-2], posts[-1]
    card.edit = AsyncMock(
        side_effect=discord.NotFound(MagicMock(), {"message": "Unknown Message"})
    )
    text_channel.send.reset_mock()
    gone_room.members = []
    gone_next = fake_voice_channel(channel_id=9, members=[gone], guild=guild)
    await _update(cog, gone, gone_room, gone_next)
    ping.delete.assert_awaited_once()
    _assert_voice_posts(text_channel)
    assert _channel_field(_card_kwargs(text_channel)["embed"]) == gone_next.mention


async def test_teardown_drops_state_and_missing_channel_skips(
    cog, text_channel, guild, store, clock, tmp_path
):
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)
    guild.voice_channels = [voice]
    await _update(cog, member, None, voice)
    await cog.on_guild_remove(guild)
    assert voice.id not in cog.active_channels
    assert not cog.notices
    assert cog._announced == set()

    await _update(cog, member, None, voice)
    voice.members = []
    await _update(cog, member, voice, None)
    assert voice.id in cog._emptied_at
    await cog.on_guild_channel_delete(voice)
    assert voice.id not in cog.active_channels
    assert voice.id not in cog._emptied_at

    text_channel.send.reset_mock()
    guild.get_channel.return_value = None
    await _update(cog, member, None, voice)
    text_channel.send.assert_not_awaited()
    assert voice.id in cog.active_channels
    assert cog.notices.live(FEATURE_VOICE, guild.id, str(member.id)) is not None

    guild.get_channel.return_value = text_channel
    voice.members = [member]
    await _update(cog, member, None, voice)
    text_channel.send.reset_mock()
    await cog.on_guild_channel_delete(text_channel)
    assert not cog.notices
    new_channel = fake_text_channel(88, guild=guild)
    store.set_announce_channel_id(GUILD_ID, new_channel.id)
    guild.get_channel.side_effect = lambda channel_id: {
        new_channel.id: new_channel
    }.get(channel_id)
    voice.members = []
    await _update(cog, member, voice, None)
    clock.advance(RECONNECT_GRACE_SECONDS)
    voice.members = [member]
    await _update(cog, member, None, voice)
    new_channel.send.assert_awaited()
    text_channel.send.assert_not_awaited()

    bot = fake_bot()
    path = tmp_path / "voice_loop.json"
    first = _voice_notify(store, clock, path, bot)
    await first.cog_load()
    assert first.notices._purge.is_running()
    await first.cog_unload()
    await asyncio.sleep(0)
    assert not first.notices._purge.is_running()
    second = _voice_notify(store, clock, path, bot)
    await second.cog_load()
    assert second.notices is first.notices
    assert second.notices._purge.is_running()
    second.notices.stop(force=True)


async def test_a_missed_card_stays_open_and_a_bad_session_file_is_kept(
    store, clock, guild, text_channel, tmp_path
):
    retry = _voice_notify(store, clock, tmp_path / "retry.json")
    first = fake_member("First", 21)
    second = fake_member("Second", 22)
    room = fake_voice_channel(channel_id=70, members=[first], guild=guild)
    text_channel.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), {"message": "no"})
    )
    await _update(retry, first, None, room)
    assert room.id not in retry.active_channels
    assert (guild.id, first.id) not in retry._announced
    _capture_posts(text_channel)
    room.members = [first, second]
    await _update(retry, second, None, room)
    _assert_voice_posts(text_channel)
    assert (guild.id, second.id) in retry._announced
    retry.notices.stop(force=True)

    held = _voice_notify(store, clock, tmp_path / "held.json")
    member = fake_member("Held", 23)
    voice = fake_voice_channel(channel_id=71, members=[member], guild=guild)
    guild.voice_channels = [voice]
    held.bot.guilds = [guild]
    await _update(held, member, None, voice)
    guild.unavailable = True
    voice.members = []
    await held.on_ready()
    assert (guild.id, member.id) in held._announced
    assert held.notices.live(FEATURE_VOICE, guild.id, str(member.id)) is not None
    held.notices.stop(force=True)

    notices = tmp_path / "broken_notices.json"
    sessions = notices.with_name("voice_sessions.json")
    sessions.write_text("{", encoding="utf-8")
    broken = _voice_notify(store, clock, notices)
    person = fake_member("Person", 24)
    somewhere = fake_voice_channel(channel_id=72, members=[person], guild=guild)
    await _update(broken, person, None, somewhere)
    assert sessions.read_text(encoding="utf-8") == "{"
    broken.notices.stop(force=True)
