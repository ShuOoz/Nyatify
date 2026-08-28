from __future__ import annotations

from nyatify import bot_intents


def test_all_cogs_request_voice_and_message_content():
    intents = bot_intents(None)
    assert intents.voice_states is True
    assert intents.message_content is True
    assert intents.members is True


def test_admin_isolation_does_not_request_voice_states():
    intents = bot_intents(["admin"])
    assert intents.voice_states is False
    assert intents.message_content is True
    assert intents.members is True


def test_voice_isolation_does_not_request_message_content():
    intents = bot_intents(["voice"])
    assert intents.voice_states is True
    assert intents.message_content is False
    assert intents.members is True
