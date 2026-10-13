"""The Bilibili post, and one account's read of it.

The poll picks the source before it asks: the app phone, the login page, or
the visitor list. A blank ends that account. HTTP 412 sits the proxy out of
the next poll.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from fetch import FetchError, FetchedItem, http_url
from news.source.post.common import pixel

log = logging.getLogger(__name__)

CHARGE = "充电专属"
WATCH = "watch:"
LIST = "list"
# A list row shorter than a card. Its id is history, not a post.
QUIET = "quiet"
PLATFORM = "bilibili"
# A title fits on one card line: 24 Chinese characters, or 48 letters.
_TITLE_SPAN = 48
_CN = timezone(timedelta(hours=8))
_CLOCK_TAIL = r"[^\d]{0,8}(\d{1,2})\s*[:：]\s*(\d{2})"
_CN_CLOCK = re.compile(rf"(?:(\d{{4}})\s*年\s*)?(\d{{1,2}})\s*月\s*(\d{{1,2}})\s*日{_CLOCK_TAIL}")
_DASH_CLOCK = re.compile(rf"(?:(\d{{4}})\s*年\s*)?(\d{{1,2}})\s*[-/]\s*(\d{{1,2}}){_CLOCK_TAIL}")
OPUS_URL = "https://www.bilibili.com/opus/{id}"
LIVE_URL = "https://live.bilibili.com/{room}"
_NAMES = frozenset({"bilibili", "bili"})
_LETTER = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")
_REPOST_RE = re.compile(r"转发自|转发了.{0,40}的动态|\bRT\s+@|^reposted?\b", re.I | re.M)
_LOTTERY_RE = re.compile(r"互动抽奖|官方抽奖|/h5/lottery|\bLottery\b", re.I)
_RAFFLE_STAMP_RE = re.compile(
    r"关注\s*并转发(?:本条|此动态).{0,160}|关注\s*并转发|关注即可|转发即可", re.I,
)
_WATCH_RE = re.compile(rf"{WATCH}(\d{{4}})-(\d{{1,2}})-(\d{{1,2}})-(\d{{2}}):(\d{{2}})")
_LIVE_START_RE = re.compile(r"开播|正在直播|直播开始|started\s+(?:a\s+)?live", re.I)
_LIVE_ROOM_RE = re.compile(r"https?://(?:www\.)?live\.bilibili\.com/\S*|直播间地址", re.I)
# The opus list has no only-fans flag. Its whole body is this lock line.
_CHARGE_MARK_RE = re.compile(r"充电专属动态|解锁观看")
_CHARGE_STAMP_RE = re.compile(
    r"为\s*TA\s*充电专属动态|充电专属动态|加入当前\s*UP主的|"
    r"加入即可解锁观看|即可解锁观看|包月充电|\d+\s*元档",
)
_RESCUES = ("repull",)
# "app" is the phone. "login" is the recent page. "visitor" is the list.
_mode: ContextVar[str] = ContextVar("bili_read_mode", default="app")
_slot: ContextVar[int] = ContextVar("bili_slot", default=0)

def abs_url(url: object) -> str | None:
    text = str(url or "").strip()
    return http_url("https:" + text if text.startswith("//") else text)

def room_url(room: object) -> str:
    return LIVE_URL.format(room=room) if room else ""

def join_text(desc: str, body: str) -> str:
    """Caption plus body. One copy when the body repeats the caption."""
    left, right = desc.strip(), body.strip()
    if not left or not right:
        return left or right
    flat_left, flat_right = " ".join(left.split()), " ".join(right.split())
    same = (
        flat_left == flat_right
        or _is_copy(flat_left, flat_right)
        or _is_copy(flat_right, flat_left)
    )
    if same:
        return left if len(flat_left) >= len(flat_right) else right
    return f"{left}\n{right}"

def drop_labels(text: str, labels: Collection[str]) -> str:
    """`text` with those sticker labels taken out."""
    cleaned = text.strip()
    for label in sorted((label for label in labels if label), key=len, reverse=True):
        cleaned = cleaned.replace(label, "")
    return cleaned.strip()

def same_caption(text: str, other: str, labels: Collection[str]) -> str:
    """`other` when `text` is that line with sticker labels added. `text` stays otherwise."""
    if not labels:
        return text
    cleaned = drop_labels(text, labels)
    return cleaned if cleaned == other.strip() else text

def _is_copy(longer: str, shorter: str) -> bool:
    if not shorter or longer.startswith(shorter):
        return bool(shorter)
    if not shorter.endswith(("...", "…")):
        return False
    head = shorter.rstrip(".…").strip()
    return bool(head) and longer.startswith(head)

def forward_stamp(name: str) -> str:
    return f"转发了{name}的动态" if name else "转发了"

def with_live(text: str, live: str) -> str:
    return f"{text}\n{live}".strip() if live and live not in text else text

def reserve_notice(title: str, when: str, posted: datetime | None) -> tuple[str, str]:
    title, when = " ".join(title.split()), " ".join(when.split())
    title_clock = _notice_clock(title, posted)
    clock = _notice_clock(when, posted) or title_clock
    if clock is None:
        return title, ""
    shown = f"{clock.month}月{clock.day}日 {clock.hour:02d}:{clock.minute:02d}"
    if not (title_clock == clock and _CN_CLOCK.search(title)):
        title = " ".join(part for part in (title, shown) if part)
    tag = f"{WATCH}{clock.year}-{clock.month}-{clock.day}-{clock.hour:02d}:{clock.minute:02d}"
    return title, tag

def watch_quote(tags: tuple[str, ...] | list[str]) -> str:
    """Clock sentence for a watch tag. Empty when the post has none."""
    for tag in tags:
        match = _WATCH_RE.fullmatch(tag)
        if match is not None:
            year, month, day, hour, minute = (int(part) for part in match.groups())
            return f"{year}年{month}月{day}日 {hour:02d}:{minute:02d}"
    return ""

def _notice_clock(text: str, posted: datetime | None) -> datetime | None:
    match = _CN_CLOCK.search(text) or _DASH_CLOCK.search(text)
    if match is None:
        return None
    year_s, month, day, hour, minute = match.groups()
    posted_cn = (posted or datetime.now(timezone.utc)).astimezone(_CN)
    year = _show_year(year_s, month, day, posted_cn)
    try:
        return datetime(year, int(month), int(day), int(hour), int(minute), tzinfo=_CN)
    except ValueError:
        return None

def _show_year(year_s: str | None, month: str, day: str, posted: datetime) -> int:
    """A month and day with no year is this year, or next year once that day has passed."""
    if year_s:
        return int(year_s)
    year = posted.year
    try:
        shown = datetime(year, int(month), int(day), tzinfo=posted.tzinfo).date()
    except ValueError:
        return year
    if shown < posted.date():
        return year + 1
    return year

def charge_text(title: str, text: str, hint: str) -> str:
    return (hint or CHARGE) if not title and not text else text

def is_paragraph(text: str) -> bool:
    """True when this list text is wider than one card line."""
    flat = " ".join(text.split())
    return sum(2 if "\u4e00" <= char <= "\u9fff" else 1 for char in flat) > _TITLE_SPAN

def is_quiet(item: FetchedItem) -> bool:
    return QUIET in item.tags


def catch_floor(items) -> str | None:
    """Highest numeric id on a first pull. That page is history, not a card."""
    numbers = [int(item.item_id) for item in items if item.item_id and str(item.item_id).isdigit()]
    return str(max(numbers)) if numbers else None


def for_model(item: FetchedItem) -> FetchedItem:
    """Drop list and watch markers. The model writes from the post."""
    tags = tuple(tag for tag in item.tags if tag != LIST and not tag.startswith(WATCH))
    return item if tags == item.tags else replace(item, tags=tags)


def note_list_post(
    item: FetchedItem, account: str, audience: Collection[tuple[int, str]], handled: Collection[str],
) -> bool:
    """A list post uses the same bookmark as any other post."""
    del item, account, audience, handled
    return False

def first_image(
    rows: object, url_key: object, width_key: object, height_key: object,
) -> tuple[str | None, int | None, int | None]:
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and (found := abs_url(row.get(url_key))):
                return found, pixel(row.get(width_key)), pixel(row.get(height_key))
    return None, None, None

def live_rcmd(raw: object) -> tuple[str, str | None, str]:
    content: object = raw.get("content", raw) if isinstance(raw, dict) else raw
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}
    if not isinstance(content, dict):
        return "", None, ""
    info = content.get("live_play_info")
    info = info if isinstance(info, dict) else content
    return (
        str(info.get("title") or "").strip(), abs_url(info.get("cover")),
        room_url(info.get("room_id") or info.get("roomid")),
    )

def post(
    *, item_id: str, account: str, title: str = "", text: str = "", name: str = "",
    image: str | None = None, width: int | None = None, height: int | None = None,
    charge: bool = False, occurred_at: datetime | None = None,
    author_icon: str | None = None, reserve: str = "", watch: str = "",
) -> FetchedItem | None:
    """One post from either feed. A 直播预约 card joins the text."""
    if reserve:
        text = join_text(text, reserve)
    heading = title or text.split("\n", 1)[0].strip() or name
    if not heading and not text:
        return None
    tags = tuple(tag for tag in (CHARGE if charge else "", watch) if tag)
    return FetchedItem(
        item_id=item_id, title=heading, text=text or heading,
        url=OPUS_URL.format(id=item_id), image_url=image, platform=PLATFORM,
        account=account, source_name=name or None, occurred_at=occurred_at,
        image_width=width, image_height=height, author_icon=author_icon, tags=tags,
    )

def should_skip(item: FetchedItem, *, now: datetime | None = None) -> bool | None:
    if item.platform not in _NAMES:
        return None
    if CHARGE in item.tags or _charge_lock(item.title, item.text):
        return True
    haystack = " ".join((item.title, item.text, *item.tags))
    if _REPOST_RE.search(haystack) or _lottery_only(haystack):
        return True
    return None

def is_live_room(item: FetchedItem) -> bool:
    if item.platform not in _NAMES:
        return False
    if _LIVE_START_RE.search(item.title or "") or _LIVE_START_RE.search(" ".join(item.tags)):
        return True
    body = item.text or ""
    return _LIVE_ROOM_RE.search(f"{body} {item.url or ''}") is not None and _wordless(body, _LIVE_ROOM_RE)

def _charge_lock(title: str, text: str) -> bool:
    flat = " ".join(f"{title}\n{text}".split())
    if "充电" not in flat or _CHARGE_MARK_RE.search(flat) is None:
        return False
    return _wordless(flat, _CHARGE_STAMP_RE)

def _lottery_only(haystack: str) -> bool:
    return _LOTTERY_RE.search(haystack) is not None and _wordless(
        haystack, _LOTTERY_RE, _RAFFLE_STAMP_RE, hashes=True
    )

def _wordless(text: str, *patterns: re.Pattern[str], hashes: bool = False) -> bool:
    for pattern in patterns:
        text = pattern.sub(" ", text)
    if hashes:
        text = re.sub(r"#\S+", " ", text)
    return _LETTER.search(text) is None

@dataclass
class _Fetch:
    slot: int = 0
    first: str = ""
    via: str = ""
    node: str = ""

def _node_now() -> str:
    from .app import current_node
    return current_node()

class SpaceStats:

    def __init__(self) -> None:
        self._fetches: dict[str, _Fetch] = {}
        self._repulls: dict[str, str] = {}

    def _fetch(self, account: str, slot: int | None = None) -> _Fetch:
        return self._fetches.setdefault(account, _Fetch() if slot is None else _Fetch(slot))

    def _stamp(self, account: str, slot: int | None = None) -> _Fetch:
        fetch = self._fetch(account, slot)
        fetch.node = fetch.node or _node_now()
        return fetch

    def begin(self, account: str, slot: int) -> None:
        self._stamp(account, slot)

    def first(self, account: str, outcome: str) -> None:
        fetch = self._stamp(account)
        fetch.first = fetch.first or outcome

    def done(self, account: str, via: str) -> None:
        fetch = self._fetch(account)
        fetch.first = fetch.first or "error"
        fetch.via = via if via == "failed" else self._repulls.get(account, via)

    def repull(self, account: str) -> None:
        self._repulls[account] = "repull"

    def flush(self) -> str | None:
        rows = list(self._fetches.items())
        self._fetches.clear()
        self._repulls.clear()
        if not rows:
            return None
        for account, fetch in rows:
            fetch.first = fetch.first or "error"
            log.info(
                "Bilibili per account: %s %s first=%s via=%s",
                account, fetch.node or "direct", fetch.first, fetch.via or "failed",
            )
        fetches = [fetch for _account, fetch in rows]
        got = Counter(fetch.slot for fetch in fetches if fetch.first == "posts")
        seen = Counter(fetch.slot for fetch in fetches)
        slots = ", ".join(f"slot {slot} {got[slot]}/{seen[slot]}" for slot in sorted(seen))
        vias = Counter(fetch.via or "failed" for fetch in fetches)
        misses = Counter(fetch.first for fetch in fetches if fetch.first != "posts")
        parts = [
            f"{sum(got.values())}/{len(fetches)} got posts ({slots})",
            "rescued by " + ", ".join(f"{name} {vias[name]}" for name in _RESCUES),
            f"failed {vias['failed']}",
        ]
        if misses:
            listed = ", ".join(f"{name} {count}" for name, count in misses.most_common())
            parts.append(f"first misses: {listed}")
        return "Bilibili first try: " + "; ".join(parts)

def _in_round(name: str, *args: object) -> None:
    stats = _round.get()
    if stats is not None:
        getattr(stats, name)(*args)

class _RoundRecorder:
    """Records into this task's round. Outside a round, the calls do nothing."""

    @contextmanager
    def round(self) -> Iterator[SpaceStats]:
        stats = SpaceStats()
        token = _round.set(stats)
        try:
            yield stats
        finally:
            _round.reset(token)

    def begin(self, account: str, slot: int) -> None:
        _in_round("begin", account, slot)

    def first(self, account: str, outcome: str) -> None:
        _in_round("first", account, outcome)

    def done(self, account: str, via: str) -> None:
        _in_round("done", account, via)

    def repull(self, account: str) -> None:
        _in_round("repull", account)

