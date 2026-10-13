"""Visible `/news` commands."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands

from announce import ACCENT_COLOR, defer_response, notice_text, respond
from fetch import FetchError
from news.follows import (
    FEATURE_GAME,
    FEATURE_MATCH,
    FEATURE_TICKET,
    SHOW_TOPICS,
    TOPIC_UNASSIGNED,
    add_news_follow,
    club_already_followed,
    follow_display_name,
    follow_game_name,
    follow_option_name,
    follow_pick_line,
    follow_shown_name,
    list_news_follows,
    match_news_follow,
    news_follow_ref,
    normalize_news_topic,
    remember_source,
    remember_ticket_artist,
    release_blank_ticket,
    remove_news_follow,
    set_news_follow_topic,
    sort_news_follows,
    topics_for_platform,
)
from news.source.match.thesportsdb import NAME as SPORTS_PLATFORM
from news.source.match.thesportsdb.clubs import suggest_clubs
from news.source.post.weibo.ticket import ArtistName, ArtistRead, one_artist
from news.source.registry import (
    FETCH_PLATFORMS,
    PLATFORM_LABELS,
    add_account_help,
    ambiguous_tokens,
    card_kind,
    cookie_hint,
    is_hard_skip,
    is_match_platform,
    normalize_fetch_account,
    normalize_fetch_platform,
    pick_preview,
)

NEWS_SHOW_SECONDS = 20.0
AUTOCOMPLETE_CAP = 25
_SUBCOMMAND_TYPES = {1, 2}
_SHOW_PLATFORM = {
    "bilibili": "bil",
    "thesportsdb": "sdb",
    "pandascore": "pds",
    "weibo": "wb",
}
PLATFORM_CHOICES = [
    app_commands.Choice(name=PLATFORM_LABELS[name], value=name)
    for name in FETCH_PLATFORMS
]
_MANAGE = discord.Permissions(manage_guild=True)

news = app_commands.Group(
    name="news",
    description="Follow news accounts by topic",
    guild_only=True,
)


def _manage(command):
    return app_commands.default_permissions(_MANAGE)(
        app_commands.checks.has_permissions(manage_guild=True)(command)
    )


def slash_option_value(interaction: discord.Interaction, name: str) -> str | None:
    """Read a slash option from namespace, then from the nested payload Discord sends."""
    raw = _attr(getattr(interaction, "namespace", None), name)
    if isinstance(raw, app_commands.Choice):
        raw = raw.value
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    options = list(_attr(getattr(interaction, "data", None), "options") or [])
    while options:
        kind = _attr(options[0], "type")
        if int(getattr(kind, "value", kind) or 0) not in _SUBCOMMAND_TYPES:
            break
        options = list(_attr(options[0], "options") or [])
    for option in options:
        if _attr(option, "name") != name:
            continue
        value = _attr(option, "value")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def chosen_platform(interaction: discord.Interaction) -> str | None:
    raw = slash_option_value(interaction, "platform")
    return normalize_fetch_platform(raw) if raw else None


def add_account_choices(
    platform: str | None,
    current: str,
    clubs: Sequence | None = None,
    suggestions: Sequence[tuple[str, str]] | None = None,
) -> list[app_commands.Choice[str]]:
    typed = current.strip()
    if platform is None or not typed:
        return []
    if not is_match_platform(platform):
        clipped = typed[:100]
        return [app_commands.Choice(name=clipped, value=clipped)]
    rows: list[tuple[str, str]] = []
    if suggestions is not None:
        rows.extend(suggestions)
    elif platform == SPORTS_PLATFORM:
        aliases = clubs if clubs is not None else suggest_clubs(typed, limit=AUTOCOMPLETE_CAP)
        rows.extend(
            (
                f"{alias.name} · {alias.sport}" if alias.sport else alias.name,
                alias.team_id or alias.name,
            )
            for alias in aliases
        )
    if typed.isdigit() or typed.lower().startswith(f"{platform}:") or not rows:
        rows.append((typed, typed))
    choices: list[app_commands.Choice[str]] = []
    seen: set[str] = set()
    for name, value in rows:
        value, name = value.strip()[:100], name.strip()[:100]
        if not value or value in seen or len(choices) >= AUTOCOMPLETE_CAP:
            continue
        seen.add(value)
        choices.append(app_commands.Choice(name=name, value=value))
    return choices


def add_topic_choices(platform: str | None, current: str) -> list[app_commands.Choice[str]]:
    topics = topics_for_platform(platform)
    needle = current.strip().lower()
    picked = [name for name in topics if not needle or needle in name] or list(topics)
    return [app_commands.Choice(name=name.capitalize(), value=name) for name in picked]


async def _say(interaction: discord.Interaction, line: str | None = None, **kwargs: Any) -> None:
    content = notice_text(line) if line is not None else None
    await respond(interaction, content, **kwargs)


def _guild_id(interaction: discord.Interaction) -> int:
    guild = interaction.guild
    assert guild is not None
    return guild.id


def _attr(option: Any, name: str) -> Any:
    return option.get(name) if isinstance(option, dict) else getattr(option, name, None)


def _same_follow(follows: list[dict[str, str]], follow: dict[str, str]) -> dict[str, str]:
    return next(
        (
            item
            for item in follows
            if item["platform"] == follow["platform"]
            and item["account"] == follow["account"]
            and item["topic"] == follow["topic"]
        ),
        follow,
    )


def _topic_reject(platform: str, topic: str) -> str | None:
    topic_key = normalize_news_topic(topic)
    if topic_key in topics_for_platform(platform):
        return None
    if is_match_platform(platform):
        return "Match platforms only use Match."
    if topic_key == FEATURE_MATCH:
        return "Match is for match platforms."
    return "Pick game, ticket, or other."


def _follow_notice(follow: dict[str, str], status: str) -> str:
    return f"Following {follow_display_name(follow)}. {status}"


def _undo_follow(store, guild_id: int, follow: dict[str, str]) -> bool:
    return remove_news_follow(
        store, guild_id, follow["platform"], follow["account"], follow["topic"]
    )


def _show_line(follow: dict[str, str]) -> str:
    tag = _SHOW_PLATFORM.get(follow["platform"], follow["platform"])
    name = (
        follow_shown_name(follow)
        if follow.get("topic") == FEATURE_TICKET
        else follow_game_name(follow)
    )
    return f"{tag} - {name}"


def _topic_groups(follows: list[dict[str, str]]):
    for topic in SHOW_TOPICS:
        group = [item for item in follows if item["topic"] == topic]
        yield topic, sort_news_follows(group, topic or FEATURE_GAME)


class UserCommands:
    """`/news add`, `feature`, `delete`, and `show`."""

    news = news

    @news.command(name="add", description="Follow a platform account")
    @_manage
    @app_commands.describe(
        platform="TheSportsDB, PandaScore, Bilibili, or Weibo",
        account="Club, team, uid, or profile for that platform",
    )
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def news_add(
        self,
        interaction: discord.Interaction,
        platform: str,
        account: str | None = None,
    ) -> None:
        typed = (account or "").strip()
        if not typed:
            await _say(interaction, add_account_help(platform))
            return
        await defer_response(interaction)
        line = await self._follow_account(_guild_id(interaction), platform, typed)
        await _say(interaction, line)

    @news_add.autocomplete("account")
    async def news_add_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        platform = chosen_platform(interaction)
        typed = current.strip()
        clubs = None
        suggestions = None
        if platform == SPORTS_PLATFORM and typed:
            clubs = await self.fetcher.search_clubs(typed, limit=AUTOCOMPLETE_CAP)
        elif platform is not None and is_match_platform(platform) and typed:
            suggestions = await self.fetcher.search_accounts(
                platform, typed, limit=AUTOCOMPLETE_CAP
            )
        return add_account_choices(platform, typed, clubs, suggestions)

    @news.command(name="feature", description="Set the topic for a followed account")
    @_manage
    @app_commands.describe(
        account="Pick a followed account by name",
        topic="Match for match platforms. Game, ticket, or other for Bilibili and Weibo",
    )
    async def news_feature(
        self,
        interaction: discord.Interaction,
        account: str,
        topic: str,
    ) -> None:
        follow = await self._require_follow(interaction, account)
        if follow is None:
            return
        error = _topic_reject(follow["platform"], topic)
        if error:
            await _say(interaction, error)
            return
        guild_id = _guild_id(interaction)
        named: ArtistName | None = None
        if (
            normalize_news_topic(topic) == FEATURE_TICKET
            and not (follow.get("artist") or "").strip()
        ):
            await defer_response(interaction)
            read = await self._ticket_artist(follow)
            if read is None or not read.answered:
                await _say(interaction, "Couldn't read that profile.")
                return
            if read.name is None:
                if follow["topic"] == FEATURE_TICKET:
                    release_blank_ticket(
                        self.guild_config, guild_id, follow["platform"], follow["account"]
                    )
                await _say(interaction, "No one artist on that account.")
                return
            named = read.name
        try:
            updated = set_news_follow_topic(
                self.guild_config, guild_id, follow, topic
            )
        except ValueError as exc:
            await _say(interaction, str(exc))
            return
        if named is not None:
            remember_ticket_artist(
                self.guild_config,
                [guild_id],
                updated["platform"],
                updated["account"],
                named.written,
            )
            updated = _same_follow(
                list_news_follows(self.guild_config, guild_id), updated
            )
        await _say(interaction, f"{follow_shown_name(updated)} is `{updated['topic']}`.")

    @news_feature.autocomplete("topic")
    async def news_feature_topic_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        account = slash_option_value(interaction, "account")
        if not account or interaction.guild is None:
            return []
        follow = match_news_follow(
            list_news_follows(self.guild_config, interaction.guild.id), account
        )
        return add_topic_choices(follow["platform"], current) if follow else []

    @news.command(name="delete", description="Stop following a news account")
    @_manage
    @app_commands.describe(account="Pick a followed account by name")
    async def news_delete(self, interaction: discord.Interaction, account: str) -> None:
        follow = await self._require_follow(interaction, account)
        if follow is None:
            return
        if not _undo_follow(self.guild_config, _guild_id(interaction), follow):
            await _say(interaction, "That follow was not found.")
            return
        self._prune_bookmarks()
        await _say(interaction, f"Removed {follow_game_name(follow)}.")

    @news.command(name="show", description="Show followed news accounts")
    async def news_show(self, interaction: discord.Interaction) -> None:
        follows = list_news_follows(self.guild_config, _guild_id(interaction))
        if not follows:
            await _say(interaction, "No follows yet. `/news add` `/news check`.")
            return
        embed = discord.Embed(title="News follows", color=ACCENT_COLOR)
        for topic, group in _topic_groups(follows):
            body = "\n".join(_show_line(item) for item in group) or "none"
            if len(body) > 1024:
                body = body[:1023] + "…"
            embed.add_field(
                name="Unassigned" if topic == TOPIC_UNASSIGNED else topic.capitalize(),
                value=body,
                inline=False,
            )
        await _say(interaction, embed=embed, delete_after=NEWS_SHOW_SECONDS)

    async def _locked(self, awaitable):
        async with self._fetch_lock:
            return await awaitable

    async def _follow_account(self, guild_id: int, platform: str, typed: str) -> str:
        site = normalize_fetch_platform(platform)
        account = normalize_fetch_account(site, typed) if site else None
        if site is None or account is None:
            return add_account_help(site or platform)
        try:
            follow = add_news_follow(
                self.guild_config, guild_id, site, account, TOPIC_UNASSIGNED
            )
        except ValueError as exc:
            return str(exc)
        try:
            got = await self._locked(self.fetcher.pull(follow["platform"], follow["account"]))
        except FetchError as exc:
            return self._follow_fetch_failed(guild_id, follow, site, exc)
        if club_already_followed(
            list_news_follows(self.guild_config, guild_id),
            follow,
            team_id=got.team_id,
            source_name=got.name,
            ignore_self=True,
        ):
            _undo_follow(self.guild_config, guild_id, follow)
            return "Already following that account"
        match = card_kind(follow["platform"]) == "match"
        remember_source(
            self.guild_config,
            {guild_id},
            follow["platform"],
            follow["account"],
            got.name,
            datetime.now(timezone.utc),
            sport=got.sport,
            kickoffs=got.kickoffs,
            team_id=got.team_id,
        )
        if not match and (got.name or got.certification or got.bio):
            async with self._ai_lock:
                artist = await one_artist(
                    self.ai, (got.name or "").strip(), got.certification, got.bio
                )
            if artist.name is not None:
                remember_ticket_artist(
                    self.guild_config,
                    [guild_id],
                    follow["platform"],
                    follow["account"],
                    artist.name.written,
                )
        shown = {**follow, "label": got.name} if got.name else follow
        if match and self._poll_match:
            item = pick_preview(
                [card for card in got.visible if card.item_id and not is_hard_skip(card)]
            )
            if item is not None:
                await self._announce_item(
                    follow["platform"],
                    got.team_id or follow["account"],
                    [(guild_id, follow["topic"])],
                    item,
                )
        if match:
            hint = "Bookmarked." if self._poll_match else "Poll off."
            return _follow_notice(shown, hint)
        name = follow_display_name(shown)
        if self._poll_post:
            return f"No posts until `/news feature`. Following {name}"
        return f"Poll off. Following {name}. `/news feature`."

    def _follow_fetch_failed(
        self, guild_id: int, follow: dict[str, str], platform: str, exc: FetchError
    ) -> str:
        if ambiguous_tokens(platform, exc):
            _undo_follow(self.guild_config, guild_id, follow)
            return "Name matches more than one team."
        if str(exc).startswith("No team"):
            _undo_follow(self.guild_config, guild_id, follow)
            return "No team."
        status = "Cookie expired." if cookie_hint(platform, exc) else "Fetch failed."
        return _follow_notice(follow, status)

    async def _ticket_artist(self, follow: dict[str, str]) -> ArtistRead | None:
        """Read one person from the profile. None means the profile could not be loaded."""
        try:
            loaded = await self._locked(
                self.fetcher.load(follow["platform"], follow["account"], limit=1)
            )
        except FetchError:
            return None
        display = (loaded.name or follow_display_name(follow)).strip()
        async with self._ai_lock:
            return await one_artist(self.ai, display, loaded.certification, loaded.bio)

    async def _require_follow(
        self,
        interaction: discord.Interaction,
        account: str,
        *,
        ephemeral: bool = False,
    ) -> dict[str, str] | None:
        follows = list_news_follows(self.guild_config, _guild_id(interaction))
        follow = match_news_follow(follows, account)
        if follow is None:
            await _say(interaction, follow_pick_line(follows, account), ephemeral=ephemeral)
            return None
        return follow

    def _followed_account_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []
        follows = list_news_follows(self.guild_config, guild.id)
        needle = current.lower()
        choices: list[app_commands.Choice[str]] = []
        for _, group in _topic_groups(follows):
            for follow in group:
                name = follow_option_name(follow, follows)
                haystack = "\n".join(
                    (
                        name,
                        follow_display_name(follow),
                        follow.get("artist") or "",
                        follow["account"],
                    )
                )
                if needle and needle not in haystack.lower():
                    continue
                choices.append(
                    app_commands.Choice(name=name[:100], value=news_follow_ref(follow))
                )
                if len(choices) >= AUTOCOMPLETE_CAP:
                    return choices
        return choices
