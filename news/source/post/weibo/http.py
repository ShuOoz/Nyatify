"""HTTP to m.weibo.cn: one session, one jar per proxy, homepage warmup.

Each proxy keeps its own SUB and visit cookies. A guest is never sent on
another proxy. Visit cookies (_T_WM, XSRF-TOKEN, …) come from Weibo after
warmup, including Set-Cookie on redirect hops, and stay in that proxy's jar.
The saved header is never replayed on later GETs. Guest identity is SUB from
genvisitor2 (optional SUBP). On 401/403, strip back to SUB and warm once.
432 cools immediately with no remint. That rest is saved. 5xx and bad JSON
retry with a short pause. A homepage 5xx drops the visit jar and keeps the
guest. Headers stay fixed (no random UA/Referer).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from config import BASE_DIR, proxy_url, write_text_atomic
from fetch import FetchError, redact_fetch_url
from news.source.post.common import format_cookie_header, parse_cookie_header, read_json

WEIBO_HOME = "https://m.weibo.cn"
WEIBO_CONFIG_URL = "https://m.weibo.cn/api/config"
WEIBO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://m.weibo.cn/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "MWeibo-Pwa": "1",
}
_COOKIE_STATUSES = frozenset({401, 403})
_JSON_TRIES = 3
_RETRY_PAUSES = (1.0, 2.0)

log = logging.getLogger(__name__)
WeiboCall = Callable[..., Awaitable["WeiboHttp"]]
_session = None
_session_loop: asyncio.AbstractEventLoop | None = None
_bound: ContextVar[str | None] = ContextVar("weibo_proxy", default=None)


@dataclass
class _Lane:
    """One proxy's jar. A guest SUB is never sent on another proxy."""

    jar: dict[str, str] = field(default_factory=dict)
    warmed: bool = False
    identity_sub: str = ""
    guest: str = ""


_lanes: dict[str, _Lane] = {}
GUEST_PATH = BASE_DIR / "data" / "weibo_guests.json"


class GuestBook:
    """The guest SUB saved for each proxy. A restart keeps it."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = GUEST_PATH if path is None else path
        self._rows: dict[str, str] | None = None

    def get(self, proxy: str) -> str:
        return self._load().get(proxy, "")

    def put(self, proxy: str, cookie: str) -> None:
        rows = self._load()
        text = cookie.strip()
        if text:
            rows[proxy] = text
        else:
            rows.pop(proxy, None)
        self._save()

    def drop(self, proxy: str) -> None:
        rows = self._load()
        if proxy not in rows:
            return
        rows.pop(proxy, None)
        self._save()

    def _load(self) -> dict[str, str]:
        if self._rows is not None:
            return self._rows
        self._rows = {}
        for proxy, cookie in read_json(self.path).items():
            text = str(cookie or "").strip()
            if text:
                self._rows[str(proxy)] = text
        return self._rows

    def _save(self) -> None:
        payload = {proxy: cookie for proxy, cookie in sorted((self._rows or {}).items()) if cookie}
        write_text_atomic(self.path, json.dumps(payload, indent=2) + "\n")


guests = GuestBook()


@dataclass(frozen=True)
class WeiboHttp:
    status: int
    text: str
    cookies: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.text)


def identity_cookies(cookie: str) -> dict[str, str]:
    """Keep SUB (and SUBP). Drop visit fingerprints so they are not sent on warmup."""
    pairs = parse_cookie_header(cookie) if cookie else {}
    sub = str(pairs.get("SUB") or "").strip()
    if not sub:
        return {}
    found = {"SUB": sub}
    subp = str(pairs.get("SUBP") or "").strip()
    if subp:
        found["SUBP"] = subp
    return found


def cookies_from_response(response: Any) -> dict[str, str]:
    """Set-Cookie from the final response and every redirect hop before it.

    `deleted` is kept as empty so the jar drops that name.
    """
    found: dict[str, str] = {}
    hops = [*(getattr(response, "history", None) or ()), response]
    for hop in hops:
        cookies = getattr(hop, "cookies", None)
        if cookies is None or not hasattr(cookies, "items"):
            continue
        for key, morsel in cookies.items():
            found[str(key)] = _cookie_text(morsel)
    return found


def _cookie_text(morsel: object) -> str:
    value = morsel.value if hasattr(morsel, "value") else morsel
    text = str(value or "").strip()
    if not text or text.lower() == "deleted":
        return ""
    return text


def bind_proxy(proxy: str) -> Token[str | None]:
    """Use `proxy` until `reset_proxy`. Empty is a direct connection."""
    return _bound.set(proxy)


def reset_proxy(token: Token[str | None]) -> None:
    _bound.reset(token)


@contextmanager
def bound_proxy(proxy: str) -> Iterator[None]:
    """Bind `proxy` for the fetches in this block."""
    token = bind_proxy(proxy)
    try:
        yield
    finally:
        reset_proxy(token)


def current_proxy() -> str:
    """The proxy bound for this fetch. Empty is direct. Unbound uses `WEIBO_PROXY`."""
    chosen = _bound.get()
    if chosen is not None:
        return chosen.strip()
    return (proxy_url("weibo_proxy") or "").strip()


def request_proxy() -> str | None:
    """Proxy URL for this fetch. None is a direct connection."""
    return current_proxy() or None


def _lane() -> _Lane:
    key = current_proxy()
    lane = _lanes.get(key)
    if lane is None:
        lane = _Lane()
        _lanes[key] = lane
    return lane


def lane_guest() -> str:
    """Guest SUB remembered for this proxy. Empty when this proxy has not minted one."""
    lane = _lane()
    if lane.guest:
        return lane.guest
    saved = guests.get(current_proxy())
    if saved:
        lane.guest = saved
    return lane.guest


def remember_guest(cookie: str) -> None:
    """Keep this guest on the current proxy. It is saved for the next start."""
    text = (cookie or "").strip()
    _lane().guest = text
    guests.put(current_proxy(), text)


def jar_cookies() -> dict[str, str]:
    return dict(_lane().jar)


def session_identity() -> str:
    """SUB (and SUBP) for config. Visit cookies stay in the jar."""
    lane = _lane()
    sub = str(lane.jar.get("SUB") or lane.identity_sub or "").strip()
    if not sub:
        return ""
    pairs = {"SUB": sub}
    subp = str(lane.jar.get("SUBP") or "").strip()
    if subp:
        pairs["SUBP"] = subp
    return format_cookie_header(pairs)


def forget_proxy(proxy: str) -> None:
    """Drop this proxy's jar and guest. The next ask on it mints a new guest."""
    _lanes.pop(proxy, None)
    guests.drop(proxy)


