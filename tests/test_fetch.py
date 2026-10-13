from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

from fetch import (
    SEEN_CAP,
    SEEN_HARD_CAP,
    BookmarkStore,
    FetchedItem,
    FetchError,
    format_fetched_item,
    get_text,
    poll_delay,
    public_fetch_message,
)
from tests.helpers import scratch


async def test_fetch(tmp_path):
    case_bookmarks_delay_and_errors(scratch(tmp_path, "bookmarks_delay_and_errors"))
    await case_get_text_keeps_the_underlying_error()


def case_bookmarks_delay_and_errors(tmp_path):
    case_bookmarks_cap_legacy_keep_only_and_delivered_refs(tmp_path)
    case_judged_words_survive_and_a_bad_file_is_kept(tmp_path)
    case_catch_line_stays_on_the_bookmark(tmp_path)
    case_poll_delay()
    case_format_and_redact_fetch_errors()

def case_bookmarks_cap_legacy_keep_only_and_delivered_refs(tmp_path: Path):
    bookmarks = BookmarkStore(tmp_path / "bookmarks.json")
    first = [f"old-{i}" for i in range(SEEN_CAP)]
    bookmarks.remember("weibo", "1195230310", first)
    bookmarks.remember("weibo", "1195230310", ["new-1"])
    ids = bookmarks.seen_ids("weibo", "1195230310")
    assert len(ids) == SEEN_CAP + 1
    assert "new-1" in ids
    bookmarks.remember("weibo", "1195230310", [f"id-{i}" for i in range(SEEN_HARD_CAP)])
    bookmarks.remember("weibo", "1195230310", ["fresh"])
    hard = bookmarks.seen_ids("weibo", "1195230310")
    assert len(hard) == SEEN_HARD_CAP
    assert "fresh" in hard
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps(
            {
                "bili:99": {"ids": ["post-1"], "seeded": True},
                "weibo:gone": {"ids": []},
            }
        ),
        encoding="utf-8",
    )
    legacy = BookmarkStore(path)
    assert legacy.seen_ids("bilibili", "99") == {"post-1"}
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "bilibili:99": {"ids": ["post-1"]}
    }
    bookmarks = BookmarkStore(tmp_path / "keep.json")
    bookmarks.remember("weibo", "Sports", ["1"])
    bookmarks.remember("bilibili", "99", ["2"])
    assert bookmarks.keep_only([("bilibili", "99")]) == 1
    assert not bookmarks.is_seeded("weibo", "Sports")
    bookmarks.remember("weibo", "1195230310", ["guid-old"])
    bookmarks.mark_delivered("weibo", "1195230310", "guid-new", ["1:game"])
    bookmarks.remember("weibo", "1195230310", ["guid-old"])
    assert bookmarks.delivered_refs("weibo", "1195230310", "guid-new") == {"1:game"}
    bookmarks.remember("weibo", "1195230310", ["guid-new"])
    assert bookmarks.delivered_refs("weibo", "1195230310", "guid-new") == {"1:game"}
    bookmarks.mark_delivered("weibo", "1195230310", "guid-new", ["2:game"])
    assert bookmarks.delivered_refs("weibo", "1195230310", "guid-new") == {
        "1:game",
        "2:game",
    }
    bookmarks.remember("thesportsdb", "Zzzfc United", ["seed"])
    bookmarks.mark_delivered("thesportsdb", "Zzzfc United", "10:next", ["1:match"])
    bookmarks.move("thesportsdb", "Zzzfc United", "133999")
    assert not bookmarks.is_seeded("thesportsdb", "Zzzfc United")
    assert bookmarks.seen_ids("thesportsdb", "133999") == {"seed"}
    assert bookmarks.delivered_refs("thesportsdb", "133999", "10:next") == {"1:match"}
    bookmarks.move("thesportsdb", "Zzzfc United", "133999")
    assert bookmarks.seen_ids("thesportsdb", "133999") == {"seed"}


def case_judged_words_survive_and_a_bad_file_is_kept(tmp_path: Path):
    path = tmp_path / "words.json"
    store = BookmarkStore(path)
    store.remember("bilibili", "9", ["post"])
    store.note_words("bilibili", "9", "post", "abc")
    again = BookmarkStore(path)
    assert again.words("bilibili", "9") == {"post": "abc"}
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    bad = BookmarkStore(broken)
    bad.remember("bilibili", "9", ["post"])
    assert broken.read_text(encoding="utf-8") == "{"


