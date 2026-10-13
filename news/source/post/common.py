"""Post rules both sources share: cookie headers, fetch faults, account groups, card pictures, and the proxy exit check.

A picture fits when its height is between half its width and twice its width,
from a 16:9 cover through a 9:16 screenshot. Anything taller or wider is a
long picture. A Weibo file that is too heavy to attach, or that is not a
usable image, is treated the same way. The card then shows the platform
notice instead of the photo.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from config import BASE_DIR, settings, write_text_atomic
from fetch import http_url

log = logging.getLogger(__name__)

COOKIE_SKIP_SECONDS = 6 * 60 * 60
DOWN_SKIP_SECONDS = 45 * 60
_HTTP_STATUS_RE = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)
_BILI_COOKIE_MARKERS = ("-352", "CAPTCHA", "风控")


@dataclass(frozen=True)
class FeedFault:
    """Skip this follow after a failed fetch. `cookie` means hint `/news cookie`.

    `stop_round` means stop the remaining accounts on this proxy. Other
    proxies keep going. An HTTP 412 leaves that Bilibili proxy out of the
    next poll. An HTTP 432 does the same for Weibo. Nobody is parked past
    this poll.
    """

    skip_seconds: float
    cookie: bool = False
    stop_round: bool = False


_COOKIE = FeedFault(COOKIE_SKIP_SECONDS, cookie=True)
_DOWN = FeedFault(DOWN_SKIP_SECONDS)
# 412 and 432 stop the remaining accounts on that proxy and leave it out
# of the next poll. A blank Bilibili space feed is not this.
_PROXY_PAUSE = FeedFault(0, stop_round=True)


def bili_blocked(exc: BaseException) -> bool:
    """True when Bilibili answered HTTP 412."""
    return _http_status(exc) == 412


def _http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    match = _HTTP_STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else None


def classify_feed_fault(platform: str, exc: BaseException) -> FeedFault | None:
    """Classify a failed fetch: cookie, backoff, down, or ignore."""
    body = getattr(exc, "body", "") or ""
    text = f"{exc} {body}".upper()
    status = _http_status(exc)
    if platform == "bilibili":
        # 412 keeps its own path even when the body also says -352. Pushing on
        # after a 412 is how an address gets blocked harder. The round stops
        # and that proxy sits out the next poll. A login cookie is not
        # treated as expired.
        if bili_blocked(exc):
            return _PROXY_PAUSE
        if status in {401, 403} or any(mark in text for mark in _BILI_COOKIE_MARKERS):
            return _COOKIE
        return _DOWN if status == 503 else None
    if platform == "weibo":
        if status == 432 or "weibo guest rejected" in str(exc).lower():
            return _PROXY_PAUSE
        return _COOKIE if status in {401, 403} else None
    return None


@dataclass(frozen=True)
class Share:
    """Accounts dealt to one proxy for this poll, in the order given."""

    proxy: str
    accounts: tuple[str, ...]


def group_accounts(
    accounts: Sequence[str], slots: int, per_slot: int
) -> tuple[tuple[str, ...], ...]:
    """Contiguous groups. `per_slot` is the size while that covers `slots`.

    A longer list grows the groups so every account is still included.
    """
    count = len(accounts)
    if count == 0:
        return ()
    slots = max(slots, 1)
    size = max(per_slot, 1)
    if count > 1 and (count + size - 1) // size > slots:
        size = (count + slots - 1) // slots
    rows = tuple(accounts)
    return tuple(tuple(rows[index : index + size]) for index in range(0, count, size))


def share_accounts(
    accounts: Sequence[str],
    proxies: Sequence[str],
    sitting: Sequence[str] = (),
) -> tuple[list[Share], tuple[str, ...]]:
    """One slice per proxy still in the round, and the sit-outs this round spends.

    Account order stays as given. A sitting proxy is skipped and its accounts
    are dealt onto the proxies that remain. An empty proxy list is one direct
    connection. The same accounts stay on the same proxy each poll.
    """
    if not accounts:
        return [], ()
    listed = tuple(proxies) if proxies else ("",)
    quiet = set(sitting)
    live = tuple(proxy for proxy in listed if proxy not in quiet)
    spent = tuple(proxy for proxy in listed if proxy in quiet)
    if not live:
        return [], spent
    groups = group_accounts(accounts, len(live), 1)
    return [Share(proxy, group) for proxy, group in zip(live, groups)], spent


def read_json(path: Path) -> dict[str, Any]:
    """A saved JSON object. A missing or unreadable file is empty."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("Could not read %s", path, exc_info=True)
        return {}
    return raw if isinstance(raw, dict) else {}


