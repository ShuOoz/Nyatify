"""Bilibili web feeds: the login recent page and the visitor list.

The poll chooses one of these before it asks. The login page stays on the
first proxy. The visitor list stays on the account's proxy and leaves the
login off. One ordinary mint failure is tried again. An HTTP 412 or a
refusal is not. A row longer than a title is a card. A shorter row is only
its id.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from fetch import FetchError, FetchedItem, redact_fetch_url
from news.source.post.common import (
    bili_blocked, classify_feed_fault, parse_cookie_header, pixel, proxy_label,
)

from . import browser
from .app import _when_id
from .browser import LOGIN_COOKIES, dm_params, fetch_wbi_key, sign_wbi, space_xhr_headers
from .dynamics import (
    CHARGE, LIST, QUIET, abs_url, charge_text, drop_labels, first_image, forward_stamp,
    is_paragraph, join_text, live_rcmd, post, reserve_notice, room_url, same_caption,
    with_live,
)

BILI_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
BILI_SPACE_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
BILI_OPUS_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/feed/space"
BILI_CARD_URL = "https://api.bilibili.com/x/web-interface/card"
SPACE_WEB_LOCATION = "333.1387"
_SPACE_PAGE = "https://space.bilibili.com/{uid}/dynamic"
_FEATURES = (
    "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,forwardListHidden,"
    "decorationCard,commentsNewVersion,onlyfansAssetsV2,ugcDelete,onlyfansQaCard"
)
_SPI_TRIES = 2
_MINT_PAUSE = 15.0
_LIST_SIZE = 10
_Rows = tuple[str | None, list[FetchedItem]]

log = logging.getLogger(__name__)


def items_from_space(payload: Mapping[str, Any], *, account: str) -> _Rows:
    name = None
    items: list[FetchedItem] = []
    for row in _feed_items(payload) or []:
        if not isinstance(row, dict):
            continue
        if name is None:
            name = _text(_author(row).get("name")) or None
        item = dynamic_item(row, account=account, source_name=name)
        if item is not None:
            items.append(item)
    return name, items


def dynamic_item(
    row: Mapping[str, Any], *, account: str, source_name: str | None = None
) -> FetchedItem | None:
    item_id = _text(row.get("id_str"))
    if not item_id:
        return None
    dynamic = _as_dict(_as_dict(row.get("modules")).get("module_dynamic"))
    title, text, image, width, height = _major(dynamic)
    desc = _caption(dynamic.get("desc"))
    kind = str(row.get("type") or "")
    major = _as_dict(dynamic.get("major"))
    opus = _as_dict(major.get("opus"))
    if opus and kind != "DYNAMIC_TYPE_FORWARD":
        labels = (*_sticker_labels(dynamic.get("desc")), *_sticker_labels(opus.get("summary")))
        text = same_caption(text, desc, labels)
        desc = same_caption(desc, text, labels)
    if kind == "DYNAMIC_TYPE_FORWARD":
        text = _forward_text(desc, row.get("orig"))
        if image is None:
            _, _, image, width, height = _major(_orig_dynamic(row.get("orig")))
    elif desc:
        text = join_text(desc, text)
    author = _author(row)
    charge = _charged(row, author)
    if charge:
        hint = _text(_as_dict(major.get("blocked")).get("hint_message"))
        text = charge_text(title, text, hint)
    if kind.startswith("DYNAMIC_TYPE_LIVE"):
        live_title, live_url, live_image = _live(dynamic)
        title, text, image = title or live_title, with_live(text, live_url), image or live_image
    posted = _when(author.get("pub_ts"))
    reserve, watch = _reserve(dynamic, posted)
    return post(
        item_id=item_id, account=account, title=title, text=text,
        name=_text(author.get("name")) or source_name or "",
        image=image, width=width, height=height, charge=charge,
        occurred_at=posted, author_icon=abs_url(author.get("face")),
        reserve=reserve, watch=watch,
    )


def opus_item(
    row: Mapping[str, Any], *, account: str, source_name: str | None = None,
    author_icon: str | None = None,
) -> FetchedItem | None:
    item_id = _text(row.get("opus_id"))
    if not item_id.isdigit():
        return None
    cover = _as_dict(row.get("cover"))
    # Same snowflake as a dynamic id, so a livestream stamp can see this day.
    return post(
        item_id=item_id, account=account, title="",
        text=_text(row.get("content")), name=source_name or "",
        image=abs_url(cover.get("url")),
        width=pixel(cover.get("width")), height=pixel(cover.get("height")),
        author_icon=author_icon, occurred_at=_when_id(item_id),
    )


async def fetch_login(
    account: str, *, cookie: str = "", call: Any | None = None, seen: Collection[str] = ()
) -> _Rows:
    """The recent page on the first proxy. A blank page is an empty list."""
    do = call or browser.bili_json
    headers = space_xhr_headers(_SPACE_PAGE.format(uid=account))
    found = await _login_fixed(
        account, cookie=cookie, do=do, seen=seen, headers=headers, live=call is None
    )
    return found if found is not None else (None, [])


async def fetch_visitor(
    account: str, *, cookie: str = "", call: Any | None = None, seen: Collection[str] = ()
) -> _Rows:
    """The text-and-image list on the account's proxy. The login stays off."""
    del cookie
    do = call or browser.bili_json
    headers = space_xhr_headers(_SPACE_PAGE.format(uid=account))
    live = call is None
    cookies = {"buvid3": await _spi_buvid3(do, live=live), "opus-goback": "1"}
    key = await fetch_wbi_key(call=do, cookies=cookies, headers=headers)
    found = await _opus_list(account, cookies=cookies, key=key, do=do, seen=seen)
    return found if found is not None else (None, [])


