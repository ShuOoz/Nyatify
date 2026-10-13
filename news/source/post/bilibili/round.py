"""One Bilibili poll: the phone split, then one planned read per account.

Account order stays as given. `BILI_ACCOUNTS_PER_PHONE` accounts share a phone
while the phones can cover the list. A longer list puts more accounts on each
phone, so every account is still asked. `BILI_PHONES_PER_PROXY` is how many
phones one proxy may carry. The first phone on every proxy is used before a
second phone is put on any proxy. The account asked first rotates each poll.
The phones in use slide by one group. A proxy that is sitting out is skipped,
and the next phone fills its place. A group still left goes to a phone that
is still in this poll. Phones that are not needed rest. A phone is never sent
on another proxy. The poll then asks the first account on every phone, then
the next, after the pass before it has finished. Each account has one source,
chosen before the request: the app phone on its proxy, or the login page on
the first proxy. An account whose last app read was empty reads the visitor
list on its own proxy. A blank ends that account. HTTP 412 sits that proxy
out of the next poll.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config import BASE_DIR, settings
from news.follows import GuildTopic, SourceKey, TICKET_BACKFILL
from news.rounds import Poll, hold_tickets, is_due, pause_between, poll_source
from news.source.post.bilibili import dynamics
from news.source.post.bilibili.app import (
    bind_phone,
    bind_proxy,
    fresh_phones,
    proxy_key,
    reset_phone,
    reset_proxy,
)
from news.source.post.common import (
    ProxyRest,
    bili_blocked,
    group_accounts,
    proxy_label,
    read_json,
    vetted_window,
    write_json,
)

log = logging.getLogger(__name__)

BiliRoster = tuple[tuple[str, ...], tuple[str, ...], int, int]
ROTATION_PATH = BASE_DIR / "data" / "bili_rotation.json"
SIT_OUT_PATH = BASE_DIR / "data" / "bili_empty_cap.json"
BLANK_PATH = BASE_DIR / "data" / "bili_blank.json"

# Proxies left out of the rest of this poll. None outside a poll.
_resting: ContextVar[set[str] | None] = ContextVar("bili_backup_rest", default=None)
_failed_exit: ContextVar[set[str] | None] = ContextVar("bili_failed_exit", default=None)


class BlankPlan:
    """Accounts whose last app read was empty. The next poll reads the visitor list."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = BLANK_PATH if path is None else path
        self._rows: set[str] | None = None

    def due(self, account: str) -> bool:
        return account in self._load()

    def note(self, account: str) -> None:
        rows = self._load()
        if account in rows:
            return
        rows.add(account)
        self._save()

    def clear(self, account: str) -> None:
        rows = self._load()
        if account not in rows:
            return
        rows.remove(account)
        self._save()

    def _load(self) -> set[str]:
        if self._rows is None:
            raw = read_json(self.path).get("accounts")
            self._rows = {str(account) for account in raw} if isinstance(raw, list) else set()
        return self._rows

    def _save(self) -> None:
        write_json(self.path, {"accounts": sorted(self._rows or ())})


cap = ProxyRest(SIT_OUT_PATH)
blanks = BlankPlan()


def _proxy_bag(var: ContextVar[set[str] | None]):
    """A proxy set for this poll. Outside a poll the set is missing, so a check still reads."""

    @contextmanager
    def enter(proxies: Collection[str]) -> Iterator[None]:
        token = var.set(set(proxies))
        try:
            yield
        finally:
            var.reset(token)

    def add(proxy: str) -> None:
        current = var.get()
        if current is not None:
            current.add(proxy)

    def has(proxy: str) -> bool:
        current = var.get()
        return current is not None and proxy in current

    return enter, add, has


resting_proxies, rest_backup, backup_resting = _proxy_bag(_resting)
failed_exits, note_failed_exit, exit_failed = _proxy_bag(_failed_exit)


def halt_round(poll: Poll, account: str, exc: BaseException, *, proxy: str | None = None) -> None:
    """Stop the rest of this proxy. HTTP 412 also sits it out of the next poll."""
    proxy = proxy_key()
    poll.bili_stopped.add(proxy)
    rest_backup(proxy)
    rested = bili_blocked(exc)
    if rested:
        cap.rest(proxy)
    log.warning(
        "Stopping the rest of this Bilibili proxy (%s) after %s%s",
        proxy_label(proxy),
        account,
        "; it rests the next poll" if rested else "",
    )


def current_slot() -> int:
    """Which pass this account is in. Zero outside a poll pass."""
    return dynamics._slot.get()


@contextmanager
def polling(
    proxy: str, *, slot: int, mode: str = "app", phone: int = 0
) -> Iterator[dynamics.PollAsk]:
    """Bind this account's proxy, phone, and the source chosen for this read."""
    ask = dynamics.PollAsk()
    # Reset is the reverse of bind, so a failure still drops the last token first.
    tokens = (
        (reset_proxy, bind_proxy(proxy)),
        (reset_phone, bind_phone(phone)),
        (dynamics._mode.reset, dynamics._mode.set(mode)),
        (dynamics._slot.reset, dynamics._slot.set(slot)),
        (dynamics._ask.reset, dynamics._ask.set(ask)),
    )
    try:
        yield ask
    finally:
        for reset, token in reversed(tokens):
            reset(token)


