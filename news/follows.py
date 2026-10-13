"""Per-guild news follow records. Does not talk to Discord or fetch feeds."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone

from fetch import FetchedItem
from guild import GuildConfig
from news.source.match.thesportsdb.clubs import club_alias, official_club_name, sport_for_club
from news.source.match.common import watch_open
from news.source.match.pandascore import NAME as PANDASCORE_PLATFORM
from news.source.match.thesportsdb import NAME as SPORTS_PLATFORM
from news.source.registry import (
    FETCH_PLATFORMS,
    feed_display_name,
    is_live_room,
    is_match_platform,
    normalize_fetch_account,
    normalize_fetch_platform,
)

FEATURE_GAME = "game"
FEATURE_TICKET = "ticket"
FEATURE_MATCH = "match"
FEATURE_OTHER = "other"
TOPIC_UNASSIGNED = ""
NEWS_FEATURES = (FEATURE_GAME, FEATURE_TICKET, FEATURE_MATCH, FEATURE_OTHER)
SHOW_TOPICS = (
    TOPIC_UNASSIGNED,
    FEATURE_GAME,
    FEATURE_MATCH,
    FEATURE_TICKET,
    FEATURE_OTHER,
)
FOLLOWS_KEY = "news_follows"
_EXTRA = ("added_at", "sport", "fetched_at", "next_kickoff", "artist")
IDLE_REFRESH = timedelta(hours=24)

Follow = dict[str, str]
SourceKey = tuple[str, str]
GuildTopic = tuple[int, str]


def parse_stored_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def parse_stored_times(value: str | None) -> tuple[datetime, ...]:
    parsed = (parse_stored_time(part) for part in str(value or "").split(","))
    return tuple(dict.fromkeys(stamp for stamp in parsed if stamp is not None))


def kickoff_times(value: datetime | Sequence[datetime] | None) -> tuple[datetime, ...]:
    if value is None:
        return ()
    return (value,) if isinstance(value, datetime) else tuple(value)


def sports_fetch_due(
    *,
    fetched_at: datetime | None,
    next_kickoff: datetime | Sequence[datetime] | None,
    now: datetime,
    sport: str = "",
) -> bool:
    """Fetch in a fixture's preview/on/result window, else about once a day."""
    open_now = any(watch_open(stamp, now, sport) for stamp in kickoff_times(next_kickoff))
    return fetched_at is None or open_now or now - fetched_at >= IDLE_REFRESH


