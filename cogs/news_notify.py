"""Watch followed accounts and post short news cards."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands, tasks

from ai import AI
from announce import NotifyCog, notice_text, send_announcement
from config import settings
from fetch import poll_delay
from guild import (
    GuildConfig,
    guild_config as default_guild_config,
    register_announce_feature,
    unregister_announce_feature,
)
from news import drafts
from news.cards import (
    match_event_id, match_notice_extra, match_recap_notes, news_card,
    older_match_card, saved_match_summary, stream_fingerprint,
)
from news.command import NewsSlash
from news.feeds import Fetcher, PullResult
from news.follows import (
    FEATURE_TICKET, FOLLOWS_KEY, NEWS_FEATURES, GuildTopic, SourceKey,
    block_live_room, iter_news_follows, parse_stored_times, poll_plan,
)
from news.rounds import Poll, poll_list
from news.source.match.common import (
    START_LATE, START_POLL_JITTER_SECONDS, START_POLL_SECONDS, kickoff_id,
    match_delete_after, match_stage, sources_awaiting_start,
)
from news.source.post.bilibili import BiliCookies, is_bili_login_cookie
from news.source.post.bilibili.dynamics import watch_quote
from news.source.post.bilibili.round import BiliState, poll_bili
from news.source.post.weibo import round as weibo_round
from news.source.post.weibo import ticket as weibo_ticket
from news.source.registry import (
    auth_expired_message, hold_bookmark, is_match_platform, note_list_post, shows_post,
)
from notice import DEFAULT_NOTICES_PATH

log = logging.getLogger(__name__)

POST_POLL_SECONDS, POST_POLL_JITTER_SECONDS = 45 * 60, 15 * 60
MATCH_POLL_SECONDS, MATCH_POLL_JITTER_SECONDS = 10 * 60, 5 * 60
TICKET_POLL_SECONDS = 12 * 60 * 60
DAMAI_POLL_SECONDS = 24 * 60 * 60
POLL_START_SECONDS, POLL_START_JITTER_SECONDS = 45.0, 15.0
# 30 to 90 seconds between Bilibili phones, Weibo accounts, and other post accounts.
POST_FETCH_GAP_SECONDS, POST_FETCH_JITTER_SECONDS = 30.0, 60.0
TICKET_FETCH_GAP_SECONDS = 2 * 60
DAMAI_FETCH_GAP_SECONDS = 15.0
MATCH_FETCH_GAP_SECONDS = 2.0
POST_CHANNEL_KEEP = 15

# Bilibili and Weibo ask through their own round. Everything else is a plain list.
_ROUND_PLATFORMS = frozenset({"bilibili", "weibo"})


def _poll_flags(
    match: bool | None, post: bool | None, start: bool | None,
) -> tuple[bool, bool, bool]:
    """Settings when nothing was passed. One explicit flag turns the others on."""
    if match is None and post is None and start is None:
        return settings.news_poll_match, settings.news_poll_post, settings.news_poll
    match_on, post_on = match is not False, post is not False
    running = (match_on or post_on) if start is None else start
    return match_on, post_on, running


def _accounts(sources, platform: str) -> list[str]:
    """Accounts on one platform."""
    return sorted({account for source_platform, account in sources if source_platform == platform})


def _retain(store, kept) -> None:
    """Replace contents. A poll already running holds this same object."""
    store.clear()
    store.update(kept)


def _stream_stamp(cache, item, topic: str) -> str | None:
    job = cache.get(drafts.filter_cache_key(item, topic))
    if not isinstance(job, drafts.FilterJob):
        return None
    return stream_fingerprint(
        job.quotes, item.occurred_at, keep=job.keep, kind=job.kind, watch=watch_quote(item.tags),
    )


@dataclass
class _Delivery:
    """One item being announced. `new` refs get a delivery mark, `done` refs are handled."""

    platform: str
    account: str
    item: Any
    already: set[str]
    posted: int = 0
    new: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    stamps: list[tuple[str, str]] = field(default_factory=list)


class _OpenEdits:
    """One poll's fetcher. A post this cog already judged comes back when its words change."""

    def __init__(self, inner: Any, decided: dict[tuple[str, str, str], str]) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_decided", decided)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)

    async def pull(self, platform: str, account: str, **kwargs: Any) -> PullResult:
        got = await self._inner.pull(platform, account, **kwargs)
        return _reopen_edits(self._decided, platform, got.team_id or account, got)