async def _opus_list(
    account: str, *, cookies: Mapping[str, str], key: str, do: Any, seen: Collection[str]
) -> _Rows | None:
    referer = _SPACE_PAGE.format(uid=account)
    payload = await _json(
        do, BILI_OPUS_URL, cookies=cookies, label="opus list", strict=True,
        params=_query(sign_wbi(_opus_query(account), key)),
        headers=space_xhr_headers(referer),
    )
    raw = _as_dict(payload.get("data") if payload else None).get("items")
    rows = [row for row in raw if isinstance(row, dict)][:_LIST_SIZE] if isinstance(raw, list) else []
    if not rows:
        log.warning("Bilibili opus list is blank for %s", account)
        return None
    name, face = _opus_author(rows)
    del seen
    items: list[FetchedItem] = []
    for row in rows:
        item = opus_item(row, account=account, source_name=name, author_icon=face)
        if item is None:
            continue
        tag = LIST if is_paragraph(str(row.get("content") or "")) else QUIET
        items.append(replace(item, tags=(*item.tags, tag)))
    cards = [item for item in items if QUIET not in item.tags]
    if cards and (name is None or face is None):
        card_name, card_face = await _space_card(account, cookies=cookies, do=do, referer=referer)
        name, face = name or card_name, face or card_face
        items = [
            _with_author(item, name, face) if QUIET not in item.tags else item for item in items
        ]
    log.info("Bilibili visitor list for %s read %s rows, sent %s", account, len(rows), len(cards))
    return name, items


async def _login_fixed(
    account: str, *, cookie: str, do: Any, seen: Collection[str],
    headers: Mapping[str, str], live: bool,
) -> _Rows | None:
    from . import round as bili_round
    from .app import bind_proxy, proxy_key, reset_proxy

    proxy = bili_round.backup_proxy()
    if bili_round.backup_resting(proxy):
        reason = "is resting"
    elif bili_round.exit_failed(proxy):
        reason = "failed the exit check"
    else:
        reason = None
    if reason is not None:
        log.info("Bilibili login space skipped for %s: %s %s", account, proxy_label(proxy), reason)
        return None
    home = proxy_key()
    token = bind_proxy(proxy)
    try:
        guest = {"buvid3": await _spi_buvid3(do, live=live), "opus-goback": "1"}
        return await _login_space(
            account, cookie=cookie, guest=guest, do=do, seen=seen, headers=headers, proxy=proxy
        )
    except FetchError as exc:
        if _cookie_fault(exc):
            raise
        if not bili_blocked(exc):
            log.warning("Bilibili login buvid failed for %s: %s", account, exc)
            return None
        _block_proxy(account, proxy, space=False)
        if home != proxy:
            return None
        raise
    finally:
        reset_proxy(token)


def _block_proxy(account: str, proxy: str, *, space: bool) -> None:
    from . import round as bili_round

    bili_round.rest_backup(proxy)
    if not space and bili_round.cap.sits_out(proxy):
        return
    bili_round.cap.rest(proxy)
    log.warning(
        "Bilibili login%s for %s was blocked on %s",
        " space" if space else "", account, proxy_label(proxy),
    )


async def _login_space(
    account: str, *, cookie: str, guest: Mapping[str, str], do: Any,
    seen: Collection[str], headers: Mapping[str, str], proxy: str,
) -> _Rows | None:
    failed: FetchError | None = None
    try:
        payload = await _space_feed(
            account, cookies=_login_cookies(cookie, guest), do=do, headers=headers
        )
    except FetchError as exc:
        failed, payload = exc, None
    if isinstance(payload, dict) and payload.get("code") not in (0, None):
        failed = _space_error(payload)
    if failed is not None:
        if _cookie_fault(failed):
            raise failed
        if bili_blocked(failed):
            _block_proxy(account, proxy, space=True)
            raise failed
        log.warning("Bilibili login space failed for %s: %s", account, failed)
        return None
    if not isinstance(payload, dict):
        log.warning("Bilibili login space for %s returned no page", account)
        return None
    if not _feed_items(payload):
        log.warning("Bilibili login space is blank for %s", account)
        return None
    del seen
    name, items = items_from_space(payload, account=account)
    log.info("Bilibili login space for %s read %s rows", account, len(items))
    return name, items


