from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fetch import FetchedItem
from news.follows import FEATURE_MATCH
from news.source.match.common import (
    END_TAG,
    NEXT_TAG,
    PREVIEW_BEFORE,
    SKIP_TAG,
    START_LATE,
    START_LEAD,
    START_TAG,
    hold_bookmark,
    hold_until,
    is_match_item,
    item_role,
    kickoff_id,
    kickoff_stamp,
    live_cap,
    match_card_body,
    match_delete_after,
    match_sport,
    match_stage,
    ordered_match_items,
    parse_iso,
    pick_check,
    pick_next_match,
    pick_preview,
    pinned_event_ids,
    should_skip,
    skip_label,
    sources_awaiting_start,
    sports_fetch_due,
    sports_source_due,
    start_due,
    started_keys,
    status_key,
    watch_kickoffs,
    watch_open,
    watch_role,
)


def test_match_rules():
    case_card_lines_clocks_and_order()
    case_watch_windows_preview_and_hold()
    case_start_watch_covers_both_feeds()
    case_sports_fetch_waits_out_a_quiet_day()


def _match(
    item_id: str,
    tags: tuple[str, ...],
    kickoff: datetime | None = None,
    *,
    platform: str = "thesportsdb",
    account: str = "1",
):
    return FetchedItem(
        item_id=item_id,
        title="A vs B",
        text="x",
        url="https://example.test/m",
        image_url=None,
        platform=platform,
        account=account,
        tags=tags,
        occurred_at=kickoff,
    )


def case_card_lines_clocks_and_order():
    aware = datetime(2026, 9, 5, 15, tzinfo=timezone.utc)
    unix = int(aware.timestamp())
    assert kickoff_stamp(None) == "Kickoff soon"
    assert kickoff_stamp(aware) == f"<t:{unix}:F> · <t:{unix}:R>"
    assert kickoff_stamp(datetime(2026, 9, 5, 15)) == kickoff_stamp(aware)
    assert parse_iso("2026-09-05T15:00:00Z") == aware
    assert parse_iso("2026-09-05") == datetime(2026, 9, 5, tzinfo=timezone.utc)
    assert parse_iso("  ") is None
    assert parse_iso("not-a-date") is None

    live = match_card_body(
        "On", "Arsenal", "Premier League", "2nd half", live=True, serie="Group A"
    )
    assert live.splitlines() == [
        "**Game Status** | On",
        "**Home** | Arsenal",
        "**Stage** | Group A",
        "**League** | Premier League",
        "Game Phase | 2nd half",
    ]
    bare = match_card_body("Over", "  ", "", "Enjoy the rest of your day.", live=False)
    assert "**Home**" not in bare and "**League**" not in bare
    assert bare.endswith("**Details**\nEnjoy the rest of your day.")

    assert status_key("Status Final") == "final"
    assert status_key("Match_Finished") == "match finished"
    assert skip_label("Match Postponed") == "Postponed"
    assert skip_label("canceled") == skip_label("cancelled") == "Cancelled"
    assert skip_label("abandoned") == "Abandoned"
    assert skip_label("suspended") == "Suspended"
    assert skip_label("delayed") == "Postponed"

    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    assert live_cap("Soccer") == timedelta(hours=4)
    assert live_cap("Basketball") == timedelta(hours=3, minutes=30)
    assert live_cap("Football") == timedelta(hours=5)
    assert live_cap("Hockey") == timedelta(hours=4)
    assert live_cap("Tennis") == timedelta(hours=8)
    assert live_cap("Curling") == timedelta(hours=4)
    assert watch_open(now, now + timedelta(hours=40), "soccer")
    assert not watch_open(now, now + timedelta(hours=40, seconds=1), "soccer")

    tagged = _match("tagged", ("match", "next", "soccer"), now + timedelta(hours=8))
    assert is_match_item(tagged)
    assert match_sport(tagged) == "soccer"
    assert match_stage(tagged) == NEXT_TAG
    assert item_role(tagged, now) == "later"
    post = _match("p", ("post",))
    assert not is_match_item(post)
    assert match_stage(post) is None
    assert match_delete_after(post, now) is None

    dated = _match("dated", ("match", "next"), now + timedelta(hours=1))
    undated = _match("undated", ("match", "next"))
    started = _match("started", ("match", "start"), now)
    rows = [("blank", None), ("u", undated), ("d", dated), ("s", started)]
    ordered = ordered_match_items(
        rows,
        to_item=lambda row: row[1],
        sort_at=lambda row: row[1].occurred_at if row[1] is not None else None,
        now=now,
    )
    assert [item.item_id for item in ordered] == ["started", "dated"]
    kept = ordered_match_items(
        [("u", undated)],
        to_item=lambda row: row[1],
        sort_at=lambda row: None,
        now=now,
    )
    assert [item.item_id for item in kept] == ["undated"]


