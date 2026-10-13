"""Weibo user mblogs via m.weibo.cn getIndex."""

from __future__ import annotations

import logging
import re
from collections.abc import Collection
from contextvars import ContextVar
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from config import CONFIG_PATH, settings, upsert_config_values
from fetch import FetchedItem, FetchError, http_url
from news.source.post.common import FeedFault, classify_feed_fault
from .http import (
    WEIBO_CONFIG_URL,
    current_proxy,
    forget_proxy,
    identity_cookies,
    lane_guest,
    remember_guest,
    session_identity,
    warmup_weibo,
    weibo_json,
)
from .mblog import fetch_mblogs
from .visitor import (
    is_weibo_visitor_cookie,
    mint_weibo_visitor_cookie,
)

NAME = "weibo"
NAMES = frozenset({NAME, "wb"})
CARD_KIND = "post"
FETCH_FIELDS = (("cookie", "weibo_cookie"), ("call", "weibo_call"))
ADD_EXAMPLE = "a Weibo uid"
AUTH_EXPIRED = "Weibo cookie expired. Use `/news cookie`."
log = logging.getLogger(__name__)
_IDENTITY_EXPIRED = "cookie expired"


HOSTS = frozenset(
    {
        "weibo.com",
        "www.weibo.com",
        "m.weibo.com",
        "weibo.cn",
        "www.weibo.cn",
        "m.weibo.cn",
    }
)
_RESERVED = frozenset(
    {
        "about",
        "ajax",
        "fav",
        "follow",
        "friends",
        "home",
        "hot",
        "index",
        "login",
        "n",
        "p",
        "register",
        "search",
        "security",
        "signup",
        "tv",
    }
)
_UID_RE = re.compile(r"^\d{5,16}$")
_NAME_SUFFIX = re.compile(r"的微博$")


def weibo_uid(account: str, *, allow_uid: bool = False) -> str | None:
    raw = account.strip()
    if raw.lower().startswith(("weibo:", "wb:")):
        raw = raw.split(":", 1)[1].strip()
        return raw if _UID_RE.match(raw) else None
    found = http_url(raw)
    if found is None:
        return raw if allow_uid and _UID_RE.match(raw) else None
    parts = urlsplit(found)
    segs = [seg for seg in parts.path.split("/") if seg]
    if (parts.hostname or "").lower() not in HOSTS or not segs:
        return None
    if segs[0].lower() in ("u", "profile") and len(segs) >= 2:
        uid = segs[1]
    elif segs[0].lower() in _RESERVED:
        return None
    else:
        uid = segs[0]
    return uid if _UID_RE.match(uid) else None


def parse_account(account: str, *, explicit: bool = False) -> str | None:
    return weibo_uid(account, allow_uid=explicit)


def configured_proxies() -> tuple[str, ...]:
    """The proxy list. Empty means one direct connection."""
    return tuple(settings.weibo_proxies) or ("",)


def poll_proxies() -> tuple[str, ...]:
    """Proxies for this poll. A saved login stays on the first one.

    When that proxy fails the exit check, the poll uses the others and
    leaves the login off them.
    """
    proxies = configured_proxies()
    saved = _saved_cookie()
    if saved and not is_weibo_visitor_cookie(saved):
        return (proxies[0],)
    return proxies


# This poll is not sending the saved login. False outside that poll.
_login_held: ContextVar[bool] = ContextVar("weibo_login_held", default=False)


def login_pins(proxies: tuple[str, ...]) -> bool:
    """True when `proxies` is only the first one because a login is saved."""
    saved = _saved_cookie()
    if not saved or is_weibo_visitor_cookie(saved):
        return False
    configured = configured_proxies()
    return len(configured) > 1 and proxies == (configured[0],)


def set_login_held(held: bool) -> None:
    """This poll leaves the saved login off. A later poll sets it again."""
    _login_held.set(held)


def login_held() -> bool:
    """True when this poll must not send the saved login."""
    return _login_held.get()


def fresh_identity(proxy: str) -> None:
    """Drop the guest on `proxy`. A saved visitor is cleared. A login stays."""
    forget_proxy(proxy)
    saved = _saved_cookie()
    if not saved or not is_weibo_visitor_cookie(saved):
        return
    proxies = configured_proxies()
    if proxies and proxy != proxies[0]:
        return
    settings.weibo_cookie = None
    try:
        upsert_config_values(CONFIG_PATH, {"WEIBO_COOKIES": ""})
    except Exception:
        log.warning("Could not clear Weibo visitor cookie after an exit change", exc_info=True)


async def fetch_items(
    account: str,
    *,
    cookie: str | None = None,
    call: Any | None = None,
    request: Any | None = None,
    seen: Collection[str] = (),
    **_kwargs: object,
) -> tuple[str | None, list[FetchedItem], str, str, bool]:
    """Latest mblogs. Extra tuple slots match the sports `fetch_items` shape."""
    cookie_value = _fetch_cookie(cookie)
    if not cookie_value and call is None:
        cookie_value = await mint_weibo_visitor_cookie(request=request)
        _store_guest(cookie_value)
    try:
        name, items, certification, bio = await fetch_mblogs(
            account, cookie=cookie_value, call=call, seen=seen,
        )
    except FetchError as exc:
        if call is None and _guest_rejected(exc, cookie_value):
            fresh_identity(current_proxy())
            raise FetchError("Weibo guest rejected") from exc
        raise
    return name, items, certification, bio, True


