"""Load settings from the environment, then fall back to config.txt."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.txt"


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write text by replacing the file, so a crash cannot leave it half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", encoding=encoding) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(path)


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

        enabled_raw = _first(environ, file_values, "ENABLED_COGS") or ""
        enabled = [part.strip() for part in enabled_raw.split(",") if part.strip()]
        self.enabled_cogs: list[str] | None = enabled or None

        # Optional. Set to a server ID so slash commands appear immediately while testing.
        raw_sync = (_first(environ, file_values, "SYNC_GUILD_ID") or "").strip()
        self.sync_guild_id: int | None = None
        if raw_sync:
            try:
                self.sync_guild_id = int(raw_sync)
            except ValueError as exc:
                raise RuntimeError(
                    "SYNC_GUILD_ID must be a Discord server ID (integer)."
                ) from exc

    def require_token(self) -> str:
        if not self.token:
            raise RuntimeError(
                "DISCORD_TOKEN is missing. Set it as an environment variable "
                "or in config.txt."
            )
        return self.token


settings = Settings()
