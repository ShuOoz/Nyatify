from __future__ import annotations

from loader import (
    extension_name,
    required_intent_names,
    selected_extensions,
)


def test_all_cogs_load_when_no_filter():
    extensions = selected_extensions(None)
    assert "cogs.voice_notify" in extensions
    assert "cogs.admin" in extensions


def test_isolation_selects_named_cogs():
    assert selected_extensions(["voice"]) == ["cogs.voice_notify"]
    assert selected_extensions(["cogs.admin"]) == ["cogs.admin"]


def test_unknown_name_is_ignored():
    assert selected_extensions(["not_a_real_cog"]) == []


def test_extension_name_maps_aliases():
    assert extension_name("voice") == "cogs.voice_notify"
    assert extension_name("voice_notify") == "cogs.voice_notify"


def test_isolation_requests_only_that_cogs_intents():
    assert required_intent_names(["voice"]) == ("voice_states",)
    assert required_intent_names(["admin"]) == ("message_content",)


def test_all_cogs_union_their_intents():
    names = required_intent_names(None)
    assert "voice_states" in names
    assert "message_content" in names