def _clear_visit_jar() -> None:
    """Drop visit cookies. The guest stays for the next ask on this proxy."""
    lane = _lane()
    lane.jar.clear()
    lane.warmed = False
    lane.identity_sub = ""


def _reset_lane() -> None:
    lane = _lane()
    lane.jar.clear()
    lane.warmed = False
    lane.identity_sub = ""
    lane.guest = ""
    guests.drop(current_proxy())


async def warmup_weibo(
    cookie: str = "",
    *,
    request: WeiboCall | None = None,
    force: bool = False,
) -> dict[str, str]:
    """GET the mobile homepage with SUB (or empty). Keep Set-Cookie."""
    lane = _lane()
    raw = (cookie or "").strip()
    identity = identity_cookies(raw)
    if not identity and lane.identity_sub:
        identity = {"SUB": lane.identity_sub}
    sub = identity.get("SUB", "")
    if lane.warmed and not force and (not raw or sub == lane.identity_sub):
        return jar_cookies()
    lane.jar.clear()
    lane.jar.update(identity)
    lane.identity_sub = sub
    resp = await _do(request, "GET", WEIBO_HOME, cookies=dict(lane.jar))
    if resp.status >= 500:
        _clear_visit_jar()
        raise FetchError(
            f"HTTP {resp.status} from {redact_fetch_url(WEIBO_HOME)}",
            status=resp.status,
            body=resp.text,
        )
    if resp.status >= 400:
        _reset_lane()
        raise FetchError(
            f"HTTP {resp.status} from {redact_fetch_url(WEIBO_HOME)}",
            status=resp.status,
            body=resp.text,
        )
    _merge_cookies(resp.cookies)
    lane.warmed = True
    return jar_cookies()


async def weibo_pause(seconds: float, *, live: bool) -> None:
    """Sleep on live fetches only. Injected test `request`/`call` skips the wait."""
    if live and seconds > 0:
        await asyncio.sleep(seconds)


def _retry_pause(attempt: int) -> float:
    return _RETRY_PAUSES[min(attempt, len(_RETRY_PAUSES) - 1)]


def _http_error(resp: WeiboHttp, url: str) -> FetchError:
    return FetchError(
        f"HTTP {resp.status} from {redact_fetch_url(url)}", status=resp.status, body=resp.text
    )


def _json_body(resp: WeiboHttp, url: str) -> Any:
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise FetchError(f"Weibo did not return JSON from {redact_fetch_url(url)}") from exc


