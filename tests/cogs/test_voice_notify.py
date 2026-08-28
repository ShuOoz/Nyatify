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
    VOICE_NOTICE_CONTENT,
    VOICE_NOTICE_GIF_URLS,
    VOICE_NOTICE_TITLE,
    VoiceNotify,
)
from guild_config import GuildConfig
from tests.helpers import (
    FakeClock,
    fake_bot,
    fake_guild,
    fake_member,
    fake_text_channel,
    fake_voice_channel,
    fake_voice_state,
)

GUILD_ID = 1
NOTIFY_CHANNEL_ID = 99


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
    config = GuildConfig(tmp_path / "guild_config.json")
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


_CHANNEL_MENTION = re.compile(r"<#\d+>")


def _capture_posts(text_channel) -> list:
    posts: list = []

    async def send(*args, **kwargs):
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


def _assert_split_start(text_channel, member, channel, started_at: float) -> None:
    ping, card = text_channel.send.await_args_list[-2:]
    assert ping.kwargs["content"] == VOICE_NOTICE_CONTENT
    assert ping.kwargs.get("embed") is None
    assert card.kwargs.get("silent") is True
    _assert_compact_notice(card.kwargs["embed"], member, channel, started_at)


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


async def test_first_join_of_empty_channel_sends_announcement(
    cog, text_channel, guild, clock
):
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)

    await _update(cog, member, None, voice)

    _assert_split_start(text_channel, member, voice, clock.t)
    assert voice.id in cog.active_channels
    guild.get_channel.assert_called_with(NOTIFY_CHANNEL_ID)


def test_gif_bag_uses_each_url_before_repeat(cog, monkeypatch):
    monkeypatch.setattr("cogs.voice_notify.random.shuffle", lambda seq: seq.sort())
    first_cycle = [cog._pick_gif() for _ in range(3)]
    assert first_cycle == sorted(VOICE_NOTICE_GIF_URLS, reverse=True)
    second = cog._pick_gif()
    assert second in VOICE_NOTICE_GIF_URLS
    assert second != first_cycle[-1]


async def test_occupied_channel_does_not_announce_again(cog, text_channel, guild):
    first = fake_member("First", 1)
    second = fake_member("Second", 2)
    third = fake_member("Third", 3)
    voice = fake_voice_channel(members=[first], guild=guild)
    await _update(cog, first, None, voice)
    text_channel.send.reset_mock()

    voice.members = [first, second]
    await _update(cog, second, None, voice)
    text_channel.send.assert_not_awaited()

    already = fake_voice_channel(channel_id=20, members=[first, third], guild=guild)
    await _update(cog, third, None, already)
    text_channel.send.assert_not_awaited()
    assert already.id in cog.active_channels


async def test_concurrent_first_joins_announce_once(cog, text_channel, guild):
    first = fake_member("First", 1)
    second = fake_member("Second", 2)
    voice = fake_voice_channel(members=[first], guild=guild)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_send(*args, **kwargs):
        started.set()
        await release.wait()
        posted = MagicMock()
        posted.id = 1000 + text_channel.send.await_count
        posted.channel = text_channel
        posted.edit = AsyncMock()
        posted.delete = AsyncMock()
        return posted

    text_channel.send = AsyncMock(side_effect=slow_send)
    first_join = asyncio.create_task(_update(cog, first, None, voice))
    await started.wait()
    second_join = asyncio.create_task(_update(cog, second, None, voice))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first_join, second_join)

    _assert_voice_posts(text_channel)
    assert voice.id in cog.active_channels


async def test_two_people_already_in_member_list_does_not_announce(
    cog, text_channel, guild
):
    first = fake_member("First", 1)
    second = fake_member("Second", 2)
    voice = fake_voice_channel(members=[first, second], guild=guild)

    await asyncio.gather(
        _update(cog, first, None, voice),
        _update(cog, second, None, voice),
    )

    text_channel.send.assert_not_awaited()
    assert voice.id in cog.active_channels


