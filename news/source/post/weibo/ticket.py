"""Ticket sales read from Weibo. Not a follow platform.

One artist comes from a profile. Shows are the card. 大麦官博 is the backup
sale feed for artists a server already follows.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, replace
from typing import AbstractSet, Any

from ai import AI, AIError
from fetch import EXCERPT_CHARS, FetchedItem, excerpt
from news.cards import (
    TITLE_CHARS,
    _fit_lines,
    _json_object,
    _one_line,
    _prose,
    news_card,
)
from news.follows import FEATURE_TICKET, remember_ticket_artist

log = logging.getLogger(__name__)

ARTIST_PROMPT = (
    "JSON only: "
    '{"artist":""}. '
    "Read the display name, the certification, and the bio together. "
    "Copy one person named in those three, as written. "
    "A display name that is one person is that person, even when the bio does not repeat it. "
    "When the display name is one person followed by 工作室, copy that person. "
    "A studio brand, a company, or 官方微博 is not the person. "
    "When the bio names the person and the display name is a studio, copy the person from the bio. "
    "When several people are named, or no person is named, artist is empty. "
    "Do not translate. Do not simplify."
)
ARTIST_MAX_TOKENS = 64


@dataclass(frozen=True)
class ArtistName:
    written: str
    simplified: str


@dataclass(frozen=True)
class ArtistRead:
    """A model answer. `answered` is false when the profile or the model could not be read."""

    name: ArtistName | None = None
    answered: bool = False


def profile_brief(display_name: str, certification: str, bio: str) -> str:
    """User text for the one artist call."""
    rows = (("Display name", display_name), ("Certification", certification), ("Bio", bio))
    return "\n".join(
        f"{label}: {text}" for label, value in rows if (text := " ".join(value.split()))
    )


async def one_artist(ai: AI, display_name: str, certification: str, bio: str) -> ArtistRead:
    """One person, nobody, or an unread profile."""
    brief = profile_brief(display_name, certification, bio)
    if not brief or not ai.available():
        return ArtistRead()
    try:
        reply = await ai.complete(
            [
                {"role": "system", "content": ARTIST_PROMPT},
                {"role": "user", "content": brief},
            ],
            max_tokens=ARTIST_MAX_TOKENS,
            temperature=0,
            thinking={"type": "disabled"},
        )
    except AIError:
        log.exception("AI artist failed")
        return ArtistRead()
    return artist_read(reply)


def artist_read(reply: str) -> ArtistRead:
    """An empty artist string is no person. Anything else unreadable is not an answer."""
    data = _json_object(reply)
    raw = data.get("artist") if data else None
    if not isinstance(raw, str):
        return ArtistRead()
    name = " ".join(raw.split())
    if not name:
        return ArtistRead(answered=True)
    return ArtistRead(
        name=ArtistName(written=name, simplified=simplified(name)),
        answered=True,
    )


def simplified(name: str) -> str:
    """Traditional characters as Damai spells them. Simplified text stays put."""
    from opencc import OpenCC

    return OpenCC("t2s").convert(name)


SHOW_FP_PREFIX = "musicshow:"
_DAY = re.compile(r"(?:(\d{4})\s*[.\-/年]\s*)?(\d{1,2})\s*[.\-/月]\s*(\d{1,2})")
_CLOCK = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
# A saved sale time is "8-28" or "8-28 18:00". A venue hyphen is not one.
_SALE_STAMP = re.compile(r"\d{1,2}-\d{1,2}(?: \d{2}:\d{2})?\Z")


def sale_stamp(window: str) -> str:
    """Month, day, and clock of a sale. A second clock is a second sale."""
    match = _DAY.search(window)
    if match is None:
        return ""
    month, day = int(match.group(2)), int(match.group(3))
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return ""
    stamp = f"{month}-{day}"
    clock = _CLOCK.search(window)
    if clock is None:
        return stamp
    hour, minute = int(clock.group(1)), int(clock.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return stamp
    return f"{stamp} {hour:02d}:{minute:02d}"


def sale_seen(key: str, stored: AbstractSet[str]) -> bool:
    """True when this sale was already sent.

    A stored show with no sale time still counts. A different sale time does not.
    """
    if key in stored:
        return True
    show, _, stamp = key.rpartition("\n")
    if not show.startswith(SHOW_FP_PREFIX):
        return False
    if _SALE_STAMP.fullmatch(stamp):
        return show in stored
    return any(item == key or item.startswith(key + "\n") for item in stored)


@dataclass(frozen=True)
class TicketShow:
    performer: str
    date: str
    place: str
    price: str = ""
    window: str = ""

    @property
    def remember_key(self) -> str | None:
        if not self.performer or not self.date or not self.place:
            return None
        base = f"{SHOW_FP_PREFIX}{self.performer}\n{self.date}\n{self.place}"
        stamp = sale_stamp(self.window)
        return f"{base}\n{stamp}" if stamp else base


@dataclass(frozen=True)
class TicketSend:
    title: str
    summary: str
    remember: tuple[str, ...]


@dataclass(frozen=True)
class TicketDraft:
    title: str
    about: str
    window: str
    shows: tuple[TicketShow, ...]
    price: str = ""
    where: str = ""

    def plan(self, already: AbstractSet[str]) -> TicketSend | None:
        kept: list[TicketShow] = []
        remember: list[str] = []
        for show in self.shows:
            key = show.remember_key
            if key is not None and sale_seen(key, already):
                continue
            kept.append(show)
            if key is not None:
                remember.append(key)
        if not kept:
            return None
        return TicketSend(
            title=self.title,
            summary=self._summary(tuple(kept)),
            remember=tuple(remember),
        )

    def _summary(self, kept: tuple[TicketShow, ...]) -> str:
        parts = [self.title, self.about, self.window, self.where, self.price]
        for show in kept:
            parts.extend((show.performer, show.date, show.place, show.price))
        labels = _labels("\n".join(parts))
        blocks: list[str] = []
        if self.about:
            blocks.append(f"**{labels.about}**\n{self.about}")
        lines = [
            _fact(label, value, labels.han)
            for label, value in ((labels.sale, self.window), (labels.buy, self.where))
            if value
        ]
        per_show = not self.price and len({show.price for show in kept}) > 1
        shared = self.price or (kept[0].price if kept else "")
        footer = "" if per_show else shared
        for show in kept:
            rows = (
                (labels.date, show.date),
                (labels.venue, show.place),
                (labels.price, show.price if per_show else ""),
            )
            lines.extend(_fact(label, value, labels.han) for label, value in rows if value)
        if footer:
            lines.append(_fact(labels.price, footer, labels.han))
        if lines:
            blocks.append(f"**{labels.schedule}**\n" + "\n".join(lines))
        return _fit_lines("\n\n".join(blocks), EXCERPT_CHARS)


@dataclass(frozen=True)
class _CardLabels:
    about: str
    schedule: str
    sale: str
    buy: str
    date: str
    venue: str
    price: str
    han: bool


_LABELS_EN = _CardLabels(
    "About", "Schedule", "Sale", "Buy", "Date", "Venue", "Price", False
)
_ZH_WORDS = ("简介", "日程", "售票", "购买", "日期", "地点", "票价")
_LABELS_ZH = _CardLabels(*_ZH_WORDS, True)
_HAN = re.compile(r"[\u4e00-\u9fff]")


def _labels(text: str) -> _CardLabels:
    if not _HAN.search(text):
        return _LABELS_EN
    if simplified(text) == text:
        return _LABELS_ZH
    from opencc import OpenCC

    convert = OpenCC("s2t").convert
    return _CardLabels(*(convert(word) for word in _ZH_WORDS), True)


def _fact(label: str, value: str, han: bool) -> str:
    return f"{label}：{value}" if han else f"{label}: {value}"


def parse_ticket_draft(text: str, post: str) -> TicketDraft | None:
    data = _json_object(text)
    if data is None or not isinstance(data.get("title"), str):
        return None
    title = excerpt(" ".join(data["title"].split()), TITLE_CHARS)
    if not title:
        return None
    raw_shows = data.get("shows")
    if not isinstance(raw_shows, list):
        return None
    haystack = " ".join(post.split())
    window = _one_line(data.get("window"))
    shows = [show for entry in raw_shows if (show := _parse_show(entry, haystack, window))]
    if not shows:
        return None
    return TicketDraft(
        title=title,
        about=_prose(data.get("about")),
        window=window,
        shows=tuple(shows),
        price=_one_line(data.get("price")),
        where=_one_line(data.get("where")),
    )


def _parse_show(entry: Any, haystack: str, window: str) -> TicketShow | None:
    if not isinstance(entry, dict):
        return None
    performer = _one_line(entry.get("performer"))
    date = _one_line(entry.get("date"))
    place = _one_line(entry.get("place"))
    if date not in haystack:
        date = ""
    if place not in haystack:
        place = ""
    if not date and not place:
        return None
    return TicketShow(
        performer=performer,
        date=date,
        place=place,
        price=_one_line(entry.get("price")),
        window=window,
    )


DAMAI_WEIBO = "1722647874"
TICKET_BACKFILL = timedelta(days=7)
StoredShows = Callable[[str, str, str], set[str]]


def _recent_missed(got: Any, chosen: Sequence[FetchedItem]) -> list[FetchedItem]:
    """Seen posts from the past week. A new artist gets one more look at them."""
    have = {item.item_id for item in chosen if item.item_id}
    cutoff = datetime.now(timezone.utc) - TICKET_BACKFILL
    extra: list[FetchedItem] = []
    for item in got.items:
        when = item.occurred_at
        if not item.item_id or item.item_id in have or when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        else:
            when = when.astimezone(timezone.utc)
        if when < cutoff:
            continue
        extra.append(item)
        have.add(item.item_id)
    return extra


@dataclass(frozen=True)
class WatchedArtist:
    guild_id: int
    platform: str
    account: str
    artist: ArtistName


@dataclass(frozen=True)
class SalePost:
    guild_id: int
    title: str
    summary: str
    stamps: tuple[tuple[str, str, str], ...]


def weibo_sales(
    draft: TicketDraft,
    watched: Sequence[WatchedArtist],
    stored: StoredShows,
) -> tuple[SalePost, ...]:
    """One post per guild that still has a new sale for a followed artist."""
    by_guild: dict[int, list[WatchedArtist]] = {}
    for watch in watched:
        by_guild.setdefault(watch.guild_id, []).append(watch)
    posts: list[SalePost] = []
    for guild_id, watches in by_guild.items():
        names = {watch.artist.simplified.casefold() for watch in watches}
        shows = tuple(
            show for show in draft.shows if simplified(show.performer).casefold() in names
        )
        if not shows:
            continue
        ref = f"{guild_id}:{FEATURE_TICKET}"
        seen = {key for watch in watches for key in stored(watch.platform, watch.account, ref)}
        plan = replace(draft, shows=shows).plan(seen)
        if plan is None:
            continue
        stamps = tuple(
            (watch.platform, watch.account, key)
            for key in plan.remember
            for watch in watches
            if _key_names(key, watch.artist)
        )
        posts.append(SalePost(guild_id, plan.title, plan.summary, stamps))
    return tuple(posts)


def _key_names(key: str, artist: ArtistName) -> bool:
    person = key.split("\n", 1)[0].removeprefix(SHOW_FP_PREFIX)
    return simplified(person).casefold() == artist.simplified.casefold()


SendSale = Callable[[int, Any, list[Any] | None], Awaitable[bool]]


async def send_sale(
    send: Callable[..., Awaitable[Any]], guild_id: int, embed: Any, files: list[Any] | None
) -> bool:
    sent = await send(guild_id, FEATURE_TICKET, embed=embed, queue=True, files=files)
    return sent is not None


async def post_ticket_shows(
    draft: TicketDraft,
    platform: str,
    account: str,
    topic: str,
    pending: Sequence[int],
    item: FetchedItem,
    stored: StoredShows,
    send: Callable[..., Awaitable[Any]],
) -> tuple[int, list[str], list[tuple[str, str]]]:
    """Post one ticket draft to each guild. Empty plans are still marked delivered."""
    posted = 0
    refs: list[str] = []
    stamps: list[tuple[str, str]] = []
    for guild_id in pending:
        ref = f"{guild_id}:{topic}"
        plan = draft.plan(stored(platform, account, ref))
        if plan is not None:
            embed, files = await news_card(item, topic, title=plan.title, summary=plan.summary)
            if not await send(guild_id, topic, embed=embed, queue=True, files=files or None):
                continue
            posted += 1
            stamps.extend((key, ref) for key in plan.remember)
        refs.append(ref)
    return posted, refs, stamps


class DamaiWatch:
    """大麦官博 checked against ticket artists learned from earlier account polls."""

    def __init__(self) -> None:
        self._watched: list[WatchedArtist] = []
        self._artists: dict[tuple[str, str, str, str, str], ArtistName] = {}
        # The next pull reads the past week again. One new artist, one look.
        self._replay = False

    def prune(self, followed: set[tuple[int, str, str]]) -> None:
        """Drop artists whose ticket follow is gone. Kept notes last until the next daily pull."""
        accounts = {(platform, account) for _, platform, account in followed}
        self._watched = [
            watch
            for watch in self._watched
            if (watch.guild_id, watch.platform, watch.account) in followed
        ]
        self._artists = {
            key: artist
            for key, artist in self._artists.items()
            if (key[0], key[1]) in accounts
        }

    async def note(
        self,
        platform: str,
        account: str,
        guilds: Sequence[int],
        *,
        display: str,
        certification: str,
        bio: str,
        ai: Any,
        lock: Any,
        ai_on: bool,
    ) -> ArtistRead | None:
        if not guilds or not ai_on or not ai.available() or not (display or certification or bio):
            return None
        key = (platform, account, display, certification, bio)
        cached = self._artists.get(key)
        if cached is None:
            async with lock:
                read = await one_artist(ai, display, certification, bio)
            artist = read.name
            if artist is None:
                return read
            self._artists[key] = artist
        else:
            artist = cached
            read = ArtistRead(name=artist, answered=True)
        guild_ids = set(guilds)
        known = {
            watch.guild_id for watch in self._watched
            if (watch.platform, watch.account) == (platform, account)
        }
        # A new person, or a guild that was not watching this account, rereads the week.
        if cached is None or guild_ids - known:
            self._replay = True
        # These guilds are written again below. A guild left out keeps its old artist.
        self._watched = [
            watch for watch in self._watched
            if (watch.platform, watch.account) != (platform, account)
            or watch.guild_id not in guild_ids
        ]
        self._watched.extend(
            WatchedArtist(guild_id, platform, account, artist) for guild_id in guild_ids
        )
        return read

    def listeners(self) -> list[int]:
        return list(dict.fromkeys(watch.guild_id for watch in self._watched))

    def armed(self, *, ai_on: bool, available: bool) -> bool:
        """True when this wake will ask 大麦."""
        return bool(self._watched) and ai_on and available

    async def post(
        self,
        fetcher: Any,
        *,
        ai: Any,
        filter_cache: Any,
        write_cache: Any,
        lock: Any,
        ai_on: bool,
        stored: StoredShows,
        send: SendSale,
        gap: float = 0,
    ) -> tuple[int, bool]:
        """Post new sales. The bool is true when this daily pull finished."""
        if not self.armed(ai_on=ai_on, available=ai.available()):
            return 0, False
        if gap > 0:
            await asyncio.sleep(gap)
        replay = self._replay
        got = await fetcher.pull("weibo", DAMAI_WEIBO, backfill=TICKET_BACKFILL)
        from news.drafts import card_fields

        posted = 0
        retry = False
        posts = list(got.new)
        if replay:
            posts.extend(_recent_missed(got, posts))
        for item in posts:
            fields = await card_fields(
                ai, filter_cache, write_cache, lock, item, FEATURE_TICKET, ai_on_poll=ai_on
            )
            if isinstance(fields, TicketDraft):
                sales = weibo_sales(fields, self._watched, stored)
            elif fields is False:
                sales = []
            else:
                # None, or a title pair, is not a finished read. Try this post again.
                retry = True
                continue
            failed = False
            for sale in sales:
                embed, files = await news_card(
                    item, FEATURE_TICKET, title=sale.title, summary=sale.summary
                )
                if not await send(sale.guild_id, embed, files or None):
                    failed = True
                    continue
                posted += 1
                for platform, account, key in sale.stamps:
                    fetcher.mark_delivered(
                        platform, account, key, [f"{sale.guild_id}:{FEATURE_TICKET}"]
                    )
            if failed:
                retry = True
            else:
                fetcher.mark_seen("weibo", DAMAI_WEIBO, [item.item_id])
        if replay and not retry:
            self._replay = False
        return posted, not retry


async def take_artist(
    watch: DamaiWatch,
    store: Any,
    platform: str,
    account: str,
    live: Sequence[tuple[int, str]],
    *,
    display: str,
    certification: str,
    bio: str,
    ai: Any,
    lock: Any,
    ai_on: bool,
) -> list[tuple[int, str]]:
    ticket_guilds = [guild_id for guild_id, topic in live if topic == FEATURE_TICKET]
    found = await watch.note(
        platform, account, ticket_guilds, display=display, certification=certification,
        bio=bio, ai=ai, lock=lock, ai_on=ai_on,
    )
    if found is not None and found.name is not None:
        remember_ticket_artist(store, ticket_guilds, platform, account, found.name.written)
    return list(live)
