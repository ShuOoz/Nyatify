"""Hidden `/news` commands: check and cookie."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands

from announce import (
    ACCENT_COLOR,
    command_error_message,
    defer_response,
    notice_text,
    owner_only,
    respond,
    unwrap_command_error,
)
from config import CONFIG_PATH, settings, upsert_config_values
from fetch import FetchError
from news.cards import news_card
from news.command.user import UserCommands, _guild_id, _say, news
from news.drafts import card_fields, filter_kept, keep_post
from news.follows import (
    FEATURE_TICKET,
    TOPIC_UNASSIGNED,
    block_live_room,
    follow_display_name,
    follow_shown_name,
    remember_source,
    remember_ticket_artist,
    release_blank_ticket,
)
from news.source.post.bilibili import (
    VISITOR_COOKIE_UID,
    bili_cookie_uid,
    cookie_effective as bili_cookie_effective,
    is_bili_login_cookie,
)
from news.source.post.bilibili.app import stored_buvid
from news.source.post.weibo import (
    cookie_effective as weibo_cookie_effective,
    is_weibo_visitor_cookie,
)
from news.source.post.weibo.ticket import DAMAI_WEIBO, TicketDraft, one_artist, simplified
from news.source.registry import (
    FETCH_PLATFORMS,
    PLATFORM_LABELS,
    apply_news_cookie,
    cookie_hint,
    normalize_fetch_platform,
    pick_check,
    previewable,
    shows_post,
    uses_ai,
)

CHECK_SCAN = 15
_COOKIE_SITES = frozenset({"bilibili", "weibo"})
_EFFECT = {True: "effective", False: "not effective"}
_COOKIE_CHOICES = [
    app_commands.Choice(name=PLATFORM_LABELS[name], value=name)
    for name in FETCH_PLATFORMS
    if name in _COOKIE_SITES
]


def _post_time(item: Any) -> float:
    """Newest posts sort first. A missing clock sorts last."""
    at = getattr(item, "occurred_at", None)
    if at is None:
        return float("-inf")
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.timestamp()


def _show_picks(shown: Any, sale: Any) -> list[tuple[Any, bool]]:
    """Newer sale first. The other sale is the backup when that card cannot be written."""
    if sale is not None and _later_than(sale, shown):
        pair = [(sale, True), (shown, False)]
    else:
        pair = [(shown, False), (sale, True)]
    return [(item, damai) for item, damai in pair if item is not None]


def _later_than(sale: Any, shown: Any) -> bool:
    """True when the Damai sale is the post to send."""
    if shown is None:
        return True
    sale_at = getattr(sale, "occurred_at", None)
    shown_at = getattr(shown, "occurred_at", None)
    return sale_at is not None and (shown_at is None or sale_at > shown_at)


def _usable(items, topic: str) -> list:
    """Posts this topic could show: previewable, visible here, and not a live room."""
    return [
        item for item in items
        if previewable(item) and shows_post(item, topic) and not block_live_room(item, topic)
    ]


async def cookie_status_embed() -> discord.Embed:
    """Saved cookies. Looking does not mint a phone id. Values stay in config."""
    embed = discord.Embed(title="Cookie status", color=ACCENT_COLOR)
    for name, row in (
        ("Bilibili", await _bili_cookie_status()),
        ("Weibo", await _weibo_cookie_status()),
    ):
        kind, status = row
        embed.add_field(name=name, value=kind, inline=True)
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
    return embed


def _with_phone(kind: str, status: str) -> tuple[str, str]:
    """Phone id sits under the Bilibili login row. A blank line keeps it aligned."""
    phone = "saved" if stored_buvid() else "missing"
    gap = "\n\u200b" * kind.count("\n")
    return f"{kind}\nPhone id", f"{status}{gap}\n{phone}"


async def _bili_cookie_status() -> tuple[str, str]:
    cookie = (settings.bili_cookie or "").strip()
    if not cookie:
        return _with_phone("not set", "not set")
    if not is_bili_login_cookie(cookie):
        return _with_phone("not a login", "not used")
    uid = (settings.bili_cookie_uid or "").strip()
    if uid in {"", VISITOR_COOKIE_UID}:
        uid = bili_cookie_uid(cookie)
    line = "Login" if uid in {"", VISITOR_COOKIE_UID} else f"Login · `{uid}`"
    if not (settings.bili_ac_time or "").strip():
        line = f"{line}\nno ac_time"
    return _with_phone(line, _EFFECT.get(await bili_cookie_effective(cookie), "unknown"))


async def _weibo_cookie_status() -> tuple[str, str]:
    cookie = (settings.weibo_cookie or "").strip()
    if not cookie:
        return "not set", "not set"
    kind = "Guest" if is_weibo_visitor_cookie(cookie) else "Login"
    return kind, _EFFECT.get(await weibo_cookie_effective(cookie), "unknown")


class TestCommands:
    """`/news check` and `/news cookie`. Hidden until `/admin` shows them."""

    @news.command(
        name="check",
        description="Test preview of a recent post or latest match",
    )
    @app_commands.describe(
        account="Followed account to preview (name or uid). This is not a cookie uid",
    )
    async def news_check(self, interaction: discord.Interaction, account: str) -> None:
        follow = await self._require_follow(interaction, account, ephemeral=True)
        if follow is None:
            return
        if follow["topic"] == TOPIC_UNASSIGNED:
            await _say(interaction, "Pick a topic. `/news feature`.", ephemeral=True)
            return
        await defer_response(interaction, ephemeral=True)
        if follow["topic"] == FEATURE_TICKET:
            content, embed, files = await self._ticket_check(follow, guild_id=_guild_id(interaction))
        else:
            content, embed, files = await self._check_preview(follow, guild_id=_guild_id(interaction))
        if isinstance(interaction, discord.Interaction) and interaction.response.is_done():
            await interaction.edit_original_response(
                content=content, embed=embed, attachments=list(files or [])
            )
            return
        await respond(interaction, content, embed=embed, ephemeral=True, files=files or None)

    @UserCommands.news_feature.autocomplete("account")
    @UserCommands.news_delete.autocomplete("account")
    @news_check.autocomplete("account")
    async def news_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._followed_account_choices(interaction, current)

    @news.command(
        name="cookie",
        description="Write a login cookie, or show status when empty (bot owner)",
    )
    @owner_only()
    @app_commands.describe(
        platform="Bilibili or Weibo. Leave every option empty to show status",
        login="Login Cookie header. Bilibili may include ac_time_value",
    )
    @app_commands.choices(platform=_COOKIE_CHOICES)
    async def news_cookie(
        self, interaction: discord.Interaction, platform: str | None = None,
        login: str | None = None,
    ) -> None:
        await defer_response(interaction, ephemeral=True)
        raw = (platform or "").strip()
        site = normalize_fetch_platform(raw) if raw else None
        if site not in _COOKIE_SITES:
            if raw or login is not None:
                await _say(interaction, "Pick Bilibili or Weibo.", ephemeral=True)
            else:
                await _say(
                    interaction, embed=await self._locked(cookie_status_embed()), ephemeral=True
                )
            return
        if login is None:
            await _say(interaction, "Paste a login cookie.", ephemeral=True)
            return
        bili = site == "bilibili"
        try:
            result = await self._locked(
                apply_news_cookie(
                    site, cookie=login,
                    fallback_cookie=settings.bili_cookie if bili else settings.weibo_cookie,
                    fallback_ac_time=settings.bili_ac_time if bili else None,
                )
            )
        except ValueError as exc:
            await _say(interaction, str(exc), ephemeral=True)
            return
        if result.get("changed", True):
            await self._save_cookie_result(result)
            await self._resume_cookie_feed(site)
        await _say(interaction, result["line"], ephemeral=True)

    async def _resume_cookie_feed(self, site: str) -> None:
        """A written cookie unparks that site and takes down its cookie warning."""
        self._skip_until = {key: until for key, until in self._skip_until.items() if key[0] != site}
        bili = getattr(self, "bili", None)
        if site == "bilibili" and bili is not None:
            bili.refreshed_until = 0.0
        prefix = f"cookie:{site}:"
        for notice in list(self.notices):
            if str(notice.subject).startswith(prefix):
                await self.notices.delete_now(notice)

    async def _save_cookie_result(self, result: dict[str, Any]) -> None:
        if result.get("config"):
            upsert_config_values(CONFIG_PATH, result["config"])
        updates = result.get("settings_update") or {}
        for attr, value in updates.items():
            setattr(settings, attr, value)
        fetcher = getattr(self, "fetcher", None)
        if fetcher is not None:
            for attr in ("bili_cookie", "weibo_cookie"):
                if attr in updates:
                    setattr(fetcher, attr, updates[attr] or None)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError,
    ) -> None:
        message = command_error_message(
            unwrap_command_error(error),
            owner="Only the bot owner can set the cookie.",
            permissions="Need Manage Server to change news follows.",
        )
        if message is None:
            raise error
        await _say(interaction, message, ephemeral=True)

    async def _scan_for_check(self, follow: dict[str, str]):
        platform, account = follow["platform"], follow["account"]
        try:
            loaded = await self._locked(
                self.fetcher.load(platform, account, limit=CHECK_SCAN, include_hard_skip=True)
            )
        except FetchError as exc:
            failed = notice_text("Fetch failed.", cookie_hint(platform, exc)), None, None
            return None, failed
        return loaded, None

    def _remember_check_source(self, follow: dict[str, str], loaded, guild_id: int | None) -> None:
        if guild_id is None:
            return
        remember_source(
            self.guild_config, {guild_id}, follow["platform"], follow["account"],
            next((item.source_name for item in loaded.items if item.source_name), None),
            datetime.now(timezone.utc), sport=loaded.sport, kickoffs=loaded.kickoffs,
            team_id=loaded.team_id or None, schedule=False,
        )

    async def _check_preview(
        self, follow: dict[str, str], *, guild_id: int | None = None
    ) -> tuple[str, discord.Embed | None, list[discord.File] | None]:
        topic = follow["topic"]
        loaded, failed = await self._scan_for_check(follow)
        if failed is not None:
            return failed
        self._remember_check_source(follow, loaded, guild_id)
        if not loaded.items:
            return notice_text("No posts right now."), None, None
        usable = _usable(loaded.items, topic)
        if not usable:
            return notice_text("Recent posts are skips."), None, None
        kept, fallback = await self._preview_item(usable, topic)
        item = kept or usable[0]
        title = summary = None
        if kept is not None:
            fields = await self._written(item, topic)
            if isinstance(fields, TicketDraft):
                plan = fields.plan(set())
                if plan is not None:
                    title, summary = plan.title, plan.summary
            elif isinstance(fields, tuple):
                title, summary = fields
        embed, files = await news_card(item, topic, title=title, summary=summary)
        name = follow_shown_name(follow)
        line = f"Dropped {name}." if kept is None and not fallback else f"News Preview ({name})"
        return notice_text(line), embed, files or None

    async def _ticket_check(self, follow: dict[str, str], *, guild_id: int):
        """One card: the newer sale from this account and from 大麦."""
        loaded, failed = await self._scan_for_check(follow)
        if failed is not None:
            return failed
        platform, account = follow["platform"], follow["account"]
        if not (follow.get("artist") or "").strip():
            display = (loaded.name or follow_display_name(follow)).strip()
            async with self._ai_lock:
                read = await one_artist(self.ai, display, loaded.certification, loaded.bio)
            if read.answered and read.name is None:
                release_blank_ticket(self.guild_config, guild_id, platform, account)
                return notice_text("No one artist on that account."), None, None
            if read.name is not None:
                remember_ticket_artist(
                    self.guild_config, [guild_id], platform, account, read.name.written
                )
                follow = {**follow, "artist": read.name.written}
        self._remember_check_source(follow, loaded, guild_id)
        usable = _usable(loaded.items, FEATURE_TICKET)
        shown = await self._kept_show(usable)
        sale = await self._damai_show(follow)
        picks = _show_picks(shown, sale)
        if not picks:
            if not usable:
                return notice_text("No posts right now."), None, None
            return notice_text(f"Dropped {follow_shown_name(follow)}."), None, None
        for item, damai in picks:
            card = await self._one_card(follow, item, damai=damai)
            if card is not None:
                return card
        return notice_text(f"Dropped {follow_shown_name(follow)}."), None, None

    async def _first_kept(self, items, topic: str):
        """First kept post in the given order. The flag is unsure, and only if none kept."""
        unsure = False
        for item in items:
            if not uses_ai(item):
                return item, False
            decision = await keep_post(
                self.ai, self._filter_cache, self._ai_lock, item, topic, remember=False
            )
            if filter_kept(decision) is True:
                return item, False
            unsure = unsure or decision is None
        return None, unsure

    async def _kept_show(self, items):
        """The newest post the ticket filter calls a show."""
        kept, _unsure = await self._first_kept(sorted(items, key=_post_time, reverse=True), FEATURE_TICKET)
        return kept

    async def _damai_show(self, follow: dict[str, str]):
        """The newest 大麦 sale that names this artist."""
        artist = (follow.get("artist") or "").strip()
        if not artist or not self.ai.available():
            return None
        try:
            damai = await self._locked(
                self.fetcher.load("weibo", DAMAI_WEIBO, limit=CHECK_SCAN, include_hard_skip=True)
            )
        except FetchError:
            return None
        needle = simplified(artist)
        named = [i for i in damai.items if previewable(i) and needle in simplified(i.text or "")]
        return await self._kept_show(named)

    async def _one_card(self, follow: dict[str, str], item, *, damai: bool):
        fields = await self._written(item, FEATURE_TICKET)
        plan = fields.plan(set()) if isinstance(fields, TicketDraft) else None
        if plan is None:
            return None
        embed, files = await news_card(item, FEATURE_TICKET, title=plan.title, summary=plan.summary)
        line = "Damai backup" if damai else f"News Preview ({follow_shown_name(follow)})"
        return notice_text(line), embed, files or None

    async def _written(self, item, topic: str):
        return await card_fields(
            self.ai, self._filter_cache, self._write_cache, self._ai_lock, item, topic,
            remember=False, use_ai=True, filter=False,
        )

    async def _preview_item(self, usable, topic: str):
        kept = pick_check(usable)
        if kept is not None:
            return kept, False
        kept, unsure = await self._first_kept(usable, topic)
        if kept is not None:
            return kept, False
        return None, unsure or not self.ai.available()
