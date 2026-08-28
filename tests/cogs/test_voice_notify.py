"""Isolated tests for the voice-notify function. No Discord login."""

from __future__ import annotations

import pytest

from cogs.voice_notify import VoiceNotify
from tests.helpers import (
    fake_bot,
    fake_member,
    fake_text_channel,
    fake_voice_channel,
    fake_voice_state,
)

NOTIFY_CHANNEL_ID = 99


@pytest.fixture
def text_channel():
    return fake_text_channel()


@pytest.fixture
def cog(text_channel):
    bot = fake_bot(get_channel_result=text_channel)
    return VoiceNotify(bot, channel_id=NOTIFY_CHANNEL_ID)


@pytest.mark.asyncio
async def test_first_join_of_empty_channel_sends_announcement(cog, text_channel):
    member = fake_member()
    voice = fake_voice_channel(members=[member])

    await cog.on_voice_state_update(
        member,
        fake_voice_state(channel=None),
        fake_voice_state(channel=voice),
    )

    text_channel.send.assert_awaited_once()
    kwargs = text_channel.send.await_args.kwargs
    assert "@everyone" in kwargs["content"]
    assert voice.id in cog.active_channels


@pytest.mark.asyncio
async def test_second_member_joining_does_not_announce_again(cog, text_channel):
    first = fake_member("First", 1)
    second = fake_member("Second", 2)
    voice = fake_voice_channel(members=[first])

    await cog.on_voice_state_update(
        first,
        fake_voice_state(channel=None),
        fake_voice_state(channel=voice),
    )
    text_channel.send.reset_mock()

    voice.members = [first, second]
    await cog.on_voice_state_update(
        second,
        fake_voice_state(channel=None),
        fake_voice_state(channel=voice),
    )

    text_channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_channel_can_announce_again_after_everyone_leaves(cog, text_channel):
    member = fake_member()
    voice = fake_voice_channel(members=[member])

    await cog.on_voice_state_update(
        member,
        fake_voice_state(channel=None),
        fake_voice_state(channel=voice),
    )
    voice.members = []
    await cog.on_voice_state_update(
        member,
        fake_voice_state(channel=voice),
        fake_voice_state(channel=None),
    )
    assert voice.id not in cog.active_channels

    text_channel.send.reset_mock()
    voice.members = [member]
    await cog.on_voice_state_update(
        member,
        fake_voice_state(channel=None),
        fake_voice_state(channel=voice),
    )
    text_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_notify_channel_does_not_crash(cog, text_channel):
    cog.bot.get_channel.return_value = None
    member = fake_member()
    voice = fake_voice_channel(members=[member])

    await cog.on_voice_state_update(
        member,
        fake_voice_state(channel=None),
        fake_voice_state(channel=voice),
    )

    text_channel.send.assert_not_awaited()
    assert voice.id in cog.active_channels


@pytest.mark.asyncio
async def test_moving_between_channels_is_ignored(cog, text_channel):
    member = fake_member()
    left = fake_voice_channel(channel_id=1, members=[])
    right = fake_voice_channel(channel_id=2, members=[member])

    await cog.on_voice_state_update(
        member,
        fake_voice_state(channel=left),
        fake_voice_state(channel=right),
    )

    text_channel.send.assert_not_awaited()
