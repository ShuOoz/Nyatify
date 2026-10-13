"""One Weibo poll: each guest on its proxy, then the Damai backup."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fetch import FetchError
from news.drafts import AiCache
from news.follows import FEATURE_TICKET, GuildTopic, SourceKey
from news.rounds import Poll, hold_tickets, is_due, pause_between, poll_source, skip_failed
from news.source.post.common import check_enabled, proxy_label, share_accounts, vet, weibo_rest
from news.source.post.weibo import (
    configured_proxies,
    fresh_identity,
    login_pins,
    poll_proxies,
    set_login_held,
)
from news.source.post.weibo.http import bound_proxy, current_proxy
from news.source.post.weibo.ticket import DAMAI_WEIBO, TICKET_BACKFILL

SendSale = Callable[..., Awaitable[Any]]

log = logging.getLogger(__name__)


def halt_round(poll: Poll, account: str, _exc: BaseException, *, proxy: str | None = None) -> None:
    """Stop the rest of this proxy and leave it out of the next poll.

    Damai reports the proxy it bound. That call passes `proxy` because the
    `with` has already ended. An account ask uses the proxy bound right now.
    """
    stopped = current_proxy() if proxy is None else proxy
    poll.weibo_stopped.add(stopped)
    poll.weibo_quiet.add(stopped)
    weibo_rest.rest(stopped)
    log.warning(
        "Stopping the rest of this Weibo proxy (%s) after %s; it rests the next poll",
        proxy_label(stopped),
        account,
    )


@dataclass
class ProxyPool:
    """Proxies for this check. Built once.

    `login_held` means the saved login stays off because its proxy failed
    the exit check.
    """

    chosen: tuple[str, ...] | None = None
    login_held: bool = False


async def poll_weibo(
    poll: Poll,
    due: list[str],
    roster: list[str],
    live: dict[SourceKey, list[GuildTopic]],
    now_wall: datetime,
    only: set[SourceKey] | None,
    *,
    backfill_for: set[str],
    pool: ProxyPool,
    post_gap: float,
    ticket_gap: float,
    jitter: float,
    rng: random.Random,
) -> tuple[int, bool]:
    """Ask Weibo one account at a time. Each guest stays on its proxy.

    The full follow list is dealt across the proxies, so an account that
    sits this poll out does not move the others. 30–90 seconds between
    post accounts, 2 minutes between ticket accounts. An HTTP 432 stops
    the rest of that proxy and leaves it out of the next poll. When the
    exit check is on, a proxy whose lookup fails, or that shares an exit,
    sits this check out. A proxy whose exit moved gets a new guest.
    A saved login stays on the first proxy. When that proxy fails the
    exit check, the other proxies are used and the login stays off them.
    """
    if not due:
        return 0, False
    asking = set(due)
    proxies = await _open_proxies(pool)
    if not proxies:
        hold_tickets(poll, "weibo", due, backfill_for, only)
        return 0, False
    sitting = [proxy for proxy in proxies if weibo_rest.sits_out(proxy)]
    shares, spent = share_accounts(roster, proxies, sitting)
    for proxy in spent:
        weibo_rest.take(proxy)
    poll.weibo_quiet.update(spent)
    if not shares:
        hold_tickets(poll, "weibo", due, backfill_for, only)
        return 0, False
    posted = 0
    fetched = False
    for share in shares:
        for index, account in enumerate(share.accounts):
            if share.proxy in poll.weibo_stopped:
                hold_tickets(poll, "weibo", share.accounts[index:], backfill_for, only)
                break
            source: SourceKey = ("weibo", account)
            if account not in asking or not is_due(poll, source, only):
                continue
            ticket = account in backfill_for
            if fetched:
                await pause_between(
                    ticket_gap if ticket else post_gap, 0.0 if ticket else jitter, rng
                )
            with bound_proxy(share.proxy):
                posted += await poll_source(
                    poll, source, live[source], now_wall,
                    backfill=TICKET_BACKFILL if ticket else None,
                )
            fetched = True
    return posted, fetched


async def _open_proxies(pool: ProxyPool) -> tuple[str, ...]:
    """Proxies for this check. Built once.

    A proxy that is already sitting stays, so this check can spend that
    rest. A failed exit, or a shared one, is left out until the next check.
    A saved login uses only the first proxy. When that proxy fails the
    exit check, the others are used and the login stays off them.
    """
    if pool.chosen is not None:
        set_login_held(pool.login_held)
        return pool.chosen
    listed = poll_proxies()
    chosen = listed if not check_enabled() else await _vet_listed(listed)
    held = False
    if not chosen and login_pins(listed):
        rest = tuple(proxy for proxy in configured_proxies() if proxy != listed[0])
        chosen = rest if not check_enabled() else await _vet_listed(rest)
        held = bool(chosen)
        if held:
            log.info(
                "Weibo login stays off this check: %s failed the exit check",
                proxy_label(listed[0]),
            )
    pool.chosen = chosen
    pool.login_held = held
    set_login_held(held)
    return chosen


async def _vet_listed(listed: tuple[str, ...]) -> tuple[str, ...]:
    """`listed`, without a proxy that failed this exit check."""
    quiet = {proxy for proxy in listed if weibo_rest.sits_out(proxy)}
    live = [proxy for proxy in listed if proxy not in quiet]
    ok, _taken = await vet(
        live,
        {},
        platform="weibo",
        refresh=fresh_identity,
        refresh_note="fresh guest",
    )
    passed = set(ok)
    return tuple(proxy for proxy in listed if proxy in quiet or proxy in passed)


async def _damai_proxy(poll: Poll, pool: ProxyPool) -> str | None:
    """The first proxy still in this round. A sitting proxy is spent."""
    for proxy in await _open_proxies(pool):
        if proxy in poll.weibo_quiet:
            continue
        if weibo_rest.sits_out(proxy):
            weibo_rest.take(proxy)
            poll.weibo_quiet.add(proxy)
            continue
        return proxy
    return None


async def poll_damai(
    poll: Poll,
    *,
    gap: float,
    pool: ProxyPool,
    filter_cache: AiCache,
    write_cache: AiCache,
    send: SendSale,
) -> tuple[int, bool]:
    source: SourceKey = ("weibo", DAMAI_WEIBO)
    if time.monotonic() < poll.skip_until.get(source, 0.0):
        return 0, False
    if not poll.damai.armed(ai_on=poll.ai_on, available=poll.ai.available()):
        return 0, False
    proxy = await _damai_proxy(poll, pool)
    if proxy is None:
        return 0, False
    try:
        with bound_proxy(proxy):
            count, done = await poll.damai.post(
                poll.fetcher,
                ai=poll.ai,
                filter_cache=filter_cache,
                write_cache=write_cache,
                lock=poll.ai_lock,
                ai_on=poll.ai_on,
                stored=poll.fetcher.stored_shows,
                send=send,
                gap=gap,
            )
    except FetchError as exc:
        await skip_failed(
            poll,
            source,
            [(guild_id, FEATURE_TICKET) for guild_id in poll.damai.listeners()],
            exc,
            proxy=proxy,
        )
        return 0, False
    return count, done