@dataclass(frozen=True)
class Phone:
    """One phone on a proxy and the accounts it asks this poll, first account first."""

    proxy: str
    accounts: tuple[str, ...]
    index: int = 0


def configured_proxies() -> tuple[str, ...]:
    """The proxy list. Empty means one direct connection."""
    return tuple(settings.bili_proxies) or ("",)


def backup_proxy() -> str:
    """The one proxy a saved login uses. Always the first configured proxy."""
    return configured_proxies()[0]


def plan(
    accounts: Sequence[str],
    proxies: Sequence[str],
    turn: int,
    sitting: Sequence[str],
    *,
    phones_per_proxy: int | None = None,
    accounts_per_phone: int | None = None,
) -> tuple[list[Phone], tuple[str, ...]]:
    """Phones for this poll, and the sitting proxies this window spends.

    A sitting proxy the window does not reach is left for a later poll.
    Groups that do not fit are added to phones still in this poll.
    """
    groups, ring = _groups_and_ring(
        accounts, proxies, turn, phones_per_proxy, accounts_per_phone
    )
    sitting = set(sitting)
    chosen: list[tuple[str, int]] = []
    spent: list[str] = []
    for proxy, index in ring:
        if len(chosen) == len(groups):
            break
        if proxy in sitting:
            if proxy not in spent:
                spent.append(proxy)
            continue
        chosen.append((proxy, index))
    phones = [Phone(proxy, group, index) for (proxy, index), group in zip(chosen, groups)]
    if phones:
        for offset, group in enumerate(groups[len(phones) :]):
            at = offset % len(phones)
            phone = phones[at]
            phones[at] = Phone(phone.proxy, phone.accounts + group, phone.index)
    return phones, tuple(spent)


def _groups_and_ring(
    accounts: Sequence[str], proxies: Sequence[str], turn: int,
    phones_per_proxy: int | None = None, accounts_per_phone: int | None = None,
) -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[str, int], ...]]:
    per_proxy = settings.bili_phones_per_proxy if phones_per_proxy is None else max(1, phones_per_proxy)
    per_phone = (
        settings.bili_accounts_per_phone if accounts_per_phone is None else max(1, accounts_per_phone)
    )
    groups = tuple(
        _rotate(group, turn)
        for group in group_accounts(accounts, len(proxies) * per_proxy, per_phone)
    )
    return groups, _ring(proxies, per_proxy, len(groups), turn)


