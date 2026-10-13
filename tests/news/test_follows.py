from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from guild import (
    GuildConfig,
    announce_features,
    register_announce_feature,
    unregister_announce_feature,
)
from news.follows import (
    FEATURE_GAME,
    FEATURE_MATCH,
    FEATURE_OTHER,
    FEATURE_TICKET,
    FOLLOWS_KEY,
    NEWS_FEATURES,
    TOPIC_UNASSIGNED,
    add_news_follow,
    canonical_follow,
    fill_news_follow,
    follow_display_name,
    follow_game_name,
    follow_option_name,
    follow_pick_line,
    follow_shown_name,
    iter_news_follows,
    list_news_follows,
    match_news_follow,
    news_follow_ref,
    normalize_news_topic,
    ordered_announce_features,
    parse_stored_time,
    parse_stored_times,
    release_blank_ticket,
    remember_source,
    remember_ticket_artist,
    remove_news_follow,
    set_news_follow_topic,
    sort_news_follows,
    topics_for_platform,
)
from tests.helpers import ACCOUNT, GUILD_ID


def test_follows(news_store, tmp_path):
    case_announce_features_put_voice_first_and_other_last()
    case_follow_round_trip_validation_and_club_ids(news_store)
    case_same_org_is_one_follow_per_game(news_store)
    case_ticket_studio_option_marks_the_artist()
    case_stored_times()
    case_topics_follow_the_platform()
    case_other_follow_keeps_the_studio_name_until_ticket(GuildConfig(tmp_path / "other.json"))
    reload = tmp_path / "reload"
    reload.mkdir()
    case_follow_reload_labels_and_legacy_topics(reload)
    case_source_clock_and_cleared_kickoff(GuildConfig(tmp_path / "plan.json"))
    case_blank_ticket_returns_to_unassigned(GuildConfig(tmp_path / "blank.json"))
    case_show_order_and_ref()

def case_announce_features_put_voice_first_and_other_last():
    before = announce_features()
    for name in before:
        unregister_announce_feature(name)
    register_announce_feature("voice")
    register_announce_feature("admin")
    register_announce_feature("extra")
    for name in NEWS_FEATURES:
        register_announce_feature(name)
    try:
        assert ordered_announce_features() == (
            "voice",
            "extra",
            "game",
            "ticket",
            "match",
            "other",
        )
        assert "admin" not in ordered_announce_features()
    finally:
        for name in ("voice", "admin", "extra", *NEWS_FEATURES):
            unregister_announce_feature(name)
        for name in before:
            register_announce_feature(name)


