"""Per-server settings stored on disk.

Each guild can set one default announcement channel for every function,
plus an optional override per function.
Cogs call register_announce_feature when they load, and unregister when they
unload, so this module does not name them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from config import BASE_DIR, write_text_atomic

log = logging.getLogger(__name__)

DEFAULT_PATH = BASE_DIR / "data" / "guild.json"
_LEGACY_NAME = "guild_config.json"

# alias (including the canonical name) -> canonical feature name
_aliases: dict[str, str] = {}


def _feature_key(name: str) -> str:
    key = name.strip().lower()
    prefix = "cogs."
    if key.startswith(prefix):
        key = key[len(prefix) :]
    return key


def register_announce_feature(canonical: str, *aliases: str) -> None:
    """Register a notify function that can have its own announce channel."""
    name = _feature_key(canonical)
    if not name:
        raise ValueError("Announce function name is empty")
    _aliases[name] = name
    for alias in aliases:
        key = _feature_key(alias)
        if key:
            _aliases[key] = name


def unregister_announce_feature(canonical: str) -> None:
    """Drop a function from the registry when its cog unloads."""
    name = _feature_key(canonical)
    if not name:
        return
    for key, value in list(_aliases.items()):
        if value == name:
            _aliases.pop(key, None)


def announce_features() -> tuple[str, ...]:
    return tuple(sorted(set(_aliases.values())))


def canonical_announce_feature(name: str) -> str | None:
    return _aliases.get(_feature_key(name))


def parse_snowflake(value: Any) -> int | None:
    try:
        snowflake = int(value)
    except (TypeError, ValueError):
        return None
    return snowflake if snowflake > 0 else None


def _adopt_legacy_guild_file(path: Path) -> None:
    """Move data/guild_config.json to guild.json once. A newer guild.json wins."""
    if path.name != "guild.json" or path.exists():
        return
    legacy = path.with_name(_LEGACY_NAME)
    if not legacy.is_file():
        return
    legacy.replace(path)


class GuildConfig:
    """JSON map of guild id -> default and per-function announce channels."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH
        _adopt_legacy_guild_file(self.path)
        self._data: dict[str, Any] | None = None

    def get_default_announce_channel_id(self, guild_id: int) -> int | None:
        return parse_snowflake(self._guild_entry(guild_id).get("announce_channel_id"))

    def get_feature_override_id(self, guild_id: int, feature: str) -> int | None:
        return self._override_id(self._guild_entry(guild_id), feature)

    def get_feature_overrides(self, guild_id: int) -> dict[str, int]:
        features = self._guild_entry(guild_id).get("features")
        if not isinstance(features, dict):
            return {}
        overrides: dict[str, int] = {}
        for name, raw in features.items():
            canon = canonical_announce_feature(str(name))
            channel_id = parse_snowflake(raw)
            if canon is not None and channel_id is not None:
                overrides[canon] = channel_id
        return overrides

    def set_announce_channel_id(self, guild_id: int, channel_id: int) -> None:
        """Point every function at one channel and drop per-function overrides."""
        data, entry = self._mutable_guild(guild_id)
        entry["announce_channel_id"] = channel_id
        entry.pop("features", None)
        entry.pop("setup_hint_sent", None)
        entry.pop("fallback_hints", None)
        self._save(data)

    def set_feature_announce_channel_id(
        self,
        guild_id: int,
        feature: str,
        channel_id: int,
    ) -> None:
        canon = canonical_announce_feature(feature)
        if canon is None:
            raise ValueError(f"Unknown announce function: {feature}")
        data, entry = self._mutable_guild(guild_id)
        features = self._ensure_map(entry, "features")
        self._drop_feature_keys(features, canon)
        features[canon] = channel_id
        entry.pop("setup_hint_sent", None)
        hints = entry.get("fallback_hints")
        if isinstance(hints, dict):
            hints.pop(canon, None)
            if not hints:
                entry.pop("fallback_hints", None)
        self._save(data)

    def clear_feature_announce_channel_id(self, guild_id: int, feature: str) -> None:
        canon = canonical_announce_feature(feature)
        if canon is None:
            return
        data = self._load()
        guilds = data.get("guilds", {})
        entry = guilds.get(str(guild_id))
        if not isinstance(entry, dict):
            return
        features = entry.get("features")
        if not isinstance(features, dict):
            return
        self._drop_feature_keys(features, canon)
        if not features:
            entry.pop("features", None)
        self._drop_empty_guild(guilds, guild_id)
        self._save(data)

    def remove_guild(self, guild_id: int) -> None:
        data = self._load()
        guilds = data.get("guilds", {})
        if guilds.pop(str(guild_id), None) is not None:
            self._save(data)

    def forget_announce_channel(
        self,
        guild_id: int,
        channel_id: int,
    ) -> tuple[bool, tuple[str, ...]]:
        """Drop stored ids that pointed at this channel.

        Feature overrides are cleared so those functions use the default again.
        Returns whether the default was cleared, and which functions were reset.
        """
        snapshot = self._guild_entry(guild_id)
        if not snapshot:
            return False, ()

        default_id = parse_snowflake(snapshot.get("announce_channel_id"))
        default_cleared = default_id == channel_id

        dropped: list[str] = []
        features_snap = snapshot.get("features")
        if isinstance(features_snap, dict):
            for name, stored in features_snap.items():
                canon = canonical_announce_feature(str(name))
                stored_id = parse_snowflake(stored)
                if canon is None or stored_id != channel_id or canon in dropped:
                    continue
                dropped.append(canon)

        if not default_cleared and not dropped:
            return False, ()

        data, entry = self._mutable_guild(guild_id)
        if default_cleared:
            entry.pop("announce_channel_id", None)
        features = entry.get("features")
        if dropped and isinstance(features, dict):
            for canon in dropped:
                self._drop_feature_keys(features, canon)
            if not features:
                entry.pop("features", None)
        self._drop_empty_guild(data.get("guilds", {}), guild_id)
        self._save(data)
        return default_cleared, tuple(dropped)

    def guild_ids(self) -> list[int]:
        ids: list[int] = []
        for guild_key in self._load().get("guilds", {}):
            guild_id = parse_snowflake(guild_key)
            if guild_id is not None:
                ids.append(guild_id)
        return ids

    def guild_list(self, guild_id: int, key: str) -> list[Any]:
        """Copy of a per-guild JSON list, or [] if missing."""
        raw = self._guild_entry(guild_id).get(key)
        return list(raw) if isinstance(raw, list) else []

    def set_guild_list(self, guild_id: int, key: str, items: list[Any]) -> None:
        data, entry = self._mutable_guild(guild_id)
        if items:
            entry[key] = list(items)
        else:
            entry.pop(key, None)
            self._drop_empty_guild(data.get("guilds", {}), guild_id)
        self._save(data)

    def should_send_setup_hint(self, guild_id: int) -> bool:
        return not bool(self._guild_entry(guild_id).get("setup_hint_sent"))

    def mark_setup_hint_sent(self, guild_id: int) -> None:
        data, entry = self._mutable_guild(guild_id)
        entry["setup_hint_sent"] = True
        self._save(data)

    def should_send_fallback_hint(self, guild_id: int, feature: str) -> bool:
        canon = canonical_announce_feature(feature)
        if canon is None:
            return False
        hints = self._guild_entry(guild_id).get("fallback_hints")
        if not isinstance(hints, dict):
            return True
        return not bool(hints.get(canon))

    def mark_fallback_hint_sent(self, guild_id: int, feature: str) -> None:
        canon = canonical_announce_feature(feature)
        if canon is None:
            return
        data, entry = self._mutable_guild(guild_id)
        self._ensure_map(entry, "fallback_hints")[canon] = True
        self._save(data)

    def _mutable_guild(self, guild_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        data = self._load()
        entry = data.setdefault("guilds", {}).setdefault(str(guild_id), {})
        return data, entry

    @staticmethod
    def _ensure_map(entry: dict[str, Any], key: str) -> dict[str, Any]:
        value = entry.get(key)
        if isinstance(value, dict):
            return value
        entry[key] = {}
        return entry[key]

    @staticmethod
    def _drop_feature_keys(features: dict[str, Any], canon: str) -> None:
        for key in list(features):
            if canonical_announce_feature(str(key)) == canon:
                features.pop(key, None)

    @staticmethod
    def _drop_empty_guild(guilds: dict[str, Any], guild_id: int) -> None:
        if not guilds.get(str(guild_id)):
            guilds.pop(str(guild_id), None)

    @staticmethod
    def _override_id(entry: dict[str, Any], feature: str) -> int | None:
        canon = canonical_announce_feature(feature)
        if canon is None:
            return None
        features = entry.get("features")
        if not isinstance(features, dict):
            return None
        stored = features.get(canon)
        if stored is None:
            for name, raw in features.items():
                if canonical_announce_feature(str(name)) == canon:
                    stored = raw
                    break
        return parse_snowflake(stored)

    def _guild_entry(self, guild_id: int) -> dict[str, Any]:
        guilds = self._load().get("guilds", {})
        entry = guilds.get(str(guild_id), {})
        return entry if isinstance(entry, dict) else {}

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            self._data = self._read()
        return self._data

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"guilds": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("Could not read %s; using empty guild config.", self.path)
            return {"guilds": {}}
        if not isinstance(data, dict):
            return {"guilds": {}}
        guilds = data.get("guilds")
        if not isinstance(guilds, dict):
            data["guilds"] = {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, indent=2, sort_keys=True)
        write_text_atomic(self.path, payload + "\n")
        self._data = data


guild_config = GuildConfig()