def write_json(path: Path, payload: object) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2) + "\n")


REST_PATH = BASE_DIR / "data" / "weibo_rest.json"


class ProxyRest:
    """A proxy left out of the next round that would use it.

    The mark is saved, like a Bilibili sit-out, so a restart still skips that proxy once.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = REST_PATH if path is None else path
        self._out: set[str] | None = None

    def sits_out(self, proxy: str) -> bool:
        """True when this proxy sits out the next round that would use it."""
        return proxy in self._load()

    def rest(self, proxy: str) -> None:
        """Leave this proxy out of the next round that would use it."""
        self._load().add(proxy)
        self._save()

    def take(self, proxy: str) -> bool:
        """Skip this round once, then clear that mark."""
        rows = self._load()
        if proxy not in rows:
            return False
        rows.remove(proxy)
        self._save()
        return True

    def _load(self) -> set[str]:
        if self._out is None:
            self._out = {
                str(proxy)
                for proxy, row in read_json(self.path).items()
                if isinstance(row, dict) and row.get("sit_out")
            }
        return self._out

    def _save(self) -> None:
        body = {proxy: {"sit_out": True} for proxy in sorted(self._out or ())}
        write_json(self.path, body)


weibo_rest = ProxyRest()


def proxy_label(proxy: str) -> str:
    """Host and port. A user and password are left out of the log."""
    text = (proxy or "").strip()
    if not text:
        return "direct"
    parts = urlsplit(text if "://" in text else f"//{text}")
    host = parts.hostname or text
    return f"{host}:{parts.port}" if parts.port else host


_EXIT_URL = "https://api.ipify.org"
_EXIT_SECONDS = 8
EXITS_PATH = BASE_DIR / "data" / "proxy_exits.json"
_EXIT_NAMES = {"bilibili": "Bilibili", "weibo": "Weibo"}

Lookup = Callable[[str], Awaitable[str | None]]
Refresh = Callable[[str], None]
Plan = Callable[..., tuple[list[Any], tuple[str, ...]]]


def check_enabled() -> bool:
    """True unless `IP_CHECK` is off. A blank setting leaves the check on."""
    return bool(getattr(settings, "ip_check", True))


def _parse_exit(body: str) -> str | None:
    text = body.strip()
    if not text or len(text) > 64 or any(char.isspace() for char in text):
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


async def _fetch_exit(proxy: str) -> tuple[int, str]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=_EXIT_SECONDS)
    extra = {"proxy": proxy} if proxy else {}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(_EXIT_URL, **extra) as response:
            return response.status, await response.text()


async def lookup_exit(proxy: str) -> str | None:
    """The public IP seen through `proxy`. None when the check fails.

    An empty proxy is a direct connection. The proxy string is not logged.
    """
    try:
        status, body = await _fetch_exit(proxy)
    except Exception:
        return None
    return _parse_exit(body) if status == 200 else None


class ExitBook:
    """The last exit IP saved for each platform and proxy."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = EXITS_PATH if path is None else path
        self._rows: dict[str, dict[str, str]] | None = None

    def changed(self, platform: str, proxy: str, ip: str) -> bool:
        """True when this proxy had a different exit. A first sight is not a change."""
        old = self._load().get(platform, {}).get(proxy)
        return bool(old) and old != ip

    def remember(self, platform: str, proxy: str, ip: str) -> None:
        rows = self._load().setdefault(platform, {})
        if rows.get(proxy) == ip:
            return
        rows[proxy] = ip
        self._save()

    def _load(self) -> dict[str, dict[str, str]]:
        if self._rows is not None:
            return self._rows
        self._rows = {}
        for platform, rows in read_json(self.path).items():
            if not isinstance(platform, str) or not isinstance(rows, dict):
                continue
            kept = {
                proxy: ip
                for proxy, ip in rows.items()
                if isinstance(proxy, str) and isinstance(ip, str) and _parse_exit(ip)
            }
            if kept:
                self._rows[platform] = kept
        return self._rows

    def _save(self) -> None:
        if self._rows is not None:
            write_json(self.path, self._rows)