def _trim(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text[:limit].rstrip() if len(text) > limit else text


def normalize_news_topic(topic: str) -> str | None:
    key = topic.strip().lower()
    return key if key in NEWS_FEATURES else None


def topics_for_platform(platform: str | None) -> tuple[str, ...]:
    key = normalize_fetch_platform(platform) if platform else None
    if key is None:
        return ()
    if is_match_platform(key):
        return (FEATURE_MATCH,)
    return (FEATURE_GAME, FEATURE_TICKET, FEATURE_OTHER)


def block_live_room(item: FetchedItem, topic: str) -> bool:
    """Game/ticket leave live-room go-live to a live notice. Other may post it."""
    return is_live_room(item) and topic != FEATURE_OTHER


def follow_id(follow: Follow) -> tuple[str, str, str]:
    return follow["platform"], follow["account"], follow["topic"]


def canonical_follow(
    platform: str, account: str, topic: str, label: str | None = None
) -> Follow:
    platform_key = normalize_fetch_platform(platform)
    if platform_key is None:
        raise ValueError(f"Unknown news platform: {platform}")
    if is_match_platform(platform_key):
        topic_key = FEATURE_MATCH
    elif not str(topic).strip():
        topic_key = TOPIC_UNASSIGNED
    else:
        topic_key = normalize_news_topic(topic)
        if topic_key is None:
            raise ValueError(f"Unknown news topic: {topic}")
        if topic_key == FEATURE_MATCH:
            raise ValueError("Match is for match platforms.")
    account_key = normalize_fetch_account(platform_key, account)
    if account_key is None:
        raise ValueError(f"Invalid {platform_key} account")
    record: Follow = {"platform": platform_key, "account": account_key, "topic": topic_key}
    if isinstance(label, str) and label.strip():
        record["label"] = label.strip()
    return record


def _read_follow(item: object) -> Follow | None:
    if not isinstance(item, dict):
        return None
    raw_label = item.get("label")
    try:
        parsed = canonical_follow(
            str(item.get("platform") or ""),
            str(item.get("account") or ""),
            str(item.get("topic") or ""),
            raw_label if isinstance(raw_label, str) else None,
        )
    except ValueError:
        return None
    for key in _EXTRA:
        raw = item.get(key)
        if isinstance(raw, str) and raw.strip():
            parsed[key] = raw.strip()
    return parsed


def list_news_follows(
    store: GuildConfig, guild_id: int, topic: str | None = None
) -> list[Follow]:
    wanted: str | None = None
    if topic is not None:
        wanted = topic if topic in SHOW_TOPICS else normalize_news_topic(topic)
        if wanted is None:
            return []
    found: dict[tuple[str, str, str], Follow] = {}
    for item in store.guild_list(guild_id, FOLLOWS_KEY):
        parsed = _read_follow(item)
        if parsed is None or (wanted is not None and parsed["topic"] != wanted):
            continue
        found.setdefault(follow_id(parsed), parsed)
    return list(found.values())


def iter_news_follows(store: GuildConfig) -> list[tuple[int, Follow]]:
    return [
        (guild_id, follow)
        for guild_id in store.guild_ids()
        for follow in list_news_follows(store, guild_id)
    ]


def add_news_follow(
    store: GuildConfig, guild_id: int, platform: str, account: str, topic: str,
    label: str | None = None,
) -> Follow:
    record = canonical_follow(platform, account, topic, label)
    record["added_at"] = datetime.now(timezone.utc).isoformat()
    follows = list_news_follows(store, guild_id)
    if club_already_followed(follows, record):
        raise ValueError("Already following that account")
    follows.append(record)
    store.set_guild_list(guild_id, FOLLOWS_KEY, follows)
    return record


def _club_ids(follow: Follow) -> set[str]:
    ids = {follow["account"]} if follow["account"].isdigit() else set()
    alias = club_alias(follow["account"]) or club_alias(follow.get("label") or "")
    if alias is not None and alias.team_id.isdigit():
        ids.add(alias.team_id)
    return ids


def _same_club(left: Follow, right: Follow) -> bool:
    if left["platform"] != right["platform"]:
        return False
    if left["account"] == right["account"]:
        return True
    if left["platform"] != SPORTS_PLATFORM:
        return False
    return bool(_club_ids(left) & _club_ids(right))


def club_already_followed(
    follows: list[Follow],
    follow: Follow,
    *,
    team_id: str | None = None,
    source_name: str | None = None,
    ignore_self: bool = False,
) -> bool:
    """True when another saved follow is this club, including a name learned after fetch."""
    resolved = (team_id or "").strip()
    name = (source_name or "").strip().casefold()
    added = follow_id(follow)
    for item in follows:
        if ignore_self and follow_id(item) == added:
            continue
        if _same_club(item, follow):
            return True
        if not is_match_platform(follow["platform"]) or item["platform"] != follow["platform"]:
            continue
        names = (item["account"].casefold(), (item.get("label") or "").casefold())
        if (resolved and item["account"] == resolved) or (
            follow["platform"] != PANDASCORE_PLATFORM and name and name in names
        ):
            return True
    return False


def release_blank_ticket(
    store: GuildConfig, guild_id: int, platform: str, account: str
) -> bool:
    """Move a ticket follow with no artist back to unassigned."""
    current = next(
        (
            item for item in list_news_follows(store, guild_id, FEATURE_TICKET)
            if item["platform"] == platform and item["account"] == account
        ),
        None,
    )
    if current is None or (current.get("artist") or "").strip():
        return False
    set_news_follow_topic(store, guild_id, current, TOPIC_UNASSIGNED)
    return True


def set_news_follow_topic(
    store: GuildConfig, guild_id: int, follow: Follow, topic: str
) -> Follow:
    """Move an existing follow to another topic. Keeps label, sport, and schedule."""
    wanted = canonical_follow(
        follow["platform"], follow["account"], topic, follow.get("label")
    )
    identity, target = follow_id(follow), follow_id(wanted)
    follows = list_news_follows(store, guild_id)
    current = next((item for item in follows if follow_id(item) == identity), None)
    if current is None:
        raise ValueError("That account is not in the follow list.")
    if target == identity:
        return current
    if any(follow_id(item) == target for item in follows):
        raise ValueError(f"Already following that account for `{wanted['topic']}`")
    current["topic"] = wanted["topic"]
    store.set_guild_list(guild_id, FOLLOWS_KEY, follows)
    return current


def fill_news_follow(
    store: GuildConfig, guild_id: int, platform: str, account: str, *,
    label: str | None = None, sport: str | None = None,
    fetched_at: datetime | None = None,
    next_kickoff: datetime | Sequence[datetime] | None = None,
    team_id: str | None = None, artist: str | None = None,
) -> bool:
    """Backfill label, sport, fetch schedule, and/or ticket artist on matching follows."""
    platform_key = normalize_fetch_platform(platform)
    account_key = normalize_fetch_account(platform_key, account) if platform_key else None
    if platform_key is None or account_key is None:
        return False
    team_key = (team_id or "").strip()
    if platform_key == SPORTS_PLATFORM and team_key and not team_key.isdigit():
        team_key = ""
    rename = bool(team_key) and team_key != account_key
    # An account id is not a label. Renaming a word account keeps that word.
    name = _trim(feed_display_name(label, platform_key) if label is not None else None, 100)
    if name == account_key and not rename:
        name = None
    elif name is None and rename and not account_key.isdigit():
        name = account_key
    kind = _trim(sport, 40) if sport is not None else None
    person = _trim(artist, 40) if artist is not None else None
    fetched = fetched_at.isoformat() if fetched_at is not None else None
    kickoff = ",".join(stamp.isoformat() for stamp in sorted(kickoff_times(next_kickoff)))
    if not (name or kind or fetched or team_key or person):
        return False

    follows = list_news_follows(store, guild_id)

    def same(item: Follow, account: str) -> bool:
        return item["platform"] == platform_key and item["account"] == account

    def patch(item: Follow) -> bool:
        changed = False
        if team_key and item["account"] != team_key:
            item["account"] = team_key
            changed = True
        for key, value in (("label", name), ("sport", kind), ("artist", person)):
            if value is not None and item.get(key) != value:
                item[key] = value
                changed = True
        # A missing kickoff is not "", so clearing one still counts as a write.
        if fetched is not None and (
            item.get("fetched_at") != fetched or item.get("next_kickoff") != kickoff
        ):
            item["fetched_at"] = fetched
            item["next_kickoff"] = kickoff
            if not kickoff:
                item.pop("next_kickoff")
            changed = True
        return changed

    occupant = next((item for item in follows if rename and same(item, team_key)), None)
    if occupant is not None:
        patch(occupant)
        kept = [item for item in follows if not same(item, account_key)]
        store.set_guild_list(guild_id, FOLLOWS_KEY, kept)
        return True
    changed = False
    for item in follows:
        if same(item, account_key):
            changed |= patch(item)
    if changed:
        store.set_guild_list(guild_id, FOLLOWS_KEY, follows)
    return changed


def sports_source_due(follows: list[Follow], now: datetime) -> bool:
    latest: tuple[datetime, Follow] | None = None
    for item in follows:
        stamp = parse_stored_time(item.get("fetched_at"))
        if stamp is None:
            return True
        if latest is None or stamp > latest[0]:
            latest = (stamp, item)
    if latest is None:
        return False
    fetched_at, item = latest
    return sports_fetch_due(
        fetched_at=fetched_at,
        next_kickoff=parse_stored_times(item.get("next_kickoff")),
        now=now,
        sport=item.get("sport") or "",
    )


def remember_source(
    store: GuildConfig, guild_ids: Iterable[int], platform: str, account: str,
    source_name: str | None, now: datetime, *,
    sport: str | None = None,
    kickoffs: datetime | Sequence[datetime] | None = None,
    team_id: str | None = None,
    schedule: bool = True,
) -> None:
    match = is_match_platform(platform)
    timed = match and schedule
    for guild_id in guild_ids:
        fill_news_follow(
            store, guild_id, platform, account, label=source_name,
            sport=sport if source_name else None, fetched_at=now if timed else None,
            next_kickoff=kickoffs if timed else None, team_id=team_id if match else None,
        )


def remember_ticket_artist(
    store: GuildConfig, guild_ids: Iterable[int], platform: str, account: str, artist: str,
) -> None:
    for guild_id in guild_ids:
        fill_news_follow(store, guild_id, platform, account, artist=artist)


def poll_plan(
    store: GuildConfig, *, now: float, now_wall: datetime, poll_post: bool,
    poll_match: bool, skip_until: dict[SourceKey, float], guild_ok: Callable[[int], bool],
    poll_ticket: bool = True,
) -> tuple[set[SourceKey], dict[SourceKey, list[GuildTopic]], list[SourceKey], list[SourceKey], list[SourceKey]]:
    keep: set[SourceKey] = set()
    live: dict[SourceKey, list[GuildTopic]] = {}
    sports_follows: dict[SourceKey, list[Follow]] = {}
    for guild_id, follow in iter_news_follows(store):
        source = (follow["platform"], follow["account"])
        keep.add(source)
        if follow["topic"] == TOPIC_UNASSIGNED or not guild_ok(guild_id):
            continue
        live.setdefault(source, []).append((guild_id, follow["topic"]))
        if is_match_platform(source[0]):
            sports_follows.setdefault(source, []).append(follow)
    sports: list[SourceKey] = []
    posts: list[SourceKey] = []
    tickets: list[SourceKey] = []
    for source in sorted(live):
        if now < skip_until.get(source, 0.0):
            continue
        if is_match_platform(source[0]):
            if poll_match and sports_source_due(sports_follows.get(source, []), now_wall):
                sports.append(source)
            continue
        topics = {topic for _, topic in live[source]}
        if topics == {FEATURE_TICKET}:
            if poll_ticket:
                tickets.append(source)
        elif poll_post:
            posts.append(source)
    return keep, live, sports, posts, tickets


def remove_news_follow(
    store: GuildConfig, guild_id: int, platform: str, account: str, topic: str
) -> bool:
    try:
        record = canonical_follow(platform, account, topic)
    except ValueError:
        return False
    identity = follow_id(record)
    follows = list_news_follows(store, guild_id)
    kept = [item for item in follows if follow_id(item) != identity]
    if len(kept) == len(follows):
        return False
    store.set_guild_list(guild_id, FOLLOWS_KEY, kept)
    return True


def news_follow_ref(follow: Follow) -> str:
    raw = f"{follow['platform']}|{follow['account']}|{follow['topic']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def follow_shown_name(follow: Follow) -> str:
    """A ticket follow shows the artist with * when that differs from the display name."""
    label = follow_display_name(follow)
    if follow.get("topic") != FEATURE_TICKET:
        return label
    artist = (follow.get("artist") or "").strip()
    if artist and artist != label:
        return f"{artist}*"[:100]
    return label


def follow_display_name(follow: Follow) -> str:
    if follow.get("platform") == SPORTS_PLATFORM:
        return official_club_name(follow["account"], follow.get("label") or "")
    return (follow.get("label") or follow["account"]).strip()


def follow_game_name(follow: Follow) -> str:
    """Team label plus its game, so the same org stays distinct across squads."""
    label = follow_display_name(follow)
    if follow.get("platform") != PANDASCORE_PLATFORM:
        return label
    game = (follow.get("sport") or "").strip()
    return f"{label} · {game}" if game else label


def follow_option_name(follow: Follow, siblings: list[Follow]) -> str:
    label = follow_display_name(follow)
    shown = follow_shown_name(follow)
    source = (follow["platform"], follow["account"])
    if sum(1 for item in siblings if (item["platform"], item["account"]) == source) > 1:
        return f"{shown} ({follow['topic']})"[:100]
    if follow["platform"] == PANDASCORE_PLATFORM:
        named = follow_game_name(follow)
        if named != label:
            return named[:100]
        if sum(1 for item in siblings if follow_display_name(item) == label) > 1:
            return f"{label} · {follow['account']}"[:100]
    if sum(1 for item in siblings if follow_shown_name(item) == shown) > 1:
        return f"{shown} ({follow['platform']})"[:100]
    return shown[:100]


def sort_news_follows(follows: list[Follow], topic: str) -> list[Follow]:
    rank = {name: index for index, name in enumerate(FETCH_PLATFORMS)}

    def key(pair: tuple[int, Follow]) -> tuple:
        index, follow = pair
        name = follow_display_name(follow).casefold()
        added = follow.get("added_at") or f"{index:06d}"
        platform = follow["platform"]
        if topic != FEATURE_MATCH:
            return (rank.get(platform, 99), platform, added, name)
        stored = follow.get("sport") or ""
        if platform == PANDASCORE_PLATFORM:
            kind = "Esports"
        elif platform == SPORTS_PLATFORM:
            kind = sport_for_club(follow.get("account") or "", follow.get("label") or "", stored)
        else:
            kind = stored.strip()
        if kind:
            return (0, kind.casefold(), added, name)
        if platform == SPORTS_PLATFORM:
            return (1, "", added, name)
        return (2, rank.get(platform, 99), platform, added, name)

    return [follow for _, follow in sorted(enumerate(follows), key=key)]


def account_hits(follows: list[Follow], query: str) -> list[Follow]:
    return [
        follow
        for follow in follows
        if follow["account"] == query
        or normalize_fetch_account(follow["platform"], query) == follow["account"]
    ]


def follow_pick_line(follows: list[Follow], query: str) -> str:
    hits = account_hits(follows, query.strip())
    if len(hits) < 2:
        return "That account is not in the follow list."
    kind = "platform" if len({item["platform"] for item in hits}) > 1 else "topic"
    return f"Account on more than one {kind}. Pick one."


def match_news_follow(follows: list[Follow], query: str) -> Follow | None:
    query = query.strip()
    if not query:
        return None
    for follow in follows:
        if news_follow_ref(follow) == query:
            return follow
    labeled = [follow for follow in follows if follow.get("label", "").lower() == query.lower()]
    if len(labeled) == 1:
        return labeled[0]
    hits = account_hits(follows, query)
    return hits[0] if len(hits) == 1 else None