async def test_reconnect_within_grace_does_not_announce(
    cog, text_channel, guild, clock
):
    human = fake_member()
    bot_user = fake_member("Music", 9, bot=True)
    voice = fake_voice_channel(members=[human], guild=guild)
    await _update(cog, human, None, voice)
    posted = text_channel.send.return_value
    voice.members = [bot_user]
    await _update(cog, human, voice, None)
    posted.delete.assert_awaited_once()
    text_channel.send.reset_mock()

    clock.advance(RECONNECT_GRACE_SECONDS - 1)
    voice.members = [bot_user, human]
    await _update(cog, human, None, voice)

    text_channel.send.assert_not_awaited()
    assert voice.id in cog.active_channels


async def test_announces_again_after_leave_and_grace(cog, text_channel, guild, clock):
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)
    await _update(cog, member, None, voice)
    posted = text_channel.send.return_value
    voice.members = []
    await _update(cog, member, voice, None)
    posted.delete.assert_awaited_once()
    text_channel.send.reset_mock()

    clock.advance(RECONNECT_GRACE_SECONDS)
    voice.members = [member]
    await _update(cog, member, None, voice)

    _assert_voice_posts(text_channel)
    assert voice.id in cog.active_channels
    assert voice.id not in cog._emptied_at


async def test_ttl_does_not_reannounce_while_still_in_voice(
    cog, text_channel, guild, clock
):
    member = fake_member()
    first_room = fake_voice_channel(channel_id=1, members=[member], guild=guild)
    await _update(cog, member, None, first_room)
    posted = text_channel.send.return_value
    text_channel.send.reset_mock()

    clock.advance(NOTICE_TTL_SECONDS)
    await cog.notices.delete_expired()
    posted.delete.assert_awaited_once()
    first_room.members = []
    second_room = fake_voice_channel(channel_id=2, members=[member], guild=guild)
    await _update(cog, member, first_room, second_room)

    text_channel.send.assert_not_awaited()


async def test_leave_after_ttl_can_announce_again(cog, text_channel, guild, clock):
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)
    await _update(cog, member, None, voice)
    clock.advance(NOTICE_TTL_SECONDS)
    await cog.notices.delete_expired()
    text_channel.send.reset_mock()

    voice.members = []
    await _update(cog, member, voice, None)
    clock.advance(RECONNECT_GRACE_SECONDS)
    voice.members = [member]
    await _update(cog, member, None, voice)

    _assert_voice_posts(text_channel)


async def test_hop_from_occupied_to_empty_still_announces_after_ttl(
    cog, text_channel, guild, clock
):
    first = fake_member("First", 1)
    second = fake_member("Second", 2)
    occupied = fake_voice_channel(channel_id=1, members=[first], guild=guild)
    await _update(cog, first, None, occupied)
    occupied.members = [first, second]
    await _update(cog, second, None, occupied)
    text_channel.send.reset_mock()

    clock.advance(NOTICE_TTL_SECONDS)
    await cog.notices.delete_expired()
    occupied.members = [first]
    empty = fake_voice_channel(channel_id=2, members=[second], guild=guild)
    await _update(cog, second, occupied, empty)

    _assert_voice_posts(text_channel)


async def test_ready_keeps_ttl_from_reannouncing_a_stayer(
    cog, text_channel, guild, clock
):
    member = fake_member()
    first_room = fake_voice_channel(channel_id=1, members=[member], guild=guild)
    guild.voice_channels = [first_room]
    cog.bot.guilds = [guild]
    await _update(cog, member, None, first_room)
    text_channel.send.reset_mock()

    clock.advance(NOTICE_TTL_SECONDS)
    await cog.on_ready()
    first_room.members = []
    second_room = fake_voice_channel(channel_id=2, members=[member], guild=guild)
    await _update(cog, member, first_room, second_room)

    text_channel.send.assert_not_awaited()


async def test_restart_keeps_ttl_from_reannouncing_a_stayer(
    store, clock, tmp_path, guild, text_channel
):
    path = tmp_path / "voice_notices.json"
    first = VoiceNotify(
        fake_bot(),
        guild_config=store,
        clock=clock,
        notices_path=path,
    )
    member = fake_member()
    first_room = fake_voice_channel(channel_id=1, members=[member], guild=guild)
    await _update(first, member, None, first_room)
    clock.advance(NOTICE_TTL_SECONDS)
    await first.notices.delete_expired()
    text_channel.send.reset_mock()
    await first.cog_unload()

    bot = fake_bot()
    second = VoiceNotify(
        bot,
        guild_config=store,
        clock=clock,
        notices_path=path,
    )
    first_room.members = [member]
    guild.voice_channels = [first_room]
    bot.guilds = [guild]
    await second.on_ready()
    first_room.members = []
    second_room = fake_voice_channel(channel_id=2, members=[member], guild=guild)
    await _update(second, member, first_room, second_room)

    text_channel.send.assert_not_awaited()
    await second.cog_unload()


