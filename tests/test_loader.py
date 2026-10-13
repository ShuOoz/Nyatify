from loader import selected_extensions


def test_selected_extensions():
    extensions = selected_extensions(None)
    assert "cogs.voice_notify" in extensions
    assert "cogs.news_notify" in extensions
    assert "cogs.admin" in extensions
    assert selected_extensions(["voice"]) == ["cogs.voice_notify"]
    assert selected_extensions(["news"]) == ["cogs.news_notify"]
    assert selected_extensions(["cogs.admin"]) == ["cogs.admin"]
    assert selected_extensions(["not_a_real_cog"]) == []
