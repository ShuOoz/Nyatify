"""Helpers for the search card and the genre card."""

from __future__ import annotations

import discord

from music.panel import MARK_ADD

def _action_rows(view):
    return [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]

def _button(view, face):
    for row in _action_rows(view):
        for child in row.children:
            if getattr(child, "label", None) == face or getattr(child, "emoji", None) == face:
                return child
    for item in view.walk_children():
        accessory = getattr(item, "accessory", None)
        if getattr(accessory, "label", None) == face or getattr(accessory, "emoji", None) == face:
            return accessory
    raise AssertionError(face)

def _sections(view):
    return [
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.Section) and getattr(item.accessory, "label", None) == MARK_ADD
    ]

def _page_text(view) -> str:
    for item in view.walk_children():
        if isinstance(item, discord.ui.TextDisplay) and item.content.startswith("Page "):
            return item.content
    return ""
