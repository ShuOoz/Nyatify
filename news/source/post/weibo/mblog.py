"""Weibo mblog cards: getIndex JSON to FetchedItem.

GET /api/container/getIndex?containerid=230413{uid} on the warmed m.weibo.cn
session. First page only. card_type 9 is an mblog; 11 unwraps a group.
A pinned card (`isTop` or a 置顶 title) is placed after the regular timeline.
The same post already in that timeline stays there and is not repeated.
Missing `data` retries on the same session. Guest SUB from genvisitor2 is enough for public accounts. Login SUB is optional.
Item links are the desktop page weibo.com/{uid}/{bid}. `mblogid` or `bid`
on the card is used when present; otherwise the numeric mid is encoded.
`image_url` is the sinaimg photo or video cover. Discord cannot fetch that
host, so the card downloads it through `preview.fetch_cover`.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from fetch import FetchedItem, FetchError, http_url, redact_fetch_url
from .http import weibo_json, weibo_pause, _retry_pause
from .preview import cover_frame, cover_url

WEIBO_INDEX_URL = "https://m.weibo.cn/api/container/getIndex"
_WEB_URL = "https://weibo.com/{uid}/{bid}"
_DETAIL_URL = "https://m.weibo.cn/detail/{id}"
_PLATFORM = "weibo"
_CONTAINER = "230413"
_DATA_TRIES = 3
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_BID_RE = re.compile(r"^[0-9A-Za-z]+$")
_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"(?i)<br\s*/?>|</p>")

log = logging.getLogger(__name__)


def index_params(account: str) -> dict[str, str]:
    return {"containerid": f"{_CONTAINER}{account}", "page": "1"}


def items_from_index(
    payload: Mapping[str, Any], *, account: str
) -> tuple[str | None, list[FetchedItem]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    name = _profile_name(data) if isinstance(data, dict) else None
    regular: list[FetchedItem] = []
    pinned: list[FetchedItem] = []
    for card in _cards(data if isinstance(data, dict) else {}):
        item = mblog_item(card, account=account, source_name=name)
        if item is None:
            continue
        if name is None:
            name = item.source_name or None
        mblog = card.get("mblog") if isinstance(card.get("mblog"), dict) else {}
        if _pinned(card, mblog):
            pinned.append(item)
        else:
            regular.append(item)
    seen = {item.item_id for item in regular}
    return name, regular + [item for item in pinned if item.item_id not in seen]


def mblog_item(
    card: Mapping[str, Any],
    *,
    account: str,
    source_name: str | None = None,
) -> FetchedItem | None:
    mblog = card.get("mblog") if isinstance(card.get("mblog"), dict) else None
    if mblog is None:
        return None
    if str(card.get("card_type") or "9") not in {"9", "11"}:
        return None
    item_id = str(mblog.get("id") or mblog.get("mid") or "").strip()
    if not item_id:
        return None
    text = _plain(mblog.get("text") or "")
    orig = mblog.get("retweeted_status")
    if isinstance(orig, dict) and (orig.get("id") or orig.get("text")):
        text = _forward_text(text, orig)
    author = _user_name(mblog) or source_name
    heading = text.split("\n", 1)[0].strip() if text else (author or "")
    if not heading and not text:
        return None
    width, height = cover_frame(mblog)
    return FetchedItem(
        item_id=item_id,
        title=heading,
        text=text or heading,
        url=_status_url(mblog, account=account, item_id=item_id),
        image_url=cover_url(mblog),
        platform=_PLATFORM,
        account=account,
        source_name=author,
        occurred_at=_when(mblog.get("created_at")),
        image_width=width,
        image_height=height,
        author_icon=_user_icon(mblog),
    )


async def fetch_mblogs(
    account: str,
    *,
    cookie: str = "",
    call: Any | None = None,
) -> tuple[str | None, list[FetchedItem], str, str]:
    """Latest mblogs for uid `account`. `call` is weibo_json-compatible."""
    do = call or weibo_json
    params = index_params(account)
    last: FetchError | None = None
    for attempt in range(_DATA_TRIES):
        payload = await do("GET", WEIBO_INDEX_URL, params=params, cookie=cookie)
        if not isinstance(payload, dict):
            raise FetchError(
                f"Weibo did not return JSON from {redact_fetch_url(WEIBO_INDEX_URL)}"
            )
        if payload.get("ok") == -100:
            raise FetchError(
                f"Weibo cookie expired from {redact_fetch_url(WEIBO_INDEX_URL)}",
                status=403,
                body=str(payload)[:1000],
            )
        if _has_cards_data(payload):
            name, items = items_from_index(payload, account=account)
            certification, bio = profile_fields(payload)
            return name, items, certification, bio
        last = FetchError(
            f"Weibo mblog feed had no data from {redact_fetch_url(WEIBO_INDEX_URL)}",
            body=str(payload)[:1000],
        )
        if attempt + 1 < _DATA_TRIES:
            log.warning("Weibo getIndex had no data; retrying")
            await weibo_pause(_retry_pause(attempt), live=call is None)
    assert last is not None
    raise last


def _has_cards_data(payload: Mapping[str, Any]) -> bool:
    data = payload.get("data")
    return isinstance(data, dict) and "cards" in data


def _cards(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("cards")
    if not isinstance(rows, list):
        return []
    cards: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("card_type") or "") == "11":
            group = row.get("card_group")
            if isinstance(group, list):
                cards.extend(card for card in group if isinstance(card, dict))
            continue
        cards.append(row)
    return cards


def _pinned(card: Mapping[str, Any], mblog: Mapping[str, Any]) -> bool:
    if str(mblog.get("isTop") or "") == "1":
        return True
    title = card.get("title")
    text = title.get("text") if isinstance(title, dict) else title
    return str(text or "").strip() == "置顶"


def _profile_name(data: Mapping[str, Any]) -> str | None:
    name = _profile_text(_user_info(data), "screen_name")
    return name or None


def profile_fields(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Certification and bio from a getIndex body. Display name stays separate.

    A list container often has no userInfo. The same fields then sit on the post author.
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        data = {}
    info = _user_info(data)
    user = _mblog_user(data)
    certification = _profile_text(info, "verified_reason") or _profile_text(user, "verified_reason")
    bio = _profile_text(info, "description") or _profile_text(user, "description")
    return certification, bio


def _mblog_user(data: Mapping[str, Any]) -> Mapping[str, Any]:
    for card in _cards(data):
        mblog = card.get("mblog") if isinstance(card.get("mblog"), dict) else None
        if mblog is None:
            continue
        user = mblog.get("user")
        if isinstance(user, dict):
            return user
    return {}


def _user_info(data: Mapping[str, Any]) -> Mapping[str, Any]:
    info = data.get("userInfo")
    return info if isinstance(info, dict) else {}


def _profile_text(info: Mapping[str, Any], key: str) -> str:
    return " ".join(str(info.get(key) or "").split())


def _status_url(mblog: Mapping[str, Any], *, account: str, item_id: str) -> str:
    bid = _mblog_bid(mblog) or _mid_to_bid(item_id)
    uid = account.strip()
    if bid and uid:
        return _WEB_URL.format(uid=uid, bid=bid)
    return _DETAIL_URL.format(id=item_id)


def _mblog_bid(mblog: Mapping[str, Any]) -> str | None:
    for key in ("mblogid", "bid"):
        text = str(mblog.get(key) or "").strip()
        if _BID_RE.fullmatch(text):
            return text
    return None


def _mid_to_bid(mid: str) -> str | None:
    """Encode a numeric Weibo mid into the desktop path id.

    Groups of 7 digits from the right, each encoded in base62. Every group
    except the leftmost is padded to 4 characters.
    """
    text = mid.strip()
    if not text.isdigit():
        return None
    parts: list[str] = []
    while text:
        parts.append(text[-7:])
        text = text[:-7]
    parts.reverse()
    encoded: list[str] = []
    for index, part in enumerate(parts):
        piece = _base62(int(part))
        if index:
            piece = piece.zfill(4)
        encoded.append(piece)
    return "".join(encoded) or None


def _base62(number: int) -> str:
    if number == 0:
        return _ALPHABET[0]
    chars: list[str] = []
    while number:
        number, rem = divmod(number, 62)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def _user_name(mblog: Mapping[str, Any]) -> str:
    user = mblog.get("user") if isinstance(mblog.get("user"), dict) else {}
    return str(user.get("screen_name") or "").strip()


def _user_icon(mblog: Mapping[str, Any]) -> str | None:
    user = mblog.get("user") if isinstance(mblog.get("user"), dict) else {}
    for key in ("profile_image_url", "avatar_large", "avatar_hd"):
        found = _http_icon(user.get(key))
        if found:
            return found
    return None


def _http_icon(url: object) -> str | None:
    text = str(url or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    return http_url(text)


def _forward_text(comment: str, orig: Mapping[str, Any]) -> str:
    name = _user_name(orig)
    stamp = f"转发了{name}的微博" if name else "转发微博"
    orig_text = _plain(orig.get("text") or "")
    return "\n".join(part for part in (comment, stamp, orig_text) if part)


def _plain(raw: object) -> str:
    text = str(raw or "").replace("\r\n", "\n")
    if not text:
        return ""
    text = _BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _when(stamp: object) -> datetime | None:
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        found = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if found.tzinfo is None:
        found = found.replace(tzinfo=timezone.utc)
    return found.astimezone(timezone.utc)
