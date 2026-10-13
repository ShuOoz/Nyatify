"""Bilibili space dynamics via the app gRPC feed.

DynSpace on grpc.biliapi.net, guest buvid, no login. The fingerprint and the
first-open time stay with that buvid. The session id lasts until this process
stops. A connection failure retries on app.bilibili.com. A blank list and an
HTTP 412 stay on the first host.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import BASE_DIR, proxy_url, settings, write_text_atomic
from fetch import FetchError, FetchedItem, redact_fetch_url
from news.source.post.common import proxy_label

from .dynamics import (
    CHARGE, abs_url, charge_text, drop_labels, first_image, forward_stamp, join_text,
    live_rcmd, post, reserve_notice, room_url, same_caption, with_live,
)
from .protobuf import decode_message, encode_message

_DYN_PATH = "/bilibili.app.dynamic.v2.Dynamic/DynSpace"
DYN_SPACE_URL = "https://grpc.biliapi.net" + _DYN_PATH
_FAILOVER_URL = "https://app.bilibili.com" + _DYN_PATH
# One published phone build. The device name stays with the saved buvid.
_BUILD, _VERSION = 7380300, "7.38.0"
_MODEL, _BRAND, _OSVER, _DEVICE_BUILD = "Pixel 6", "google", "13", "oriole"
_CRONET = "grpc-java-cronet/1.36.1"
_BUVID_RE = re.compile(r"^XU[0-9A-F]{35}$")
_FP_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_PHONES_PATH = BASE_DIR / "data" / "bili_phones.json"
# The single phone from before proxies were a list. Read once, never moved.
LEGACY_BUVID_PATH = BASE_DIR / "data" / "bili_buvid.txt"
_bound: ContextVar[str | None] = ContextVar("bili_bound_proxy", default=None)
_phone: ContextVar[int] = ContextVar("bili_phone_index", default=0)
# Dynamic ids are snowflakes. The high 32 bits are seconds after 2017-07-01 00:00 Asia/Shanghai.
_DYN_EPOCH = 1_498_838_400
_MAX_FRAME = 8_000_000
# Description.type for a sticker, and the opus text-node type for one.
_EMOJI, _EMOTE = 9, 2
_SESSION_CHARS = "0123456789abcdefghijklmnopqrstuvwxyz"
_View = tuple[str, str, str | None, int | None, int | None, str]
_UA = (
    f"Dalvik/2.1.0 (Linux; U; Android {_OSVER}; {_MODEL} Build/{_DEVICE_BUILD}) "
    f"{_VERSION} os/android model/{_MODEL} mobi_app/android build/{_BUILD} "
    f"channel/master innerVer/{_BUILD} osVer/{_OSVER} network/2"
)

AppPost = Callable[[bytes, Mapping[str, str]], Awaitable[bytes]]
MintBuvid = Callable[..., str]
log = logging.getLogger(__name__)
_session: Any = None
_session_loop: asyncio.AbstractEventLoop | None = None

def bind_proxy(proxy: str) -> Token[str | None]:
    """Use `proxy` for the app and the website until `reset_proxy`. Empty is direct."""
    return _bound.set(proxy)

def reset_proxy(token: Token[str | None]) -> None:
    _bound.reset(token)

def bind_phone(index: int) -> Token[int]:
    """Use this phone on the bound proxy until `reset_phone`. Zero is the first."""
    return _phone.set(index if index > 0 else 0)

def reset_phone(token: Token[int]) -> None:
    _phone.reset(token)

def current_phone() -> int:
    return _phone.get()

def proxy_key() -> str:
    chosen = _bound.get()
    return chosen.strip() if chosen is not None else (proxy_url("bili_proxy") or "").strip()

def current_proxy() -> str | None:
    """None is direct, including a bound direct phone."""
    if _bound.get() is not None:
        return proxy_key() or None
    return proxy_url("bili_proxy")

def current_node() -> str:
    return proxy_label(proxy_key())

@dataclass
class _Phone:
    buvid: str
    fp: str = ""
    fts: int = 0

    def dump(self) -> str | dict[str, Any]:
        if self.fp and self.fts > 0:
            return {"buvid": self.buvid, "fp": self.fp, "fts": self.fts}
        return self.buvid

def _phone_from(value: Any) -> _Phone | None:
    # A string is a phone from an older file.
    if isinstance(value, str):
        return _Phone(value) if _BUVID_RE.fullmatch(value) else None
    if not isinstance(value, dict):
        return None
    buvid = value.get("buvid")
    if not isinstance(buvid, str) or not _BUVID_RE.fullmatch(buvid):
        return None
    fp, fts = value.get("fp"), value.get("fts")
    if not isinstance(fp, str) or not _FP_RE.fullmatch(fp):
        fp = ""
    if isinstance(fts, bool) or not isinstance(fts, int) or fts <= 0:
        fts = 0
    return _Phone(buvid, fp, fts)

def _slots_from(value: Any) -> dict[int, _Phone]:
    one = _phone_from(value)
    if one is not None:
        return {0: one}
    if not isinstance(value, list):
        return {}
    return {index: phone for index, item in enumerate(value) if (phone := _phone_from(item))}

def _slots_dump(slots: Mapping[int, _Phone]) -> str | dict[str, Any] | list[Any]:
    kept = {index: phone for index, phone in slots.items() if _BUVID_RE.fullmatch(phone.buvid)}
    if not kept:
        return ""
    if set(kept) == {0}:
        return kept[0].dump()
    last = max(kept)
    return [kept[index].dump() if index in kept else "" for index in range(last + 1)]

class PhoneBook:
    """Phones saved per proxy. The first use of an older phone fills its fingerprint."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = DEFAULT_PHONES_PATH if path is None else path
        self._rows: dict[str, dict[int, _Phone]] | None = None
        self._memory: dict[str, dict[int, _Phone]] = {}

    def get(
        self, proxy: str | None = None, mint: MintBuvid | None = None, *, index: int | None = None
    ) -> str:
        key = proxy_key() if proxy is None else proxy
        slot = current_phone() if index is None else max(0, index)
        found = self._memory.get(key, {}).get(slot) or self._load().get(key, {}).get(slot)
        if found is not None:
            self._keep(found)
            return found.buvid
        phone = _Phone((mint or app_buvid)())
        self._memory.setdefault(key, {})[slot] = phone
        if _BUVID_RE.fullmatch(phone.buvid):
            if self._rows is None:
                self._rows = {}
            self._rows.setdefault(key, {})[slot] = phone
            self._keep(phone)
        return phone.buvid

    def peek(self) -> str | None:
        rows = {key: dict(slots) for key, slots in self._load().items()}
        for key, slots in self._memory.items():
            rows.setdefault(key, {}).update(slots)
        # The bound proxy is asked first. pop keeps it out of the rest.
        current = rows.pop(proxy_key(), {})
        phones = (phone for slots in (current, *rows.values()) for phone in slots.values())
        return next((phone.buvid for phone in phones if _BUVID_RE.fullmatch(phone.buvid)), None)

    def fingerprint(self, buvid: str) -> tuple[str, int]:
        phone = self._find(buvid)
        if phone is None:
            phone = _loose_phones.setdefault(buvid, _Phone(buvid))
        else:
            _loose_phones.pop(buvid, None)
        self._keep(phone)
        return phone.fp, phone.fts

    def forget(self, proxy: str) -> None:
        loaded = self._load()
        slots = {**loaded.get(proxy, {}), **self._memory.get(proxy, {})}
        if not slots:
            return
        if self._rows is None:
            self._rows = dict(loaded)
        for phone in slots.values():
            _session_ids.pop(phone.buvid, None)
            _loose_phones.pop(phone.buvid, None)
        self._rows.pop(proxy, None)
        self._memory.pop(proxy, None)
        self._save()

    def _keep(self, phone: _Phone) -> None:
        if phone.fp and phone.fts > 0:
            return
        now = datetime.now()
        phone.fp = _device_fp(phone.buvid, now=now, nonce=os.urandom(8).hex())
        phone.fts = int(now.timestamp() * 1000)
        if self._rows is None:
            return
        if any(item is phone for slots in self._rows.values() for item in slots.values()):
            self._save()

    def _find(self, buvid: str) -> _Phone | None:
        pools = (*self._load().values(), *self._memory.values())
        return next(
            (phone for slots in pools for phone in slots.values() if phone.buvid == buvid), None
        )

    def _load(self) -> dict[str, dict[int, _Phone]]:
        if self._rows is not None:
            return self._rows
        text = _read_text(self.path)
        if text is None and self.path == DEFAULT_PHONES_PATH:
            text = _read_text(LEGACY_BUVID_PATH)
        if text is None:
            return {}
        self._rows = _phones_from_text(text, self.path)
        return self._rows

    def _save(self) -> None:
        assert self._rows is not None
        payload = {
            key: dumped
            for key, slots in sorted(self._rows.items())
            if (dumped := _slots_dump(slots))
        }
        write_text_atomic(self.path, json.dumps(payload, indent=2) + "\n")