def _reopen_edits(
    decided: dict[tuple[str, str, str], str], platform: str, account: str, got: PullResult,
) -> PullResult:
    known = {item.item_id for item in got.new}
    extra = []
    for item in got.items:
        if not item.item_id or item.item_id in known:
            continue
        saved = decided.get((platform, account, item.item_id))
        if saved is not None and saved != drafts.word_fingerprint(item):
            extra.append(item)
            known.add(item.item_id)
    return replace(got, new=[*got.new, *extra]) if extra else got


class NewsNotify(NewsSlash, NotifyCog):
    """Poll followed news sources and post cards per topic."""

    def __init__(
        self, bot: commands.Bot, guild_config: GuildConfig | None = None, *,
        fetcher: Fetcher | None = None, ai: AI | None = None,
        ai_on_poll: bool | None = None, poll_match: bool | None = None,
        poll_post: bool | None = None, start_polling: bool | None = None,
        clock: Callable[[], float] | None = None,
        notices_path: Path | None = DEFAULT_NOTICES_PATH,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(bot, notices_path=notices_path, clock=clock)
        self.guild_config = default_guild_config if guild_config is None else guild_config
        saved_bili = settings.bili_cookie or ""
        self.fetcher = fetcher or Fetcher(
            sportsdb_key=settings.sportsdb_key,
            pandascore_token=settings.pandascore_token,
            bili_cookie=saved_bili if is_bili_login_cookie(saved_bili) else None,
            weibo_cookie=settings.weibo_cookie,
        )
        self.ai = ai if ai is not None else AI(
            api_key=settings.ai_api_key, base_url=settings.ai_base_url, model=settings.ai_model,
        )
        self._ai_on_poll = settings.ai_on_poll if ai_on_poll is None else ai_on_poll
        self._poll_match, self._poll_post, self._start_polling = _poll_flags(
            poll_match, poll_post, start_polling,
        )
        self._fetch_lock, self._ai_lock = asyncio.Lock(), asyncio.Lock()
        # Claimed Discord sends that have not finished: platform, account, item id, guild ref.
        self._sending: set[tuple[str, str, str, str]] = set()
        self._filter_cache = drafts.AiCache()
        # Words already judged, keyed by platform, account, and post id.
        self._decided: dict[tuple[str, str, str], str] = {}
        self._write_cache = drafts.AiCache()
        self._damai = weibo_ticket.DamaiWatch()
        self._skip_until: dict[SourceKey, float] = {}
        self._bili_stopped: set[str] = set()
        self._weibo_stopped: set[str] = set()
        self._weibo_quiet: set[str] = set()
        self._bili_state, self.bili = BiliState(), BiliCookies()
        self._post_fetch_gap = POST_FETCH_GAP_SECONDS
        self._ticket_fetch_gap = TICKET_FETCH_GAP_SECONDS
        self._damai_fetch_gap = DAMAI_FETCH_GAP_SECONDS
        self._match_fetch_gap = MATCH_FETCH_GAP_SECONDS
        self._ticket_due_at = self._damai_due_at = self._match_full_at = 0.0
        # The last ticket wake left accounts unasked, so the 12-hour wait stays put.
        self._ticket_held = False
        self._seen_live: set[tuple[str, str, int]] = set()
        self._rng = rng or random.Random()
        if self._ai_on_poll and not self.ai.available():
            log.warning("AI_ON_POLL is on but AI is not configured; news poll will post excerpts")

    async def cog_load(self) -> None:
        for name in NEWS_FEATURES:
            register_announce_feature(name)
        await super().cog_load()
        self._prune_bookmarks()
        if not self._start_polling:
            return
        if self._poll_match and not self.poll_matches.is_running():
            self.poll_matches.start()
        if self._poll_post and not self.poll_feeds.is_running():
            self.poll_feeds.start()

    async def cog_unload(self) -> None:
        for loop in (self.poll_matches, self.poll_feeds):
            if loop.is_running():
                loop.cancel()
        for name in NEWS_FEATURES:
            unregister_announce_feature(name)
        await super().cog_unload()

    @tasks.loop(seconds=START_POLL_SECONDS)
    async def poll_matches(self) -> None:
        await self._poll_match_round()
        # The loop booked a wake at the fast floor. After a long fetch that
        # time is already past, so this sleep is the real gap.
        await asyncio.sleep(self._match_gap())

    @tasks.loop(seconds=POST_POLL_SECONDS)
    async def poll_feeds(self) -> None:
        post, ticket, damai = self._scheduled(time.monotonic())
        await self.poll_once(
            poll_post=post, poll_match=False, poll_ticket=ticket, poll_damai=damai,
        )
        if ticket and not self._ticket_held:
            self._ticket_due_at = time.monotonic() + TICKET_POLL_SECONDS
        self.poll_feeds.change_interval(seconds=self._poll_interval())

    def _scheduled(self, now: float) -> tuple[bool, bool, bool]:
        """Posts, ticket-only accounts, and the Damai backup for this wake."""
        post = self._poll_post
        return post, post and now >= self._ticket_due_at, post and now >= self._damai_due_at

    @poll_matches.before_loop
    @poll_feeds.before_loop
    async def _wait_to_poll(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(poll_delay(
            POLL_START_SECONDS, POLL_START_JITTER_SECONDS, self._rng, min_seconds=0.0,
        ))

    def _poll_interval(self) -> float:
        return poll_delay(POST_POLL_SECONDS, POST_POLL_JITTER_SECONDS, self._rng)

    def _match_rows(self) -> list[tuple[str, str, tuple[datetime, ...]]]:
        """One kickoff list per club. Guilds that follow it contribute every stored time."""
        kickoffs: dict[SourceKey, set[datetime]] = {}
        for _, follow in iter_news_follows(self.guild_config):
            platform, account = follow["platform"], follow["account"]
            if not is_match_platform(platform):
                continue
            kickoffs.setdefault((platform, account), set()).update(
                parse_stored_times(follow.get("next_kickoff")),
            )
        cutoff = kickoff_id(datetime.now(timezone.utc) - START_LATE)
        _retain(self._seen_live, {
            key for key in self._seen_live if key[2] >= cutoff and (key[0], key[1]) in kickoffs
        })
        return [
            (platform, account, tuple(sorted(times)))
            for (platform, account), times in kickoffs.items()
        ]

    def _awaiting_start(self, now: datetime) -> list[SourceKey]:
        return sources_awaiting_start(self._match_rows(), self._seen_live, now)

    def _match_gap(self) -> float:
        """Short while either match feed still owes a start card. Otherwise the slow poll."""
        if self._awaiting_start(datetime.now(timezone.utc)):
            return poll_delay(
                START_POLL_SECONDS, START_POLL_JITTER_SECONDS, self._rng, min_seconds=0.0,
            )
        return poll_delay(MATCH_POLL_SECONDS, MATCH_POLL_JITTER_SECONDS, self._rng)

    async def _poll_match_round(self) -> None:
        now_wall = datetime.now(timezone.utc)
        awaiting = self._awaiting_start(now_wall)
        narrow = bool(awaiting) and time.monotonic() < self._match_full_at
        await self.poll_once(
            poll_post=False, poll_ticket=False, poll_damai=False,
            only=set(awaiting) if narrow else None,
        )
        if not narrow:
            self._match_full_at = time.monotonic() + poll_delay(
                MATCH_POLL_SECONDS, MATCH_POLL_JITTER_SECONDS, self._rng,
            )

    def _prune_bookmarks(self, keep: set[SourceKey] | None = None) -> None:
        follows = list(iter_news_follows(self.guild_config))
        if keep is None:
            keep = {(follow["platform"], follow["account"]) for _, follow in follows}
        if any(follow["topic"] == FEATURE_TICKET for _, follow in follows):
            keep = {*keep, ("weibo", weibo_ticket.DAMAI_WEIBO)}
        self.fetcher.prune_unused(keep)
        _retain(self._skip_until, {
            key: until for key, until in self._skip_until.items() if key in keep
        })
        self._filter_cache.drop_sources(keep)
        self._write_cache.drop_sources(keep)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await super().on_guild_remove(guild)
        self.guild_config.set_guild_list(guild.id, FOLLOWS_KEY, [])
        self._prune_bookmarks()

    async def poll_once(
        self, *, poll_post: bool | None = None, poll_match: bool | None = None,
        poll_ticket: bool | None = None, poll_damai: bool | None = None,
        only: set[SourceKey] | None = None,
    ) -> int:
        posting = self._poll_post if poll_post is None else poll_post
        matching = self._poll_match if poll_match is None else poll_match
        tickets_on = posting if poll_ticket is None else poll_ticket
        damai_on = posting if poll_damai is None else poll_damai
        try:
            return await self._poll_sources(posting, matching, tickets_on, damai_on, only)
        finally:
            if posting:
                await self.bili.end(self.fetcher)

    async def _poll_sources(
        self, posting: bool, matching: bool, tickets_on: bool, damai_on: bool,
        only: set[SourceKey] | None,
    ) -> int:
        now_wall = datetime.now(timezone.utc)
        keep, live, sports, posts, tickets = poll_plan(
            self.guild_config, now=time.monotonic(), now_wall=now_wall,
            poll_post=posting, poll_match=matching, poll_ticket=tickets_on,
            skip_until=self._skip_until,
            guild_ok=lambda guild_id: self.bot.get_guild(guild_id) is not None,
        )
        self._damai.prune({
            (guild_id, follow["platform"], follow["account"])
            for guild_id, follow in iter_news_follows(self.guild_config)
            if follow["topic"] == FEATURE_TICKET
        })
        self._prune_bookmarks(keep)
        post_sources = (*posts, *tickets)
        bili_due = _accounts(post_sources, "bilibili")
        weibo_due = _accounts(post_sources, "weibo")
        other_posts = [source for source in posts if source[0] not in _ROUND_PLATFORMS]
        other_tickets = [source for source in tickets if source[0] not in _ROUND_PLATFORMS]
        self._rng.shuffle(other_posts)
        # A match check must not wipe a stop the post check is still using.
        if weibo_due or damai_on:
            self._weibo_stopped.clear()
            self._weibo_quiet.clear()
        poll = self._poll_state()
        pool = weibo_round.ProxyPool()

        async def listed(sources, gap, jitter, backfill=None):
            return await poll_list(
                poll, sources, live, now_wall, gap=gap, jitter=jitter,
                backfill=backfill, only=only, rng=self._rng,
            )

        rounds = [
            await listed(sports, self._match_fetch_gap, 0.0),
            await poll_bili(
                poll, self._bili_state, bili_due, live, now_wall, only,
                backfill_for=set(_accounts(tickets, "bilibili")),
                gap=self._post_fetch_gap, jitter=POST_FETCH_JITTER_SECONDS, rng=self._rng,
            ),
            await weibo_round.poll_weibo(
                poll, weibo_due, _accounts(live, "weibo"), live, now_wall, only,
                backfill_for=set(_accounts(tickets, "weibo")), pool=pool,
                post_gap=self._post_fetch_gap, ticket_gap=self._ticket_fetch_gap,
                jitter=POST_FETCH_JITTER_SECONDS, rng=self._rng,
            ),
            await listed(other_posts, self._post_fetch_gap, POST_FETCH_JITTER_SECONDS),
            await listed(other_tickets, self._ticket_fetch_gap, 0.0, weibo_ticket.TICKET_BACKFILL),
        ]
        posted = sum(count for count, _ in rounds)
        if damai_on:
            count, done = await weibo_round.poll_damai(
                poll, gap=self._damai_fetch_gap if any(fetched for _, fetched in rounds) else 0,
                pool=pool, filter_cache=self._filter_cache, write_cache=self._write_cache,
                send=partial(weibo_ticket.send_sale, self._send),
            )
            if done:
                self._damai_due_at = time.monotonic() + DAMAI_POLL_SECONDS
            posted += count
        if tickets_on:
            self._ticket_held = bool(poll.held_tickets)
        return posted

    def _poll_state(self) -> Poll:
        return Poll(
            fetcher=_OpenEdits(self.fetcher, self._decided), fetch_lock=self._fetch_lock,
            bili=self.bili, guild_config=self.guild_config, damai=self._damai, ai=self.ai,
            ai_lock=self._ai_lock, ai_on=self._ai_on_poll, skip_until=self._skip_until,
            seen_live=self._seen_live, bili_stopped=self._bili_stopped,
            weibo_stopped=self._weibo_stopped, weibo_quiet=self._weibo_quiet,
            announce=self._announce_item, save_cookie=self._save_cookie_result,
            warn_cookie=self._warn_cookie,
        )

    async def _warn_cookie(
        self, key: SourceKey, subscribers: list[GuildTopic],
        exc: BaseException, skip_seconds: float,
    ) -> None:
        platform, account = key
        hint = notice_text(auth_expired_message(platform))
        log.warning("%s (%s)", hint, exc)
        subject = f"cookie:{platform}:{account}"
        now = self.notices._now()
        warned = {
            notice.guild_id for notice in self.notices
            if notice.subject == subject and notice.expires_at > now
        }
        for guild_id, topic in subscribers:
            if guild_id in warned:
                continue
            await self._send(
                guild_id, topic, content=hint, delete_after=skip_seconds,
                persist=True, notice_key=subject,
            )

    def _match_saved_summary(
        self, event_id: str | None, stage: str | None, notes: str,
    ) -> str | None:
        if not (event_id and stage):
            return None
        for notice in self.notices:
            if notice.subject != event_id:
                continue
            found = self.notices.live(notice.feature, notice.guild_id, event_id)
            if found is not None and (
                saved := saved_match_summary(found.extra, stage, notes)
            ) is not None:
                return saved
        return None

    async def _announce_item(
        self, platform: str, account: str, audience: list[GuildTopic], item,
    ) -> int:
        already = self.fetcher.delivered_refs(platform, account, item.item_id) | self._sending_refs(
            platform, account, item.item_id,
        )
        delivery = _Delivery(platform, account, item, already)
        event_id = match_event_id(item)
        by_topic: dict[str, list[int]] = {}
        for guild_id, topic in audience:
            if not shows_post(item, topic):
                continue
            live = bool(event_id and self.notices.live(topic, guild_id, event_id))
            if f"{guild_id}:{topic}" in already and not live:
                continue
            by_topic.setdefault(topic, []).append(guild_id)

        saved = self._match_saved_summary(event_id, match_stage(item), match_recap_notes(item))
        send_show = partial(self._send, item_id=item.item_id)
        for topic, guilds in by_topic.items():
            refs = [f"{guild_id}:{topic}" for guild_id in guilds]
            if block_live_room(item, topic):
                delivery.done += refs
                continue
            fields = (None, saved) if saved is not None else await drafts.card_fields(
                self.ai, self._filter_cache, self._write_cache, self._ai_lock,
                item, topic, ai_on_poll=self._ai_on_poll,
            )
            if fields is False:
                delivery.done += refs
            elif isinstance(fields, weibo_ticket.TicketDraft):
                count, handled, stamps = await weibo_ticket.post_ticket_shows(
                    fields, platform, account, topic, guilds, item,
                    self.fetcher.stored_shows, send_show,
                )
                delivery.posted += count
                delivery.done += handled
                delivery.new += handled
                delivery.stamps += stamps
            elif fields is not None:
                await self._post_cards(delivery, topic, guilds, event_id, *fields)

        self._mark_announced(delivery, audience)
        return delivery.posted

    def _mark_announced(self, delivery: _Delivery, audience: list[GuildTopic]) -> None:
        """Stamp what was sent. Mark the item seen once every follower has it."""
        platform, account, item = delivery.platform, delivery.account, delivery.item
        if delivery.new:
            self.fetcher.mark_delivered(platform, account, item.item_id, delivery.new)
        for stamp, ref in delivery.stamps:
            self.fetcher.mark_delivered(platform, account, stamp, [ref])
        # A claim still waiting on Discord is not sent yet, so it does not count.
        handled = (delivery.already | set(delivery.done)) - self._sending_refs(
            platform, account, item.item_id,
        )
        refs = {f"{guild}:{topic}" for guild, topic in audience}
        noted = note_list_post(item, account, audience, handled)
        if refs and refs <= handled and not hold_bookmark(item) and not noted:
            self._decided[(platform, account, item.item_id)] = drafts.word_fingerprint(item)
            self.fetcher.mark_seen(platform, account, [item.item_id])

    def _sending_refs(self, platform: str, account: str, item_id: str) -> set[str]:
        """Guild refs whose Discord send for this item is claimed and not finished."""
        return {
            ref for sent_platform, sent_account, sent_id, ref in self._sending
            if (sent_platform, sent_account, sent_id) == (platform, account, item_id)
        }

    def _claimed(self, platform: str, account: str, item_id: str, ref: str) -> bool:
        """True when this guild already has the item, or a send for it is in flight."""
        return ref in self.fetcher.delivered_refs(platform, account, item_id) or (
            platform, account, item_id, ref
        ) in self._sending

    async def _post_cards(
        self, delivery: _Delivery, topic: str, guilds: list[int], event_id: str | None,
        title: str | None, summary: str | None,
    ) -> None:
        """One card per guild. A live match card is edited in place when there is one."""
        item, platform, account = delivery.item, delivery.platform, delivery.account
        stamp = _stream_stamp(self._filter_cache, item, topic)
        ttl = match_delete_after(item, datetime.now(timezone.utc))
        for guild_id in guilds:
            ref = f"{guild_id}:{topic}"
            if stamp and self._claimed(platform, account, stamp, ref):
                delivery.done.append(ref)
                continue
            embed, files = await news_card(item, topic, title=title, summary=summary)
            extra = match_notice_extra(item, embed, summary) if ttl else None
            editing = bool(ttl and event_id and self.notices.live(topic, guild_id, event_id))
            sending = (platform, account, item.item_id, ref)
            if sending in self._sending or (
                self._claimed(platform, account, item.item_id, ref) and not editing
            ):
                continue
            if stamp and self._claimed(platform, account, stamp, ref):
                delivery.done.append(ref)
                continue
            # Claim before Discord. The bookmark is written only after the send is kept.
            held = [sending]
            if stamp:
                held.append((platform, account, stamp, ref))
            self._sending.update(held)
            updated = kept = False
            try:
                updated = bool(ttl and event_id and await self._update_match_card(
                    guild_id, topic, event_id, embed, ttl, extra or {}, files=files,
                ))
                message = None if updated else await self._send(
                    guild_id, topic, embed=embed, delete_after=ttl, persist=ttl is not None,
                    notice_key=event_id if ttl else None, extra=extra, queue=ttl is None,
                    files=files or None, item_id=item.item_id,
                )
                kept = bool(updated or message)
            finally:
                if not kept:
                    self._sending.difference_update(held)
            if not kept:
                continue
            fresh = not (updated and ref in delivery.already)
            if fresh:
                self.fetcher.mark_delivered(platform, account, item.item_id, [ref])
            if stamp:
                self.fetcher.mark_delivered(platform, account, stamp, [ref])
            self._sending.difference_update(held)
            delivery.done.append(ref)
            if fresh:
                delivery.posted += 1

    async def _update_match_card(
        self, guild_id: int, topic: str, event_id: str, embed: discord.Embed,
        ttl: float, extra: dict[str, Any], files: list[discord.File] | None = None,
    ) -> bool:
        notice = self.notices.live(topic, guild_id, event_id)
        if notice is None:
            return False
        if older_match_card(notice.extra, extra):
            return True
        try:
            message = await self.notices.resolve_message(notice)
            if message is None:
                log.warning("Could not update match card %s in guild %s", event_id, guild_id)
                return True
            if notice.extra.get("card") == extra.get("card"):
                notice.message = await message.fetch()
                return True
            edit = {"embed": embed, "attachments": files} if files else {"embed": embed}
            await message.edit(**edit)
        except discord.NotFound:
            log.warning("Match card %s in guild %s is gone; posting a new one", event_id, guild_id)
            self.notices.forget(topic, guild_id, event_id)
            return False
        except discord.HTTPException:
            log.warning("Could not update match card %s in guild %s", event_id, guild_id, exc_info=True)
            return True
        self.notices.track(topic, guild_id, event_id, message, ttl=ttl, extra=extra)
        return True

    async def _send(
        self, guild_id: int, topic: str, *, embed: discord.Embed | None = None,
        content: str | None = None, delete_after: float | None = None, persist: bool = False,
        notice_key: str | None = None, extra: dict[str, Any] | None = None, queue: bool = False,
        files: list[discord.File] | None = None, item_id: str | None = None,
    ) -> discord.Message | None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        message = await send_announcement(
            guild, self.guild_config, topic, content=content, embed=embed,
            delete_after=delete_after if persist else None, notice_key=notice_key,
            extra=extra, notices=self.notices if persist else None, files=files,
        )
        if message is None:
            return None
        if item_id:
            log.info("Sent %s message %s", item_id, message.id)
        if delete_after is not None and not persist:
            try:
                await message.delete(delay=delete_after)
            except discord.HTTPException:
                pass
        if queue:
            await self.notices.keep_recent(guild_id, message, POST_CHANNEL_KEEP)
        return message


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NewsNotify(bot))
