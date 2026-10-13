from __future__ import annotations

import json
import logging
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from config import settings as live_settings
from fetch import FetchError
from news.source.post.common import (
    COOKIE_SKIP_SECONDS,
    DOWN_SKIP_SECONDS,
    CardPicture,
    ExitBook,
    ProxyRest,
    _parse_exit,
    author_face,
    card_picture,
    check_enabled,
    classify_feed_fault,
    embed_image_url,
    format_cookie_header,
    image_size,
    long_shape,
    lookup_exit,
    notice_picture,
    parse_cookie_header,
    pixel,
    share_accounts,
    vet,
    vetted_window,
    weibo_rest,
)
from tests.helpers import COOKIE, scratch

_WEIBO_NOTICE = (
    Path(__file__).resolve().parents[4] / "image/news_image/long/weibo_long.jpg"
).read_bytes()


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    crc = struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + chunk + crc


def _jpeg(width: int, height: int) -> bytes:
    sof = (
        b"\xff\xc0\x00\x0b\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


async def test_post_pictures(monkeypatch):
    case_cookie_pixels_and_embed_urls()
    case_long_shape_keeps_a_normal_frame_and_flags_a_strip()
    case_classify_feed_fault()
    case_share_accounts()
    await case_card_picture_attaches_the_bilibili_notice()
    await case_card_picture_uses_the_notice_for_a_long_picture()
    await case_card_picture_uses_the_notice_when_the_photo_cannot_be_used()
    await case_author_face_downloads_a_weibo_icon(monkeypatch)


def case_cookie_pixels_and_embed_urls():
    assert parse_cookie_header("SESSDATA=s; bili_jct=j; bare; ac_time_value=") == {
        "SESSDATA": "s",
        "bili_jct": "j",
        "ac_time_value": "",
    }
    assert (
        format_cookie_header({"SESSDATA": "s", "bili_jct": "j", "ac_time_value": ""})
        == "SESSDATA=s; bili_jct=j"
    )
    assert pixel(True) is None
    assert pixel(None) is None
    assert pixel(0) is None
    assert pixel(-3) is None
    assert pixel("1080") == 1080
    assert pixel("1080.9") == 1080
    assert pixel("nope") is None
    assert long_shape("1080", "4000") is True

    def item(**kwargs):
        return type("Item", (), kwargs)()

    assert embed_image_url(item(platform="weibo", image_url="https://wx1.sinaimg.cn/a.jpg")) is None
    assert (
        embed_image_url(
            item(
                platform="bilibili",
                image_url="https://i0.hdslb.com/a.jpg",
                image_width=1080,
                image_height=4000,
            )
        )
        is None
    )
    assert (
        embed_image_url(
            item(
                platform="bilibili",
                image_url="https://i0.hdslb.com/a.jpg",
                image_width=1920,
                image_height=1080,
            )
        )
        == "https://i0.hdslb.com/a.jpg"
    )
    assert embed_image_url(item(platform="bilibili", image_url="/relative.jpg")) is None
    assert (
        embed_image_url(item(platform="rss", image_url="https://example.test/a.png"))
        == "https://example.test/a.png"
    )


def case_classify_feed_fault():
    assert classify_feed_fault("bilibili", FetchError("HTTP 401 from api")).cookie
    blocked = classify_feed_fault("bilibili", FetchError("HTTP 412 from api"))
    assert blocked is not None
    assert blocked.stop_round and not blocked.cookie
    mixed = classify_feed_fault(
        "bilibili",
        FetchError("HTTP 412 from api", status=412, body="code -352"),
    )
    assert mixed is not None and mixed.stop_round
    assert not mixed.cookie
    assert (
        classify_feed_fault(
            "bilibili",
            FetchError("Bilibili dynamics returned no posts from api"),
        )
        is None
    )
    assert (
        classify_feed_fault(
            "bilibili",
            FetchError(
                "Bilibili dynamics failed",
                body=(
                    '{"code":0,"message":"0","data":{"has_more":false,"total":"0","items":[]}}'
                ),
            ),
        )
        is None
    )
    assert (
        classify_feed_fault("bilibili", FetchError("HTTP 503 from api")).skip_seconds
        == DOWN_SKIP_SECONDS
    )
    assert classify_feed_fault("bilibili", COOKIE).cookie
    assert classify_feed_fault("unknown", FetchError("HTTP 503 from feed")) is None
    weibo_cool = classify_feed_fault("weibo", FetchError("HTTP 432 from api", status=432))
    assert weibo_cool is not None
    assert weibo_cool.stop_round
    assert not weibo_cool.cookie
    assert weibo_cool.skip_seconds == 0
    weibo_auth = classify_feed_fault("weibo", FetchError("HTTP 403 from api", status=403))
    assert weibo_auth is not None and weibo_auth.cookie
    assert weibo_auth.skip_seconds == COOKIE_SKIP_SECONDS
    guest = classify_feed_fault("weibo", FetchError("Weibo guest rejected"))
    assert guest is not None
    assert guest.stop_round
    assert not guest.cookie
    assert guest.skip_seconds == 0

def case_share_accounts():
    shares, spent = share_accounts(("a", "b", "c", "d", "e", "f"), ("p0", "p1", "p2"), ())
    assert spent == ()
    assert [(share.proxy, share.accounts) for share in shares] == [
        ("p0", ("a", "b")),
        ("p1", ("c", "d")),
        ("p2", ("e", "f")),
    ]
    shares, spent = share_accounts(("a", "b", "c", "d", "e", "f"), ("p0", "p1", "p2"), ("p0",))
    assert spent == ("p0",)
    assert [(share.proxy, share.accounts) for share in shares] == [
        ("p1", ("a", "b", "c")),
        ("p2", ("d", "e", "f")),
    ]
    shares, spent = share_accounts(("a", "b"), ("p0", "p1", "p2"), ())
    assert [share.proxy for share in shares] == ["p0", "p1"]
    assert [share.accounts for share in shares] == [("a",), ("b",)]
    shares, spent = share_accounts(("a",), (), ())
    assert spent == ()
    assert [(share.proxy, share.accounts) for share in shares] == [("", ("a",))]
    shares, spent = share_accounts((), ("p0",), ("p0",))
    assert shares == [] and spent == ()
    shares, spent = share_accounts(("a", "b"), ("p0",), ("p0",))
    assert shares == [] and spent == ("p0",)


def case_long_shape_keeps_a_normal_frame_and_flags_a_strip():
    assert long_shape(1920, 1080) is False
    assert long_shape(1080, 1920) is False
    assert long_shape(1080, 2160) is False
    assert long_shape(1080, 2161) is True
    assert long_shape(3000, 1000) is True
    assert long_shape(None, 4000) is False
    assert image_size(_jpeg(1080, 4000)) == (1080, 4000)
    assert image_size(b"\xff\xd8\xff") is None
    assert image_size(_png(32, 16)) == (32, 16)
    assert image_size(b"GIF89a" + (8).to_bytes(2, "little") + (4).to_bytes(2, "little")) == (8, 4)
    webp = bytearray(30)
    webp[0:4] = b"RIFF"
    webp[8:12] = b"WEBP"
    webp[12:16] = b"VP8X"
    webp[24:27] = (63).to_bytes(3, "little")
    webp[27:30] = (31).to_bytes(3, "little")
    assert image_size(bytes(webp)) == (64, 32)


async def case_card_picture_attaches_the_bilibili_notice():
    class _Item:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    notice_path = Path(__file__).resolve().parents[4] / "image/news_image/long/bilibili_long.jpg"
    notice = notice_path.read_bytes()
    tall = await card_picture(
        _Item(
            platform="bilibili",
            image_url="https://i0.hdslb.com/bfs/tall.jpg",
            image_width=1080,
            image_height=4000,
        )
    )
    assert tall is not None
    assert tall.filename == "bilibili_long.jpg"
    assert tall.data == notice
    assert notice_picture(notice_path).data == notice
    cover = await card_picture(
        _Item(
            platform="bilibili",
            image_url="https://i0.hdslb.com/bfs/cover.png",
            image_width=1920,
            image_height=1080,
        )
    )
    assert cover is None
    assert await card_picture(_Item(platform="rss", image_url="https://example.test/a.png")) is None


async def case_card_picture_uses_the_notice_for_a_long_picture():
    class _Item:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    known = await card_picture(
        _Item(
            platform="weibo",
            image_url="https://wx1.sinaimg.cn/large/tall.jpg",
            image_width=1080,
            image_height=4320,
        )
    )
    assert known is not None
    assert known.filename == "weibo_long.jpg"
    assert known.data == _WEIBO_NOTICE

    async def call(_url, _headers):
        return 200, "image/png", _png(100, 400)

    measured = await card_picture(
        _Item(platform="weibo", image_url="https://wx1.sinaimg.cn/large/tall.jpg"),
        call=call,
    )
    assert measured is not None
    assert measured.data == _WEIBO_NOTICE

    async def wide(_url, _headers):
        return 200, "image/png", _png(160, 90)

    cover = await card_picture(
        _Item(platform="weibo", image_url="https://wx1.sinaimg.cn/large/pic.jpg"),
        call=wide,
    )
    assert cover is not None
    assert cover.filename == "cover.png"
    assert cover.data == _png(160, 90)


async def case_card_picture_uses_the_notice_when_the_photo_cannot_be_used():
    class _Item:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    async def heavy(_url, _headers):
        return 200, "image/jpeg", b"x" * (2_000_001)

    heavy_card = await card_picture(
        _Item(platform="weibo", image_url="https://wx4.sinaimg.cn/mw2000/pic.jpg"),
        call=heavy,
    )
    assert heavy_card is not None
    assert heavy_card.filename == "weibo_long.jpg"
    assert heavy_card.data == _WEIBO_NOTICE

    async def blocked(_url, _headers):
        return 403, "text/html", b"<html>"

    blocked_card = await card_picture(
        _Item(platform="weibo", image_url="https://wx1.sinaimg.cn/large/pic.jpg"),
        call=blocked,
    )
    assert blocked_card is not None
    assert blocked_card.data == _WEIBO_NOTICE

    async def junk(_url, _headers):
        return 200, "image/jpeg", b"\xff\xd8\xff"

    illegal = await card_picture(
        _Item(platform="weibo", image_url="https://wx1.sinaimg.cn/large/pic.jpg"),
        call=junk,
    )
    assert illegal is not None
    assert illegal.data == _WEIBO_NOTICE
    assert await card_picture(_Item(platform="weibo", image_url=None)) is None


async def case_author_face_downloads_a_weibo_icon(monkeypatch):
    class _Item:
        platform = "weibo"
        author_icon = "https://tvax1.sinaimg.cn/crop/s.jpg"

    async def fake_cover(url, *, name="cover"):
        assert url == _Item.author_icon
        assert name == "face"

        return CardPicture(b"face", "face.jpg")

    monkeypatch.setattr("news.source.post.weibo.preview.fetch_cover", fake_cover)
    face = await author_face(_Item())
    assert face is not None
    assert face.filename == "face.jpg"
    bare = type("Bare", (), {"platform": "weibo", "author_icon": None})()
    assert await author_face(bare) is None
    bili = type("Bili", (), {"platform": "bilibili", "author_icon": "https://i0.hdslb.com/x.jpg"})()
    assert await author_face(bili) is None


@dataclass
class _Node:
    proxy: str
    accounts: tuple[str, ...]


def _pairs(accounts, proxies, _turn, _sitting):
    """A stand-in plan: two accounts on each proxy, in order."""
    nodes = [_Node(proxy, tuple(accounts[n * 2 : n * 2 + 2])) for n, proxy in enumerate(proxies)]
    return [node for node in nodes if node.accounts], ()


def _exit_table(*pairs: tuple[str, str | None]):
    found = dict(pairs)

    async def lookup(proxy: str) -> str | None:
        return found[proxy]

    return lookup


async def test_exits_and_rests(monkeypatch, fresh, caplog, tmp_path):
    case_parse_exit_keeps_a_single_address(fresh)
    with monkeypatch.context() as mp:
        await case_lookup_reads_the_body_through_the_proxy(mp, fresh)
    await case_a_failed_or_shared_exit_sits_out(caplog, fresh)
    await case_a_moved_exit_refreshes_once_and_a_first_sight_does_not(fresh)
    case_a_bad_exit_file_is_ignored(scratch(tmp_path, "a_bad_exit_file_is_ignored"), caplog, fresh)
    with monkeypatch.context() as mp:
        await case_a_failed_node_leaves_the_window_and_the_plan_is_redone(mp, fresh)
    with monkeypatch.context() as mp:
        await case_check_off_does_not_look_up(mp, fresh)
    case_weibo_432_rest_is_saved(fresh)


def case_parse_exit_keeps_a_single_address(fresh):
    fresh()
    assert _parse_exit(" 203.0.113.8\n") == "203.0.113.8"
    assert _parse_exit("2001:db8::1") == "2001:db8::1"
    assert _parse_exit("not an ip") is None
    assert _parse_exit("1.2.3.4 extra") is None
    assert _parse_exit("") is None


async def case_lookup_reads_the_body_through_the_proxy(monkeypatch, fresh):
    fresh()
    sent: list[tuple[str, dict]] = []
    answers = iter(
        (
            (200, " 203.0.113.8\n"),
            (200, "nope"),
            (500, "203.0.113.8"),
        )
    )

    class _Response:
        def __init__(self, status: int, body: str) -> None:
            self.status = status
            self._body = body

        async def text(self) -> str:
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Session:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def get(self, url, **kwargs):
            sent.append((url, kwargs))
            status, body = next(answers)
            return _Response(status, body)


    monkeypatch.setattr(aiohttp, "ClientSession", _Session)
    assert await lookup_exit("http://user:secret@127.0.0.1:7911") == "203.0.113.8"
    assert sent[0][1]["proxy"] == "http://user:secret@127.0.0.1:7911"
    assert await lookup_exit("http://p") is None
    assert await lookup_exit("") is None
    assert "proxy" not in sent[2][1]

    class _Down:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            raise OSError("down")

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(aiohttp, "ClientSession", _Down)
    assert await lookup_exit("http://p") is None


async def case_a_failed_or_shared_exit_sits_out(caplog, fresh):
    fresh()
    caplog.clear()
    caplog.set_level(logging.INFO, logger="news.source.post.common")
    refreshed: list[str] = []
    ok, taken = await vet(
        ("http://user:secret@h:1", "http://b", "http://c"),
        {},
        platform="bilibili",
        refresh=refreshed.append,
        refresh_note="fresh phone ids",
        lookup=_exit_table(
            ("http://user:secret@h:1", None),
            ("http://b", "203.0.113.1"),
            ("http://c", "203.0.113.1"),
        ),
    )
    assert ok == ("http://b",)
    assert taken == {"203.0.113.1": "http://b"}
    assert refreshed == []
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "Bilibili h:1 sits out this check: exit check failed" in text
    assert "Bilibili c sits out this check: same exit as b" in text
    assert "secret" not in text


async def case_a_moved_exit_refreshes_once_and_a_first_sight_does_not(fresh):
    fresh()
    from news.source.post.common import exits

    refreshed: list[str] = []
    exits.remember("bilibili", "http://a", "203.0.113.1")
    ok, _taken = await vet(
        ("http://a", "http://b"),
        {},
        platform="bilibili",
        refresh=refreshed.append,
        refresh_note="fresh phone ids",
        lookup=_exit_table(("http://a", "203.0.113.9"), ("http://b", "203.0.113.2")),
    )
    assert ok == ("http://a", "http://b")
    assert refreshed == ["http://a"]
    ok, _taken = await vet(
        ("http://a",),
        {},
        platform="bilibili",
        refresh=refreshed.append,
        lookup=_exit_table(("http://a", "203.0.113.9")),
    )
    assert ok == ("http://a",)
    assert refreshed == ["http://a"]
    assert exits.changed("bilibili", "http://a", "203.0.113.9") is False
    saved = json.loads(exits.path.read_text(encoding="utf-8"))
    assert saved["bilibili"]["http://b"] == "203.0.113.2"


def case_a_bad_exit_file_is_ignored(tmp_path, caplog, fresh):
    fresh()
    caplog.clear()
    caplog.set_level(logging.WARNING, logger="news.source.post.common")
    path = tmp_path / "proxy_exits.json"
    path.write_text("{", encoding="utf-8")
    book = ExitBook(path)
    assert book.changed("weibo", "http://a", "203.0.113.1") is False
    assert any("Could not read" in record.getMessage() for record in caplog.records)


async def case_a_failed_node_leaves_the_window_and_the_plan_is_redone(monkeypatch, fresh):
    fresh()

    monkeypatch.setattr(live_settings, "ip_check", True)
    calls: list[str] = []

    async def lookup(proxy: str) -> str | None:
        calls.append(proxy)
        if proxy == "http://p0":
            return None
        return f"203.0.113.{proxy[-1]}"

    phones, spent, pool, taken = await vetted_window(
        ("1", "2", "3", "4"),
        tuple(f"http://p{n}" for n in range(6)),
        0,
        (),
        plan=_pairs,
        platform="bilibili",
        refresh_note="fresh phone ids",
        lookup=lookup,
    )
    assert [phone.proxy for phone in phones] == ["http://p1", "http://p2"]
    assert [phone.accounts for phone in phones] == [("1", "2"), ("3", "4")]
    assert spent == ()
    assert pool[0] == "http://p1"
    assert "http://p0" not in pool
    assert taken["203.0.113.1"] == "http://p1"
    assert calls.count("http://p1") == 1


async def case_check_off_does_not_look_up(monkeypatch, fresh):
    fresh()

    monkeypatch.setattr(live_settings, "ip_check", False)
    assert check_enabled() is False

    async def lookup(_proxy: str) -> str | None:
        raise AssertionError("lookup")

    phones, _spent, pool, taken = await vetted_window(
        ("1", "2", "3", "4"),
        ("http://p0", "http://p1"),
        0,
        (),
        plan=_pairs,
        platform="weibo",
        lookup=lookup,
    )
    assert [phone.proxy for phone in phones] == ["http://p0", "http://p1"]
    assert pool == ("http://p0", "http://p1")
    assert taken == {}


def case_weibo_432_rest_is_saved(fresh):
    fresh()


    weibo_rest.rest("http://p0")
    assert json.loads(weibo_rest.path.read_text(encoding="utf-8")) == {
        "http://p0": {"sit_out": True}
    }
    assert ProxyRest(weibo_rest.path).sits_out("http://p0")
    assert ProxyRest(weibo_rest.path).sits_out("http://p1") is False
    assert weibo_rest.take("http://p0")
    assert ProxyRest(weibo_rest.path).sits_out("http://p0") is False
