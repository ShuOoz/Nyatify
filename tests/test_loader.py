from loader import selected_extensions


def test_all_cogs_load_when_no_filter():
    extensions = selected_extensions(None)
    assert "cogs.voice_notify" in extensions
    assert "cogs.admin" in extensions


def test_isolation_selects_named_cogs():
    assert selected_extensions(["voice"]) == ["cogs.voice_notify"]
    assert selected_extensions(["cogs.admin"]) == ["cogs.admin"]


def test_unknown_name_is_ignored():
    assert selected_extensions(["not_a_real_cog"]) == []
