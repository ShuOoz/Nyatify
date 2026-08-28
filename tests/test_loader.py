from __future__ import annotations

from loader import normalize_cog_name, selected_extensions


def test_all_cogs_load_when_no_filter():
    extensions = selected_extensions(None)
    assert "cogs.voice_notify" in extensions
    assert "cogs.admin" in extensions


def test_isolation_loads_only_named_function():
    extensions = selected_extensions(["voice_notify"])
    assert extensions == ["cogs.voice_notify"]


def test_isolation_accepts_full_extension_name():
    extensions = selected_extensions(["cogs.admin"])
    assert extensions == ["cogs.admin"]


def test_unknown_name_is_ignored():
    extensions = selected_extensions(["not_a_real_cog"])
    assert extensions == []


def test_normalize_strips_package_prefix():
    assert normalize_cog_name("cogs.voice_notify") == "voice_notify"
    assert normalize_cog_name("voice_notify") == "voice_notify"
