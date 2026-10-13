"""Thin esports banners attached on a match card. 1500×500, 3:1."""

from __future__ import annotations

from typing import Any

from config import BASE_DIR
from news.source.post.common import CardPicture

_DIR = BASE_DIR / "image" / "news_image" / "banners"
_BY_GAME = {
    "valorant": _DIR / "valorant.jpg",
    "counter-strike": _DIR / "cs2.jpg",
    "counter-strike 2": _DIR / "cs2.jpg",
    "cs2": _DIR / "cs2.jpg",
    "lol": _DIR / "lol.jpg",
    "league of legends": _DIR / "lol.jpg",
}
_FALLBACK = _DIR / "vs.jpg"
_LOADED: dict[str, bytes] = {}


def banner_picture(item: Any) -> CardPicture | None:
    if getattr(item, "platform", None) != "pandascore":
        return None
    path = _FALLBACK
    tags = getattr(item, "tags", ()) or ()
    for tag in tags:
        found = _BY_GAME.get(str(tag))
        if found is not None and found.is_file():
            path = found
            break
    if not path.is_file():
        return None
    key = str(path)
    data = _LOADED.get(key)
    if data is None:
        data = path.read_bytes()
        _LOADED[key] = data
    return CardPicture(data, path.name)
