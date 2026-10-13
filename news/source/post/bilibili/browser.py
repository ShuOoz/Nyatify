"""The Bilibili browser: Chrome session, login refresh, and WBI signing.

One session per proxy and buvid3, so a connection opened on one proxy is
never reused on another. Calls without a buvid3 share one session on that
proxy. Login cookies are per call and are not kept.

Every call is Chrome 150 on macOS in en-US, and the connection impersonates
that Chrome. The default headers are an XHR from www.bilibili.com.
`space_xhr_headers` is an XHR from a space page.

`sign_wbi` and `dm_params` only build a query. `fetch_wbi_key` reads nav once
and keeps the mixin until `clear_wbi_key`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from urllib.parse import urlencode

from fetch import FetchError, redact_fetch_url
from news.source.post.common import format_cookie_header, parse_cookie_header, proxy_label

from .app import current_proxy

BILI_COOKIE_INFO_URL = "https://passport.bilibili.com/x/passport-login/web/cookie/info"
BILI_CORRESPOND_URL = "https://www.bilibili.com/correspond/1/{path}"
BILI_REFRESH_URL = "https://passport.bilibili.com/x/passport-login/web/cookie/refresh"
BILI_CONFIRM_URL = "https://passport.bilibili.com/x/passport-login/web/confirm/refresh"
BILI_REFRESH_PUBKEY = (
    b"-----BEGIN PUBLIC KEY-----\n"
    b"MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg\n"
    b"Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71\n"
    b"nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40\n"
    b"JNrRuoEUXpabUzGB8QIDAQAB\n"
    b"-----END PUBLIC KEY-----\n"
)
# sec-ch-ua matches the chrome150 impersonation profile.
_CHROME = "chrome150"
BILI_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}
_XHR_ACCEPT = "application/json, text/plain, */*"
_REFRESH_FAIL = "Could not refresh the Bilibili login cookie"
BILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
WEB_LOCATION = 1550101
WEBGL_VENDOR = "Google Inc. (Apple)"
WEBGL_RENDERER = (
    "ANGLE (Apple, ANGLE Metal Renderer: Apple M-series, Unspecified Version)"
)
WEBGL_VERSION = "WebGL 1.0 (OpenGL ES 2.0 Chromium)"
# Shuffle table from the public WBI mixin (bilibili-api-python / API-collect).
MIXIN_INDEX = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)
_REFRESH_CSRF_RE = re.compile(r'<div id="1-name">([^<]+)</div>')

log = logging.getLogger(__name__)
_sessions: dict[tuple[str, str], Any] = {}
_session_loop: asyncio.AbstractEventLoop | None = None
# Rotated ids leave old sessions behind. The least recently used one closes.
_MAX_SESSIONS = 6
# A later web call must not send a login session left in the shared jar.
LOGIN_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")


@dataclass(frozen=True)
class BiliHttp:
    status: int
    text: str
    cookies: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.text)


@dataclass(frozen=True)
class BiliRefresh:
    cookie: str
    ac_time: str
    changed: bool


def space_xhr_headers(referer: str) -> dict[str, str]:
    """An API call made by the space page `referer`."""
    return {
        "Origin": "https://space.bilibili.com",
        "Referer": referer,
        "Accept": _XHR_ACCEPT,
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }


def bili_correspond_path(timestamp_ms: int) -> str:
    """RSA-OAEP hex path for Bilibili's cookie refresh correspond URL."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:
        raise FetchError("Need the cryptography package to refresh a Bilibili login cookie") from exc
    key = serialization.load_pem_public_key(BILI_REFRESH_PUBKEY)
    return key.encrypt(
        f"refresh_{timestamp_ms}".encode(),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    ).hex()


async def close_bili_http() -> None:
    """Drop every web session."""
    global _session_loop
    sessions = list(_sessions.values())
    _sessions.clear()
    _session_loop = None
    for session in sessions:
        await _close(session)


async def close_bili_connections() -> None:
    """Close the app session and every web session. A close error is ignored."""
    from .app import close_app_http

    for close in (close_app_http, close_bili_http):
        try:
            await close()
        except Exception:
            log.warning("Could not close Bilibili connections", exc_info=True)


