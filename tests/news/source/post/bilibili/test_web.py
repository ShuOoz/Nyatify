from __future__ import annotations

from datetime import datetime, timezone

from news.source.post.bilibili.app import _when_id
from news.source.post.bilibili.web import dynamic_item, items_from_space, opus_item

UID = "1195230310"


def _author(name: str = "Studio", ts: int = 1_700_000_000) -> dict:
    return {
        "module_author": {
            "name": name,
            "pub_ts": ts,
            "face": "//i0.hdslb.com/bfs/face/s.jpg",
        }
    }


def _row(
    item_id: str,
    *,
    kind: str = "DYNAMIC_TYPE_WORD",
    desc: str = "",
    major: dict | None = None,
    orig: dict | None = None,
    name: str = "Studio",
) -> dict:
    dynamic: dict = {}
    if desc:
        dynamic["desc"] = {"text": desc}
    if major is not None:
        dynamic["major"] = major
    row: dict = {
        "id_str": item_id,
        "type": kind,
        "modules": {**_author(name), "module_dynamic": dynamic},
    }
    if orig is not None:
        row["orig"] = orig
    return row


def case_a_draw_with_a_reserve_is_the_show():
    posted = datetime(2026, 9, 30, 3, tzinfo=timezone.utc)
    row = _row(
        "1253695825563877448",
        kind="DYNAMIC_TYPE_DRAW",
        major={
            "draw": {
                "items": [
                    {
                        "src": "//i0.hdslb.com/bfs/new_dyn/a.png",
                        "width": 2160,
                        "height": 3840,
                    }
                ]
            }
        },
    )
    row["modules"]["module_author"]["pub_ts"] = int(posted.timestamp())
    row["modules"]["module_dynamic"]["additional"] = {
        "type": "ADDITIONAL_TYPE_RESERVE",
        "reserve": {
            "title": "直播预约：「丹青渡」版本前瞻特别节目",
            "desc1": {"text": "10-06 19:30 直播"},
            "desc2": {"text": "10.6万人预约"},
            "desc3": {"text": "预约有奖：石墩子摆件"},
            "rid": 5797125,
        },
    }
    item = dynamic_item(row, account=UID)
    assert item is not None
    assert item.text == "直播预约：「丹青渡」版本前瞻特别节目 10月6日 19:30"
    assert item.title == item.text
    assert item.tags == ("watch:2026-10-6-19:30",)
    assert "预约有奖" not in item.text and "万人" not in item.text
    assert item.image_url == "https://i0.hdslb.com/bfs/new_dyn/a.png"
    assert item.source_name == "Studio"


def case_items_from_space_maps_word_opus_video_and_skips_empty():
    name, items = items_from_space(
        {
            "code": 0,
            "data": {
                "items": [
                    _row("", desc="no id"),
                    _row("11", desc="hello from space"),
                    _row(
                        "22",
                        kind="DYNAMIC_TYPE_DRAW",
                        major={
                            "opus": {
                                "title": "Photo day",
                                "summary": {"text": "cats"},
                                "pics": [
                                    {
                                        "url": "//i0.hdslb.com/bfs/x.jpg",
                                        "width": 1080,
                                        "height": 4000,
                                    }
                                ],
                            }
                        },
                    ),
                    _row(
                        "33",
                        kind="DYNAMIC_TYPE_AV",
                        major={
                            "archive": {
                                "aid": "77",
                                "title": "Match vlog",
                                "desc": "full game",
                                "cover": "//i0.hdslb.com/bfs/cover.png",
                            }
                        },
                    ),
                ]
            },
        },
        account=UID,
    )
    assert name == "Studio"
    assert [item.item_id for item in items] == ["11", "22", "33"]
    word, opus, video = items
    assert word.title == "hello from space"
    assert word.text == "hello from space"
    assert word.url == "https://www.bilibili.com/opus/11"
    assert word.account == UID
    assert word.source_name == "Studio"
    assert word.author_icon == "https://i0.hdslb.com/bfs/face/s.jpg"
    assert word.occurred_at == datetime.fromtimestamp(1_700_000_000, timezone.utc)
    assert opus.title == "Photo day"
    assert opus.text == "cats"
    assert opus.image_url == "https://i0.hdslb.com/bfs/x.jpg"
    assert opus.image_width == 1080
    assert opus.image_height == 4000
    assert video.title == "Match vlog"
    assert video.text == "full game"
    assert video.image_url == "https://i0.hdslb.com/bfs/cover.png"
    assert items_from_space({"data": {"items": []}}, account=UID) == (None, [])


