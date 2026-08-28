from __future__ import annotations

from pathlib import Path

from config import Settings


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


def test_empty_enabled_cogs_means_all(tmp_path: Path):
    path = tmp_path / "config.txt"
    path.write_text("", encoding="utf-8")
    settings = Settings(environ={}, config_path=path)
    assert settings.enabled_cogs is None


def test_missing_token_raises(tmp_path: Path):
    path = tmp_path / "config.txt"
    path.write_text("", encoding="utf-8")
    settings = Settings(environ={}, config_path=path)
    try:
        settings.require_token()
    except RuntimeError as exc:
        assert "DISCORD_TOKEN" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