def _is_closed(session: Any) -> bool:
    return bool(getattr(session, "_closed", False))


async def _close(session: Any) -> None:
    if _is_closed(session):
        return
    try:
        await session.close()
    except Exception:
        pass


def _drop_login_cookies(session: Any) -> None:
    """Login cookies are for one call. A Set-Cookie must not ride into the next."""
    for name in LOGIN_COOKIES:
        session.cookies.delete(name)


def _response_cookies(response: Any) -> dict[str, str]:
    return {str(key): str(value) for key, value in response.cookies.items()}


async def _bili_session(guest: str = ""):
    """The session for this proxy and guest id. '' is a call with no buvid3."""
    from curl_cffi.requests import AsyncSession

    global _session_loop
    loop = asyncio.get_running_loop()
    if _session_loop is not loop:
        await close_bili_http()
        _session_loop = loop
    key = (current_proxy() or "", guest)
    session = _sessions.pop(key, None)
    if session is None or _is_closed(session):
        while len(_sessions) >= _MAX_SESSIONS:
            await _close(_sessions.pop(next(iter(_sessions))))
        # TLS and HTTP/2 follow Chrome 150. Headers stay explicit, so an API
        # call does not inherit the profile's navigation headers.
        session = AsyncSession(
            impersonate=_CHROME, default_headers=False, timeout=20, trust_env=False
        )
    _sessions[key] = session
    return session


async def bili_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    json_body: Any = None,
    data: Any = None,
) -> BiliHttp:
    """`headers` override the defaults."""
    from curl_cffi.requests.exceptions import RequestException

    hdrs = {**BILI_BROWSER_HEADERS, **(headers or {})}
    jar = dict(cookies or {})
    jar.setdefault("opus-goback", "1")
    proxy = current_proxy()
    extra: dict[str, Any] = {}
    if proxy:
        extra["proxy"] = proxy
    if json_body is not None:
        extra["json"] = json_body
    if data is not None:
        extra["data"] = data
    try:
        session = await _bili_session(jar.get("buvid3", ""))
        _drop_login_cookies(session)
        response = await session.request(method, url, headers=hdrs, cookies=jar, **extra)
        return BiliHttp(response.status_code, response.text, _response_cookies(response))
    except RequestException as exc:
        raise FetchError(f"Could not fetch {redact_fetch_url(url)}") from exc


async def bili_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    json_body: Any = None,
    data: Any = None,
    params: Mapping[str, str] | None = None,
) -> Any:
    if params:
        url = f"{url}?{urlencode(params)}"
    resp = await bili_request(
        method, url, headers=headers, cookies=cookies, json_body=json_body, data=data,
    )
    where = redact_fetch_url(url)
    if resp.status >= 400:
        raise FetchError(f"HTTP {resp.status} from {where}", status=resp.status, body=resp.text)
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise FetchError(f"Bilibili did not return JSON from {where}") from exc


async def bili_login_effective(
    cookie: str,
    *,
    request: Callable[..., Awaitable[BiliHttp]] | None = None,
) -> bool | None:
    """True when passport still accepts the login cookie. None if the check could not run."""
    pairs = parse_cookie_header(cookie)
    if not pairs.get("SESSDATA") or not pairs.get("bili_jct"):
        return False
    try:
        info = await (request or bili_request)("GET", BILI_COOKIE_INFO_URL, cookies=pairs)
    except FetchError:
        info = None
    finally:
        if request is None:
            await close_bili_connections()
    if info is None:
        return None
    if info.status in {401, 403}:
        return False
    if info.status >= 400:
        return None
    try:
        payload = info.json()
    except json.JSONDecodeError:
        return None
    return {0: True, -101: False}.get(payload.get("code") if isinstance(payload, dict) else None)