async def test_someone_else_can_start_the_room_after_leave(
    cog, text_channel, guild, clock
):
    first = fake_member("First", 1)
    second = fake_member("Second", 2)
    posts = _capture_posts(text_channel)
    voice = fake_voice_channel(members=[first], guild=guild)
    await _update(cog, first, None, voice)
    ping, card = posts[0], posts[1]
    voice.members = []
    await _update(cog, first, voice, None)
    ping.delete.assert_awaited_once()
    card.delete.assert_awaited_once()

    clock.advance(RECONNECT_GRACE_SECONDS)
    voice.members = [second]
    await _update(cog, second, None, voice)

    _assert_voice_posts(text_channel, 2)
    posts[2].delete.assert_not_awaited()
    posts[3].delete.assert_not_awaited()
    assert cog.notices.live(FEATURE_VOICE, guild.id, str(first.id)) is None
    assert cog.notices.live(FEATURE_VOICE, guild.id, str(second.id)) is not None


async def test_new_start_in_another_room_keeps_the_other_notice(cog, text_channel, guild):
    first = fake_member("First", 1)
    second = fake_member("Second", 2)
    posts = _capture_posts(text_channel)
    lobby = fake_voice_channel(channel_id=1, members=[first], guild=guild)
    gaming = fake_voice_channel(channel_id=2, members=[second], guild=guild)

    await _update(cog, first, None, lobby)
    await _update(cog, second, None, gaming)

    _assert_voice_posts(text_channel, 2)
    for posted in posts:
        posted.delete.assert_not_awaited()
    assert cog.notices.live(FEATURE_VOICE, guild.id, str(first.id)) is not None
    assert cog.notices.live(FEATURE_VOICE, guild.id, str(second.id)) is not None


async def test_start_in_old_room_does_not_delete_a_hopped_notice(
    cog, text_channel, guild, clock
):
    first = fake_member("First", 1)
    second = fake_member("Second", 2)
    posts = _capture_posts(text_channel)
    lobby = fake_voice_channel(channel_id=1, members=[first], guild=guild)
    await _update(cog, first, None, lobby)
    ping, card = posts[0], posts[1]

    lobby.members = []
    gaming = fake_voice_channel(channel_id=2, members=[first], guild=guild)
    await _update(cog, first, lobby, gaming)
    clock.advance(RECONNECT_GRACE_SECONDS)
    lobby.members = [second]
    await _update(cog, second, None, lobby)

    _assert_voice_posts(text_channel, 2)
    ping.edit.assert_not_awaited()
    card.edit.assert_awaited()
    ping.delete.assert_not_awaited()
    card.delete.assert_not_awaited()
    assert cog.notices.live(FEATURE_VOICE, guild.id, str(first.id)) is not None
    assert cog.notices.live(FEATURE_VOICE, guild.id, str(second.id)) is not None


async def test_ready_syncs_occupied_and_skips_empty_afk_and_bots(
    cog, text_channel, guild
):
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


async def test_ready_during_grace_does_not_allow_reannounce(
    cog, text_channel, guild, clock
):
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)
    guild.voice_channels = [voice]
    cog.bot.guilds = [guild]
    await _update(cog, member, None, voice)
    voice.members = []
    await _update(cog, member, voice, None)
    text_channel.send.reset_mock()

    await cog.on_ready()
    clock.advance(RECONNECT_GRACE_SECONDS - 1)
    voice.members = [member]
    await _update(cog, member, None, voice)

    text_channel.send.assert_not_awaited()
    assert voice.id in cog.active_channels


