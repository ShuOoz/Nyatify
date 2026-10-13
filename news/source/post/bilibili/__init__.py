"""Bilibili user dynamics via the app space feed."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable, Collection
from typing import Any

from config import settings
from fetch import FetchedItem, FetchError
from news.source.post.common import (
    COOKIE_SKIP_SECONDS, FeedFault, classify_feed_fault, format_cookie_header,
    parse_cookie_header,
)
from .app import app_buvid, saved_buvid
# Read by name through the source registry: is_live_room, should_skip,
# for_model, note_list_post, is_quiet, catch_floor.
from .dynamics import (  # noqa: F401
    LIST, catch_floor, fetch_dynamics, for_model, is_live_room, is_quiet,
    note_list_post, should_skip, space_stats,
)
from .browser import (
    bili_login_effective, clear_wbi_key, close_bili_connections, refresh_bili_login_cookie,
)

NAME = "bilibili"
NAMES = frozenset({NAME, "bili"})
CARD_KIND = "post"
FETCH_FIELDS = (
    ("cookie", "bili_cookie"), ("call", "bili_call"),
    ("post", "bili_post"), ("buvid", "bili_buvid"),
)
ADD_EXAMPLE = "a uid or space link"
AUTH_EXPIRED = "Bilibili cookie expired. Use `/news cookie`."
# 0 means no login uid.
VISITOR_COOKIE_UID = "0"

log = logging.getLogger(__name__)
_DEDE_USER_ID_RE = re.compile(r"(?:^|;\s*)DedeUserID=(\d+)")


_SPACE_RE = re.compile(r"^(?:https?://)?(?:www\.)?space\.bilibili\.com/(\d+)", re.IGNORECASE)
_NAME_PREFIX = re.compile(r"^(?:the\s+posts?\s+by|posts?\s+by)\s+", re.IGNORECASE)
_NAME_SUFFIX = re.compile(
    r"(?:\s*的\s*(?:bilibili\s*)?(?:动态|動態|视频|視頻)"
    r"|(?:'s)?\s+bilibili\s+dynamics?)$",
    re.IGNORECASE,
)


def bili_uid(account: str) -> str | None:
    space = _SPACE_RE.match(account.strip())
    if space:
        return space.group(1)
    uid = account.removeprefix("UID:").removeprefix("uid:").strip()
    return uid if uid.isdigit() else None


def parse_account(account: str, *, explicit: bool = False) -> str | None:
    return bili_uid(account)


def is_bili_login_cookie(cookie: str) -> bool:
    pairs = parse_cookie_header(cookie)
    return bool(pairs.get("SESSDATA") and pairs.get("bili_jct"))


async def fetch_items(
    account: str, *, cookie: str | None = None, call: Any | None = None,
    post: Any | None = None, buvid: str | None = None, seen: Collection[str] = (),
    **_kwargs: object,
) -> tuple[str | None, list[FetchedItem], str, str, bool]:
    """Latest dynamics. Extra tuple slots match the sports `fetch_items` shape.

    The poll has already chosen this read: the app phone, the login page on
    the first proxy, or the visitor list on the account's proxy. A blank list
    is an empty result. The app session and the web sessions close when this
    check finishes.
    """
    # round imports this package, so this stays inside the function.
    from .round import current_slot

    cookie_value = (cookie or "").strip()
    if not cookie_value and is_bili_login_cookie(_saved_cookie()):
        cookie_value = _saved_cookie()
    space_stats.begin(account, current_slot())
    try:
        name, items = await fetch_dynamics(
            account, cookie=cookie_value, call=call, post=post, buvid=buvid or "", seen=seen
        )
    except FetchError:
        space_stats.done(account, "failed")
        raise
    else:
        return name, items, "", "", True
    finally:
        await close_bili_connections()


def _saved_cookie() -> str:
    return (getattr(settings, "bili_cookie", None) or "").strip()


def display_name(title: str) -> str:
    name = _NAME_PREFIX.sub("", title).strip()
    return _NAME_SUFFIX.sub("", name).strip(" -·|/")


def feed_fault(exc: BaseException) -> FeedFault | None:
    return classify_feed_fault(NAME, exc)


def block_live_room(item: FetchedItem, topic: str) -> bool:
    """Game/ticket leave live-room go-live to a live notice. Other may post it."""
    from news.follows import FEATURE_OTHER

    return is_live_room(item) and topic != FEATURE_OTHER


def shows_post(item: FetchedItem, topic: str) -> bool:
    from news.follows import FEATURE_GAME

    return LIST not in item.tags or topic == FEATURE_GAME


def halt_round(poll: Any, account: str, exc: BaseException, *, proxy: str | None = None) -> None:
    """The poll module stops the rest of this proxy."""
    # round imports this package, so this stays inside the function.
    from .round import halt_round as stop_proxy

    stop_proxy(poll, account, exc, proxy=proxy)


def split_bili_ac_time(cookie: str) -> tuple[str, str | None]:
    """Pull ac_time_value out of a pasted Cookie header. It is localStorage, not HTTP."""
    pairs = parse_cookie_header(cookie)
    ac_time = pairs.pop("ac_time_value", None) or None
    return format_cookie_header(pairs), ac_time


async def recover_bili_cookie(
    platform: str, exc: BaseException, *, cookie: str, ac_time: str | None,
    refreshed_until: float, now: float, apply_cookie: Callable[..., Awaitable[dict]],
    save_result: Callable[[dict], Awaitable[object]],
) -> tuple[bool | None, float]:
    """True: a new login cookie, one more pull. False: park. None: no extra pull."""
    from news.source.registry import feed_fault as fault_for

    fault = fault_for(platform, exc)
    if (
        fault is None or not fault.cookie or platform != "bilibili"
        or not is_bili_login_cookie(cookie) or not (ac_time or "").strip()
    ):
        return False, refreshed_until
    if now < refreshed_until:
        return None, refreshed_until
    try:
        result = await apply_cookie(
            "bili", cookie=None, fallback_cookie=cookie, fallback_ac_time=ac_time
        )
    except ValueError:
        log.exception("Bilibili login cookie refresh failed")
        return False, refreshed_until
    if result.get("changed"):
        await save_result(result)
        log.warning("%s", result.get("line") or "Refreshed Bilibili cookie")
        return True, now + COOKIE_SKIP_SECONDS
    log.warning("%s", result.get("line") or "Bilibili cookie unchanged")
    return None, refreshed_until


def bili_cookie_uid(cookie: str) -> str:
    """The saved uid: DedeUserID in the cookie, or none."""
    match = _DEDE_USER_ID_RE.search(cookie)
    return match.group(1) if match else VISITOR_COOKIE_UID


async def cookie_effective(cookie: str) -> bool | None:
    """True when a saved login cookie can still be used. None if the check could not run."""
    header = (cookie or "").strip()
    if not is_bili_login_cookie(header):
        return None
    return await bili_login_effective(header)


class BiliCookies:
    """The app phone for the proxy bound to this fetch. A login cookie goes with it.

    The poll loop keeps one of these. Each proxy keeps its own phones. A flagged
    fetch stops the rest of that proxy. The phone stays saved. Each account
    check closes the Bilibili connections, and so does the end of the round.
    """

    def __init__(self) -> None:
        self.refreshed_until = 0.0

    def using_login(self) -> bool:
        return is_bili_login_cookie(settings.bili_cookie or "")

    async def prepare(self, fetcher: Any) -> None:
        """Point this fetch at the phone saved for the bound proxy."""
        fetcher.bili_buvid = saved_buvid(app_buvid)
        fetcher.bili_cookie = settings.bili_cookie if self.using_login() else None

    async def end(self, fetcher: Any) -> None:
        """Drop the phone off the fetcher and close Bilibili connections.

        The saved phones stay for the next wake.
        """
        clear_wbi_key()
        fetcher.bili_buvid = None
        if not self.using_login():
            fetcher.bili_cookie = None
        await close_bili_connections()

    def adjust(self, fault: FeedFault | None) -> FeedFault | None:
        """A refused guest cookie stops the rest of that proxy. It does not ask for a pasted cookie."""
        if fault is None or self.using_login() or not fault.cookie:
            return fault
        return FeedFault(0, stop_round=True)

    async def on_failure(
        self, platform: str, account: str, exc: BaseException, *,
        apply_cookie: Callable[..., Awaitable[dict]],
        save_result: Callable[[dict], Awaitable[object]],
    ) -> bool:
        """One more pull after a new login cookie. A guest cookie is not repaired."""
        if platform != "bilibili" or not self.using_login():
            return False
        recovered, until = await recover_bili_cookie(
            platform, exc, cookie=settings.bili_cookie or "", ac_time=settings.bili_ac_time,
            refreshed_until=self.refreshed_until, now=time.monotonic(),
            apply_cookie=apply_cookie, save_result=save_result,
        )
        self.refreshed_until = until
        if not recovered:
            return False
        space_stats.repull(account)
        return True


async def _refresh_login(cookie: str, ac_time: str) -> tuple[str, str, bool]:
    try:
        result = await refresh_bili_login_cookie(cookie, ac_time)
    except FetchError as exc:
        raise ValueError("Could not refresh the login cookie. Paste a new Cookie header.") from exc
    return result.cookie, result.ac_time, result.changed


async def apply_cookie(
    *, cookie: str | None, fallback_cookie: str | None = None,
    fallback_ac_time: str | None = None,
) -> dict[str, object]:
    pasted = (cookie or "").strip()
    if pasted and ("\n" in pasted or "\r" in pasted):
        raise ValueError("Cookie missing or not a single line.")
    cookie_value, ac_time = split_bili_ac_time(pasted)
    fallback, fallback_ac = split_bili_ac_time((fallback_cookie or "").strip())
    ac_time = ac_time or (fallback_ac_time or "").strip() or fallback_ac
    refreshed = False

    if not cookie_value and fallback and is_bili_login_cookie(fallback):
        cookie_value = fallback
        ac_time = ac_time or fallback_ac
    if not cookie_value:
        raise ValueError("Paste a login Cookie header.")
    if not is_bili_login_cookie(cookie_value):
        raise ValueError("Need a login Cookie header (SESSDATA).")
    if not pasted and ac_time:
        cookie_value, ac_time, refreshed = await _refresh_login(cookie_value, ac_time)

    uid_key = bili_cookie_uid(cookie_value)
    if uid_key == VISITOR_COOKIE_UID:
        raise ValueError("Need DedeUserID in the login cookie.")
    unchanged = not pasted and not refreshed and cookie_value == fallback
    if unchanged:
        return {
            "line": "Login cookie still valid.", "config": {}, "settings_update": {},
            "changed": False,
        }
    line = "Refreshed login cookie." if refreshed else f"Wrote `{uid_key}`."
    config = {"BILI_COOKIE_UID": uid_key, "BILI_COOKIE": cookie_value}
    settings_update: dict[str, object] = {
        "bili_cookie_uid": uid_key, "bili_cookie": cookie_value,
    }
    if ac_time:
        config["BILI_AC_TIME"] = ac_time
        settings_update["bili_ac_time"] = ac_time
    return {
        "line": line, "config": config, "settings_update": settings_update, "changed": True,
    }
