"""Discover and select feature cogs without starting Discord."""

from __future__ import annotations

import logging
from pathlib import Path

COGS_DIR = Path(__file__).resolve().parent / "cogs"
COGS_PACKAGE = "cogs"

log = logging.getLogger(__name__)


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


def selected_extensions(enabled: list[str] | None) -> list[str]:
    """Return extension names to load.

    enabled=None or [] means every available cog.
    Otherwise only the named cogs are returned, so one function can run alone.
    """
    available = available_cog_stems()
    if not enabled:
        return [f"{COGS_PACKAGE}.{stem}" for stem in available]

    wanted = {normalize_cog_name(name) for name in enabled if name.strip()}
    unknown = wanted - set(available)
    if unknown:
        log.warning("Unknown function(s) in ENABLED_COGS: %s", ", ".join(sorted(unknown)))

    chosen = [stem for stem in available if stem in wanted]
    skipped = [stem for stem in available if stem not in wanted]
    if skipped:
        log.info("Isolation mode: skipping %s", ", ".join(skipped))
    return [f"{COGS_PACKAGE}.{stem}" for stem in chosen]