async def weibo_raw(
    method: str,
    url: str,
    *,
    data: Mapping[str, str] | None = None,
    request: WeiboCall | None = None,
) -> WeiboHttp:
    """One HTTP call without warming the m.weibo.cn jar (passport mint)."""
    return await _do(request, method, url, cookies={}, data=data)


async def weibo_json(
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    cookie: str = "",
    request: WeiboCall | None = None,
) -> Any:
    """JSON GET/POST on the warmed jar. Does not overlay the saved header."""
    live = request is None
    if params:
        url = f"{url}?{urlencode(params)}"
    await warmup_weibo(cookie, request=request)
    for attempt in range(_JSON_TRIES):
        retry = attempt + 1 < _JSON_TRIES
        try:
            resp = await _do(request, method, url, cookies=dict(_lane().jar))
        except FetchError:
            if not retry:
                raise
            log.warning("Weibo request failed; retrying")
            await weibo_pause(_retry_pause(attempt), live=live)
            continue
        _merge_cookies(resp.cookies)
        if resp.status in _COOKIE_STATUSES:
            log.warning("Weibo cookie rejected; warming m.weibo.cn again")
            await warmup_weibo(cookie, request=request, force=True)
            await weibo_pause(_retry_pause(0), live=live)
            resp = await _do(request, method, url, cookies=dict(_lane().jar))
            _merge_cookies(resp.cookies)
            if resp.status in _COOKIE_STATUSES or resp.status >= 400:
                raise _http_error(resp, url)
            return _json_body(resp, url)
        if retry and resp.status >= 500:
            log.warning("Weibo HTTP %s; retrying", resp.status)
        elif resp.status >= 400:
            raise _http_error(resp, url)
        else:
            try:
                return _json_body(resp, url)
            except FetchError:
                if not retry:
                    raise
                log.warning("Weibo did not return JSON; retrying")
        await weibo_pause(_retry_pause(attempt), live=live)


async def _do(
    request: WeiboCall | None,
    method: str,
    url: str,
    *,
    cookies: Mapping[str, str],
    data: Mapping[str, str] | None = None,
) -> WeiboHttp:
    sent = dict(cookies)
    if request is not None:
        return await request(method, url, cookies=sent, data=data)
    return await _live_request(method, url, cookies=sent, data=data)


def _merge_cookies(updates: Mapping[str, str]) -> None:
    jar = _lane().jar
    for key, value in updates.items():
        text = _cookie_text(value)
        if text:
            jar[key] = text
        else:
            jar.pop(key, None)


async def _close_session() -> None:
    global _session, _session_loop
    session = _session
    _session = None
    _session_loop = None
    if session is None or session.closed:
        return
    try:
        await session.close()
    except Exception:
        pass


async def _weibo_session():
    import aiohttp

    global _session, _session_loop
    loop = asyncio.get_running_loop()
    if _session is not None and not _session.closed and _session_loop is loop:
        return _session
    await _close_session()
    _session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20),
        cookie_jar=aiohttp.DummyCookieJar(),
    )
    _session_loop = loop
    return _session


def _headers_for(url: str, cookies: Mapping[str, str]) -> dict[str, str]:
    host = (urlsplit(url).hostname or "").lower()
    if "passport.weibo." in host:
        return {
            "User-Agent": WEIBO_HEADERS["User-Agent"],
            "Referer": WEIBO_HEADERS["Referer"],
            "Accept": "*/*",
        }
    headers = dict(WEIBO_HEADERS)
    token = str(cookies.get("XSRF-TOKEN") or "").strip()
    if token:
        headers["X-XSRF-TOKEN"] = token
    return headers


async def _live_request(
    method: str,
    url: str,
    *,
    cookies: Mapping[str, str],
    data: Mapping[str, str] | None = None,
) -> WeiboHttp:
    import aiohttp

    try:
        session = await _weibo_session()
        headers = _headers_for(url, cookies)
        sent = dict(data) if data else None
        kwargs: dict[str, Any] = {
            "headers": headers,
            "cookies": dict(cookies),
            "data": sent,
        }
        proxy = request_proxy()
        if proxy:
            kwargs["proxy"] = proxy
        async with session.request(method, url, **kwargs) as response:
            body = await response.text()
            return WeiboHttp(
                response.status, body, cookies_from_response(response)
            )
    except (aiohttp.ClientError, TimeoutError) as exc:
        # The total timer raises TimeoutError, which is not a ClientError.
        # Left alone, /news check stays on Discord's "thinking" reply.
        raise FetchError(f"Could not fetch {redact_fetch_url(url)}") from exc
