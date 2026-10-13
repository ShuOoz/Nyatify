"""Mint a Weibo guest SUB via the Sina Visitor System.

Logged-out m.weibo.cn still needs that guest identity. Homepage warmup
alone returns getIndex `ok: -100`. genvisitor2 issues SUB (and SUBP).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fetch import FetchError, redact_fetch_url
from news.source.post.common import format_cookie_header
from .http import WeiboCall, identity_cookies, weibo_raw

WEIBO_VISITOR_URL = "https://visitor.passport.weibo.cn/visitor/genvisitor2"
_VISITOR_FORM = {
    "cb": "visitor_gray_callback",
    "ver": "20250916",
    "tid": "",
    "from": "weibo",
    "webdriver": "false",
    "return_url": "https://m.weibo.cn/",
}
_VISITOR_OK = 20000000
_VISITOR_SUB_PREFIX = "_2Ak"

log = logging.getLogger(__name__)


def is_weibo_visitor_cookie(cookie: str) -> bool:
    """True when SUB is a Sina Visitor System guest, not a login."""
    sub = identity_cookies(cookie).get("SUB", "")
    return sub.startswith(_VISITOR_SUB_PREFIX)


def visitor_cookie_header(sub: str, subp: str = "") -> str:
    return format_cookie_header({"SUB": sub, "SUBP": subp})


def parse_visitor_payload(text: str) -> dict[str, str]:
    """JSONP from genvisitor2 → SUB / SUBP."""
    payload = _jsonp_object(text)
    if payload.get("retcode") != _VISITOR_OK:
        raise FetchError(
            f"Weibo visitor mint failed from {redact_fetch_url(WEIBO_VISITOR_URL)}",
            body=text[:1000],
        )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    sub = str(data.get("sub") or "").strip()
    if not sub:
        raise FetchError(
            f"Weibo visitor mint had no SUB from {redact_fetch_url(WEIBO_VISITOR_URL)}",
            body=text[:1000],
        )
    return {
        "SUB": sub,
        "SUBP": str(data.get("subp") or "").strip(),
    }


async def mint_weibo_visitor_cookie(*, request: WeiboCall | None = None) -> str:
    """POST genvisitor2 and return SUB / SUBP. Does not touch the m.weibo.cn jar."""
    resp = await weibo_raw(
        "POST",
        WEIBO_VISITOR_URL,
        data=_VISITOR_FORM,
        request=request,
    )
    if resp.status >= 400:
        raise FetchError(
            f"HTTP {resp.status} from {redact_fetch_url(WEIBO_VISITOR_URL)}",
            status=resp.status,
            body=resp.text,
        )
    identity = parse_visitor_payload(resp.text)
    log.info("Minted Weibo visitor SUB")
    return visitor_cookie_header(identity["SUB"], identity.get("SUBP", ""))


def _jsonp_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise FetchError(
            f"Weibo visitor mint did not return JSON from {redact_fetch_url(WEIBO_VISITOR_URL)}"
        )
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise FetchError(
            f"Weibo visitor mint did not return JSON from {redact_fetch_url(WEIBO_VISITOR_URL)}"
        ) from exc
    if not isinstance(payload, dict):
        raise FetchError(
            f"Weibo visitor mint did not return JSON from {redact_fetch_url(WEIBO_VISITOR_URL)}"
        )
    return payload