def _ring(
    proxies: Sequence[str], per_proxy: int, group_count: int, turn: int
) -> tuple[tuple[str, int], ...]:
    """Window order. A later phone joins only once every first phone is full."""
    slots = _slots(proxies, per_proxy)
    if not slots or group_count <= 0:
        return ()
    layers = min(per_proxy, (group_count + len(proxies) - 1) // len(proxies))
    start = turn % (layers * len(proxies))
    return slots[start:] + slots[:start]


def _slots(proxies: Sequence[str], phones_per_proxy: int) -> tuple[tuple[str, int], ...]:
    # First phone on every proxy, then a second phone, and so on.
    return tuple((proxy, index) for index in range(phones_per_proxy) for proxy in proxies)


def _rotate(group: tuple[str, ...], turn: int) -> tuple[str, ...]:
    if len(group) < 2:
        return group
    shift = turn % len(group)
    return group if shift == 0 else group[shift:] + group[:shift]


def roster_key(roster: tuple) -> str:
    """16 hex chars over the roster. Proxy strings are not written out."""
    payload = json.dumps(roster, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class Rotation:
    """The window counter. A restart continues it. A new roster starts at 0."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = ROTATION_PATH if path is None else path

    def load(self, key: str) -> int:
        """The saved turn for `key`. 0 when the file is missing, bad, or for another roster."""
        raw = read_json(self.path)
        if raw.get("roster") != key:
            return 0
        turn = raw.get("turn")
        if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
            return 0
        return turn

    def save(self, turn: int, key: str) -> None:
        write_json(self.path, {"turn": turn, "roster": key})


rotation = Rotation()


@dataclass
class BiliState:
    """What one poll leaves for the next: the roster, its window turn, and the exits seen."""

    roster: BiliRoster | None = None
    turn: int = 0
    exits: dict[str, str] = field(default_factory=dict)


@dataclass
class _Run:
    poll: Poll
    state: BiliState
    accounts: list[str]
    live: dict[SourceKey, list[GuildTopic]]
    now_wall: datetime
    only: set[SourceKey] | None
    backfill_for: set[str]
    gap: float
    jitter: float
    rng: random.Random
    posted: int = 0
    fetched: bool = False

    async def pause(self, due: bool) -> None:
        if due:
            await pause_between(self.gap, self.jitter, self.rng)

    def skipped(self, proxy: str) -> bool:
        """A 412 stopped this proxy, or it is resting from the website backup."""
        return proxy in self.poll.bili_stopped or backup_resting(proxy)

    def due(self, account: str) -> bool:
        return is_due(self.poll, ("bilibili", account), self.only)

    async def fetch(
        self, account: str, proxy: str, slot: int, phone: int, *, mode: str,
    ) -> dynamics.PollAsk:
        source: SourceKey = ("bilibili", account)
        backfill = TICKET_BACKFILL if account in self.backfill_for else None
        with polling(proxy, slot=slot, mode=mode, phone=phone) as ask:
            self.posted += await poll_source(
                self.poll, source, self.live[source], self.now_wall, backfill=backfill
            )
        self.fetched = True
        return ask


async def poll_bili(
    poll: Poll,
    state: BiliState,
    accounts: list[str],
    live: dict[SourceKey, list[GuildTopic]],
    now_wall: datetime,
    only: set[SourceKey] | None,
    *,
    backfill_for: set[str],
    gap: float,
    jitter: float,
    rng: random.Random,
) -> tuple[int, bool]:
    """Ask in passes. Each account has one source. A 412 rests that proxy next poll.

    A failed or shared exit is left out and the window is planned again. A
    moved exit gets new phones. A resting proxy skips its visitor list and,
    when it is the first proxy, the login page. The first-try line is logged
    when this poll finishes.
    """
    if not accounts:
        return 0, False
    run = _Run(poll, state, accounts, live, now_wall, only, backfill_for, gap, jitter, rng)
    with dynamics.space_stats.round() as stats:
        posted, fetched = await _ask_passes(run)
    if line := stats.flush():
        log.info("%s", line)
    return posted, fetched


async def _ask_passes(run: _Run) -> tuple[int, bool]:
    proxies = configured_proxies()
    accounts, state = run.accounts, run.state
    current: BiliRoster = (
        tuple(accounts), proxies, settings.bili_phones_per_proxy, settings.bili_accounts_per_phone
    )
    key = roster_key(current)
    if current != state.roster:
        state.roster, state.turn = current, rotation.load(key)
    asked = state.turn
    sitting = [proxy for proxy in proxies if cap.sits_out(proxy)]
    with resting_proxies(sitting):
        phones, spent, pool, state.exits = await vetted_window(
            accounts, proxies, asked, sitting, plan=plan, platform="bilibili",
            refresh=fresh_phones, refresh_note="fresh phone ids",
        )
        for proxy in spent:
            if cap.take(proxy):
                log.info("Bilibili %s sits out this round after HTTP 412", proxy_label(proxy))
        run.poll.bili_stopped.clear()
        if phones:
            with failed_exits([proxy for proxy in proxies if proxy not in pool]):
                if _use_login(run):
                    await _login_passes(run, phones)
                else:
                    await _app_passes(run, phones)
            # A crash before this leaves the same window for the next poll.
            rotation.save(asked + 1, key)
            state.turn = asked + 1
        else:
            hold_tickets(run.poll, "bilibili", accounts, run.backfill_for, run.only)
    return run.posted, run.fetched


def _use_login(run: _Run) -> bool:
    """Login reads the first proxy. A skipped or failed first proxy uses the app passes."""
    proxy = backup_proxy()
    return _login_saved() and not run.skipped(proxy) and not exit_failed(proxy)


def _login_saved() -> bool:
    from news.source.post.bilibili import is_bili_login_cookie

    return is_bili_login_cookie(settings.bili_cookie or "")


async def _login_passes(run: _Run, phones: list[Phone]) -> None:
    """The recent page, on the first proxy, for every account this poll asks."""
    proxy = backup_proxy()
    if run.skipped(proxy) or exit_failed(proxy):
        log.info("Bilibili login read skipped: %s is not in this poll", proxy_label(proxy))
        hold_tickets(
            run.poll, "bilibili",
            [account for phone in phones for account in phone.accounts],
            run.backfill_for, run.only,
        )
        return
    asked = False
    for phone in phones:
        for slot, account in enumerate(phone.accounts):
            if run.skipped(proxy):
                hold_tickets(run.poll, "bilibili", [account], run.backfill_for, run.only)
                continue
            if not run.due(account):
                continue
            await run.pause(asked)
            asked = True
            await run.fetch(account, proxy, slot, 0, mode="login")


async def _app_passes(run: _Run, phones: list[Phone]) -> None:
    """The app phone, or the visitor list when the last app read was empty."""
    for slot in range(max(len(phone.accounts) for phone in phones)):
        asked_one = False
        for phone in phones:
            if slot >= len(phone.accounts):
                continue
            account = phone.accounts[slot]
            visitor = blanks.due(account)
            if run.skipped(phone.proxy) or (visitor and exit_failed(phone.proxy)):
                hold_tickets(run.poll, "bilibili", [account], run.backfill_for, run.only)
                continue
            if not run.due(account):
                continue
            await run.pause(asked_one)
            asked_one = True
            if visitor:
                await run.fetch(account, phone.proxy, slot, phone.index, mode="visitor")
                blanks.clear(account)
                continue
            ask = await run.fetch(account, phone.proxy, slot, phone.index, mode="app")
            if ask.blank and not run.skipped(phone.proxy):
                blanks.note(account)