def case_start_watch_covers_both_feeds():
    kick = datetime(2026, 9, 29, 9, tzinfo=timezone.utc)
    assert start_due(kick, kick - START_LEAD)
    assert not start_due(kick, kick - START_LEAD - timedelta(seconds=1))
    assert start_due(kick, kick + START_LATE)
    assert not start_due(kick, kick + START_LATE + timedelta(seconds=1))
    soon = kick - timedelta(minutes=5)
    rows = [
        ("pandascore", "paper-rex-valorant", (kick,)),
        ("thesportsdb", "133604", (kick + timedelta(days=2),)),
    ]
    assert sources_awaiting_start(rows, set(), soon) == [("pandascore", "paper-rex-valorant")]
    preview = _match(
        "9:next", ("match", "next"), kick, platform="pandascore", account="paper-rex-valorant"
    )
    assert started_keys("pandascore", "paper-rex-valorant", [preview]) == set()
    live = _match(
        "9:start", ("match", "start"), kick, platform="pandascore", account="paper-rex-valorant"
    )
    seen = started_keys("pandascore", "paper-rex-valorant", [live])
    assert ("pandascore", "paper-rex-valorant", kickoff_id(kick)) in seen
    assert sources_awaiting_start(rows, seen, soon) == []
    club = [("thesportsdb", "133604", (kick,))]
    assert sources_awaiting_start(club, set(), kick) == [("thesportsdb", "133604")]
    done = started_keys(
        "thesportsdb",
        "Arsenal",
        [_match("10:start", ("match", "start", "soccer"), kick)],
        team_id="133604",
    )
    assert ("thesportsdb", "133604", kickoff_id(kick)) in done
    assert sources_awaiting_start(club, done, kick) == []
    assert sources_awaiting_start(club, set(), kick + START_LATE + timedelta(seconds=1)) == []


