from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import commands

from ai import AI, AIError
from fetch import FetchedItem, FetchError
from news.drafts import AiCache
from news.feeds import FeedSnapshot, Fetcher, PullResult
from news.follows import FEATURE_GAME, FOLLOWS_KEY, add_news_follow
from news.rounds import Poll
from news.source.match.thesportsdb.matchstate import (
    _CUP_BY_LEAGUE,
    MatchState,
    cup_scan_days,
    day_scan_labels,
    scan_days,
)
from news.source.match.thesportsdb.clubs import sportsdb_url
from news.source.post.bilibili import BiliCookies
from news.source.post.bilibili.round import BiliState, poll_bili
from news.source.post.weibo.round import ProxyPool, poll_damai, poll_weibo
from news.source.post.weibo.ticket import DamaiWatch
from news.source.registry import is_hard_skip

GUILD_ID = 1
NOTIFY_CHANNEL_ID = 99
ACCOUNT = "1265652806123"
OTHER = "2"
FILTER_KEEP = '{"type":"ops","facts":{"down":"5 hours","how":"Predownload"}}'
FILTER_DROP = '{"type":"drop","facts":{}}'
WRITE_JSON = '{"title": "AI title", "summary": "AI body"}'
COOKIE = FetchError("code -352 from api")


class FakeClock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def utc(*parts) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


def fake_guild(guild_id: int = 1) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.get_channel.return_value = None
    guild.system_channel = None
    guild.text_channels = []
    guild.me = None
    guild.voice_channels = []
    guild.stage_channels = []
    guild.afk_channel = None
    guild.fetch_channel = AsyncMock(
        side_effect=discord.NotFound(MagicMock(), {"message": "Unknown Channel"})
    )
    return guild


def fake_member(name: str = "Nya", user_id: int = 1, *, bot: bool = False) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.display_avatar.url = "https://example.test/avatar.png"
    member.bot = bot
    member.__str__.return_value = name
    return member


def fake_voice_channel(
    channel_id: int = 10,
    members: list | None = None,
    guild: MagicMock | None = None,
) -> MagicMock:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = channel_id
    channel.members = members if members is not None else []
    channel.guild = fake_guild() if guild is None else guild
    channel.mention = f"<#{channel_id}>"
    channel.__str__.return_value = f"voice-{channel_id}"
    return channel


def fake_voice_state(channel=None) -> MagicMock:
    state = MagicMock(spec=discord.VoiceState)
    state.channel = channel
    return state


def fake_text_channel(
    channel_id: int = 99,
    guild: MagicMock | None = None,
) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.guild = fake_guild() if guild is None else guild
    channel.mention = f"<#{channel_id}>"
    posted = MagicMock()
    posted.id = channel_id * 1000
    posted.channel = channel
    posted.edit = AsyncMock()
    posted.delete = AsyncMock()
    posted.fetch = AsyncMock(return_value=posted)
    channel.send = AsyncMock(return_value=posted)
    channel.get_partial_message = MagicMock(return_value=posted)
    return channel


def fake_context(guild: MagicMock | None = None) -> MagicMock:
    posted = MagicMock()
    posted.delete = AsyncMock()
    posted.edit = AsyncMock()
    ctx = MagicMock(spec=commands.Context)
    ctx.send = AsyncMock(return_value=posted)
    ctx.guild = guild
    ctx.invoked_subcommand = None
    ctx.interaction = None
    return ctx


def fake_bot(get_channel_result=None) -> MagicMock:
    bot = MagicMock(spec=commands.Bot)
    bot.get_channel.return_value = get_channel_result
    bot.guilds = []
    bot.is_ready.return_value = False
    bot.extensions = {}
    bot.load_extension = AsyncMock()
    bot.unload_extension = AsyncMock()
    bot.reload_extension = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    bot.fetch_channel = AsyncMock(return_value=get_channel_result)
    bot.owner_id = None
    bot.owner_ids = set()
    app = MagicMock()
    app.owner = None
    app.team = None
    bot.application_info = AsyncMock(return_value=app)
    bot.fetch_user = AsyncMock()
    return bot