async def refresh_bili_login_cookie(
    cookie: str,
    ac_time: str,
    *,
    request: Callable[..., Awaitable[BiliHttp]] | None = None,
    correspond_path: Callable[[int], str] | None = None,
) -> BiliRefresh:
    """Refresh SESSDATA using ac_time_value. Returns the new Cookie header and token.

    The refresh stays on the first proxy, including when a phone ask is what
    surfaced the dead login. It does not run when that proxy failed this
    poll's exit check.
    """
    from .app import bind_proxy, reset_proxy
    from .round import backup_proxy, exit_failed

    ac_time = ac_time.strip()
    pairs = parse_cookie_header(cookie)
    if not pairs.get("SESSDATA") or not pairs.get("bili_jct") or not ac_time:
        raise FetchError("Bilibili login refresh needs SESSDATA, bili_jct, and ac_time_value")
    proxy = backup_proxy()
    if exit_failed(proxy):
        log.info("Bilibili login refresh skipped: %s failed the exit check", proxy_label(proxy))
        return BiliRefresh(cookie, ac_time, changed=False)
    token = bind_proxy(proxy)
    try:
        return await _refresh_login_cookie(
            request or bili_request, correspond_path or bili_correspond_path, pairs, ac_time
        )
    finally:
        if request is None:
            await close_bili_connections()
        reset_proxy(token)


def _refresh_data(resp: BiliHttp, url: str = "") -> dict[str, Any]:
    if resp.status >= 400:
        if not url:
            raise FetchError(_REFRESH_FAIL)
        raise FetchError(
            f"HTTP {resp.status} from {redact_fetch_url(url)}",
            status=resp.status,
            body=resp.text,
        )
    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise FetchError(_REFRESH_FAIL) from exc
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise FetchError(_REFRESH_FAIL)
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


async def _refresh_login_cookie(
    do: Callable[..., Awaitable[BiliHttp]],
    make_path: Callable[[int], str],
    pairs: dict[str, str],
    ac_time: str,
) -> BiliRefresh:
    data = _refresh_data(await do("GET", BILI_COOKIE_INFO_URL, cookies=pairs), BILI_COOKIE_INFO_URL)
    if not data.get("refresh"):
        return BiliRefresh(format_cookie_header(pairs), ac_time, changed=False)
    page = await do(
        "GET",
        BILI_CORRESPOND_URL.format(path=make_path(int(data.get("timestamp") or 0))),
        cookies=pairs,
    )
    match = _REFRESH_CSRF_RE.search(page.text) if page.status < 400 else None
    if match is None:
        raise FetchError(_REFRESH_FAIL)
    refreshed = await do(
        "POST",
        BILI_REFRESH_URL,
        cookies=pairs,
        data={
            "csrf": pairs["bili_jct"],
            "refresh_csrf": match.group(1),
            "refresh_token": ac_time,
            "source": "main_web",
        },
    )
    new_ac = str(_refresh_data(refreshed).get("refresh_token") or "").strip()
    updates = {key: value for key, value in refreshed.cookies.items() if value}
    if "SESSDATA" not in updates or "bili_jct" not in updates or not new_ac:
        raise FetchError(_REFRESH_FAIL)
    merged = {**pairs, **updates}
    merged.pop("ac_time_value", None)
    new_cookie = format_cookie_header(merged)
    confirm = await do(
        "POST",
        BILI_CONFIRM_URL,
        cookies=parse_cookie_header(new_cookie),
        data={"csrf": updates["bili_jct"], "refresh_token": ac_time},
    )
    if confirm.status >= 400:
        log.warning("Bilibili login cookie confirm failed HTTP %s", confirm.status)
    else:
        try:
            confirmed = confirm.json()
        except json.JSONDecodeError:
            confirmed = {}
        if not isinstance(confirmed, dict) or confirmed.get("code") != 0:
            log.warning("Bilibili login cookie confirm returned %s", confirm.text[:200])
    return BiliRefresh(new_cookie, new_ac, changed=True)


_cached_key = ""
# Element boxes from the site script: (tag id, class, box size, offset).
_DM_BOXES = (
    (2, "clearfix g-search search-container", (274, 601), (134, 30)),
    (2, "wrapper", (332, 64), (1101, 338)),
)
_dm_rng = random.Random()


class Device(NamedTuple):
    """The window dm_img measures. It does not change between polls."""

    window_width: int
    window_height: int
    renderer: str


_DEVICE = Device(1440, 875, WEBGL_RENDERER)


def current_device() -> Device:
    return _DEVICE