def case_dynamic_item_keeps_one_copy_of_a_repeated_caption():
    caption = "黑皮辣妹是好文明！ 原來娜嘉手里的手机是小米18Pro"
    same = dynamic_item(
        _row(
            "66",
            kind="DYNAMIC_TYPE_DRAW",
            desc=caption,
            major={
                "opus": {
                    "title": "用小米18Pro做道具？ 黑皮辣妹JK娜嘉COS!",
                    "summary": {"text": caption},
                }
            },
        ),
        account=UID,
    )
    assert same is not None
    assert same.title == "用小米18Pro做道具？ 黑皮辣妹JK娜嘉COS!"
    assert same.text == caption

    clipped = dynamic_item(
        _row(
            "67",
            kind="DYNAMIC_TYPE_DRAW",
            desc=caption,
            major={"opus": {"summary": {"text": "黑皮辣妹是好文明！…"}}},
        ),
        account=UID,
    )
    assert clipped is not None
    assert clipped.text == caption

    video = dynamic_item(
        _row(
            "68",
            kind="DYNAMIC_TYPE_AV",
            desc="看这个",
            major={"archive": {"title": "Match vlog", "desc": "full game"}},
        ),
        account=UID,
    )
    assert video is not None
    assert video.title == "Match vlog"
    assert video.text == "看这个\nfull game"


def test_space_rows_become_items():
    case_a_draw_with_a_reserve_is_the_show()
    case_items_from_space_maps_word_opus_video_and_skips_empty()
    case_dynamic_item_keeps_one_copy_of_a_repeated_caption()
    case_a_sticker_is_left_out_of_the_caption()
    case_a_caption_without_a_sticker_stays()
    case_dynamic_item_reads_forward_and_live()
    case_a_charge_lock_is_tagged()
    case_an_opus_row_keeps_the_post_day()


def case_an_opus_row_keeps_the_post_day():
    item_id = "1252684755882737665"
    shown = opus_item(_opus_row(item_id, "特别前瞻节目将于10月6日19:30开播"), account=UID)
    assert shown is not None
    assert shown.occurred_at == _when_id(item_id)
    short = opus_item(_opus_row("11", "hello"), account=UID)
    assert short is not None and short.occurred_at is None


def case_a_sticker_is_left_out_of_the_caption():
    row = _row("70", desc="见 [备注][呆]")
    row["modules"]["module_dynamic"]["desc"]["rich_text_nodes"] = [
        {"type": "RICH_TEXT_NODE_TYPE_TEXT", "text": "见 [备注]"},
        {"type": "RICH_TEXT_NODE_TYPE_EMOJI", "text": "[呆]"},
    ]
    item = dynamic_item(row, account=UID)
    assert item is not None
    assert item.text == "见 [备注]"

    note = dynamic_item(_row("71", desc="见 [备注]"), account=UID)
    assert note is not None
    assert note.text == "见 [备注]"

    opus = dynamic_item(
        _row(
            "72",
            kind="DYNAMIC_TYPE_DRAW",
            major={
                "opus": {
                    "summary": {
                        "text": "抱歉[呆]",
                        "rich_text_nodes": [
                            {"type": "RICH_TEXT_NODE_TYPE_TEXT", "text": "抱歉"},
                            {"type": "RICH_TEXT_NODE_TYPE_EMOJI", "text": "[呆]"},
                        ],
                    }
                }
            },
        ),
        account=UID,
    )
    assert opus is not None
    assert opus.text == "抱歉"

    bare = _row("73", desc="[呆]")
    bare["modules"]["module_dynamic"]["desc"]["rich_text_nodes"] = [
        {"type": "RICH_TEXT_NODE_TYPE_EMOJI", "text": "[呆]"},
    ]
    shown = dynamic_item(bare, account=UID)
    assert shown is not None
    assert shown.text == "Studio"

    twin = _row("74", desc="抱歉[呆]")
    twin["modules"]["module_dynamic"]["desc"]["rich_text_nodes"] = [
        {"type": "RICH_TEXT_NODE_TYPE_TEXT", "text": "抱歉"},
        {"type": "RICH_TEXT_NODE_TYPE_EMOJI", "text": "[呆]"},
    ]
    twin["modules"]["module_dynamic"]["major"] = {"opus": {"summary": {"text": "抱歉[呆]"}}}
    repeated = dynamic_item(twin, account=UID)
    assert repeated is not None
    assert repeated.text == "抱歉"

    video = _row(
        "75",
        kind="DYNAMIC_TYPE_AV",
        desc="看这个[呆]",
        major={"archive": {"title": "Match vlog", "desc": "full game [呆]"}},
    )
    video["modules"]["module_dynamic"]["desc"]["rich_text_nodes"] = [
        {"type": "RICH_TEXT_NODE_TYPE_TEXT", "text": "看这个"},
        {"type": "RICH_TEXT_NODE_TYPE_EMOJI", "text": "[呆]"},
    ]
    kept = dynamic_item(video, account=UID)
    assert kept is not None
    assert kept.title == "Match vlog"
    assert kept.text == "看这个\nfull game [呆]"