def reply_text(ctx) -> str:
    return ctx.send.await_args.args[0]


def embed_fields(embed) -> dict[str, str]:
    return {field.name: field.value for field in embed.fields}


def bot_guilds(cog, *guilds) -> None:
    found = {guild.id: guild for guild in guilds}
    cog.bot.get_guild = MagicMock(side_effect=found.get)


def map_channels(guild, *channels) -> None:
    found = {channel.id: channel for channel in channels}
    guild.get_channel.side_effect = found.get


def announce_guild(store, guild_id: int, channel_id: int):
    guild = fake_guild(guild_id)
    channel = fake_text_channel(channel_id, guild=guild)
    guild.get_channel.return_value = channel
    store.set_announce_channel_id(guild_id, channel.id)
    return guild, channel


def set_follows(store, guild_id: int, *rows) -> None:
    store.set_guild_list(guild_id, FOLLOWS_KEY, [])
    for row in rows:
        add_news_follow(store, guild_id, *row)


def fetched_item(item_id: str = "guid-1", **overrides) -> FetchedItem:
    data = dict(
        item_id=item_id,
        title="Patch notes",
        text="Hello world. Predownload 5 hours.",
        url="https://example.test/patch",
        image_url="https://example.test/cover.png",
        platform="bilibili",
        account=ACCOUNT,
    )
    data.update(overrides)
    return FetchedItem(**data)


def soccer_match(
    stage: str,
    *,
    item_id: str | None = None,
    notes: str = "Saka 12'",
    **overrides,
):
    item_id = item_id or f"10:{stage}"
    if stage == "next":
        title = "Arsenal vs Chelsea"
        text = (
            "**Game Status** | Not started\n**Home** | Arsenal\n"
            "**League** | Soccer\n\n**Details**\nKickoff soon"
        )
    elif stage == "start":
        title = "Arsenal vs Chelsea"
        text = (
            "**Game Status** | On\n**Home** | Arsenal\n"
            "**League** | Premier League\nGame Phase | 1st half"
        )
    else:
        title = "Arsenal 2–1 Chelsea"
        text = (
            "**Game Status** | Over\n**Home** | Arsenal\n"
            f"**League** | Premier League\n\n**Details**\n{notes}"
        )
    data = dict(
        url="https://example.test/match",
        platform="thesportsdb",
        account="133604",
        source_name="Arsenal",
        image_url=None,
        title=title,
        text=text,
        tags=("match", stage, "soccer"),
    )
    data.update(overrides)
    return fetched_item(item_id, **data)


def match_state(match_id: str = "1", **overrides) -> MatchState:
    data = dict(
        match_id=match_id,
        sport="Soccer",
        home="Arsenal",
        away="Chelsea",
        phase="scheduled",
    )
    data.update(overrides)
    return MatchState(**data)


def sportsdb_team(
    team_id: str,
    name: str,
    *,
    sport: str = "Soccer",
    league: str | None = None,
    league_id: str | None = None,
) -> dict:
    row = {"idTeam": str(team_id), "strTeam": name, "strSport": sport}
    if league:
        row["strLeague"] = league
    if league_id:
        row["idLeague"] = league_id
    return row


def sportsdb_event(
    event_id: str | int,
    home: str,
    away: str,
    *,
    sport: str = "Soccer",
    status: str = "NS",
    stamp: datetime | str | None = None,
    league: str | None = None,
    league_id: str | None = None,
    score: tuple[str, str] | None = None,
    **extra,
) -> dict:
    row = {
        "idEvent": str(event_id),
        "strHomeTeam": home,
        "strAwayTeam": away,
        "strSport": sport,
        "strStatus": status,
    }
    if stamp is not None:
        row["strTimestamp"] = stamp if isinstance(stamp, str) else stamp.isoformat()
    if league:
        row["strLeague"] = league
    if league_id:
        row["idLeague"] = league_id
    if score:
        row["intHomeScore"], row["intAwayScore"] = score
    row.update(extra)
    return row