def _saved_cookie() -> str:
    return (getattr(settings, "weibo_cookie", None) or "").strip()


def _spread_guests() -> bool:
    """True when each proxy keeps its own guest and config stores none of them."""
    if len(configured_proxies()) <= 1:
        return False
    if login_held():
        return True
    saved = _saved_cookie()
    return not saved or is_weibo_visitor_cookie(saved)


def _fetch_cookie(cookie: str | None) -> str:
    """The SUB for this fetch. A spread guest ignores the one saved cookie.

    A poll that left the login off does not send it, even when the caller
    passed that cookie.
    """
    passed = (cookie or "").strip()
    if login_held() and passed and not is_weibo_visitor_cookie(passed):
        passed = ""
    if _spread_guests() and (not passed or is_weibo_visitor_cookie(passed)):
        return _lane_cookie()
    if login_held():
        return passed or _lane_cookie()
    return passed or _saved_cookie()


def _lane_cookie() -> str:
    guest = lane_guest()
    if guest:
        return guest
    proxies = configured_proxies()
    if current_proxy() == proxies[0]:
        saved = _saved_cookie()
        if saved and is_weibo_visitor_cookie(saved):
            return saved
    return ""


def _store_guest(cookie: str) -> None:
    remember_guest(cookie)
    if not _spread_guests():
        _persist_visitor_cookie(cookie)


def _persist_visitor_cookie(cookie: str) -> None:
    settings.weibo_cookie = cookie
    try:
        upsert_config_values(CONFIG_PATH, {"WEIBO_COOKIES": cookie})
    except Exception:
        log.warning("Could not save minted Weibo visitor cookie", exc_info=True)


def _guest_rejected(exc: BaseException, cookie: str) -> bool:
    """True when getIndex rejected this guest (`ok: -100`). A login stays."""
    if cookie and not is_weibo_visitor_cookie(cookie):
        return False
    return _IDENTITY_EXPIRED in str(exc).lower()


def display_name(title: str) -> str:
    return _NAME_SUFFIX.sub("", title).strip(" -·|/")


async def cookie_effective(cookie: str, *, request: Any | None = None) -> bool | None:
    """True when m.weibo.cn still accepts this SUB. None if the check could not run."""
    header = (cookie or "").strip()
    if not header:
        return None
    try:
        payload = await weibo_json("GET", WEIBO_CONFIG_URL, cookie=header, request=request)
    except FetchError as exc:
        status = getattr(exc, "status", None)
        if status in {401, 403} or _IDENTITY_EXPIRED in str(exc).lower():
            return False
        return None
    ok = payload.get("ok") if isinstance(payload, dict) else None
    if ok == -100:
        return False
    if ok != 1:
        return None
    data = payload.get("data")
    return is_weibo_visitor_cookie(header) or bool(isinstance(data, dict) and data.get("login"))


def feed_fault(exc: BaseException) -> FeedFault | None:
    return classify_feed_fault(NAME, exc)


def halt_round(poll: Any, account: str, exc: BaseException, *, proxy: str | None = None) -> None:
    """The poll module stops the rest of this proxy."""
    # round imports this package, so this stays inside the function.
    from .round import halt_round as stop_proxy

    stop_proxy(poll, account, exc, proxy=proxy)


async def apply_cookie(
    *,
    cookie: str | None,
    fallback_cookie: str | None = None,
    request: Any | None = None,
    **_kwargs: object,
) -> dict[str, object]:
    """Warm m.weibo.cn and save a login SUB. A guest SUB is left to the poll."""
    pasted = _cookie_header(cookie)
    fallback = _cookie_header(fallback_cookie) or _saved_cookie()
    raw = pasted or fallback
    if not identity_cookies(raw):
        raise ValueError("Paste a Weibo Cookie header from m.weibo.cn.")
    if is_weibo_visitor_cookie(raw):
        raise ValueError(
            "That is a guest cookie." if pasted else "Paste a login Cookie header."
        )
    try:
        await warmup_weibo(raw, request=request, force=True)
    except FetchError as exc:
        raise ValueError("Could not warm m.weibo.cn.") from exc
    saved = session_identity()
    if not saved or is_weibo_visitor_cookie(saved):
        raise ValueError("Need a Weibo login cookie from m.weibo.cn.")
    warmed = saved == _saved_cookie() and not pasted
    return {
        "line": "Warmed Weibo cookie." if warmed else "Wrote Weibo SUB.",
        "config": {} if warmed else {"WEIBO_COOKIES": saved},
        "settings_update": {} if warmed else {"weibo_cookie": saved},
        "changed": True,
    }


def _cookie_header(raw: str | None) -> str:
    text = (raw or "").strip()
    if "\n" in text or "\r" in text:
        raise ValueError("Cookie missing or not a single line.")
    return text


def should_skip(item: FetchedItem, *, now: datetime | None = None) -> bool | None:
    if item.platform not in NAMES:
        return None
    if "repost" in item.tags:
        return True
    return None
