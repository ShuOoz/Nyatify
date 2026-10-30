import subprocess
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
    import music.player.card  # noqa: F401
    import music.play.search  # noqa: F401

    once, cyclic = _reload_order(_loaded_feature_modules(("music",)))
    order = once + cyclic
    assert order.index("music.source") < order.index("music.lavalink")
    assert order.index("music.lavalink") < order.index("music.playback")
    assert order.index("music.panel") < order.index("music.play.search")
    # Playback imports itself in a loop. Alphabetical order reloads the player
    # before the saved-song class, so the reload has to rebind that class.
    assert cyclic.index("music.playback.control") < cyclic.index("music.playback.remember")


def test_a_reload_keeps_the_queue_and_the_history():
    """The session stays up when music is reloaded, and the file still has it."""
    script = r"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import music.archive as archive
import music.playback as playback
import music.playback.remember as remember
import music.player.card  # noqa: F401
import music.playlist.card  # noqa: F401
from loader import reload_packages

tmp = Path(tempfile.mkdtemp()) / "music_session.json"
archive.PATH = tmp
archive._book = None
archive._cache_path = None
playback._lines.clear()
playback._heard.clear()
playback._missing.clear()

import music.playback.start as start

start._pause_token[42] = 7
remember._install(42, {
    "current": {
        "title": "Easy",
        "author": "Mac",
        "url": "https://example.com/easy",
        "platform": "apple",
        "length": 1000,
        "requester_id": 7,
    },
    "later": [{
        "title": "Next",
        "author": "Ada",
        "url": "https://example.com/next",
        "platform": "youtube",
        "length": 1000,
        "requester_id": 7,
    }],
    "history": [{
        "title": "Old",
        "author": "Bea",
        "url": "https://example.com/old",
        "platform": "spotify",
        "length": 1000,
        "requester_id": 7,
    }],
    "paused": True,
})
guild = MagicMock()
guild.id = 42
# The bot is in the room, so the song counts as on the air and the line keeps that.
guild.voice_client = MagicMock()
remember._lines[42].resting = False
assert [row.title for row in playback.queued(guild)] == ["Easy", "Next"]
assert [row.title for row in playback.played(guild)] == ["Old"]

reload_packages(("music",))
if archive.PATH != tmp:
    raise SystemExit("reload moved the session file")
assert remember._lines[42].resting is False
assert start._pause_token[42] == 7
assert [row.title for row in playback.queued(guild)] == ["Easy", "Next"]
assert [row.title for row in playback.played(guild)] == ["Old"]

playback.save_open_lines()
playback._lines.clear()
playback._heard.clear()
playback._missing.clear()
playback.load_saved_lines()
assert [row.title for row in playback.queued(guild)] == ["Easy", "Next"]
assert [row.title for row in playback.played(guild)] == ["Old"]
"""
    subprocess.check_call(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
    )


def test_reloading_music_keeps_the_saved_song_class():
    """The player and the song list must share the class a reload just defined."""
    script = """
from loader import reload_packages
import music.playback as playback
import music.playback.control as control
import music.playback.remember as remember
import music.playback.start as start
import music.player.card as card

reload_packages(("music",))
assert control._Song is remember._Song
assert control._Line is remember._Line
assert start._begin is control._begin
assert playback.toggle_pause is control.toggle_pause
assert card.toggle_pause is control.toggle_pause
"""
    subprocess.check_call(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
    )


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


def test_reload_packages_keeps_a_class_across_a_longer_cycle(tmp_path, monkeypatch):
    """The importer reloads before the class. Both must still be the same object."""
    root = tmp_path / "class_root"
    package = root / "nyatify_reload_class"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "a.py").write_text(
        "anchor = 'a1'\nfrom nyatify_reload_class.b import Box\n"
    )
    (package / "b.py").write_text(
        "class Box:\n    generation = 1\n\nfrom nyatify_reload_class.c import generation\n"
    )
    (package / "c.py").write_text(
        "generation = 'c1'\nfrom nyatify_reload_class.a import anchor\n"
    )
    (package / "d.py").write_text(
        "from nyatify_reload_class.a import Box as Seen\n"
    )
    monkeypatch.syspath_prepend(str(root))
    try:
        import nyatify_reload_class.a as cycle_a
        import nyatify_reload_class.b as cycle_b
        import nyatify_reload_class.d as cycle_d

        (package / "a.py").write_text(
            "anchor = 'a2'\nfrom nyatify_reload_class.b import Box\n"
        )
        (package / "b.py").write_text(
            "class Box:\n    generation = 2\n\nfrom nyatify_reload_class.c import generation\n"
        )
        (package / "c.py").write_text(
            "generation = 'c2'\nfrom nyatify_reload_class.a import anchor\n"
        )
        reload_packages(("nyatify_reload_class",))
        assert cycle_a.Box is cycle_b.Box
        assert cycle_d.Seen is cycle_b.Box
        assert cycle_a.Box.generation == 2
        assert isinstance(cycle_b.Box(), cycle_a.Box)
    finally:
        _drop_modules("nyatify_reload_class")


def _drop_modules(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(f"{prefix}."):
            del sys.modules[name]