def sportsdb_bodies(
    team: dict,
    events: list | None = None,
    results: list | None = None,
    *,
    now: datetime | None = None,
    day_events: dict | None = None,
) -> dict[str, str]:
    team_id = team["idTeam"]
    bodies = {
        sportsdb_url("lookupteam.php", {"id": team_id}): json.dumps({"teams": [team]}),
        sportsdb_url("eventsnext.php", {"id": team_id}): json.dumps(
            {"events": events or []}
        ),
        sportsdb_url("eventslast.php", {"id": team_id}): json.dumps(
            {"results": results or []}
        ),
    }
    if (team.get("strSport") or "").casefold() != "soccer":
        return bodies
    extra = day_events or {}
    stamp = now or utc(2026, 9, 5, 12)
    league_id = str(team.get("idLeague") or "")
    labels_by_day = {day: list(day_scan_labels(league_id)) for day in scan_days(stamp)}
    cup = _CUP_BY_LEAGUE.get(league_id.strip(), "")
    if cup:
        for day in cup_scan_days(stamp):
            labels = labels_by_day.setdefault(day, [])
            if cup not in labels:
                labels.append(cup)
    for day, labels in labels_by_day.items():
        for label in labels:
            bodies[sportsdb_url("eventsday.php", {"d": day, "l": label})] = json.dumps(
                {"events": extra.get((day, label)) or []}
            )
    return bodies


def _espn_side(place: str, name: str, score: str | None = None) -> dict:
    row = {"homeAway": place, "team": {"displayName": name}}
    if score is not None:
        row["score"] = score
    return row


def espn_board(href: str, home: str, away: str) -> str:
    return json.dumps(
        {
            "events": [
                {
                    "links": [{"href": href}],
                    "competitions": [
                        {
                            "competitors": [
                                _espn_side("home", home),
                                _espn_side("away", away),
                            ]
                        }
                    ],
                }
            ]
        }
    )


def espn_summary(
    href: str, home: str, away: str, home_score: str, away_score: str, *notes: str
) -> str:
    return json.dumps(
        {
            "header": {
                "links": [{"href": href}],
                "competitions": [
                    {
                        "competitors": [
                            _espn_side("home", home, home_score),
                            _espn_side("away", away, away_score),
                        ],
                        "status": {"type": {"state": "post"}},
                    }
                ],
            },
            "scoringPlays": [{"text": note} for note in notes],
        }
    )


class FakeHttp:
    """In-memory GET. Tests never open a real URL."""

    def __init__(self, bodies: dict[str, str]) -> None:
        self.bodies = bodies
        self.urls: list[str] = []

    async def __call__(self, url: str, _headers: dict[str, str] | None) -> str:
        self.urls.append(url)
        if url not in self.bodies:
            raise FetchError(f"unexpected url {url}")
        return self.bodies[url]


def _as_fetch_error(exc: BaseException) -> FetchError:
    return exc if isinstance(exc, FetchError) else FetchError(str(exc))


class _NoLine:
    """No Bilibili catch-up line saved."""

    def catch_line(self, _source: str, _account: str) -> tuple[None, None]:
        return None, None