def case_catch_line_stays_on_the_bookmark(tmp_path: Path):
    path = tmp_path / "old.json"
    when = "2026-10-04T12:00:00+00:00"
    later = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    earlier = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    old = {
        "bilibili:1": {
            "delivered": {"1001": ["1:game"]},
            "ids": ["1001"],
            "aliases": {"av10": "1001"},
            "floor": "1001",
            "since": when,
        }
    }
    path.write_text(json.dumps(old))
    store = BookmarkStore(path)
    assert store.seen_ids("bilibili", "1") == {"1001"}
    assert store.catch_line("bilibili", "1")[0] == 1001
    saved = json.loads(path.read_text())["bilibili:1"]
    assert "aliases" not in saved
    assert saved["floor"] == "1001"

    store.remember_line("bilibili", "1", "1002", later)
    assert store.catch_line("bilibili", "1")[0] == 1001
    store.remember("bilibili", "1", ["1002"])
    assert "1001" in store.seen_ids("bilibili", "1")
    assert store.delivered_refs("bilibili", "1", "1001") == {"1:game"}

    store.remember("bilibili", "9", ["3"])
    store.remember_line("bilibili", "9", "3", later)
    store.move("bilibili", "1", "9")
    assert store.catch_line("bilibili", "1") == (None, None)
    assert store.catch_line("bilibili", "9")[0] == 3

    store.remember("bilibili", "8", ["1"])
    store.remember_line("bilibili", "8", "50", earlier)
    store.remember("bilibili", "7", ["2"])
    store.remember_line("bilibili", "7", "80", later)
    store.move("bilibili", "7", "8")
    assert store.catch_line("bilibili", "8")[0] == 50

    assert store.keep_only([("bilibili", "8")]) == 1
    assert store.catch_line("bilibili", "9") == (None, None)
    assert BookmarkStore(path).catch_line("bilibili", "8")[0] == 50


def case_poll_delay():
    rng = random.Random(0)
    assert poll_delay(600, 300, rng) == 600 + random.Random(0).uniform(0, 300)
    assert poll_delay(600, 0, random.Random(0)) == 600
    assert 45 <= poll_delay(45, 15, random.Random(0), min_seconds=0.0) <= 60
    assert poll_delay(0, 0, random.Random(0), min_seconds=0.0) == 0


def case_format_and_redact_fetch_errors():
    raw = "HTTP 412 from http://127.0.0.1:1200/bilibili/user/dynamic/1?key=secret"
    assert "secret" not in str(FetchError(raw))
    assert "key=" not in str(FetchError(raw))
    assert "/bilibili/user/dynamic/1" in str(FetchError(raw))
    blocked = FetchError("HTTP 412 from api", status=412, body="code -352 and key=secret")
    assert blocked.status == 412
    assert "secret" not in str(blocked)
    assert blocked.body.startswith("code -352")
    assert "secret" not in public_fetch_message(raw)
    item = FetchedItem(
        item_id="guid-1",
        title="Patch notes",
        text="A" * 1000,
        url="https://example.test/patch",
        image_url=None,
        platform="weibo",
        account="https://example.test/feed.xml",
        tags=("pc",),
    )
    peek = format_fetched_item(item, peek=800)
    full = format_fetched_item(item)
    assert f"Text: {'A' * 800}…" in peek
    assert "https://example.test/patch" not in peek
    assert "Account:" not in full
    named = format_fetched_item(
        FetchedItem(
            item_id="guid-2",
            title="Flash",
            text="hi",
            url="https://example.test/a",
            image_url=None,
            platform="bilibili",
            account="1265652806",
            source_name="明日方舟",
        )
    )
    assert "Account: 明日方舟" in named
    assert "A" * 1000 in full
    assert "Tags: pc" in full


async def case_get_text_keeps_the_underlying_error():
    async def broken(_url, _headers):
        raise TimeoutError("timed out talking to https://x.test/eventsnext.php?id=1&key=secret")

    try:
        await get_text(
            "https://www.thesportsdb.com/api/v1/json/123/eventsnext.php?id=1",
            getter=broken,
        )
    except FetchError as exc:
        text = str(exc)
    else:
        raise AssertionError("expected FetchError")
    assert "timed out" in text
    assert "eventsnext.php" in text
    assert "key=secret" not in text
    assert "id=1" not in text
