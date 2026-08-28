"""Load settings from the environment, then fall back to config.txt."""

from __future__ import annotations

import os
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


class Settings:
    def __init__(self) -> None:
        file_values = _parse_config_file(CONFIG_PATH)

        self.token = os.getenv("DISCORD_TOKEN") or file_values.get("DISCORD_TOKEN")
        self.command_prefix = (
            os.getenv("COMMAND_PREFIX")
            or file_values.get("COMMAND_PREFIX")
            or "!"
        )

        notify_raw = (
            os.getenv("VOICE_NOTIFY_CHANNEL_ID")
            or file_values.get("VOICE_NOTIFY_CHANNEL_ID")
        )
        self.voice_notify_channel_id = int(notify_raw) if notify_raw else None

    def require_token(self) -> str:
        if not self.token:
            raise RuntimeError(
                "DISCORD_TOKEN is missing. Set it as an environment variable "
                "or in config.txt."
            )
        return self.token


settings = Settings()