def case_watch_windows_preview_and_hold():
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    until = hold_until(now, "Soccer")
    assert watch_role(START_TAG, None, "Soccer", now) == "live"
    assert watch_role(NEXT_TAG, now + PREVIEW_BEFORE, "Soccer", now) == "preview"
    assert watch_role(NEXT_TAG, now + PREVIEW_BEFORE + timedelta(seconds=1), "Soccer", now) == "later"
    assert watch_role(END_TAG, now, "Soccer", until) == "result"
    assert watch_role(END_TAG, now, "Soccer", until + timedelta(seconds=1)) == "idle"
    assert watch_role(SKIP_TAG, now, "Soccer", until) == "skip"
    later = _match("later", ("match", "next"), now + timedelta(hours=8))
    soon = _match("soon", ("match", "next"), now + timedelta(hours=1))
    live = _match("live", ("match", "start"), now)
    old = _match("old", ("match", "end"), now - timedelta(hours=3))
    fresh = _match("fresh", ("match", "end"), now - timedelta(hours=1))
    skip = _match("skip", ("match", "skip"), now)
    idle = _match("idle", ("match",))
    assert pick_preview([later, soon, live], now=now).item_id == "live"
    assert pick_preview([later, soon], now=now).item_id == "soon"
    assert pick_preview([old, fresh], now=now).item_id == "fresh"
    assert pick_preview([skip, later], now=now).item_id == "skip"
    assert pick_preview([later], now=now).item_id == "later"
    assert pick_preview([idle], now=now).item_id == "idle"
    gone = _match("gone", ("match", "end"), now - timedelta(days=3))
    assert pick_preview([later, gone], now=now).item_id == "later"
    assert pick_next_match([later, soon, live], now=now).item_id == "live"
    assert pick_next_match([later, gone], now=now).item_id == "later"
    assert pick_next_match([later, soon], now=now).item_id == "soon"
    assert pick_next_match([old, fresh], now=now).item_id == "fresh"
    assert pick_check([later, gone], now=now).item_id == "later"
    post = _match("p", ("post",))
    assert should_skip(later, now=now) is True
    assert should_skip(soon, now=now) is False
    assert should_skip(post, now=now) is None
    assert watch_kickoffs([later, live], now=now) == (now, now + timedelta(hours=8))
    assert watch_kickoffs([later, live, fresh, skip, gone], now=now) == (
        now - timedelta(hours=1),
        now,
        now + timedelta(hours=8),
    )
    assert match_delete_after(later, now) == timedelta(hours=48).total_seconds()
    assert match_delete_after(fresh, now) == timedelta(hours=36).total_seconds()
    assert match_delete_after(skip, now) == timedelta(hours=36).total_seconds()
    assert hold_bookmark(later, now=now)
    assert hold_bookmark(live, now=now)
    assert hold_bookmark(fresh, now=now)
    assert hold_bookmark(skip, now=now)
    assert not hold_bookmark(gone, now=now)
    assert pinned_event_ids(["2200001:next", "9:end", "note"]) == ("2200001",)
    assert pinned_event_ids(["2200001:start"]) == ("2200001",)
    assert pinned_event_ids(["2200001:next", "2200001:end"]) == ()
    assert pinned_event_ids(["2200001:skip"]) == ()


def case_sports_fetch_waits_out_a_quiet_day():
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    kickoff = now + timedelta(days=2)
    assert sports_fetch_due(fetched_at=None, next_kickoff=kickoff, now=now)
    assert not sports_fetch_due(
        fetched_at=now, next_kickoff=kickoff, now=now + timedelta(hours=1)
    )
    assert sports_fetch_due(
        fetched_at=now, next_kickoff=now + timedelta(hours=2), now=now + timedelta(minutes=10)
    )
    later_kick = now + timedelta(hours=12)
    ended = now - timedelta(days=2)
    assert not sports_fetch_due(
        fetched_at=now, next_kickoff=(ended, later_kick), now=now, sport="Soccer"
    )
    assert sports_fetch_due(
        fetched_at=now,
        next_kickoff=(ended, later_kick),
        now=now + timedelta(hours=6),
        sport="Soccer",
    )
    later = now + timedelta(days=3)
    assert sports_fetch_due(
        fetched_at=now, next_kickoff=later, now=now + timedelta(hours=24)
    )
    assert not sports_fetch_due(
        fetched_at=now,
        next_kickoff=later,
        now=now + timedelta(hours=24) - timedelta(seconds=1),
    )

    idle_follow = {
        "platform": "thesportsdb",
        "account": "1",
        "topic": FEATURE_MATCH,
        "fetched_at": now.isoformat(),
        "next_kickoff": (now + timedelta(days=2)).isoformat(),
    }
    stale = {
        "platform": "thesportsdb",
        "account": "1",
        "topic": FEATURE_MATCH,
        "fetched_at": (now - timedelta(days=3)).isoformat(),
        "next_kickoff": (now - timedelta(hours=1)).isoformat(),
    }
    assert sports_source_due([idle_follow], now) is False
    assert sports_source_due([idle_follow, stale], now) is False
    assert sports_source_due(
        [idle_follow, {"platform": "thesportsdb", "account": "1", "topic": FEATURE_MATCH}],
        now,
    ) is True