space_stats = _RoundRecorder()

@dataclass
class PollAsk:
    blank: bool = False
    posts: bool = False
    listed: bool = False

_ask = ContextVar("bili_poll_ask", default=None)
_round = ContextVar("bili_space_round", default=None)

def _flag(name: str) -> None:
    if (ask := _ask.get()) is not None:
        setattr(ask, name, True)

async def fetch_dynamics(
    account: str, *, cookie: str = "", call: Any | None = None,
    post: Any | None = None, buvid: str = "", seen: Collection[str] = (),
) -> tuple[str | None, list[FetchedItem]]:
    """One planned read. A blank list is an empty result."""
    from .app import saved_buvid

    mode = _mode.get()
    if mode in {"login", "visitor"}:
        return await _website(account, cookie=cookie, call=call, seen=seen, login=mode == "login")
    try:
        name, items = await _app_pages(
            account, buvid=(buvid or "").strip() or saved_buvid(), post=post, seen=seen
        )
    except FetchError as exc:
        space_stats.first(account, f"http {exc.status}" if exc.status else "error")
        raise
    return _finish(account, name, items, "first")


async def _app_pages(
    account: str, *, buvid: str, post: Any | None, seen: Collection[str],
) -> tuple[str | None, list[FetchedItem]]:
    """Newer pages until a bookmark, a short page, or a repeated page."""
    from .app import fetch_app_page

    seen_ids = {str(item) for item in seen if item}
    name = None
    got: list[FetchedItem] = []
    have: set[str] = set()
    full = 0
    for page in range(1, 9):
        page_name, items = await fetch_app_page(account, buvid=buvid, post=post, page=page)
        name = name or page_name
        fresh = [item for item in items if item.item_id not in have]
        if not fresh:
            break
        got.extend(fresh)
        have.update(item.item_id for item in fresh)
        overlapped = any(item.item_id in seen_ids for item in items)
        if not seen_ids or overlapped or (full and len(fresh) < full):
            break
        full = len(fresh)
    return name, got


async def _website(
    account: str, *, cookie: str, call: Any | None, seen: Collection[str], login: bool,
) -> tuple[str | None, list[FetchedItem]]:
    from .web import fetch_login, fetch_visitor

    read = fetch_login if login else fetch_visitor
    name, items = await read(account, cookie=cookie, call=call, seen=seen)
    return _finish(account, name, items, "login" if login else "visitor")

def _finish(
    account: str, name: str | None, items: list[FetchedItem], via: str,
) -> tuple[str | None, list[FetchedItem]]:
    space_stats.first(account, "posts" if items else "blank")
    _flag("posts" if items else "blank")
    space_stats.done(account, via)
    return name, items