def case_a_caption_without_a_sticker_stays():
    row = _row("76", desc="the real [备注]")
    row["modules"]["module_dynamic"]["desc"]["rich_text_nodes"] = [
        {"type": "RICH_TEXT_NODE_TYPE_TEXT", "text": "short"},
    ]
    item = dynamic_item(row, account=UID)
    assert item is not None
    assert item.text == "the real [备注]"


def case_dynamic_item_reads_forward_and_live():
    forward = dynamic_item(
        _row(
            "44",
            kind="DYNAMIC_TYPE_FORWARD",
            desc="wow",
            orig={
                "modules": {
                    **_author("Other"),
                    "module_dynamic": {
                        "major": {
                            "archive": {
                                "title": "Old clip",
                                "desc": "orig body",
                                "cover": "https://i0.hdslb.com/bfs/o.png",
                            }
                        }
                    },
                }
            },
        ),
        account=UID,
    )
    assert forward is not None
    assert "转发了Other的动态" in forward.text
    assert "Old clip" in forward.text
    assert forward.image_url == "https://i0.hdslb.com/bfs/o.png"

    live = dynamic_item(
        _row(
            "55",
            kind="DYNAMIC_TYPE_LIVE_RCMD",
            major={
                "live_rcmd": {
                    "content": (
                        '{"live_play_info":{"room_id":99,"title":"正在直播",'
                        '"cover":"https://i0.hdslb.com/bfs/live.png"}}'
                    )
                }
            },
        ),
        account=UID,
    )
    assert live is not None
    assert live.title == "正在直播"
    assert "https://live.bilibili.com/99" in live.text
    assert live.image_url == "https://i0.hdslb.com/bfs/live.png"


def case_a_charge_lock_is_tagged():
    locked = _row(
        "1252684755882737665",
        kind="DYNAMIC_TYPE_DRAW",
        name="宫叶Miyaba",
        major={
            "type": "MAJOR_TYPE_BLOCKED",
            "blocked": {
                "hint_message": "加入当前UP主的「6元档包月充电」即可解锁观看",
            },
        },
    )
    locked["basic"] = {"is_only_fans": True}
    locked["modules"]["module_author"]["icon_badge"] = {"text": "充电专属"}
    charge = dynamic_item(locked, account=UID, source_name="宫叶Miyaba")
    assert charge is not None
    assert charge.tags == ("充电专属",)
    assert charge.text == "加入当前UP主的「6元档包月充电」即可解锁观看"
    assert charge.title == charge.text
    _name, items = items_from_space(
        {"data": {"items": [locked, _row("11", desc="hello")]}},
        account=UID,
    )
    assert [item.item_id for item in items] == ["1252684755882737665", "11"]
    badge = _row("9", desc="unlocked body")
    badge["modules"]["module_author"]["icon_badge"] = {"text": "充电专属"}
    leaked = dynamic_item(badge, account=UID)
    assert leaked is not None
    assert leaked.text == "unlocked body"
    assert leaked.tags == ("充电专属",)
    assert dynamic_item(_row("8", desc="感谢充电"), account=UID).tags == ()

    locked_opus = opus_item(
        _opus_row("1252684755882737665", "充电专属动态，加入即可解锁观看"),
        account=UID,
        source_name="宫叶Miyaba",
    )
    assert locked_opus is not None
    assert locked_opus.text == "充电专属动态，加入即可解锁观看"
    assert locked_opus.tags == ()


def _opus_row(
    opus_id: str,
    content: str,
    *,
    name: str | None = None,
    face: str | None = None,
) -> dict:
    author = None
    if name or face:
        author = {"mid": int(UID)}
        if name:
            author["name"] = name
        if face:
            author["face"] = face
    return {
        "opus_id": opus_id,
        "content": content,
        "jump_url": f"//www.bilibili.com/opus/{opus_id}",
        "cover": {"url": "//i0.hdslb.com/bfs/new_dyn/c.jpg", "width": 720, "height": 1640},
        "author": author,
    }