def _phones_from_text(text: str, path: Path) -> dict[str, dict[int, _Phone]]:
    if text.startswith("{"):
        try:
            raw = json.loads(text)
        except ValueError:
            log.warning("Ignoring phones in %s", path)
            return {}
        return {key: slots for key, value in raw.items() if (slots := _slots_from(value))}
    if _BUVID_RE.fullmatch(text):
        return {settings.bili_proxies[0] if settings.bili_proxies else "": {0: _Phone(text)}}
    if text:
        log.warning("Ignoring app buvid in %s", path)
    return {}


def _read_text(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("Could not read %s", path, exc_info=True)
        return None

buvid_store = PhoneBook()
_loose_phones: dict[str, _Phone] = {}  # used for headers, never written
_session_ids: dict[str, str] = {}  # one session id per phone, until this process stops

def saved_buvid(mint: MintBuvid | None = None) -> str:
    return buvid_store.get(mint=mint)

def fresh_phones(proxy: str) -> None:
    buvid_store.forget(proxy)

def stored_buvid() -> str | None:
    return buvid_store.peek()

def app_buvid(seed: str = "") -> str:
    identity = seed or os.urandom(16).hex()
    digest = hashlib.md5(identity.encode()).hexdigest().upper()
    return f"XU{digest[2]}{digest[12]}{digest[22]}{digest}"

def items_from_app(
    payload: Mapping[int, Any], *, account: str
) -> tuple[str | None, list[FetchedItem]]:
    name = None
    items: list[FetchedItem] = []
    for card in payload.get(1) or []:
        if not isinstance(card, dict):
            continue
        author = _author(card)[0]
        if name is None and author:
            name = author
        item = _item(card, account=account, source_name=author or name)
        if item is not None:
            items.append(item)
    return name, items

async def fetch_app_page(
    account: str,
    *,
    buvid: str = "",
    post: AppPost | None = None,
    page: int = 1,
) -> tuple[str | None, list[FetchedItem]]:
    """The first page of an account's DynSpace feed."""
    if not str(account).isdigit():
        raise FetchError("Bilibili app feed needs a numeric uid")
    fields: dict[int, Any] = {1: int(account), 4: 8, 5: page, 6: "space"}
    raw = await (post or _grpc_post)(
        grpc_frame(encode_message("DynSpaceReq", fields)),
        _headers(buvid.strip() or saved_buvid()),
    )
    try:
        payload = decode_message("DynSpaceRsp", grpc_message(raw))
    except (ValueError, gzip.BadGzipFile) as exc:
        where = redact_fetch_url(DYN_SPACE_URL)
        raise FetchError(f"Bilibili app feed was not a DynSpace reply from {where}") from exc
    return items_from_app(payload, account=account)

def grpc_frame(payload: bytes) -> bytes:
    if len(payload) > _MAX_FRAME:
        raise ValueError("grpc frame too large")
    return b"\x00" + len(payload).to_bytes(4, "big") + payload

def grpc_message(body: bytes) -> bytes:
    if len(body) < 5:
        raise ValueError("short grpc frame")
    size = int.from_bytes(body[1:5], "big")
    if size > _MAX_FRAME or 5 + size > len(body):
        raise ValueError("bad grpc frame")
    data = body[5:5 + size]
    return gzip.decompress(data) if body[0] & 1 else data

def _closed(session: Any) -> bool:
    return bool(getattr(session, "_closed", False) or getattr(session, "closed", False))

async def close_app_http() -> None:
    global _session, _session_loop
    session, _session, _session_loop = _session, None, None
    if session is None or _closed(session):
        return
    try:
        await session.close()
    except Exception:
        pass  # a close error must not replace the fetch result

def _b64(name: str, fields: Mapping[int, Any]) -> str:
    return base64.b64encode(encode_message(name, fields)).decode("ascii")

def _headers(buvid: str) -> dict[str, str]:
    """Empty restriction and exps values are required. The fingerprint is this phone's."""
    fp, opened = buvid_store.fingerprint(buvid)
    locale = {1: "zh", 3: "CN"}
    token = os.urandom(16).hex()
    # A stored id is reused. An empty one stays empty; this call still sends a fresh id.
    session = _session_ids.get(buvid)
    if not session:
        session = "".join(_SESSION_CHARS[byte % 36] for byte in os.urandom(8))
        _session_ids.setdefault(buvid, session)
    return {
        "content-type": "application/grpc",
        "te": "trailers",
        "user-agent": f"{_UA} {_CRONET}",
        "buvid": buvid,
        "x-bili-mid": "0",
        "x-bili-aurora-eid": "",
        "x-bili-aurora-zone": "",
        "x-bili-trace-id": f"{token}:{token[16:]}:0:0",
        "x-bili-metadata-bin": _b64(
            "Metadata", {2: "android", 4: _BUILD, 5: "master", 6: buvid, 7: "android"}
        ),
        "x-bili-device-bin": _b64("Device", {
            1: 1, 2: _BUILD, 3: buvid, 4: "android", 5: "android", 7: "master",
            8: _BRAND, 9: _MODEL, 10: _OSVER, 11: fp, 12: fp, 13: _VERSION, 14: fp, 15: opened,
        }),
        "x-bili-network-bin": _b64("Network", {1: 1}),
        "x-bili-restriction-bin": "",
        "x-bili-exps-bin": "",
        "x-bili-fawkes-req-bin": _b64("FawkesReq", {1: "android64", 2: "prod", 3: session}),
        "x-bili-locale-bin": _b64("Locale", {1: locale, 2: locale, 4: "Asia/Shanghai"}),
    }

def _failover_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """app.bilibili.com names cronet on its own header and omits te."""
    adjusted = {key: value for key, value in headers.items() if key.lower() != "te"}
    suffix = f" {_CRONET}"
    agent = adjusted.get("user-agent", "")
    if agent.endswith(suffix):
        adjusted["user-agent"] = agent[: -len(suffix)]
    adjusted["bili-http-engine"] = "cronet"
    return adjusted

def _device_fp(buvid: str, *, now: datetime, nonce: str) -> str:
    """md5 of buvid and model, then the clock, a nonce, and a byte checksum."""
    digest = hashlib.md5(f"{buvid}{_MODEL}".encode()).hexdigest()
    raw = digest + now.strftime("%Y%m%d%H%M%S") + nonce
    check = sum(int(raw[index:index + 2], 16) for index in range(0, 62, 2)) % 256
    return raw + f"{check:02x}"

def _item(card: Mapping[int, Any], *, account: str, source_name: str | None) -> FetchedItem | None:
    ext = _dict(card.get(4))
    dyn_id = _text(ext.get(1))
    if not dyn_id:
        return None
    title, text, image, width, height, live = _content(card)
    text = with_live(text, live)
    fans = _dict(_mod(card, 2).get(16))
    charge = bool(
        fans.get(1)
        or CHARGE in str(_dict(fans.get(2)).get(2) or "")
        or _dict(ext.get(21)).get(2)
    )
    if charge:
        text = charge_text(title, text, _text(_mod(card, 33).get(3)))
    when = _when_id(dyn_id)
    up = _mod(card, 8).get(10)
    reserve, watch = ("", "")
    if isinstance(up, dict):
        reserve, watch = reserve_notice(_text(up.get(1)), _text(_dict(up.get(2)).get(1)), when)
    author, face = _author(card)
    return post(
        item_id=dyn_id, account=account, title=title, text=text,
        name=author or source_name or "", image=image, width=width, height=height,
        charge=charge, occurred_at=when, author_icon=face, reserve=reserve, watch=watch,
    )

def _content(card: Mapping[int, Any], depth: int = 0) -> _View:
    if depth > 2:
        return "", "", None, None, None, ""
    dynamic = _mod(card, 5)
    desc_mod = _mod(card, 4)
    desc = _caption(desc_mod)
    forward = dynamic.get(6)
    if isinstance(forward, dict):
        nested = forward.get(1) if isinstance(forward.get(1), dict) else {}
        ntitle, ntext, image, width, height, live = _content(nested, depth + 1)
        name = _author(nested)[0] or _text(_dict(card.get(4)).get(4))
        extra = ntext if ntext and ntext != ntitle else ""
        text = "\n".join(part for part in (desc, forward_stamp(name), ntitle, extra) if part)
        return "", text, image, width, height, live
    title, body, image, width, height, live = "", "", None, None, None, ""
    if isinstance(dynamic.get(2), dict):
        title, image = _text(dynamic[2].get(1)), abs_url(dynamic[2].get(2))
    elif isinstance(dynamic.get(7), dict):
        image, width, height = first_image(dynamic[7].get(1) or [], 1, 2, 3)
    elif isinstance(dynamic.get(8), dict):
        article = dynamic[8]
        title, body = _text(article.get(3)), _text(article.get(4))
        covers = article.get(5) or []
        image = abs_url(covers[0]) if covers else None
    elif isinstance(dynamic.get(11), dict):
        row = dynamic[11]
        title, image = _text(row.get(3)), abs_url(row.get(4))
        live = room_url(row.get(1)) or abs_url(row.get(2)) or ""
    elif isinstance(dynamic.get(15), dict):
        title, image, live = live_rcmd(dynamic[15].get(1))
    opus = _mod(card, 30)
    if opus:
        title = title or _paragraph(opus.get(1))
        body = body or _paragraph(opus.get(2))
        if image is None:
            image, width, height = first_image(opus.get(4) or [], 1, 2, 3)
    labels = _sticker_labels(desc_mod) + _emote_labels(opus)
    body = same_caption(body, desc, labels)
    desc = same_caption(desc, body, labels)
    return title, join_text(desc, body), image, width, height, live

def _dicts(raw: object) -> list[Mapping[int, Any]]:
    return [node for node in raw if isinstance(node, dict)] if isinstance(raw, list) else []


def _caption(module: Mapping[int, Any]) -> str:
    """Words in a caption. A sticker piece is left out. A line with none stays."""
    words: list[str] = []
    stickers: list[str] = []
    for node in _dicts(module.get(1)):
        (stickers if node.get(2) == _EMOJI else words).append(str(node.get(1) or ""))
    joined = _text("".join(words))
    flat = _text(module.get(3))
    return (flat or joined) if not stickers else joined or drop_labels(flat, stickers)


def _sticker_labels(module: Mapping[int, Any]) -> tuple[str, ...]:
    return tuple(
        label for node in _dicts(module.get(1))
        if node.get(2) == _EMOJI and (label := str(node.get(1) or ""))
    )


def _node_text(item: Mapping[int, Any]) -> str:
    raw = str(item.get(2) or "")
    if not raw and isinstance(item.get(3), dict):
        return str(item[3].get(1) or "")
    return raw


def _opus_bits(node: object, *, stickers: bool) -> tuple[str, ...]:
    """Opus words. Stickers keep emote nodes. Words keep every other node."""
    rows = _dicts(_dict(_dict(node).get(3)).get(1))
    return tuple(
        text for item in rows
        if (item.get(1) == _EMOTE) is stickers and (text := _node_text(item))
    )


def _emote_labels(opus: Mapping[int, Any]) -> tuple[str, ...]:
    return _opus_bits(opus.get(1), stickers=True) + _opus_bits(opus.get(2), stickers=True)


def _paragraph(node: object) -> str:
    return "".join(_opus_bits(node, stickers=False)).strip()

def _author(card: Mapping[int, Any]) -> tuple[str, str | None]:
    user = _dict(_mod(card, 2).get(3))
    return _text(user.get(2)), abs_url(user.get(3))

def _mod(card: Mapping[int, Any], field: int) -> dict[int, Any]:
    for module in card.get(3) or []:
        body = module.get(field) if isinstance(module, dict) else None
        if isinstance(body, dict):
            return body
    return {}

def _dict(value: object) -> dict[int, Any]:
    return value if isinstance(value, dict) else {}

def _text(value: object) -> str:
    return str(value or "").strip()

def _when_id(dyn_id: str) -> datetime | None:
    if not dyn_id.isdigit():
        return None
    high = int(dyn_id) >> 32
    return datetime.fromtimestamp(high + _DYN_EPOCH, timezone.utc) if high > 0 else None

def _read_grpc(
    status: int, headers: Mapping[str, str], body: bytes, *, url: str = DYN_SPACE_URL
) -> bytes:
    where = redact_fetch_url(url)
    if status >= 400:
        raise FetchError(
            f"HTTP {status} from {where}", status=status,
            body=body[:500].decode("utf-8", "replace"),
        )
    grpc_status = _header(headers, "grpc-status")
    if grpc_status and grpc_status != "0":
        raise FetchError(
            f"Bilibili app feed failed ({grpc_status}) from {where}",
            body=_header(headers, "grpc-message"),
        )
    return body

def _header(headers: Mapping[str, Any], name: str) -> str:
    value = headers.get(name, headers.get(name.lower()))
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()

async def _app_session() -> Any:
    from curl_cffi.requests import AsyncSession

    global _session, _session_loop
    loop = asyncio.get_running_loop()
    if _session_loop is not loop:
        await close_app_http()
        _session_loop = loop
    if _session is None or _closed(_session):
        _session = AsyncSession(default_headers=False, timeout=20, trust_env=False)
    return _session

async def _grpc_post(body: bytes, headers: Mapping[str, str]) -> bytes:
    from curl_cffi.requests.exceptions import RequestException

    proxy = current_proxy()
    session = await _app_session()
    last: BaseException | None = None
    for url in (DYN_SPACE_URL, _FAILOVER_URL):
        sent = dict(headers) if url == DYN_SPACE_URL else _failover_headers(headers)
        try:
            response = await session.post(
                url, headers=sent, content=body, http_version="v2",
                **({"proxy": proxy} if proxy else {}),
            )
        except RequestException as exc:
            last = exc
            if url != DYN_SPACE_URL:
                break
            log.warning("Bilibili app feed could not reach grpc.biliapi.net; trying app.bilibili.com")
            continue
        return _read_grpc(response.status_code, response.headers, bytes(response.content), url=url)
    raise FetchError(f"Could not fetch {redact_fetch_url(DYN_SPACE_URL)}") from last