exits = ExitBook()


async def vet(
    proxies: Sequence[str],
    taken: Mapping[str, str],
    *,
    platform: str,
    refresh: Refresh | None = None,
    refresh_note: str = "fresh identity",
    lookup: Lookup | None = None,
    seen: dict[str, str | None] | None = None,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Proxies that passed, and exit IP to the proxy that holds it.

    `taken` is the exits already used in this check. A failed lookup, or an
    exit another proxy holds, is left out. An exit that moved calls `refresh`
    once. `seen` remembers a lookup so a replanned window does not ask again.
    """
    ask = lookup or lookup_exit
    held = dict(taken)
    ok: list[str] = []
    name = _EXIT_NAMES.get(platform, platform)
    for proxy in proxies:
        if proxy in held.values():
            ok.append(proxy)
            continue
        if seen is not None and proxy in seen:
            ip = seen[proxy]
        else:
            try:
                ip = await ask(proxy)
            except Exception:
                ip = None
            if seen is not None:
                seen[proxy] = ip
        label = proxy_label(proxy)
        if not ip:
            log.warning("%s %s sits out this check: exit check failed", name, label)
            continue
        owner = held.get(ip)
        if exits.changed(platform, proxy, ip):
            log.info("%s %s exit changed; %s", name, label, refresh_note)
            if refresh is not None:
                refresh(proxy)
        exits.remember(platform, proxy, ip)
        if owner not in (None, proxy):
            log.warning(
                "%s %s sits out this check: same exit as %s", name, label, proxy_label(owner)
            )
            continue
        held[ip] = proxy
        ok.append(proxy)
    return tuple(ok), held


async def vetted_window(
    accounts: Sequence[str],
    proxies: Sequence[str],
    turn: int,
    sitting: Sequence[str],
    *,
    plan: Plan,
    platform: str,
    refresh: Refresh | None = None,
    refresh_note: str = "fresh identity",
    lookup: Lookup | None = None,
) -> tuple[list[Any], tuple[str, ...], tuple[str, ...], dict[str, str]]:
    """The planned phones, the sit-outs this window spends, the pool, and exits.

    A proxy that fails the check, or shares an exit, leaves the pool and the
    window is planned again. `IP_CHECK` off returns the plan unchanged.
    """
    pool = tuple(proxies)
    if not check_enabled():
        phones, spent = plan(accounts, pool, turn, sitting)
        return phones, spent, pool, {}
    blocked: set[str] = set()
    seen: dict[str, str | None] = {}
    while True:
        live = tuple(proxy for proxy in pool if proxy not in blocked)
        phones, spent = plan(accounts, live, turn, sitting)
        if not phones:
            return [], spent, live, {}
        order = list(dict.fromkeys(phone.proxy for phone in phones))
        ok, taken = await vet(
            order,
            {},
            platform=platform,
            refresh=refresh,
            refresh_note=refresh_note,
            lookup=lookup,
            seen=seen,
        )
        missed = [proxy for proxy in order if proxy not in ok]
        if not missed:
            return phones, spent, live, taken
        blocked.update(missed)


def parse_cookie_header(header: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        pairs[key.strip()] = value.strip()
    return pairs


def format_cookie_header(pairs: Mapping[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in pairs.items() if value)


_MIN_RATIO = 0.5
_MAX_RATIO = 2.0
_NOTICES: dict[str, bytes] = {}
_NOTICE_DIR = BASE_DIR / "image" / "news_image" / "long"
_BILI_NOTICE = _NOTICE_DIR / "bilibili_long.jpg"
_WEIBO_NOTICE = _NOTICE_DIR / "weibo_long.jpg"


@dataclass(frozen=True)
class CardPicture:
    data: bytes
    filename: str


def pixel(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        try:
            number = int(float(str(value)))
        except (TypeError, ValueError):
            return None
    return number if number > 0 else None


def long_shape(width: object, height: object) -> bool:
    """True when the picture is too tall or too wide for the embed slot."""
    wide = pixel(width)
    high = pixel(height)
    if wide is None or high is None:
        return False
    ratio = high / wide
    return ratio < _MIN_RATIO or ratio > _MAX_RATIO


def image_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a JPEG, PNG, GIF, or WebP header."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return _webp_size(data)
    if data.startswith(b"\xff\xd8"):
        return _jpeg_size(data)
    return None


async def author_face(item: Any) -> CardPicture | None:
    """Weibo account face. Discord cannot load sinaimg, so the card attaches it."""
    if getattr(item, "platform", None) not in {"weibo", "wb"}:
        return None
    url = getattr(item, "author_icon", None)
    if not url:
        return None
    from news.source.post.weibo.preview import fetch_cover

    return await fetch_cover(url, name="face")


def embed_image_url(item: Any) -> str | None:
    """Remote image Discord may hotlink. Weibo and long Bilibili photos stay off the URL."""
    platform = getattr(item, "platform", None)
    if platform in {"weibo", "wb"}:
        return None
    image = http_url(getattr(item, "image_url", None))
    long = long_shape(getattr(item, "image_width", None), getattr(item, "image_height", None))
    if image and platform in {"bilibili", "bili"} and long:
        return None
    return image


async def card_picture(item: Any, *, call: Any | None = None) -> CardPicture | None:
    """Picture to attach. A long, heavy, or unusable Weibo photo is the platform notice."""
    platform = getattr(item, "platform", None)
    wide = getattr(item, "image_width", None)
    high = getattr(item, "image_height", None)
    if platform in {"bilibili", "bili"}:
        return notice_picture(_BILI_NOTICE) if long_shape(wide, high) else None
    if platform not in {"weibo", "wb"} or not getattr(item, "image_url", None):
        return None
    if long_shape(wide, high):
        return notice_picture(_WEIBO_NOTICE)
    from news.source.post.weibo.preview import item_cover

    cover = await item_cover(item, call=call)
    size = None if cover is None else image_size(cover.data)
    if size is None or long_shape(*size):
        return notice_picture(_WEIBO_NOTICE)
    return cover


def notice_picture(path: Path) -> CardPicture:
    key = str(path)
    data = _NOTICES.get(key)
    if data is None:
        data = _NOTICES[key] = path.read_bytes()
    return CardPicture(data, path.name)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    index, size = 2, len(data)
    while index + 1 < size:
        if data[index] != 0xFF:
            index += 1
            continue
        while index < size and data[index] == 0xFF:
            index += 1
        if index >= size:
            return None
        marker = data[index]
        index += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if index + 1 >= size:
            return None
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2 or index + length > size:
            return None
        if marker in sof and length >= 7:
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return (width, height) if width and height else None
        index += length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return (width, height) if width and height else None
    if chunk == b"VP8L" and data[20] == 0x2F and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None
