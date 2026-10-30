"""Helpers for the player card and the album."""

from __future__ import annotations


def _fitted(mark: str, *, hair: int = 0) -> str:
    """Two halfwidth fillers on each side, so the mark stays centered."""
    side = "\uffa0" * 2
    gap = "\u200a" * hair
    return f"{side}{gap}{mark}{gap}{side}"
