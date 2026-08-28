"""Discover and select feature cogs without starting Discord."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

COGS_DIR = Path(__file__).resolve().parent / "cogs"
COGS_PACKAGE = "cogs"

log = logging.getLogger(__name__)


# Module stem -> short name shown in slash/prefix commands.
# Unlisted cogs use the filename (without .py).
COG_PUBLIC_NAMES = {
    "voice_notify": "voice",
}
_STEM_FROM_PUBLIC = {public: stem for stem, public in COG_PUBLIC_NAMES.items()}


def available_cog_stems() -> list[str]:
    return sorted(
        path.stem
        for path in COGS_DIR.glob("*.py")
        if not path.name.startswith("_")
    )


def normalize_cog_name(name: str) -> str:
    name = name.strip()
    prefix = f"{COGS_PACKAGE}."
    if name.startswith(prefix):
        return name[len(prefix) :]
    return name


def public_cog_name(stem: str) -> str:
    key = normalize_cog_name(stem)
    return COG_PUBLIC_NAMES.get(key, key)


def resolve_cog_stem(name: str) -> str:
    """Turn a command name or alias into the Python module stem."""
    key = normalize_cog_name(name)
    if key in COG_PUBLIC_NAMES:
        return key
    return _STEM_FROM_PUBLIC.get(key, key)


def public_cog_names() -> list[str]:
    return [public_cog_name(stem) for stem in available_cog_stems()]


def extension_name(name: str) -> str:
    return f"{COGS_PACKAGE}.{resolve_cog_stem(name)}"


def selected_extensions(enabled: list[str] | None) -> list[str]:
    """Return extension names to load.

    enabled=None or [] means every available cog.
    Otherwise only the named cogs are returned, so one cog can run alone.
    """
    available = available_cog_stems()
    chosen = available
    if enabled:
        wanted = {resolve_cog_stem(name) for name in enabled if name.strip()}
        unknown = wanted - set(available)
        if unknown:
            log.warning(
                "Unknown cog(s) in ENABLED_COGS: %s", ", ".join(sorted(unknown))
            )
        chosen = [stem for stem in available if stem in wanted]
        skipped = [stem for stem in available if stem not in wanted]
        if skipped:
            log.info("Isolation mode: skipping %s", ", ".join(skipped))
    return [f"{COGS_PACKAGE}.{stem}" for stem in chosen]


def required_intent_names(enabled: list[str] | None) -> tuple[str, ...]:
    """Union of REQUIRED_INTENTS from the cogs that will load."""
    names: list[str] = []
    seen: set[str] = set()
    for extension in selected_extensions(enabled):
        module = importlib.import_module(extension)
        for name in getattr(module, "REQUIRED_INTENTS", ()):
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    return tuple(names)