def case_follow_round_trip_validation_and_club_ids(news_store):
    record = add_news_follow(
        news_store, GUILD_ID, "bilibili", "1265652806123", FEATURE_GAME, "studio"
    )
    assert record["platform"] == "bilibili"
    assert record["account"] == ACCOUNT
    assert record["topic"] == "game"
    assert record["label"] == "studio"
    assert list_news_follows(news_store, GUILD_ID, FEATURE_GAME) == [record]
    assert iter_news_follows(news_store) == [(GUILD_ID, record)]
    with pytest.raises(ValueError, match="Already following that account"):
        add_news_follow(news_store, GUILD_ID, "bilibili", ACCOUNT, FEATURE_GAME)
    with pytest.raises(ValueError, match="Already following that account"):
        add_news_follow(news_store, GUILD_ID, "bilibili", "uid:1265652806123", FEATURE_TICKET)
    with pytest.raises(ValueError, match="Invalid bilibili account"):
        add_news_follow(news_store, GUILD_ID, "bilibili", "not-a-uid", FEATURE_GAME)
    with pytest.raises(ValueError, match="Invalid weibo account"):
        add_news_follow(news_store, GUILD_ID, "weibo", "rmrb", FEATURE_GAME)
    with pytest.raises(ValueError, match="Match is for match platforms"):
        add_news_follow(news_store, GUILD_ID, "bilibili", ACCOUNT, FEATURE_MATCH)
    with pytest.raises(ValueError, match="Unknown news topic"):
        canonical_follow("bilibili", ACCOUNT, "nope")
    with pytest.raises(ValueError, match="Unknown news topic"):
        canonical_follow("bilibili", ACCOUNT, "sport")
    club = add_news_follow(news_store, GUILD_ID, "thesportsdb", "Arsenal", FEATURE_TICKET)
    assert club["topic"] == FEATURE_MATCH
    assert club["account"] == "133604"
    with pytest.raises(ValueError, match="Already following that account"):
        add_news_follow(news_store, GUILD_ID, "thesportsdb", "133604", FEATURE_MATCH)
    add_news_follow(news_store, 3, "thesportsdb", "133604", FEATURE_MATCH)
    with pytest.raises(ValueError, match="Already following that account"):
        add_news_follow(news_store, 3, "thesportsdb", "Arsenal", FEATURE_MATCH)
    space = add_news_follow(
        news_store, 2, "bilibili", "https://space.bilibili.com/99", FEATURE_GAME
    )
    assert space["account"] == "99"
    assert fill_news_follow(news_store, 2, "bilibili", "99", label="Studio") is True
    assert list_news_follows(news_store, 2, FEATURE_GAME)[0]["label"] == "Studio"
    assert fill_news_follow(news_store, 2, "bilibili", "99", label="99") is False

    follow = add_news_follow(
        news_store, 4, "bilibili", ACCOUNT, FEATURE_GAME, "studio"
    )
    same = set_news_follow_topic(news_store, 4, follow, FEATURE_GAME)
    assert same["label"] == "studio"
    moved = set_news_follow_topic(news_store, 4, follow, FEATURE_TICKET)
    assert moved["topic"] == FEATURE_TICKET
    rows = list_news_follows(news_store, 4)
    rows.append({**moved, "topic": FEATURE_GAME})
    news_store.set_guild_list(4, FOLLOWS_KEY, rows)
    with pytest.raises(ValueError, match="Already following that account for `game`"):
        set_news_follow_topic(news_store, 4, moved, FEATURE_GAME)

    add_news_follow(news_store, 5, "thesportsdb", "Zzzfc United", FEATURE_MATCH)
    assert fill_news_follow(
        news_store,
        5,
        "thesportsdb",
        "Zzzfc United",
        label="Zzzfc United",
        team_id="133999",
    )
    named = list_news_follows(news_store, 5)[0]
    assert named["account"] == "133999"
    assert follow_display_name(named) == "Zzzfc United"
    esports = add_news_follow(
        news_store, 8, "pandascore", "paper-rex-valorant", FEATURE_TICKET
    )
    assert esports["topic"] == FEATURE_MATCH
    assert esports["account"] == "paper-rex-valorant"
    add_news_follow(news_store, 6, "thesportsdb", "133888", FEATURE_MATCH)
    add_news_follow(news_store, 6, "thesportsdb", "Zzzfc United", FEATURE_MATCH)
    assert fill_news_follow(
        news_store, 6, "thesportsdb", "Zzzfc United", team_id="133888"
    )
    clubs = [
        item["account"]
        for item in list_news_follows(news_store, 6)
        if item["platform"] == "thesportsdb"
    ]
    assert clubs == ["133888"]


def case_same_org_is_one_follow_per_game(news_store):
    squads = (
        ("bilibili-gaming-lol", "LoL"),
        ("bilibili-gaming-valorant", "Valorant"),
        ("bilibili-gaming-cs-go", "Counter-Strike"),
        ("bilibili-gaming-dota-2", "Dota 2"),
    )
    for slug, game in squads:
        added = add_news_follow(
            news_store, 7, "pandascore", slug, FEATURE_MATCH, label="Bilibili Gaming"
        )
        fill_news_follow(news_store, 7, "pandascore", added["account"], sport=game)
    saved = list_news_follows(news_store, 7)
    assert {item["account"] for item in saved} == {slug for slug, _game in squads}
    labels = {follow_option_name(item, saved) for item in saved}
    shown = {follow_game_name(item) for item in saved}
    assert labels == shown == {f"Bilibili Gaming · {game}" for _slug, game in squads}


def case_ticket_studio_option_marks_the_artist():
    own = canonical_follow("weibo", "10001", FEATURE_TICKET, "薛之谦")
    studio = canonical_follow("weibo", "10002", FEATURE_TICKET, "汪峰工作室")
    studio["artist"] = "汪峰"
    brand = canonical_follow("weibo", "10003", FEATURE_TICKET, "一样音乐工作室")
    brand["artist"] = "李荣浩"
    assert follow_shown_name(own) == "薛之谦"
    assert follow_shown_name(studio) == "汪峰*"
    assert follow_shown_name(brand) == "李荣浩*"
    assert follow_option_name(brand, [own, studio, brand]) == "李荣浩*"
    assert follow_shown_name({**studio, "topic": FEATURE_GAME}) == "汪峰工作室"
    other = {**brand, "topic": FEATURE_OTHER}
    assert follow_shown_name(other) == "一样音乐工作室"
    assert follow_shown_name({**other, "topic": FEATURE_TICKET}) == "李荣浩*"


