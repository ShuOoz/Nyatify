"""Ask the model whether to keep a post and what the card should say."""

from __future__ import annotations

import hashlib
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ai import AI, AIError
from fetch import FetchedItem, format_fetched_item
from news.cards import _json_object, match_recap_notes, parse_title_summary
from news.follows import FEATURE_GAME, FEATURE_TICKET
from news.prompt import FILTER_PROMPT, MATCH_WRITE_PROMPT, WRITE_PROMPT
from news.source.match.common import is_match_item
from news.source.post.weibo.ticket import TicketDraft, parse_ticket_draft
from news.source.registry import for_model, uses_ai

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