async def test_restart_keeps_grace_from_reannouncing(
    store, clock, tmp_path, guild, text_channel
):
    path = tmp_path / "voice_notices.json"
    first = VoiceNotify(
        fake_bot(),
        guild_config=store,
        clock=clock,
        notices_path=path,
    )
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)
    await _update(first, member, None, voice)
    voice.members = []
    await _update(first, member, voice, None)
    text_channel.send.reset_mock()
    await first.cog_unload()

    bot = fake_bot()
    second = VoiceNotify(
        bot,
        guild_config=store,
        clock=clock,
        notices_path=path,
    )
    guild.voice_channels = [voice]
    bot.guilds = [guild]
    await second.on_ready()
    clock.advance(RECONNECT_GRACE_SECONDS - 1)
    voice.members = [member]
    await _update(second, member, None, voice)

    text_channel.send.assert_not_awaited()
    assert voice.id in second.active_channels
    await second.cog_unload()


async def test_ready_allows_announce_if_they_left_while_offline(
    cog, text_channel, guild
):
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)
    guild.voice_channels = [voice]
    cog.bot.guilds = [guild]
    await _update(cog, member, None, voice)
    posted = text_channel.send.return_value
    voice.members = []
    text_channel.send.reset_mock()

    await cog.on_ready()

    assert (guild.id, member.id) not in cog._announced
    assert cog.notices.get(FEATURE_VOICE, guild.id, str(member.id)) is None
    posted.delete.assert_awaited_once()

    voice.members = [member]
    await _update(cog, member, None, voice)
    _assert_voice_posts(text_channel)


async def test_ready_retargets_if_they_moved_while_offline(
    cog, text_channel, guild, clock
):
    member = fake_member()
    first_room = fake_voice_channel(channel_id=1, members=[member], guild=guild)
    await _update(cog, member, None, first_room)
    posted = text_channel.send.return_value
    started_at = clock.t
    started_gif = text_channel.send.await_args.kwargs["embed"].image.url
    text_channel.send.reset_mock()

    first_room.members = []
    second_room = fake_voice_channel(channel_id=2, members=[member], guild=guild)
    guild.voice_channels = [first_room, second_room]
    cog.bot.guilds = [guild]
    await cog.on_ready()

    text_channel.send.assert_not_awaited()
    posted.edit.assert_awaited_once()
    embed = posted.edit.await_args.kwargs["embed"]
    _assert_compact_notice(embed, member, second_room, started_at)
    assert embed.image.url == started_gif
    assert cog.notices.get(FEATURE_VOICE, guild.id, str(member.id)).extra[
        "voice_channel_id"
    ] == second_room.id


async def test_unload_and_load_restarts_the_notice_loop(store, clock, tmp_path):
    bot = fake_bot()
    path = tmp_path / "voice_notices.json"
    first = VoiceNotify(
        bot,
        guild_config=store,
        clock=clock,
        notices_path=path,
    )
    await first.cog_load()
    assert first.notices._purge.is_running()

    await first.cog_unload()
    await asyncio.sleep(0)
    assert not first.notices._purge.is_running()

    second = VoiceNotify(
        bot,
        guild_config=store,
        clock=clock,
        notices_path=path,
    )
    await second.cog_load()
    assert second.notices is first.notices
    assert second.notices._purge.is_running()
    second.notices.stop(force=True)


async def test_bot_joining_does_not_announce(cog, text_channel, guild):
    bot_user = fake_member("Music", 9, bot=True)
    voice = fake_voice_channel(members=[bot_user], guild=guild)

    await _update(cog, bot_user, None, voice)

    text_channel.send.assert_not_awaited()
    assert voice.id not in cog.active_channels


async def test_human_joining_a_bot_only_channel_announces(cog, text_channel, guild):
    bot_user = fake_member("Music", 9, bot=True)
    human = fake_member("Nya", 1)
    voice = fake_voice_channel(members=[bot_user, human], guild=guild)

    await _update(cog, human, None, voice)

    _assert_voice_posts(text_channel)
    assert voice.id in cog.active_channels


async def test_afk_is_ignored_other_rooms_still_announce(cog, text_channel, guild):
    member = fake_member()
    afk = fake_voice_channel(channel_id=77, members=[member], guild=guild)
    guild.afk_channel = afk
    await _update(cog, member, None, afk)
    text_channel.send.assert_not_awaited()
    assert afk.id not in cog.active_channels

    voice = fake_voice_channel(members=[member], guild=guild)
    await _update(cog, member, afk, voice)
    _assert_voice_posts(text_channel)
    assert voice.id in cog.active_channels


