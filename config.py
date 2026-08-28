"""Load settings from the environment, then fall back to config.txt."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.txt"


def _parse_config_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _first(environ: Mapping[str, str], file_values: dict[str, str], key: str) -> str | None:
    return environ.get(key) or file_values.get(key)


class Settings:
    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        config_path: Path | None = None,
    ) -> None:
        environ = os.environ if environ is None else environ
        file_values = _parse_config_file(config_path or CONFIG_PATH)

        self.token = _first(environ, file_values, "DISCORD_TOKEN")
        self.command_prefix = _first(environ, file_values, "COMMAND_PREFIX") or "!"
        self.log_level = (_first(environ, file_values, "LOG_LEVEL") or "INFO").upper()

        notify_raw = _first(environ, file_values, "VOICE_NOTIFY_CHANNEL_ID")
        self.voice_notify_channel_id = int(notify_raw) if notify_raw else None

        enabled_raw = _first(environ, file_values, "ENABLED_COGS") or ""
        enabled = [part.strip() for part in enabled_raw.split(",") if part.strip()]
        self.enabled_cogs: list[str] | None = enabled or None

    def require_token(self) -> str:
        if not self.token:
            raise RuntimeError(
                "DISCORD_TOKEN is missing. Set it as an environment variable "
                "or in config.txt."
            )
        return self.token


settings = Settings()