def case_other_follow_keeps_the_studio_name_until_ticket(news_store):
    add_news_follow(news_store, GUILD_ID, "weibo", "10003", FEATURE_OTHER, "一样音乐工作室")
    assert fill_news_follow(news_store, GUILD_ID, "weibo", "10003", artist="李荣浩") is True
    saved = list_news_follows(news_store, GUILD_ID)[0]
    assert saved["artist"] == "李荣浩"
    assert follow_shown_name(saved) == "一样音乐工作室"
    moved = set_news_follow_topic(news_store, GUILD_ID, saved, FEATURE_TICKET)
    assert follow_shown_name(moved) == "李荣浩*"


def case_follow_reload_labels_and_legacy_topics(tmp_path):
    path = tmp_path / "guild.json"
    store = GuildConfig(path)
    add_news_follow(store, GUILD_ID, "bilibili", "uid:99", FEATURE_TICKET)
    assert remove_news_follow(store, GUILD_ID, "bilibili", "99", FEATURE_TICKET) is True
    assert list_news_follows(store, GUILD_ID) == []
    path.write_text(
        '{"guilds": {"1": {"news_follows": ['
        '{"platform": "bilibili", "account": "1265652806123", "topic": "game"},'
        '{"platform": "bilibili", "account": "1265652806123", "topic": "ticket"}'
        "]}}}\n",
        encoding="utf-8",
    )
    loaded = GuildConfig(path)
    follows = list_news_follows(loaded, GUILD_ID)
    assert {follow["topic"] for follow in follows} == {"game", "ticket"}
    assert fill_news_follow(loaded, GUILD_ID, "bilibili", ACCOUNT, label="Studio") is True
    labeled = list_news_follows(GuildConfig(path), GUILD_ID)
    assert all(follow["label"] == "Studio" for follow in labeled)
    assert match_news_follow(follows, ACCOUNT) is None
    assert follow_pick_line(follows, ACCOUNT) == (
        "Account on more than one topic. Pick one."
    )
    add_news_follow(loaded, GUILD_ID, "weibo", ACCOUNT, FEATURE_TICKET)
    mixed = list_news_follows(loaded, GUILD_ID)
    assert follow_pick_line(mixed, ACCOUNT) == (
        "Account on more than one platform. Pick one."
    )
    assert follow_pick_line(mixed, "missing") == (
        "That account is not in the follow list."
    )


def case_source_clock_and_cleared_kickoff(news_store):
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    add_news_follow(news_store, GUILD_ID, "bilibili", "2", FEATURE_GAME)
    add_news_follow(news_store, GUILD_ID, "thesportsdb", "133604", FEATURE_MATCH)
    remember_source(
        news_store,
        [GUILD_ID],
        "bilibili",
        "2",
        "Studio",
        now,
        sport="Soccer",
        kickoffs=now,
        team_id="9",
    )
    post_follow = list_news_follows(news_store, GUILD_ID, FEATURE_GAME)[0]
    assert post_follow["label"] == "Studio"
    assert "fetched_at" not in post_follow
    later_wall = now + timedelta(hours=2)
    assert fill_news_follow(
        news_store,
        GUILD_ID,
        "thesportsdb",
        "133604",
        fetched_at=now,
        next_kickoff=(later_wall, now),
    )
    club = list_news_follows(news_store, GUILD_ID, FEATURE_MATCH)[0]
    assert club["next_kickoff"] == f"{now.isoformat()},{later_wall.isoformat()}"
    assert fill_news_follow(
        news_store,
        GUILD_ID,
        "thesportsdb",
        "133604",
        fetched_at=now,
        next_kickoff=(),
    ) is True
    cleared = list_news_follows(news_store, GUILD_ID, FEATURE_MATCH)[0]
    assert cleared["fetched_at"] == now.isoformat()
    assert "next_kickoff" not in cleared
    assert fill_news_follow(
        news_store,
        GUILD_ID,
        "thesportsdb",
        "133604",
        fetched_at=now,
        next_kickoff=(),
    ) is True


def case_stored_times():
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    assert parse_stored_time("2026-09-05T12:00:00Z") == now
    assert parse_stored_time("2026-09-05T12:00:00") == now
    assert parse_stored_time("nope") is None
    assert parse_stored_time("  ") is None
    assert parse_stored_times(
        "2026-09-05T12:00:00Z, 2026-09-05T12:00:00Z,bad,2026-09-06T00:00:00+00:00"
    ) == (now, datetime(2026, 9, 6, tzinfo=timezone.utc))


