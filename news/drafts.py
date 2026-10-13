"""Ask the model whether to keep a post and what the card should say."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from ai import AI, AIError
from fetch import FetchedItem, excerpt, format_fetched_item
from news.cards import TITLE_CHARS, _fold_summary, _game_card_body, match_recap_notes
from news.follows import FEATURE_GAME, FEATURE_TICKET
from news.prompt import FILTER_PROMPT, MATCH_WRITE_PROMPT, WRITE_PROMPT
from news.source.match.common import is_match_item
from news.source.post.weibo.ticket import TicketDraft, parse_ticket_draft
from news.source.registry import for_model, uses_ai



def _json_object(text: str) -> dict[str, Any] | None:
    blob = text.strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:json)?\s*", "", blob, count=1)
        blob = re.sub(r"\s*```$", "", blob).strip()
    start, end = blob.find("{"), blob.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(blob[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None

def parse_title_summary(text: str) -> tuple[str, str] | None:
    data = _json_object(text)
    if data is None or not isinstance(data.get("title"), str):
        return None
    title = excerpt(" ".join(data["title"].split()), TITLE_CHARS)
    if isinstance(data.get("about"), str):
        summary = _game_card_body(data)
    else:
        raw = data.get("summary")
        if not isinstance(raw, str):
            return None
        summary = _fold_summary(raw)
    return (title, summary) if summary else None

STREAM_FP_PREFIX = "stream:"
_CN = timezone(timedelta(hours=8))
_CLOCK = re.compile(
    r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"[^\d]{0,8}(\d{1,2})\s*[:：]\s*(\d{2})"
)


def stream_when(quote: str, posted: datetime | None = None) -> datetime | None:
    """Watch clock in China time, copied from a fact quote."""
    match = _CLOCK.search(quote)
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


def stream_fingerprint(
    quotes: dict[str, str],
    occurred_at: datetime | None,
    *,
    keep: bool,
    kind: str,
    watch: str = "",
) -> str | None:
    """Same watch clock, posted before the show day. A reservation clock wins."""
    if not keep or kind != "stream":
        return None
    quoted = next((text for text in quotes.values() if stream_when(text, occurred_at)), "")
    quote = watch if watch and stream_when(watch, occurred_at) else quoted
    clock = stream_when(quote, occurred_at) if quote else None
    if clock is None or occurred_at is None:
        return None
    if occurred_at.astimezone(_CN).date() >= clock.date():
        return None
    return (
        f"{STREAM_FP_PREFIX}{clock.year}-{clock.month}-{clock.day}-"
        f"{clock.hour:02d}:{clock.minute:02d}"
    )

log = logging.getLogger(__name__)


def _for_model(item: FetchedItem) -> FetchedItem:
    """The post the model should read. Feed markers stay off it."""
    return for_model(item)


FILTER_PEEK_CHARS = 800
FILTER_MAX_TOKENS = 1024
WRITE_MAX_TOKENS = 1024
FILTER_THINKING = "low"
WRITE_THINKING = "low"
FILTER_CACHE_CAP = 256

FILTER_FACTS = frozenset({"until", "down", "how", "grant", "start"})
FILTER_TYPES = frozenset({"ops", "stream", "collab_grant", "notice", "release", "show", "drop"})
_QUOTE_MIN = 2
_GAME_KEEP = frozenset({"ops", "stream", "collab_grant"})
_TICKET_KEEP = frozenset({"show"})
_FOOTER_RE = re.compile(
    r"(?:——\s*欢迎来到|bilibili安卓端下载|bilibiliPC端下载|关注\s*并转发(?:本条|此动态))", re.I
)

class AiCache(OrderedDict):
    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > FILTER_CACHE_CAP:
            self.popitem(last=False)

    def drop_sources(self, keep: set[tuple[str, str]]) -> None:
        """Drop entries whose platform and account are outside `keep`."""
        for key in [item for item in self if (item[0], item[1]) not in keep]:
            del self[key]


@dataclass(frozen=True)
class FilterJob:
    keep: bool
    kind: str = ""
    quotes: dict[str, str] = field(default_factory=dict)


def filter_kept(value: Any) -> bool | None:
    if isinstance(value, FilterJob):
        return value.keep
    if value is True or value is False or value is None:
        return value
    return None


def parse_filter_job(text: str, caption: str = "", topic: str = "") -> FilterJob | None:
    data = _json_object(text)
    if data is None:
        return None
    quotes = _filter_quotes(data.get("facts", {}), caption)
    if quotes is None:
        return None
    kind = data.get("type")
    if not isinstance(kind, str) or not kind.strip():
        return FilterJob(False)
    kind = kind.strip()
    if kind not in FILTER_TYPES:
        return FilterJob(False, kind)
    if topic == FEATURE_GAME and kind not in _GAME_KEEP:
        return FilterJob(False, kind, quotes)
    if topic == FEATURE_TICKET and kind not in _TICKET_KEEP:
        return FilterJob(False, kind, quotes)
    return FilterJob(_filter_allow(kind, quotes, caption), kind, quotes)


def _filter_quotes(value: Any, caption: str) -> dict[str, str] | None:
    """Keep a fact only when its quote is copied from the unique body."""
    if isinstance(value, list):
        return {}
    if not isinstance(value, dict):
        return None
    haystack = " ".join(_filter_body(caption).split()).casefold()
    return {
        key: quote.strip() for key, quote in value.items()
        if key in FILTER_FACTS and isinstance(quote, str) and _quoted_fact(quote, haystack)
    }


def _filter_body(caption: str) -> str:
    match = _FOOTER_RE.search(caption)
    return caption[: match.start()] if match else caption


def _quoted_fact(quote: str, haystack: str) -> bool:
    """A fact counts when it is copied from the post, whichever key holds it."""
    needle = " ".join(quote.split()).casefold()
    return len(needle) >= _QUOTE_MIN and needle in haystack


def _filter_allow(kind: str, quotes: dict[str, str], _caption: str = "") -> bool:
    """The model picked the kind. A copied quote is the proof, except a show."""
    return kind in {"release", "show"} or (bool(quotes) and kind in _GAME_KEEP | {"notice"})


async def keep_post(
    ai: AI, cache: OrderedDict, lock: Any, item: FetchedItem, topic: str, *,
    remember: bool = True,
) -> FilterJob | bool | None:
    prompt = FILTER_PROMPT.get(topic)
    if prompt is None or is_match_item(item):
        return True
    user = format_fetched_item(_for_model(item), peek=FILTER_PEEK_CHARS)
    return await _complete(
        ai, cache, lock, item, topic, prompt, user, FILTER_MAX_TOKENS,
        lambda reply: parse_filter_job(reply, user, topic), remember=remember, what="filter",
    )


async def card_fields(
    ai: AI, filter_cache: OrderedDict, write_cache: OrderedDict, lock: Any,
    item: FetchedItem, topic: str, *, remember: bool = True, use_ai: bool | None = None,
    filter: bool = True, ai_on_poll: bool = False,
) -> tuple[str | None, str | None] | TicketDraft | Literal[False] | None:
    if use_ai is None:
        use_ai = ai_on_poll and ai.available()
    if is_match_item(item):
        recap = None
        notes = match_recap_notes(item)
        if use_ai and notes:
            written = await _write_post(
                ai, write_cache, lock, item, topic,
                remember=remember, prompt=MATCH_WRITE_PROMPT, user=notes,
            )
            if isinstance(written, tuple):
                recap = written[1] or None
        return None, recap
    if use_ai and uses_ai(item):
        if filter:
            keep = filter_kept(
                await keep_post(ai, filter_cache, lock, item, topic, remember=remember)
            )
            if keep is False:
                return False
            if keep is not True:
                return None
        return await _write_post(ai, write_cache, lock, item, topic, remember=remember)
    return None, None


async def _write_post(
    ai: AI, cache: OrderedDict, lock: Any, item: FetchedItem, topic: str, *,
    remember: bool = True, prompt: str | None = None, user: str | None = None,
) -> tuple[str | None, str | None] | TicketDraft | None:
    used = WRITE_PROMPT.get(topic) if prompt is None else prompt
    if used is None:
        return None, None
    post = format_fetched_item(_for_model(item)) if user is None else user
    if topic == FEATURE_TICKET and prompt is None:
        parse = lambda reply: parse_ticket_draft(reply, post)
    else:
        parse = parse_title_summary
    return await _complete(
        ai, cache, lock, item, topic, used, post, WRITE_MAX_TOKENS, parse,
        remember=remember, what="write",
    )


def word_fingerprint(item: FetchedItem) -> str:
    """The title and text, so an edit is not the same post."""
    words = " ".join(f"{item.title}\n{item.text}".split())
    return hashlib.sha256(words.encode()).hexdigest()[:16]


def filter_cache_key(item: FetchedItem, topic: str) -> tuple[str, str, str, str, str]:
    """One decision per post text. An edit is a new key."""
    return (item.platform, item.account, item.item_id, topic, word_fingerprint(item))


async def _complete(
    ai: AI, cache: OrderedDict, lock: Any, item: FetchedItem, topic: str, prompt: str,
    user: str, max_tokens: int, parse: Callable[[str], Any], *, remember: bool, what: str,
):
    key = filter_cache_key(item, topic)
    if what == "write":
        key = (item.platform, item.account, item.item_id, topic, user)
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    if not ai.available():
        return None
    try:
        async with lock:
            reply = await ai.complete(
                [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
                max_tokens=max_tokens, temperature=0,
                reasoning_effort=FILTER_THINKING if what == "filter" else WRITE_THINKING,
            )
    except AIError:
        log.exception("AI %s failed for %s", what, item.item_id)
        return None
    parsed = parse(reply)
    if parsed is None:
        log.warning("AI %s reply was unusable: %r", what, reply[:80])
        return None
    if remember:
        cache[key] = parsed
    return parsed