async def test_moving_to_afk_deletes_the_notice(cog, text_channel, guild):
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)
    afk = fake_voice_channel(channel_id=77, members=[member], guild=guild)
    guild.afk_channel = afk
    await _update(cog, member, None, voice)
    posted = text_channel.send.return_value

    voice.members = []
    await _update(cog, member, voice, afk)

    posted.delete.assert_awaited_once()
    assert cog.notices.live(FEATURE_VOICE, guild.id, str(member.id)) is None


async def test_leave_still_allows_reannounce_if_delete_fails(
    cog, text_channel, guild, clock
):
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)
    await _update(cog, member, None, voice)
    posted = text_channel.send.return_value
    posted.delete = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), {"message": "fail"})
    )
    text_channel.send.reset_mock()

    voice.members = []
    await _update(cog, member, voice, None)
    clock.advance(RECONNECT_GRACE_SECONDS)
    voice.members = [member]
    await _update(cog, member, None, voice)

    _assert_voice_posts(text_channel)
    assert cog.notices.live(FEATURE_VOICE, guild.id, str(member.id)) is not None


async def test_mute_or_deafen_does_not_announce(cog, text_channel, guild):
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)

    await _update(cog, member, voice, voice)

    text_channel.send.assert_not_awaited()


async def test_moving_into_an_empty_channel_announces(cog, text_channel, guild):
    member = fake_member()
    left = fake_voice_channel(channel_id=1, members=[], guild=guild)
    right = fake_voice_channel(channel_id=2, members=[member], guild=guild)

    await _update(cog, member, left, right)

    _assert_voice_posts(text_channel)
    assert right.id in cog.active_channels
    assert left.id not in cog.active_channels
    assert left.id not in cog._emptied_at


async def test_hop_edits_the_same_notice(cog, text_channel, guild, clock):
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
    gif_name = _assert_compact_notice(embed, member, second_room, started_at)
    assert gif_name == posted_gif

    posted.edit.reset_mock()
    second_room.members = []
    first_room.members = [member]
    await _update(cog, member, second_room, first_room)
    text_channel.send.assert_not_awaited()
    posted.edit.assert_awaited_once()
    embed = posted.edit.await_args.kwargs["embed"]
    gif_name = _assert_compact_notice(embed, member, first_room, started_at)
    assert gif_name == posted_gif


async def test_hop_after_restart_fetches_uncached_announce_channel(
    cog, text_channel, guild, clock
):
    member = fake_member()
    first_room = fake_voice_channel(channel_id=1, members=[member], guild=guild)
    await _update(cog, member, None, first_room)
    posted = text_channel.send.return_value
    started_at = clock.t
    notice = cog.notices.get(FEATURE_VOICE, guild.id, str(member.id))
    assert notice is not None
    notice.message = None
    text_channel.send.reset_mock()

    guild.get_channel.return_value = None
    cog.bot.get_channel.return_value = None
    cog.bot.fetch_channel = AsyncMock(return_value=text_channel)
    text_channel.get_partial_message.return_value = posted

    clock.advance(120)
    first_room.members = []
    second_room = fake_voice_channel(channel_id=2, members=[member], guild=guild)
    await _update(cog, member, first_room, second_room)

    text_channel.send.assert_not_awaited()
    posted.edit.assert_awaited_once()
    embed = posted.edit.await_args.kwargs["embed"]
    _assert_compact_notice(embed, member, second_room, started_at)


async def test_hop_into_an_occupied_channel_follows_the_user(
    cog, text_channel, guild
):
    first = fake_member("First", 1)
    second = fake_member("Second", 2)
    posts = _capture_posts(text_channel)
    lobby = fake_voice_channel(channel_id=1, members=[second], guild=guild)
    occupied = fake_voice_channel(channel_id=2, members=[first], guild=guild)
    await _update(cog, first, None, occupied)
    await _update(cog, second, None, lobby)
    second_notice = posts[3]
    text_channel.send.reset_mock()

    lobby.members = []
    occupied.members = [first, second]
    await _update(cog, second, lobby, occupied)

    text_channel.send.assert_not_awaited()
    second_notice.edit.assert_awaited_once()
    embed = second_notice.edit.await_args.kwargs["embed"]
    assert _channel_field(embed) == occupied.mention
    assert embed.author.name == second.display_name