async def _space_feed(
    account: str, *, cookies: Mapping[str, str], do: Any,
    headers: Mapping[str, str],
) -> Any:
    key = await fetch_wbi_key(call=do, cookies=cookies, headers=headers)
    signed = sign_wbi(dm_params(_space_query(account)), key)
    return await do(
        "GET", BILI_SPACE_URL, cookies=dict(cookies), params=_query(signed), headers=dict(headers)
    )


def _space_error(payload: object) -> FetchError:
    code = payload.get("code") if isinstance(payload, dict) else None
    return FetchError(
        f"Bilibili dynamics failed ({code}) from {redact_fetch_url(BILI_SPACE_URL)}",
        body=str(payload)[:1000],
    )


async def _space_card(
    account: str, *, cookies: Mapping[str, str], do: Any, referer: str
) -> tuple[str | None, str | None]:
    payload = await _json(
        do, BILI_CARD_URL, cookies=cookies, params={"mid": account}, strict=False,
        headers=space_xhr_headers(referer), label=f"space card for {account}",
    )
    card = _as_dict(_as_dict(payload).get("data")).get("card") if payload else None
    if not isinstance(card, dict):
        return None, None
    return _text(card.get("name")) or None, abs_url(card.get("face"))


async def _json(
    do: Any, url: str, *, cookies: Mapping[str, str], params: Mapping[str, Any],
    headers: Mapping[str, str], label: str, strict: bool,
) -> dict | None:
    try:
        payload = await do(
            "GET", url, cookies=dict(cookies), params=_query(params), headers=dict(headers)
        )
    except FetchError as exc:
        if strict and classify_feed_fault("bilibili", exc) is not None:
            raise
        log.warning("Bilibili %s failed: %s", label, exc)
        return None
    if isinstance(payload, dict) and payload.get("code") in (0, None):
        return payload
    code = payload.get("code") if isinstance(payload, dict) else None
    failed = FetchError(f"Bilibili {label} failed ({code})", body=str(payload)[:1000])
    if strict and classify_feed_fault("bilibili", failed) is not None:
        raise failed
    log.warning("Bilibili %s returned code %s", label, code)
    return None


async def _spi_buvid3(do: Any, *, live: bool) -> str:
    """One ordinary failure is tried again. A block or a refusal is not."""
    last = FetchError("Could not mint a Bilibili buvid3")
    for attempt in range(_SPI_TRIES):
        try:
            spi = await do("GET", BILI_SPI_URL)
        except FetchError as exc:
            if classify_feed_fault("bilibili", exc) is not None:
                raise
            last = exc
        else:
            buvid3 = ""
            if isinstance(spi, dict) and spi.get("code") == 0:
                buvid3 = _text(_as_dict(spi.get("data")).get("b_3"))
            if buvid3:
                return buvid3
            refused = FetchError("Could not mint a Bilibili buvid3", body=str(spi)[:1000])
            if classify_feed_fault("bilibili", refused) is not None:
                code = spi.get("code") if isinstance(spi, dict) else None
                log.warning("Bilibili buvid3 mint was refused (%s)", code)
                raise FetchError("Could not mint a Bilibili buvid3")
            last = refused
        if attempt + 1 >= _SPI_TRIES:
            raise last
        log.warning("Bilibili buvid3 mint failed; trying again: %s", last)
        await browser.close_bili_connections()
        if live and _MINT_PAUSE > 0:
            await asyncio.sleep(_MINT_PAUSE)


def _login_cookies(cookie: str, guest: Mapping[str, str]) -> dict[str, str]:
    pairs = parse_cookie_header(cookie)
    cookies = dict(guest)
    cookies.update({
        name: value for name in LOGIN_COOKIES if (value := pairs.get(name, "").strip())
    })
    return cookies


def _cookie_fault(exc: BaseException) -> bool:
    fault = classify_feed_fault("bilibili", exc)
    return fault is not None and fault.cookie


def _with_author(item: FetchedItem, name: str | None, face: str | None) -> FetchedItem:
    if item.source_name and item.author_icon:
        return item
    return replace(item, source_name=item.source_name or name, author_icon=item.author_icon or face)


def _feed_items(payload: Mapping[str, Any]) -> list[Any] | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    rows = data.get("items") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else None