def clear_wbi_key() -> None:
    """Drop the cached mixin so the next fetch hits nav again."""
    global _cached_key
    _cached_key = ""


def mixin_from_nav(payload: Mapping[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    wbi = data.get("wbi_img") if isinstance(data, dict) else None
    img = _wbi_token(wbi.get("img_url")) if isinstance(wbi, dict) else ""
    sub = _wbi_token(wbi.get("sub_url")) if isinstance(wbi, dict) else ""
    mixed = img + sub
    key = "".join(mixed[i] for i in MIXIN_INDEX if i < len(mixed))[:32]
    if not img or not sub or len(key) < 32:
        raise FetchError("Could not read Bilibili WBI key")
    return key


def sign_wbi(
    params: Mapping[str, Any],
    mixin: str,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    """Add wts and w_rid. This does not call nav."""
    signed = {key: value for key, value in params.items() if key != "w_rid"}
    signed["wts"] = int(time.time() if now is None else now)
    if not signed.get("web_location"):
        signed["web_location"] = WEB_LOCATION
    query = urlencode(sorted(signed.items()))
    signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return signed


def _dm_token(text: str) -> str:
    return base64.b64encode(text.encode()).decode()[:-2]


def dm_params(params: Mapping[str, Any], *, rng: random.Random | None = None) -> dict[str, Any]:
    """Add the page fingerprint. This does not call nav."""
    pick = rng or _dm_rng
    device = current_device()
    signed = dict(params)
    signed.update(
        {
            "dm_img_list": dm_img_list(pick),
            "dm_img_str": _dm_token(WEBGL_VERSION),
            "dm_cover_img_str": _dm_token(f"{device.renderer}{WEBGL_VENDOR}"),
            "dm_img_inter": dm_img_inter(pick),
        }
    )
    return signed


def _window() -> tuple[int, int]:
    """The inner window of this poll's machine."""
    device = current_device()
    return device.window_width, device.window_height


def dm_img_list(rng: random.Random) -> str:
    """One mouse point inside the window, encoded as the site script does."""
    width, height = _window()
    x = min(max(round(rng.gauss(width / 2, width / 6)), 0), width - 1)
    y = min(max(round(rng.gauss(height / 2, height / 6)), 0), height - 1)
    point = {
        "x": 3 * x + 2 * y,
        "y": 4 * x - 5 * y,
        "z": 0,
        "timestamp": max(round(rng.gauss(30, 5)), 0),
        "type": 0,
    }
    return _compact([point])


def dm_img_inter(rng: random.Random) -> str:
    """Element boxes, window size, and scroll offset, each with its own seed."""
    ds = []
    for tag, name, size, offset in _DM_BOXES:
        p = _dm_wh(rng, *size)
        s = _dm_of(rng, *offset)
        ds.append({"t": tag, "c": _dm_token(name), "p": [p[0], p[2], p[1]], "s": [s[2], s[0], s[1]]})
    width, height = _window()
    return _compact(
        {"ds": ds, "wh": _dm_wh(rng, width, height), "of": _dm_of(rng, 0, 0)}
    )


def _dm_wh(rng: random.Random, width: int, height: int) -> list[int]:
    seed = rng.randrange(114)
    return [2 * width + 2 * height + 3 * seed, 4 * width - height + seed, seed]


def _dm_of(rng: random.Random, top: int, left: int) -> list[int]:
    seed = rng.randrange(514)
    return [3 * top + 2 * left + seed, 4 * top - 4 * left + 2 * seed, seed]


def _compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


async def fetch_wbi_key(
    *,
    call: Callable[..., Awaitable[Any]] | None = None,
    cookies: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> str:
    """The mixin key. Nav is read once until `clear_wbi_key`."""
    global _cached_key
    if _cached_key:
        return _cached_key
    do = call or bili_json
    extra = {"headers": dict(headers)} if headers else {}
    payload = await do("GET", BILI_NAV_URL, cookies=dict(cookies or {}), **extra)
    if not isinstance(payload, dict):
        raise FetchError("Could not read Bilibili WBI key")
    _cached_key = mixin_from_nav(payload)
    return _cached_key


def _wbi_token(url: object) -> str:
    name = str(url or "").strip().rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]
