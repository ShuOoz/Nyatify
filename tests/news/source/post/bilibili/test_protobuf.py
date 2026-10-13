from __future__ import annotations

from news.source.post.bilibili.protobuf import decode_message, encode_message


def test_protobuf_codec():
    case_varint_string_and_an_unknown_field()
    case_a_message_round_trips_repeated_nested_and_long_fields()
    case_a_sticker_type_round_trips()


def case_varint_string_and_an_unknown_field():
    assert encode_message("DynSpaceReq", {1: 1}) == b"\x08\x01"
    assert encode_message("DynSpaceReq", {6: "space"}) == b"\x32\x05space"
    # Field 99, length-delimited "zz", is not a field this client knows.
    extra = encode_message("DynSpaceReq", {1: 7}) + b"\x9a\x06\x02zz"
    assert decode_message("DynSpaceReq", extra) == {1: 7}


def case_a_message_round_trips_repeated_nested_and_long_fields():
    body = {
        1: [{3: [{4: {3: "hello"}}], 4: {1: "850406986313893918"}}, {4: {1: "2"}}],
        2: "offset",
        3: 1,
    }
    assert decode_message("DynSpaceRsp", encode_message("DynSpaceRsp", body)) == body
    assert decode_message("DynSpaceRsp", b"") == {}


def case_a_sticker_type_round_trips():
    sticker = {1: "[呆]", 2: 9}
    assert decode_message("Description", encode_message("Description", sticker)) == sticker
    emote = {1: 2, 2: "[呆]"}
    assert decode_message("TextNode", encode_message("TextNode", emote)) == emote