def _opus_author(rows: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    name = face = None
    for row in rows:
        author = row.get("author")
        if not isinstance(author, dict):
            continue
        name = name or _text(author.get("name")) or None
        face = face or abs_url(author.get("face"))
        if name and face:
            break
    return name, face


def _space_query(account: str) -> dict[str, str]:
    return {
        "host_mid": account,
        "offset": "",
        "features": _FEATURES,
        "timezone_offset": "-480",
        "web_location": SPACE_WEB_LOCATION,
        "x-bili-device-req-json": '{"platform":"web","device":"pc"}',
        "x-bili-web-req-json": '{"spm_id":"333.1387"}',
    }


def _opus_query(account: str) -> dict[str, str]:
    return {
        "host_mid": account,
        "page": "1",
        "offset": "",
        "type": "all",
        "web_location": SPACE_WEB_LOCATION,
    }


def _query(params: Mapping[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in params.items()}


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _caption(block: object) -> str:
    """Words in a caption. A sticker piece is left out. A line with none stays."""
    data = _as_dict(block)
    flat = _text(data.get("text"))
    words: list[str] = []
    labels: list[str] = []
    for node in _nodes(data.get("rich_text_nodes")):
        text = _node_text(node)
        (labels if node.get("type") == "RICH_TEXT_NODE_TYPE_EMOJI" else words).append(text)
    if not labels:
        return flat
    return _text("".join(words)) or drop_labels(flat, labels)


def _sticker_labels(block: object) -> tuple[str, ...]:
    nodes = _nodes(_as_dict(block).get("rich_text_nodes"))
    return tuple(
        label for node in nodes
        if node.get("type") == "RICH_TEXT_NODE_TYPE_EMOJI" and (label := _node_text(node))
    )


def _nodes(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [node for node in raw if isinstance(node, dict)]


def _node_text(node: Mapping[str, Any]) -> str:
    return str(node.get("text") or node.get("orig_text") or "")


def _text(value: object) -> str:
    return str(value or "").strip()


def _author(row: Mapping[str, Any]) -> dict[str, Any]:
    return _as_dict(_as_dict(row.get("modules")).get("module_author"))


def _charged(row: Mapping[str, Any], author: Mapping[str, Any]) -> bool:
    if _as_dict(row.get("basic")).get("is_only_fans") is True:
        return True
    badge = author.get("icon_badge")
    return CHARGE in str(badge.get("text") if isinstance(badge, dict) else "")


def _reserve(dynamic: Mapping[str, Any], posted: datetime | None) -> tuple[str, str]:
    reserve = _as_dict(_as_dict(dynamic.get("additional")).get("reserve"))
    if not reserve:
        return "", ""
    desc = reserve.get("desc1")
    when = desc.get("text") if isinstance(desc, dict) else desc
    return reserve_notice(_text(reserve.get("title")), str(when or ""), posted)


def _orig_dynamic(orig: object) -> dict[str, Any]:
    return _as_dict(_as_dict(_as_dict(orig).get("modules")).get("module_dynamic"))


def _forward_text(comment: str, orig: object) -> str:
    name = _text(_author(orig).get("name")) if isinstance(orig, dict) else ""
    title, text, _, _, _ = _major(_orig_dynamic(orig))
    return "\n".join(part for part in (comment, forward_stamp(name), title, text) if part)


def _major(dynamic: Mapping[str, Any]) -> tuple[str, str, str | None, int | None, int | None]:
    major = _as_dict(dynamic.get("major"))
    opus, archive = major.get("opus"), major.get("archive")
    draw, article = major.get("draw"), major.get("article")
    if isinstance(opus, dict):
        image, width, height = first_image(opus.get("pics"), "url", "width", "height")
        summary = _caption(opus.get("summary"))
        return _text(opus.get("title")), summary, image, width, height
    if isinstance(archive, dict):
        title, desc = _text(archive.get("title")), _text(archive.get("desc"))
        return title, desc, abs_url(archive.get("cover")), None, None
    if isinstance(draw, dict):
        return "", "", *first_image(draw.get("items"), "src", "width", "height")
    if not isinstance(article, dict):
        return "", "", None, None, None
    covers = article.get("covers")
    image = abs_url(covers[0]) if isinstance(covers, list) and covers else None
    return _text(article.get("title")), _text(article.get("desc")), image, None, None


def _live(dynamic: Mapping[str, Any]) -> tuple[str, str, str | None]:
    live = _as_dict(_as_dict(dynamic.get("major")).get("live"))
    if not live:
        title, cover, jump = live_rcmd(_as_dict(dynamic.get("major")).get("live_rcmd"))
        return title, jump, cover
    room = live.get("id") or live.get("roomid")
    jump = abs_url(live.get("jump_url")) or room_url(room) or None
    return _text(live.get("title")), jump or "", abs_url(live.get("cover"))


def _when(stamp: object) -> datetime | None:
    try:
        value = int(stamp)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 10_000_000_000:
        value //= 1000
    return datetime.fromtimestamp(value, timezone.utc)