async def test_failed_notice_edit_posts_again(cog, text_channel, guild):
    member = fake_member()
    first_room = fake_voice_channel(channel_id=1, members=[member], guild=guild)
    await _update(cog, member, None, first_room)
    posted = text_channel.send.return_value
    posted.edit = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), {"message": "fail"})
    )
    text_channel.send.reset_mock()

    first_room.members = []
    second_room = fake_voice_channel(channel_id=2, members=[member], guild=guild)
    await _update(cog, member, first_room, second_room)

    _assert_voice_posts(text_channel)
    assert _channel_field(_card_kwargs(text_channel)["embed"]) == (
        second_room.mention
    )


async def test_hop_when_card_is_gone_deletes_the_ping(cog, text_channel, guild):
    member = fake_member()
    posts = _capture_posts(text_channel)
    first_room = fake_voice_channel(channel_id=1, members=[member], guild=guild)
    await _update(cog, member, None, first_room)
    ping, card = posts[0], posts[1]
    card.edit = AsyncMock(
        side_effect=discord.NotFound(MagicMock(), {"message": "Unknown Message"})
    )
    text_channel.send.reset_mock()

    first_room.members = []
    second_room = fake_voice_channel(channel_id=2, members=[member], guild=guild)
    await _update(cog, member, first_room, second_room)

    ping.delete.assert_awaited_once()
    _assert_voice_posts(text_channel)
    assert _channel_field(_card_kwargs(text_channel)["embed"]) == (
        second_room.mention
    )


async def test_leave_starts_grace_if_member_list_still_has_leaver(
    cog, text_channel, guild
):
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)
    await _update(cog, member, None, voice)
    await _update(cog, member, voice, None)

    assert voice.id in cog.active_channels
    assert voice.id in cog._emptied_at


async def test_hop_does_not_restart_notice_ttl(cog, text_channel, guild, clock):
    member = fake_member()
    first_room = fake_voice_channel(channel_id=1, members=[member], guild=guild)
    await _update(cog, member, None, first_room)
    posted = text_channel.send.return_value

    clock.advance(NOTICE_TTL_SECONDS - 10)
    first_room.members = []
    second_room = fake_voice_channel(channel_id=2, members=[member], guild=guild)
    await _update(cog, member, first_room, second_room)
    posted.edit.assert_awaited()
    posted.delete.assert_not_awaited()

    clock.advance(10)
    await cog.notices.delete_expired()
    posted.delete.assert_awaited_once()


async def test_guild_remove_drops_voice_state(cog, text_channel, guild):
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)
    guild.voice_channels = [voice]
    await _update(cog, member, None, voice)

    await cog.on_guild_remove(guild)

    assert voice.id not in cog.active_channels
    assert not cog.notices
    assert cog._announced == set()


async def test_voice_channel_delete_drops_channel_state(cog, text_channel, guild):
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)
    await _update(cog, member, None, voice)
    voice.members = []
    await _update(cog, member, voice, None)
    assert voice.id in cog._emptied_at

    await cog.on_guild_channel_delete(voice)

    assert voice.id not in cog.active_channels
    assert voice.id not in cog._emptied_at


async def test_deleted_announce_channel_lets_a_new_channel_post(
    cog, text_channel, guild, store, clock
):
    member = fake_member()
    first_room = fake_voice_channel(channel_id=1, members=[member], guild=guild)
    await _update(cog, member, None, first_room)
    text_channel.send.reset_mock()

    await cog.on_guild_channel_delete(text_channel)
    assert not cog.notices

    new_channel = fake_text_channel(88, guild=guild)
    store.set_announce_channel_id(GUILD_ID, new_channel.id)
    guild.get_channel.side_effect = lambda channel_id: {
        new_channel.id: new_channel
    }.get(channel_id)

    first_room.members = []
    await _update(cog, member, first_room, None)
    clock.advance(RECONNECT_GRACE_SECONDS)
    first_room.members = [member]
    await _update(cog, member, None, first_room)

    new_channel.send.assert_awaited()
    text_channel.send.assert_not_awaited()


async def test_missing_notify_channel_does_not_mark_active(cog, text_channel, guild):
    guild.get_channel.return_value = None
    member = fake_member()
    voice = fake_voice_channel(members=[member], guild=guild)

    await _update(cog, member, None, voice)

    text_channel.send.assert_not_awaited()
    assert voice.id not in cog.active_channels