def case_topics_follow_the_platform():
    assert topics_for_platform(None) == ()
    assert topics_for_platform("") == ()
    assert topics_for_platform("nope") == ()
    assert topics_for_platform("Bilibili") == (
        FEATURE_GAME,
        FEATURE_TICKET,
        FEATURE_OTHER,
    )
    assert topics_for_platform("weibo") == (FEATURE_GAME, FEATURE_TICKET, FEATURE_OTHER)
    assert topics_for_platform("thesportsdb") == (FEATURE_MATCH,)
    assert topics_for_platform("pandascore") == (FEATURE_MATCH,)
    assert normalize_news_topic(" Game ") == FEATURE_GAME
    assert normalize_news_topic("match") == FEATURE_MATCH
    assert normalize_news_topic("nope") is None
    assert normalize_news_topic("") is None


def case_blank_ticket_returns_to_unassigned(news_store):
    add_news_follow(
        news_store, GUILD_ID, "weibo", "10003", FEATURE_TICKET, "一样音乐工作室"
    )
    assert release_blank_ticket(news_store, GUILD_ID, "weibo", "10003") is True
    moved = list_news_follows(news_store, GUILD_ID)[0]
    assert moved["topic"] == TOPIC_UNASSIGNED
    assert moved["label"] == "一样音乐工作室"
    assert "artist" not in moved
    assert release_blank_ticket(news_store, GUILD_ID, "weibo", "10003") is False

    add_news_follow(news_store, GUILD_ID, "weibo", "10004", FEATURE_TICKET, "薛之谦")
    assert fill_news_follow(news_store, GUILD_ID, "weibo", "10004", artist="薛之谦")
    assert release_blank_ticket(news_store, GUILD_ID, "weibo", "10004") is False
    assert release_blank_ticket(news_store, GUILD_ID, "weibo", "999") is False
    remember_ticket_artist(news_store, [GUILD_ID], "weibo", "10004", "薛之谦工作室")
    saved = list_news_follows(news_store, GUILD_ID, FEATURE_TICKET)[0]
    assert saved["account"] == "10004"
    assert saved["artist"] == "薛之谦工作室"


def case_show_order_and_ref():
    game = [
        {
            "platform": "weibo",
            "account": "2",
            "topic": "game",
            "label": "Bee",
            "added_at": "2026-01-02T00:00:00+00:00",
        },
        {
            "platform": "bilibili",
            "account": "1",
            "topic": "game",
            "label": "Aye",
            "added_at": "2026-01-03T00:00:00+00:00",
        },
        {
            "platform": "bilibili",
            "account": "9",
            "topic": "game",
            "label": "Aye",
            "added_at": "2026-01-01T00:00:00+00:00",
        },
    ]
    assert [
        (item["platform"], item["account"]) for item in sort_news_follows(game, FEATURE_GAME)
    ] == [("bilibili", "9"), ("bilibili", "1"), ("weibo", "2")]
    match_rows = [
        {
            "platform": "pandascore",
            "account": "a",
            "topic": "match",
            "label": "BLG",
            "sport": "Valorant",
            "added_at": "2026-02-01T00:00:00+00:00",
        },
        {
            "platform": "thesportsdb",
            "account": "133604",
            "topic": "match",
            "label": "Arsenal",
            "sport": "Soccer",
            "added_at": "2026-03-01T00:00:00+00:00",
        },
        {
            "platform": "thesportsdb",
            "account": "999001",
            "topic": "match",
            "label": "Unknown Club",
            "added_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "platform": "bilibili",
            "account": "1",
            "topic": "match",
            "label": "Nope",
            "sport": "Soccer",
            "added_at": "2026-04-01T00:00:00+00:00",
        },
    ]
    assert [item["account"] for item in sort_news_follows(match_rows, FEATURE_MATCH)] == [
        "a",
        "133604",
        "1",
        "999001",
    ]
    follow = canonical_follow("bilibili", "99", FEATURE_GAME, "Studio")
    assert news_follow_ref(follow) == "40d1b4446d2c24927951"
    assert match_news_follow([follow], "40d1b4446d2c24927951") == follow
    other = {**follow, "topic": FEATURE_TICKET, "account": "100"}
    assert match_news_follow([follow, other], "Studio") is None
