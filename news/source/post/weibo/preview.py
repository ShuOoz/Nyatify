"""Weibo photo and video preview.

sinaimg refuses a fetch that does not come from Weibo, so Discord cannot load
the picture URL. This module picks the first photo, or the video cover when
the post has no photo, and downloads that file with a Weibo referer.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from fetch import http_url
from news.source.post.common import CardPicture, pixel
from .http import WEIBO_HEADERS, _weibo_session, request_proxy

_REFERER = "https://weibo.com/"
_MAX_BYTES = 2_000_000
_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}

log = logging.getLogger(__name__)


def cover_url(mblog: Mapping[str, Any]) -> str | None:
    """First photo on the post, or the video cover when there is no photo."""
    orig = mblog.get("retweeted_status")
    for post in (mblog, orig if isinstance(orig, dict) else {}):
        if found := _photo_url(post) or _video_cover(post):
            return found
    return None


def cover_frame(mblog: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Pixel size of the cover `cover_url` would pick, when Weibo sent one."""
    orig = mblog.get("retweeted_status")
    found = _photo(mblog) or (_photo(orig) if isinstance(orig, dict) else None)
    return (found[1], found[2]) if found else (None, None)


async def item_cover(item: Any, *, call: Any | None = None) -> CardPicture | None:
    """Downloaded preview for a Weibo post. card_picture swaps a long, heavy, or unusable photo."""
    url = getattr(item, "image_url", None)
    if getattr(item, "platform", None) not in {"weibo", "wb"} or not url:
        return None
    return await fetch_cover(url, call=call)


async def fetch_cover(
    url: str, *, call: Any | None = None, name: str = "cover",
) -> CardPicture | None:
    """Download one sinaimg preview. `call` replaces the HTTP GET in tests."""
    found = _image_url(url)
    if found is None:
        return None
    headers = {
        "User-Agent": WEIBO_HEADERS["User-Agent"],
        "Referer": _REFERER,
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
    }
    try:
        if call is not None:
            status, content_type, data = await call(found, headers)
        else:
            status, content_type, data = await _download(found, headers)
    except Exception:
        log.warning("Could not download Weibo preview %s", found, exc_info=True)
        return None
    if status != 200 or not data or len(data) > _MAX_BYTES:
        return None
    suffix = _IMAGE_TYPES.get(content_type.split(";", 1)[0].strip().lower())
    if suffix is None:
        return None
    return CardPicture(data, f"{name}{suffix}")


def _photo_url(mblog: Mapping[str, Any]) -> str | None:
    found = _photo(mblog)
    return found[0] if found else None


def _photo(mblog: Mapping[str, Any]) -> tuple[str, int | None, int | None] | None:
    pics = mblog.get("pics")
    if not isinstance(pics, list):
        return None
    for pic in pics:
        if not isinstance(pic, dict) or str(pic.get("type") or "") == "video":
            continue
        large = pic.get("large") if isinstance(pic.get("large"), dict) else {}
        found = _image_url(large.get("url") or pic.get("url"))
        if found:
            return found, *_frame(pic)
    return None


def _frame(pic: Mapping[str, Any]) -> tuple[int | None, int | None]:
    large = pic.get("large") if isinstance(pic.get("large"), dict) else {}
    for source in (large, pic):
        geo = source.get("geo") if isinstance(source.get("geo"), dict) else source
        if not isinstance(geo, dict):
            continue
        width = pixel(geo.get("width"))
        height = pixel(geo.get("height"))
        if width and height:
            return width, height
    return None, None


def _video_cover(mblog: Mapping[str, Any]) -> str | None:
    page = mblog.get("page_info") if isinstance(mblog.get("page_info"), dict) else {}
    if str(page.get("type") or "") != "video":
        return None
    pic = page.get("page_pic") if isinstance(page.get("page_pic"), dict) else {}
    raw = pic.get("url") if pic else page.get("page_pic")
    return _image_url(raw)


def _image_url(url: object) -> str | None:
    text = str(url or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    found = http_url(text)
    if found is None:
        return None
    host = (urlsplit(found).hostname or "").lower()
    return found if host == "sinaimg.cn" or host.endswith(".sinaimg.cn") else None


async def _download(url: str, headers: dict[str, str]) -> tuple[int, str, bytes]:
    session = await _weibo_session()
    kwargs: dict[str, Any] = {"headers": headers}
    proxy = request_proxy()
    if proxy:
        kwargs["proxy"] = proxy
    async with session.get(url, **kwargs) as response:
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > _MAX_BYTES:
            return response.status, "", b""
        content_type = response.headers.get("Content-Type") or ""
        data = await response.read()
        return response.status, content_type, data
