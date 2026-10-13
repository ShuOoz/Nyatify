"""Load settings from the environment, then fall back to config.txt."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.txt"


def write_text_atomic(path: Path, text: str) -> None:
    """Write text by replacing the file, so a crash cannot leave it half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
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


def _flag(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _count(raw: str | None, default: int, name: str) -> int:
    """A whole number of at least 1. Blank uses `default`."""
    text = (raw or "").strip()
    if not text:
        return default
    if text.isdigit() and int(text) >= 1:
        return int(text)
    raise RuntimeError(f"{name} must be a whole number, 1 or more.")


def _proxy_list(raw: str | None) -> tuple[str, ...]:
    """Comma-separated proxy URLs, in order, without duplicates."""
    found: list[str] = []
    for part in (raw or "").split(","):
        item = part.strip()
        if item and item not in found:
            found.append(item)
    return tuple(found)


class Settings:
    def __init__(
        self, environ: Mapping[str, str] | None = None, config_path: Path | None = None,
    ) -> None:
        environ = os.environ if environ is None else environ
        file_values = _parse_config_file(config_path or CONFIG_PATH)

        def text(key: str) -> str:
            return (_first(environ, file_values, key) or "").strip()

        self.token = _first(environ, file_values, "DISCORD_TOKEN")
        self.command_prefix = _first(environ, file_values, "COMMAND_PREFIX") or "!"
        self.log_level = (_first(environ, file_values, "LOG_LEVEL") or "INFO").upper()

        enabled_raw = _first(environ, file_values, "ENABLED_COGS") or ""
        enabled = [part.strip() for part in enabled_raw.split(",") if part.strip()]
        self.enabled_cogs: list[str] | None = enabled or None

        # Password for /admin. Empty leaves owner commands off the slash menu.
        self.admin_password = text("ADMIN_PASSWORD") or None

        # Optional. Server ID for instant slash commands in that server only.
        # Empty publishes globally (can take up to an hour). The unused scope
        # is cleared so Discord does not list the same commands twice.
        raw_sync = text("SYNC_GUILD_ID")
        self.sync_guild_id: int | None = None
        if raw_sync:
            try:
                self.sync_guild_id = int(raw_sync)
            except ValueError as exc:
                raise RuntimeError("SYNC_GUILD_ID must be a Discord server ID (integer).") from exc

        raw_uid = text("BILI_COOKIE_UID")
        self.bili_cookie_uid = raw_uid if raw_uid.isdigit() else None
        self.bili_cookie = text("BILI_COOKIE") or None
        self.bili_ac_time = text("BILI_AC_TIME") or None
        self.weibo_cookie = text("WEIBO_COOKIES") or None
        # Optional. Bilibili takes a list. Each empty is direct.
        # Match scores and Discord stay direct.
        self.bili_proxies = _proxy_list(_first(environ, file_values, "BILI_PROXY"))
        self.bili_proxy = self.bili_proxies[0] if self.bili_proxies else None
        self.bili_phones_per_proxy = _count(
            _first(environ, file_values, "BILI_PHONES_PER_PROXY"), 1, "BILI_PHONES_PER_PROXY"
        )
        self.bili_accounts_per_phone = _count(
            _first(environ, file_values, "BILI_ACCOUNTS_PER_PHONE"), 2, "BILI_ACCOUNTS_PER_PHONE"
        )
        # Empty leaves the exit check on. 0 skips it for every proxy platform.
        raw_ip = text("IP_CHECK")
        self.ip_check = _flag(raw_ip) if raw_ip else True
        self.weibo_proxies = _proxy_list(_first(environ, file_values, "WEIBO_PROXY"))
        self.weibo_proxy = self.weibo_proxies[0] if self.weibo_proxies else None
        self.ai_proxy = text("AI_PROXY") or None

        self.sportsdb_key = text("THESPORTSDB_KEY") or None
        self.pandascore_token = text("PANDASCORE_TOKEN") or None

        self.ai_api_key = text("AI_API_KEY") or None
        self.ai_base_url = text("AI_BASE_URL").rstrip("/") or None
        self.ai_model = text("AI_MODEL") or None
        self.news_poll_match = _flag(_first(environ, file_values, "NEWS_POLL_MATCH"))
        self.news_poll_post = _flag(_first(environ, file_values, "NEWS_POLL_POST"))
        self.news_poll = self.news_poll_match or self.news_poll_post
        self.ai_on_poll = _flag(_first(environ, file_values, "AI_ON_POLL"))

    def require_token(self) -> str:
        if not self.token:
            raise RuntimeError(
                "DISCORD_TOKEN is missing. Set it as an environment variable or in config.txt."
            )
        return self.token


def upsert_config_values(path: Path, updates: dict[str, str]) -> None:
    """Set keys in a KEY=value file without dropping comments or other keys."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    write_text_atomic(path, ("\n".join(out) + "\n") if out else "")


settings = Settings()


def proxy_url(name: str) -> str | None:
    """weibo_proxy or ai_proxy, or the first Bilibili proxy. Read per call. None is direct."""
    return (getattr(settings, name, None) or "").strip() or None