class ScriptedFetcher:
    """Stand-in Fetcher for cog tests. No HTTP."""

    def __init__(
        self,
        latest: list[FetchedItem] | None = None,
        new: list[FetchedItem] | None = None,
        source_name: str | None = None,
        *,
        loaded: list[FetchedItem] | None = None,
        kickoffs=(),
        team_id: str = "",
        pull_error: BaseException | None = None,
        pull_error_for: str | None = None,
        fetch_error: BaseException | None = None,
        blank_on: Callable[[str, str], bool] | None = None,
        error_on: Callable[[str, str], BaseException | None] | None = None,
    ) -> None:
        self.latest = latest or []
        self.new = new or []
        self.source_name = source_name
        self.pull_error = pull_error
        self.pull_error_for = pull_error_for
        self.fetch_error = fetch_error
        self.blank_on = blank_on
        self.error_on = error_on
        self.fetches: list[tuple[str, str]] = []
        self.asks: list[tuple[str, str, str]] = []
        self.seen_calls: list[tuple[str, str, list[str]]] = []
        self._delivered: dict[tuple[str, str, str], set[str]] = {}
        self.sport: str | None = None
        self.certification = ""
        self.bio = ""
        self.kickoffs = kickoffs
        self.team_id = team_id
        self.starts: list[FetchedItem] = []
        self.loaded = loaded
        self.bookmarks = _NoLine()
        self.words: dict[tuple[str, str, str], str] = {}

    def _clock(self) -> datetime:
        return datetime.now(timezone.utc)

    async def bili_call(self, _method, url, **_kwargs):
        raise AssertionError(f"cog tests must not call Bilibili: {url}")

    def _kickoffs(self) -> tuple:
        kickoffs = self.kickoffs
        return kickoffs if isinstance(kickoffs, tuple) else ((kickoffs,) if kickoffs else ())

    def _source(self) -> str | None:
        if self.source_name:
            return self.source_name
        for item in (*self.new, *self.latest, *(self.loaded or ())):
            if item.source_name:
                return item.source_name
        return None

    async def pull(self, platform: str, account: str, **_kwargs) -> PullResult:
        self.fetches.append((platform, account))
        ask = None
        mode = ""
        proxy = ""
        if platform == "bilibili":
            from news.source.post.bilibili.app import proxy_key
            from news.source.post.bilibili.dynamics import _ask, _mode

            ask = _ask.get()
            mode = _mode.get()
            proxy = proxy_key()
            self.asks.append((account, proxy, mode))
        if self.pull_error is not None and (
            self.pull_error_for is None or account == self.pull_error_for
        ):
            raise _as_fetch_error(self.pull_error)
        if self.error_on is not None and platform == "bilibili":
            exc = self.error_on(account, proxy)
            if exc is not None:
                raise _as_fetch_error(exc)
        if (
            ask is not None
            and mode == "app"
            and self.blank_on is not None
            and self.blank_on(account, proxy)
        ):
            ask.blank = True
            return PullResult(self._source(), [], [])
        if ask is not None and mode == "app":
            ask.posts = True
        items = list(self.new)
        visible = list(self.loaded) if self.loaded is not None else list(items)
        got = PullResult(
            self._source(),
            items,
            visible,
            list(self.starts),
            self.sport,
            self._kickoffs(),
            self.team_id,
            items=visible,
        )
        from news.feeds import _reopen_edits

        saved = {
            item_id: fingerprint
            for (plat, acct, item_id), fingerprint in self.words.items()
            if plat == platform and acct == (self.team_id or account)
        }
        return _reopen_edits(saved, got)

    async def load(
        self,
        platform: str,
        account: str,
        *,
        limit: int = 3,
        include_hard_skip: bool = False,
    ) -> FeedSnapshot:
        self.fetches.append((platform, account))
        if self.fetch_error is not None:
            raise self.fetch_error
        items = list(self.latest)
        if not include_hard_skip:
            items = [item for item in items if not is_hard_skip(item)]
        return FeedSnapshot(
            self._source(),
            items[:limit],
            self.sport,
            self._kickoffs(),
            self.team_id,
            certification=self.certification,
            bio=self.bio,
        )

    def mark_seen(self, platform: str, account: str, ids) -> None:
        self.seen_calls.append((platform, account, list(ids)))

    def note_words(self, platform: str, account: str, item_id: str, fingerprint: str) -> None:
        self.words[(platform, account, item_id)] = fingerprint

    def delivered_refs(self, platform: str, account: str, item_id: str) -> set[str]:
        return set(self._delivered.get((platform, account, item_id), set()))

    def delivered_item_ids(self, platform: str, account: str) -> set[str]:
        return {
            item_id
            for plat, acct, item_id in self._delivered
            if plat == platform and acct == account
        }

    def stored_shows(self, platform: str, account: str, ref: str) -> set[str]:
        return Fetcher.stored_shows(self, platform, account, ref)

    def mark_delivered(self, platform: str, account: str, item_id: str, refs) -> None:
        key = (platform, account, item_id)
        self._delivered.setdefault(key, set()).update(str(ref) for ref in refs)

    def prune_unused(self, _keep) -> int:
        return 0

    def fail_then(self, error: BaseException, times: int = 1):
        inner = self.pull
        left = times

        async def pull(platform: str, account: str, **kwargs):
            nonlocal left
            if left > 0:
                left -= 1
                self.fetches.append((platform, account))
                raise _as_fetch_error(error)
            return await inner(platform, account, **kwargs)

        self.pull = pull
        return self

