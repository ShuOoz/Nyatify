from __future__ import annotations

from pathlib import Path

import pytest

from config import Settings, write_text_atomic


def test_env_overrides_config_file(tmp_path: Path):
    path = tmp_path / "config.txt"
    path.write_text("DISCORD_TOKEN=file-token\nCOMMAND_PREFIX=?\n", encoding="utf-8")
    settings = Settings(
        environ={"DISCORD_TOKEN": "env-token", "COMMAND_PREFIX": "!"},
        config_path=path,
    )
    assert settings.token == "env-token"
    assert settings.command_prefix == "!"


def test_enabled_cogs_parses_comma_list(tmp_path: Path):
    path = tmp_path / "config.txt"
    path.write_text("", encoding="utf-8")
    settings = Settings(
        environ={"ENABLED_COGS": "voice_notify, admin"},
        config_path=path,
    )
    assert settings.enabled_cogs == ["voice_notify", "admin"]


def test_optional_settings_default_when_missing(tmp_path: Path):
    path = tmp_path / "config.txt"
    path.write_text("", encoding="utf-8")
    settings = Settings(environ={}, config_path=path)
    assert settings.enabled_cogs is None
    assert settings.sync_guild_id is None


def test_sync_guild_id_parses_int(tmp_path: Path):
    path = tmp_path / "config.txt"
    path.write_text("SYNC_GUILD_ID=123456789012345678\n", encoding="utf-8")
    settings = Settings(environ={}, config_path=path)
    assert settings.sync_guild_id == 123456789012345678


def test_missing_token_raises(tmp_path: Path):
    path = tmp_path / "config.txt"
    path.write_text("", encoding="utf-8")
    settings = Settings(environ={}, config_path=path)
    with pytest.raises(RuntimeError, match="DISCORD_TOKEN"):
        settings.require_token()


def test_write_text_atomic_replaces_the_file(tmp_path: Path):
    path = tmp_path / "nested" / "data.json"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")

    write_text_atomic(path, "new\n")

    assert path.read_text(encoding="utf-8") == "new\n"
    assert not path.with_name(f"{path.name}.tmp").exists()


def test_write_text_atomic_keeps_the_old_file_if_replace_fails(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "data.json"
    path.write_text("old\n", encoding="utf-8")

    def boom(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)

    with pytest.raises(OSError):
        write_text_atomic(path, "new\n")

    assert path.read_text(encoding="utf-8") == "old\n"
