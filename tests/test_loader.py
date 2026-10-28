import sys
from pathlib import Path

from loader import (
    _loaded_feature_modules,
    _reload_order,
    cog_feature_packages,
    reload_packages,
    selected_extensions,
)


def test_selected_extensions():
    extensions = selected_extensions(None)
    assert "cogs.voice_notify" in extensions
    assert "cogs.news_notify" in extensions
    assert "cogs.admin" in extensions
    assert selected_extensions(["voice"]) == ["cogs.voice_notify"]
    assert selected_extensions(["news"]) == ["cogs.news_notify"]
    assert selected_extensions(["music"]) == ["cogs.osa_musify"]
    assert selected_extensions(["osa_musify"]) == ["cogs.osa_musify"]
    assert selected_extensions(["cogs.admin"]) == ["cogs.admin"]
    assert selected_extensions(["not_a_real_cog"]) == []


def test_feature_packages_follow_the_cog_that_owns_them():
    voice = cog_feature_packages("voice")
    news = cog_feature_packages("news")
    music = cog_feature_packages("music")
    assert voice == ()
    assert news == ("ai", "fetch", "news")
    assert music == ("music",)
    assert cog_feature_packages("osa_musify") == music
    assert cog_feature_packages("admin") == ()
    assert set(voice).isdisjoint(news)
    assert set(voice).isdisjoint(music)
    assert set(news).isdisjoint(music)
    shared = {"announce", "notice", "guild", "config", "loader", "nyatify"}
    assert shared.isdisjoint(voice)
    assert shared.isdisjoint(news)
    assert shared.isdisjoint(music)


def test_music_modules_reload_dependencies_first():
    import music.player.view  # noqa: F401
    import music.play.search  # noqa: F401

    once, cyclic = _reload_order(_loaded_feature_modules(("music",)))
    order = once + cyclic
    assert order.index("music.source") < order.index("music.lavalink")
    assert order.index("music.lavalink") < order.index("music.playback")
    assert order.index("music.panel") < order.index("music.play.search")
    assert order.index("music.play.search") < order.index("music.play")
    assert order.index("music.playback") < order.index("music.play")


def test_reload_packages_picks_up_an_imported_name(tmp_path, monkeypatch):
    root = tmp_path / "reload_root"
    package = root / "nyatify_reload_fixture"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "leaf.py").write_text("VALUE = 1\n")
    (package / "top.py").write_text("from nyatify_reload_fixture.leaf import VALUE\n")
    monkeypatch.syspath_prepend(str(root))
    try:
        import nyatify_reload_fixture.top as top

        (package / "leaf.py").write_text("VALUE = 2\n")
        reloaded = reload_packages(("nyatify_reload_fixture",))
        assert reloaded.index("nyatify_reload_fixture.leaf") < reloaded.index(
            "nyatify_reload_fixture.top"
        )
        assert top.VALUE == 2
    finally:
        _drop_modules("nyatify_reload_fixture")


def test_reload_packages_restores_modules_when_a_later_one_fails(tmp_path, monkeypatch):
    root = tmp_path / "restore_root"
    package = root / "nyatify_reload_restore"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "leaf.py").write_text("VALUE = 1\n")
    (package / "top.py").write_text("from nyatify_reload_restore.leaf import VALUE\n")
    monkeypatch.syspath_prepend(str(root))
    try:
        import nyatify_reload_restore.leaf as leaf
        import nyatify_reload_restore.top  # noqa: F401

        (package / "leaf.py").write_text("VALUE = 2\n")
        (package / "top.py").write_text("def broken(\n")
        try:
            reload_packages(("nyatify_reload_restore",))
        except SyntaxError:
            pass
        else:
            raise AssertionError("broken module should not reload")
        assert leaf.VALUE == 1
    finally:
        _drop_modules("nyatify_reload_restore")


def test_reload_packages_refreshes_a_cycle(tmp_path, monkeypatch):
    root = tmp_path / "cycle_root"
    package = root / "nyatify_reload_cycle"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "a.py").write_text(
        "marker = 'a1'\nfrom nyatify_reload_cycle.b import marker as other\n"
    )
    (package / "b.py").write_text(
        "marker = 'b1'\nfrom nyatify_reload_cycle.a import marker as other\n"
    )
    monkeypatch.syspath_prepend(str(root))
    try:
        import nyatify_reload_cycle.a as cycle_a
        import nyatify_reload_cycle.b as cycle_b

        Path(package / "a.py").write_text(
            "marker = 'a2'\nfrom nyatify_reload_cycle.b import marker as other\n"
        )
        Path(package / "b.py").write_text(
            "marker = 'b2'\nfrom nyatify_reload_cycle.a import marker as other\n"
        )
        reload_packages(("nyatify_reload_cycle",))
        assert cycle_a.other == "b2"
        assert cycle_b.other == "a2"
    finally:
        _drop_modules("nyatify_reload_cycle")


def _drop_modules(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(f"{prefix}."):
            del sys.modules[name]
