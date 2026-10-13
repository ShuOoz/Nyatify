"""The app DynSpace client. The poll reads this feed."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
from datetime import datetime

import pytest
from curl_cffi.requests.exceptions import RequestException

import news.source.post.bilibili.app as app
from config import settings as live_settings
from fetch import FetchError
from news.source.post.bilibili import is_live_room, should_skip
from news.source.post.bilibili.app import (
    _FAILOVER_URL,
    _MODEL,
    DYN_SPACE_URL,
    PhoneBook,
    _device_fp,
    _failover_headers,
    _grpc_post,
    _headers,
    _read_grpc,
    app_buvid,
    bind_proxy,
    close_app_http,
    fetch_app_page,
    grpc_message,
    items_from_app,
    reset_proxy,
    saved_buvid,
    stored_buvid,
)
from news.source.post.bilibili.protobuf import decode_message, encode_message
from tests.helpers import scratch


def _saved_phone(book: PhoneBook, proxy: str, index: int = 0) -> str | None:
    slot = index if index > 0 else 0
    found = book._memory.get(proxy, {}).get(slot) or book._load().get(proxy, {}).get(slot)
    return found.buvid if found else None

UID = "1195230310"
DYN = "850406986313893918"
FACE = "https://i0.hdslb.com/bfs/face/s.jpg"
IMG = "//i0.hdslb.com/bfs/x.jpg"


def test_app_frames_and_phones(monkeypatch, tmp_path):
    with monkeypatch.context() as mp:
        case_app_cards_and_frames(scratch(tmp_path, "app_cards_and_frames"), mp)
    case_forgetting_a_proxy_mints_a_new_phone(scratch(tmp_path, "forgetting_a_proxy_mints_a_new_phone"))


def case_app_cards_and_frames(tmp_path, monkeypatch):
    case_word_draw_video_opus_and_article()
    case_real_dynamic_ids_keep_the_site_clock()
    case_a_space_between_text_nodes_is_kept()
    case_a_sticker_is_left_out_of_the_caption()
    case_forward_live_charge_and_a_blank()
    case_a_draw_with_a_reserve_is_the_show()
    case_grpc_status_and_http_errors()
    case_buvid_and_device_fp()
    case_saved_buvid_is_reused(tmp_path)
    case_a_phone_stays_with_its_proxy(tmp_path, monkeypatch)
    case_an_older_phone_keeps_its_story(tmp_path)
    case_an_unsaved_buvid_is_not_written(tmp_path)


async def test_fetch_app_dynamics(monkeypatch):
    await case_fetch_posts_one_dynspace_card()
    await case_fetch_reads_a_gzip_frame_and_rejects_junk()
    await case_fetch_rejects_a_bad_uid()
    await case_connection_failure_uses_the_spare_host(monkeypatch)
    await case_http_412_does_not_try_the_spare_host(monkeypatch)
    await case_grpc_post_uses_the_bound_proxy(monkeypatch)
    await case_close_app_http_ignores_a_broken_session()
    await case_a_closed_connection_keeps_the_phone_story()


def case_word_draw_video_opus_and_article():
    name, items = _cards(
        _card(
            DYN,
            _author("Studio"),
            _desc("hello\nthere"),
        ),
        _card(
            "11",
            _author("Other"),
            {5: {7: {1: [{1: IMG, 2: 1080, 3: 4000}]}}},
        ),
    )
    assert name == "Studio"
    word, draw = items
    assert word.title == "hello"
    assert word.text == "hello\nthere"
    assert word.url == f"https://www.bilibili.com/opus/{DYN}"
    assert word.account == UID
    assert word.source_name == "Studio"
    assert word.author_icon == FACE
    assert word.occurred_at is not None
    assert draw.source_name == "Other"
    assert draw.image_url == "https://i0.hdslb.com/bfs/x.jpg"
    assert (draw.image_width, draw.image_height) == (1080, 4000)
    assert draw.occurred_at is None

    _name, videos = _cards(
        _card(DYN, _author("Studio"), {5: {2: {1: "clip", 2: "https://i0.hdslb.com/a.jpg"}}})
    )
    assert videos[0].title == "clip"
    assert videos[0].text == "clip"
    assert videos[0].image_url == "https://i0.hdslb.com/a.jpg"

    _name, essays = _cards(
        _card(
            DYN,
            _author("Studio"),
            {
                5: {
                    8: {
                        3: "Column",
                        4: "a long note",
                        5: ["https://i0.hdslb.com/c.jpg"],
                    }
                }
            },
        )
    )
    assert essays[0].title == "Column"
    assert essays[0].text == "a long note"
    assert essays[0].image_url == "https://i0.hdslb.com/c.jpg"

    _name, photos = _cards(
        _card(
            DYN,
            _author("Studio"),
            _desc("cats"),
            {
                30: {
                    1: _paragraph("Photo day"),
                    2: _paragraph("cats", word=True),
                    4: [{1: "https://i0.hdslb.com/p.jpg", 2: 640, 3: 480}],
                }
            },
        )
    )
    photo = photos[0]
    assert photo.title == "Photo day"
    assert photo.text == "cats"
    assert photo.image_url == "https://i0.hdslb.com/p.jpg"
    assert (photo.image_width, photo.image_height) == (640, 480)


def case_real_dynamic_ids_keep_the_site_clock():
    samples = {
        "1251943371511955464": "2026-09-25T09:39:41+00:00",
        "1251467016719564817": "2026-09-24T02:51:11+00:00",
    }
    for dyn_id, when in samples.items():
        _name, items = _cards(_card(dyn_id, _author("Studio"), _desc("hi")))
        assert items[0].occurred_at is not None
        assert items[0].occurred_at.isoformat() == when


def case_a_space_between_text_nodes_is_kept():
    caption = {4: {1: [{1: " 黑皮"}, {1: " "}, {1: "辣妹 "}]}}
    body = {3: {1: [{2: "原来"}, {2: " "}, {2: "娜嘉"}]}}
    _name, items = _cards(
        _card(
            DYN,
            _author("Studio"),
            caption,
            {30: {2: body}},
        )
    )
    assert items[0].text == "黑皮 辣妹\n原来 娜嘉"

    _name, linked = _cards(
        _card(DYN, _author("Studio"), {30: {2: {3: {1: [{3: {1: "手机"}}]}}}})
    )
    assert linked[0].text == "手机"


def case_a_sticker_is_left_out_of_the_caption():
    caption = {4: {1: [{1: "见 [备注]", 2: 1}, {1: "[呆]", 2: 9}], 3: "见 [备注][呆]"}}
    _name, items = _cards(_card(DYN, _author("Studio"), caption))
    assert items[0].text == "见 [备注]"

    body = {3: {1: [{1: 1, 2: "抱歉"}, {1: 2, 2: "[呆]"}]}}
    _name, opus = _cards(_card(DYN, _author("Studio"), {30: {2: body}}))
    assert opus[0].text == "抱歉"

    kept = {4: {1: [{1: "short", 2: 1}], 3: "the real [备注]"}}
    _name, plain = _cards(_card(DYN, _author("Studio"), kept))
    assert plain[0].text == "the real [备注]"

    dirty = {3: {1: [{2: "见 [备注][呆]"}]}}
    _name, twin = _cards(_card(DYN, _author("Studio"), caption, {30: {2: dirty}}))
    assert twin[0].text == "见 [备注]"


def case_forward_live_charge_and_a_blank():
    nested = {
        3: [
            _author("Other", face="https://i0.hdslb.com/bfs/face/o.jpg"),
            _desc("cats"),
            {5: {7: {1: [{1: "https://i0.hdslb.com/x.jpg", 2: 10, 3: 12}]}}},
        ]
    }
    _name, forwarded = _cards(
        _card(DYN, _desc("look"), {5: {6: {1: nested}}})
    )
    post = forwarded[0]
    assert post.text == "look\n转发了Other的动态\ncats"
    assert post.image_url == "https://i0.hdslb.com/x.jpg"
    assert post.source_name is None
    assert should_skip(post) is True

    _name, lives = _cards(
        _card(DYN, _author("Studio"), {5: {11: {1: 9, 3: "开播了", 4: "https://i0.hdslb.com/l.jpg"}}})
    )
    live = lives[0]
    assert live.title == "开播了"
    assert "https://live.bilibili.com/9" in live.text
    assert live.image_url == "https://i0.hdslb.com/l.jpg"
    assert is_live_room(live) is True

    raw = json.dumps(
        {"live_play_info": {"room_id": 3, "title": "晚场", "cover": "https://i0.hdslb.com/r.jpg"}}
    )
    _name, rcmd = _cards(_card(DYN, _author("Studio"), {5: {15: {1: raw}}}))
    assert rcmd[0].title == "晚场"
    assert "https://live.bilibili.com/3" in rcmd[0].text
    assert rcmd[0].image_url == "https://i0.hdslb.com/r.jpg"

    _name, locked = _cards(
        _card(
            DYN,
            _author("Studio", charge=True),
            {33: {3: "充电专属动态，加入即可解锁观看"}},
        )
    )
    assert locked[0].tags == ("充电专属",)
    assert locked[0].text == "充电专属动态，加入即可解锁观看"
    assert should_skip(locked[0]) is True

    name, blanks = _cards({4: {1: "11"}})
    assert name is None and blanks == []


def case_a_draw_with_a_reserve_is_the_show():
    _name, items = _cards(
        _card(
            "1251943371511955464",
            _author("Studio"),
            {5: {7: {1: [{1: IMG, 2: 2160, 3: 3840}]}}},
            {
                8: {
                    10: {
                        1: "直播预约：「丹青渡」版本前瞻特别节目",
                        2: {1: "10-06 19:30 直播"},
                    }
                }
            },
        )
    )
    item = items[0]
    assert item.text == "直播预约：「丹青渡」版本前瞻特别节目 10月6日 19:30"
    assert item.title == item.text
    assert item.tags == ("watch:2026-10-6-19:30",)
    assert item.image_url == "https://i0.hdslb.com/bfs/x.jpg"


def case_grpc_status_and_http_errors():
    with pytest.raises(FetchError) as caught:
        _read_grpc(412, {}, b"no")
    assert caught.value.status == 412
    with pytest.raises(FetchError, match=r"failed \(14\)"):
        _read_grpc(200, {"grpc-status": "14", "grpc-message": "unavailable"}, b"")
    assert _read_grpc(200, {"grpc-status": "0"}, b"ok") == b"ok"


def case_buvid_and_device_fp():
    digest = hashlib.md5(b"seed").hexdigest().upper()
    buvid = app_buvid("seed")
    assert buvid == f"XU{digest[2]}{digest[12]}{digest[22]}{digest}"
    assert len(buvid) == 37
    fp = _device_fp(buvid, now=datetime(2026, 10, 1, 12, 0, 0), nonce="ab" * 8)
    assert fp.startswith(hashlib.md5(f"{buvid}{_MODEL}".encode()).hexdigest())
    assert fp[32:46] == "20261001120000"
    assert fp[46:62] == "ab" * 8
    check = sum(int(fp[index:index + 2], 16) for index in range(0, 62, 2)) % 256
    assert fp[62:] == f"{check:02x}"


def case_saved_buvid_is_reused(tmp_path):
    path = tmp_path / "phone.json"
    path.write_text("nope\n", encoding="utf-8")
    store = PhoneBook(path)
    previous = app.buvid_store
    app.buvid_store = store
    try:
        assert stored_buvid() is None
        assert path.read_text(encoding="utf-8") == "nope\n"
        first = saved_buvid()
        assert stored_buvid() == first
        assert first == saved_buvid()
        assert _headers(first)["buvid"] == first
        again = PhoneBook(path)
        assert again.get() == first
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert _phone_id(saved[""]) == first
        assert saved[""]["fp"] == again.fingerprint(first)[0]
    finally:
        app.buvid_store = previous


def case_a_phone_stays_with_its_proxy(tmp_path, monkeypatch):
    path = tmp_path / "phones.json"
    calls = {"n": 0}

    def mint(seed: str = "") -> str:
        calls["n"] += 1
        return app_buvid(str(calls["n"]))

    book = PhoneBook(path)
    first = book.get("http://a", mint=mint)
    assert book.get("http://a", mint=mint) == first
    assert calls["n"] == 1
    second = book.get("http://b", mint=mint)
    assert calls["n"] == 2
    assert first != second
    assert book.get("http://a") == first
    assert _saved_phone(book, "http://b") == second
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert _phone_id(saved["http://a"]) == first
    assert _phone_id(saved["http://b"]) == second
    assert saved["http://a"]["fp"] != saved["http://b"]["fp"]
    again = PhoneBook(path)
    assert again.get("http://a") == first
    assert again.get("http://b") == second

    legacy = tmp_path / "legacy.json"
    old = app_buvid("old")
    legacy.write_text(old + "\n", encoding="utf-8")
    monkeypatch.setattr(live_settings, "bili_proxies", ("http://only",))
    moved = PhoneBook(legacy)
    assert moved.get("http://only") == old
    other = moved.get("http://other", mint=mint)
    assert other != old
    assert _saved_phone(moved, "http://only") == old
    second_on_only = moved.get("http://only", mint=mint, index=1)
    assert second_on_only != old
    assert moved.get("http://only", index=1) == second_on_only
    assert _saved_phone(moved, "http://only") == old
    assert _saved_phone(moved, "http://only", 1) == second_on_only
    kept = json.loads(legacy.read_text(encoding="utf-8"))["http://only"]
    assert [_phone_id(item) for item in kept] == [old, second_on_only]
    reloaded = PhoneBook(legacy)
    assert _saved_phone(reloaded, "http://only", 1) == second_on_only
    assert reloaded.fingerprint(old) == (kept[0]["fp"], kept[0]["fts"])
    assert reloaded.get("http://other", index=1) != second_on_only


def case_an_older_phone_keeps_its_story(tmp_path):
    path = tmp_path / "old.json"
    buvid = app_buvid("old")
    path.write_text(json.dumps({"": buvid}) + "\n", encoding="utf-8")
    book = PhoneBook(path)
    assert book.get() == buvid
    saved = json.loads(path.read_text(encoding="utf-8"))[""]
    assert saved["buvid"] == buvid
    assert len(saved["fp"]) == 64 and saved["fts"] > 0
    again = PhoneBook(path)
    assert again.get() == buvid
    assert json.loads(path.read_text(encoding="utf-8"))[""] == saved
    assert again.fingerprint(buvid) == (saved["fp"], saved["fts"])


def case_an_unsaved_buvid_is_not_written(tmp_path):
    path = tmp_path / "empty.json"
    store = PhoneBook(path)
    previous = app.buvid_store
    app.buvid_store = store
    try:
        buvid = app_buvid("loose")
        first = _headers(buvid)
        second = _headers(buvid)
        assert _story(first) == _story(second)
        assert not path.exists()
    finally:
        app.buvid_store = previous


async def case_fetch_posts_one_dynspace_card():
    buvid = app_buvid("seed")
    seen: dict = {}

    async def post(body: bytes, headers):
        seen["body"] = body
        seen["headers"] = dict(headers)
        card = _card(DYN, _author("Studio"), _desc("hello"))
        return _frame(encode_message("DynSpaceRsp", {1: [card]}))

    name, items, *_ = await fetch_app_page(UID, buvid=buvid, post=post)
    headers = seen["headers"]
    assert headers["content-type"] == "application/grpc"
    assert headers["buvid"] == buvid
    assert headers["user-agent"].startswith("Dalvik/2.1.0 (Linux; U; Android 13; Pixel 6 Build/oriole)")
    assert headers["user-agent"].endswith("grpc-java-cronet/1.36.1")
    assert "build/7380300" in headers["user-agent"]
    assert headers["te"] == "trailers"
    assert "bili-http-engine" not in headers
    assert headers["x-bili-restriction-bin"] == ""
    assert headers["x-bili-exps-bin"] == ""
    spare = _failover_headers(headers)
    assert "te" not in spare
    assert spare["bili-http-engine"] == "cronet"
    assert not spare["user-agent"].endswith("grpc-java-cronet/1.36.1")
    assert spare["buvid"] == buvid
    assert "sec-ch-ua" not in headers
    token, rest, zero, end = headers["x-bili-trace-id"].split(":")
    assert re.fullmatch(r"[0-9a-f]{32}", token)
    assert rest == token[16:] and zero == end == "0"
    metadata = decode_message("Metadata", base64.b64decode(headers["x-bili-metadata-bin"]))
    device = decode_message("Device", base64.b64decode(headers["x-bili-device-bin"]))
    fawkes = decode_message("FawkesReq", base64.b64decode(headers["x-bili-fawkes-req-bin"]))
    network = decode_message("Network", base64.b64decode(headers["x-bili-network-bin"]))
    locale = decode_message("Locale", base64.b64decode(headers["x-bili-locale-bin"]))
    assert metadata[6] == buvid
    assert device[3] == buvid
    assert device[9] == _MODEL
    assert device[11] == device[12] == device[14]
    assert len(device[11]) == 64
    assert fawkes[1] == "android64" and fawkes[2] == "prod" and len(fawkes[3]) == 8
    assert network[1] == 1
    assert locale[1][1] == "zh" and locale[1][3] == "CN"
    assert locale[4] == "Asia/Shanghai"
    request = decode_message("DynSpaceReq", grpc_message(seen["body"]))
    assert request == {1: int(UID), 4: 8, 5: 1, 6: "space"}
    assert seen["body"].startswith(b"\x00")
    assert name == "Studio"
    assert items[0].text == "hello"
    assert DYN_SPACE_URL.endswith("/DynSpace")


async def case_fetch_reads_a_gzip_frame_and_rejects_junk():
    card = _card(DYN, _author("Studio"), _desc("zipped"))
    payload = encode_message("DynSpaceRsp", {1: [card]})
    compressed = gzip.compress(payload)

    async def post(body: bytes, headers):
        return bytes([1]) + len(compressed).to_bytes(4, "big") + compressed

    name, items, *_ = await fetch_app_page(UID, buvid=app_buvid("seed"), post=post)
    assert name == "Studio" and items[0].text == "zipped"

    async def junk(body: bytes, headers):
        return b"nope"

    with pytest.raises(FetchError, match="not a DynSpace"):
        await fetch_app_page(UID, post=junk)


async def case_fetch_rejects_a_bad_uid():
    async def post(body: bytes, headers):
        raise AssertionError("bad uid must not be sent")

    with pytest.raises(FetchError, match="numeric uid"):
        await fetch_app_page("space", post=post)


class _Reply:
    def __init__(self, status: int, body: bytes = b"ok", headers: dict | None = None) -> None:
        self.status_code = status
        self.content = body
        self.headers = headers or {"grpc-status": "0"}


async def case_connection_failure_uses_the_spare_host(monkeypatch):
    seen: list[tuple[str, dict]] = []

    class Session:
        async def post(self, url, **kwargs):
            seen.append((url, kwargs["headers"]))
            if url == DYN_SPACE_URL:
                raise RequestException("timeout")
            return _Reply(200)

    async def session():
        return Session()

    monkeypatch.setattr("news.source.post.bilibili.app._app_session", session)
    body = await _grpc_post(b"\x00", _headers("XU" + "A" * 35))
    assert body == b"ok"
    assert [url for url, _headers_sent in seen] == [DYN_SPACE_URL, _FAILOVER_URL]
    primary, spare = seen
    assert primary[1]["te"] == "trailers"
    assert "bili-http-engine" not in primary[1]
    assert "te" not in spare[1]
    assert spare[1]["bili-http-engine"] == "cronet"
    assert spare[1]["user-agent"] == primary[1]["user-agent"].removesuffix(" grpc-java-cronet/1.36.1")


async def case_http_412_does_not_try_the_spare_host(monkeypatch):
    seen: list[str] = []

    class Session:
        async def post(self, url, **kwargs):
            seen.append(url)
            return _Reply(412, b"no", {})

    async def session():
        return Session()

    monkeypatch.setattr("news.source.post.bilibili.app._app_session", session)
    with pytest.raises(FetchError) as caught:
        await _grpc_post(b"\x00", _headers("XU" + "B" * 35))
    assert caught.value.status == 412
    assert seen == [DYN_SPACE_URL]


async def case_grpc_post_uses_the_bound_proxy(monkeypatch):
    seen: list[str | None] = []

    class Session:
        async def post(self, url, **kwargs):
            seen.append(kwargs.get("proxy"))
            return _Reply(200)

    async def session():
        return Session()

    monkeypatch.setattr("news.source.post.bilibili.app._app_session", session)
    monkeypatch.setattr(live_settings, "bili_proxy", None)
    await _grpc_post(b"\x00", _headers("XU" + "C" * 35))
    token = bind_proxy("http://phone-proxy:8080")
    try:
        await _grpc_post(b"\x00", _headers("XU" + "C" * 35))
    finally:
        reset_proxy(token)
    direct = bind_proxy("")
    try:
        await _grpc_post(b"\x00", _headers("XU" + "C" * 35))
    finally:
        reset_proxy(direct)
    assert seen == [None, "http://phone-proxy:8080", None]


async def case_a_closed_connection_keeps_the_phone_story():
    buvid = saved_buvid()
    first = _headers(buvid)
    await close_app_http()
    second = _headers(buvid)
    assert _story(first) == _story(second)
    assert first["x-bili-trace-id"] != second["x-bili-trace-id"]


async def case_close_app_http_ignores_a_broken_session():
    class Broken:
        async def close(self):
            raise RuntimeError("reset")

    broken = Broken()
    app._session = broken
    app._session_loop = object()
    try:
        await app.close_app_http()
    finally:
        app._session = None
        app._session_loop = None
    assert app._session is None


def _phone_id(value: object) -> str:
    if isinstance(value, str):
        return value
    assert isinstance(value, dict)
    return str(value["buvid"])


def _story(headers: dict) -> tuple[str, int, str]:
    device = decode_message("Device", base64.b64decode(headers["x-bili-device-bin"]))
    fawkes = decode_message("FawkesReq", base64.b64decode(headers["x-bili-fawkes-req-bin"]))
    assert device[11] == device[12] == device[14]
    return device[11], device[15], fawkes[3]


def _cards(*cards: dict) -> tuple[str | None, list]:
    payload = decode_message("DynSpaceRsp", encode_message("DynSpaceRsp", {1: list(cards)}))
    return items_from_app(payload, account=UID)


def _card(dyn_id: str, *modules: dict) -> dict:
    return {3: list(modules), 4: {1: dyn_id}}


def _author(name: str, *, face: str = FACE, charge: bool = False) -> dict:
    author: dict = {3: {2: name, 3: face}}
    if charge:
        author[16] = {1: 1}
    return {2: author}


def _desc(text: str) -> dict:
    return {4: {3: text}}


def _paragraph(text: str, *, word: bool = False) -> dict:
    node = {3: {1: text}} if word else {2: text}
    return {3: {1: [node]}}


def _frame(payload: bytes) -> bytes:
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


def case_forgetting_a_proxy_mints_a_new_phone(tmp_path):
    from news.source.post.bilibili.app import PhoneBook, _session_ids, app_buvid

    book = PhoneBook(tmp_path / "phones.json")
    calls = {"n": 0}

    def mint(seed: str = "") -> str:
        calls["n"] += 1
        return app_buvid(str(calls["n"]))

    first = book.get("http://a", mint=mint)
    second = book.get("http://a", mint=mint, index=1)
    other = book.get("http://b", mint=mint)
    _session_ids[first] = "kept-for-a"
    book.forget("http://a")
    assert first not in _session_ids
    assert _saved_phone(book, "http://a") is None
    assert _saved_phone(book, "http://a", 1) is None
    assert _saved_phone(book, "http://b") == other
    again = book.get("http://a", mint=mint)
    assert again != first
    assert again != second