def script_fetcher(cog, **kwargs) -> ScriptedFetcher:
    cog.fetcher = ScriptedFetcher(**kwargs)
    return cog.fetcher


async def poll_news(cog, *guilds, poll_post=None, poll_match=None, **fetcher):
    if guilds:
        bot_guilds(cog, *guilds)
    if fetcher:
        script_fetcher(cog, **fetcher)
    return await cog.poll_once(poll_post=poll_post, poll_match=poll_match)


class ScriptedAI:
    """Stand-in AI for cog tests. No HTTP; replies are queued in order."""

    def __init__(
        self,
        replies: list[str] | None = None,
        *,
        available: bool = True,
    ) -> None:
        self.replies = list(replies or [])
        self.calls: list[list[dict[str, str]]] = []
        self._available = available

    def available(self) -> bool:
        return self._available

    async def complete(self, messages, **_options) -> str:
        if not self.available():
            raise AIError("AI is not configured.")
        self.calls.append([dict(message) for message in messages])
        if not self.replies:
            raise AIError("no scripted reply")
        return self.replies.pop(0)


class RoundRig:
    """What a cog hands one poll, without the cog. Gaps default to none."""

    def __init__(self, guild_config) -> None:
        self.fetcher = ScriptedFetcher()
        self.bili_state = BiliState()
        self.rng = random.Random(0)
        self.poll = Poll(
            fetcher=self.fetcher, fetch_lock=asyncio.Lock(), bili=BiliCookies(),
            guild_config=guild_config, damai=DamaiWatch(), ai=AI(),
            ai_lock=asyncio.Lock(), ai_on=False, skip_until={}, seen_live=set(),
            bili_stopped=set(), weibo_stopped=set(), weibo_quiet=set(),
            announce=AsyncMock(return_value=0), save_cookie=AsyncMock(),
            warn_cookie=AsyncMock(),
        )

    def script(self, **kwargs) -> ScriptedFetcher:
        self.fetcher = self.poll.fetcher = ScriptedFetcher(**kwargs)
        return self.fetcher

    def live(self, platform: str, accounts, topic: str = FEATURE_GAME) -> dict:
        return {(platform, account): [(GUILD_ID, topic)] for account in accounts}

    async def bili(self, *accounts, only=None, tickets=(), gap=0.0, jitter=0.0):
        try:
            return await poll_bili(
                self.poll, self.bili_state, list(accounts), self.live("bilibili", accounts),
                datetime.now(timezone.utc), only, backfill_for=set(tickets),
                gap=gap, jitter=jitter, rng=self.rng,
            )
        finally:
            await self.poll.bili.end(self.fetcher)

    async def weibo(
        self, *accounts, only=None, tickets=(), pool=None, post_gap=0.0, ticket_gap=0.0,
        jitter=0.0,
    ):
        return await poll_weibo(
            self.poll, list(accounts), list(accounts), self.live("weibo", accounts),
            datetime.now(timezone.utc), only, backfill_for=set(tickets),
            pool=pool if pool is not None else ProxyPool(), post_gap=post_gap,
            ticket_gap=ticket_gap, jitter=jitter, rng=self.rng,
        )

    async def damai(self, *, gap=0.0, pool=None, send=None):
        return await poll_damai(
            self.poll, gap=gap, pool=pool if pool is not None else ProxyPool(),
            filter_cache=AiCache(), write_cache=AiCache(),
            send=send if send is not None else AsyncMock(return_value=True),
        )


def scratch(base: Path, name: str) -> Path:
    """A clean sub-directory for one scenario of a test that runs several."""
    path = base / name
    path.mkdir()
    return path
