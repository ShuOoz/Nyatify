"""Protobuf for the Bilibili app messages this client reads and writes.

Only those fields are described. A newer reply can add fields; they are skipped.
"""

from __future__ import annotations

from typing import Any

# field number -> (kind, repeated). kind is "v", "s", or a message name.
_SCHEMAS: dict[str, dict[int, tuple[str, bool]]] = {
    "DynSpaceReq": {
        1: ("v", False),
        4: ("v", False),
        5: ("v", False),
        6: ("s", False),
    },
    "DynSpaceRsp": {
        1: ("DynamicItem", True),
        2: ("s", False),
        3: ("v", False),
    },
    "DynamicItem": {
        1: ("v", False),
        2: ("v", False),
        3: ("Module", True),
        4: ("Extend", False),
    },
    "Extend": {
        1: ("s", False),
        4: ("s", False),
        21: ("OnlyFansProperty", False),
    },
    "OnlyFansProperty": {1: ("v", False), 2: ("v", False)},
    "Module": {
        1: ("v", False),
        2: ("ModuleAuthor", False),
        4: ("ModuleDesc", False),
        5: ("ModuleDynamic", False),
        8: ("ModuleAdditional", False),
        30: ("ModuleOpusSummary", False),
        33: ("ModuleBlocked", False),
    },
    "ModuleAdditional": {10: ("AdditionUP", False)},
    "AdditionUP": {1: ("s", False), 2: ("HighlightText", False)},
    "HighlightText": {1: ("s", False)},
    "ModuleAuthor": {
        1: ("v", False),
        3: ("UserInfo", False),
        16: ("OnlyFans", False),
    },
    "OnlyFans": {1: ("v", False), 2: ("IconBadge", False)},
    "IconBadge": {2: ("s", False)},
    "UserInfo": {2: ("s", False), 3: ("s", False)},
    "ModuleDesc": {1: ("Description", True), 3: ("s", False)},
    "Description": {1: ("s", False), 2: ("v", False)},
    "ModuleDynamic": {
        2: ("MdlDynArchive", False),
        6: ("MdlDynForward", False),
        7: ("MdlDynDraw", False),
        8: ("MdlDynArticle", False),
        11: ("MdlDynLive", False),
        15: ("MdlDynLiveRcmd", False),
    },
    "MdlDynArchive": {1: ("s", False), 2: ("s", False)},
    "MdlDynForward": {1: ("DynamicItem", False)},
    "MdlDynDraw": {1: ("MdlDynDrawItem", True)},
    "MdlDynDrawItem": {1: ("s", False), 2: ("v", False), 3: ("v", False)},
    "MdlDynArticle": {3: ("s", False), 4: ("s", False), 5: ("s", True)},
    "MdlDynLive": {1: ("v", False), 2: ("s", False), 3: ("s", False), 4: ("s", False)},
    "MdlDynLiveRcmd": {1: ("s", False)},
    "ModuleOpusSummary": {
        1: ("Paragraph", False),
        2: ("Paragraph", False),
        4: ("MdlDynDrawItem", True),
    },
    "Paragraph": {3: ("TextParagraph", False)},
    "TextParagraph": {1: ("TextNode", True)},
    "TextNode": {1: ("v", False), 2: ("s", False), 3: ("WordNode", False)},
    "WordNode": {1: ("s", False)},
    "ModuleBlocked": {3: ("s", False)},
    "Metadata": {
        2: ("s", False),
        4: ("v", False),
        5: ("s", False),
        6: ("s", False),
        7: ("s", False),
    },
    "Device": {
        1: ("v", False),
        2: ("v", False),
        3: ("s", False),
        4: ("s", False),
        5: ("s", False),
        7: ("s", False),
        8: ("s", False),
        9: ("s", False),
        10: ("s", False),
        11: ("s", False),
        12: ("s", False),
        13: ("s", False),
        14: ("s", False),
        15: ("v", False),
    },
    "Network": {1: ("v", False)},
    "FawkesReq": {1: ("s", False), 2: ("s", False), 3: ("s", False)},
    "Locale": {1: ("LocaleIds", False), 2: ("LocaleIds", False), 4: ("s", False)},
    "LocaleIds": {1: ("s", False), 3: ("s", False)},
}


def encode_message(name: str, fields: dict[int, Any]) -> bytes:
    spec = _SCHEMAS[name]
    chunks: list[bytes] = []
    for number in sorted(fields):
        value = fields[number]
        if value is None or value is False or value == "" or value == 0 or value == []:
            continue
        kind, _repeated = spec[number]
        items = value if isinstance(value, list) else [value]
        for item in items:
            if item is None or item == "":
                continue
            piece = _encode_field(number, kind, item)
            if piece:
                chunks.append(piece)
    return b"".join(chunks)


def decode_message(name: str, payload: bytes, *, depth: int = 0) -> dict[int, Any]:
    if depth > 32:
        raise ValueError("protobuf nested too deep")
    spec = _SCHEMAS[name]
    out: dict[int, Any] = {}
    view = memoryview(payload)
    index = 0
    while index < len(view):
        key, index = _read_varint(view, index)
        field, wire = key >> 3, key & 7
        if field <= 0:
            raise ValueError("bad protobuf field")
        kind, repeated = spec.get(field, ("", False))
        if wire == 0:
            value, index = _read_varint(view, index)
            if kind == "v":
                _put(out, field, value, repeated)
        elif wire in (1, 5):
            index = _advance(view, index, 8 if wire == 1 else 4)
        elif wire == 2:
            length, index = _read_varint(view, index)
            index = _advance(view, index, length)
            raw = bytes(view[index - length:index])
            if kind == "s":
                _put(out, field, raw.decode("utf-8", "replace"), repeated)
            elif kind not in ("", "v"):
                _put(out, field, decode_message(kind, raw, depth=depth + 1), repeated)
        else:
            raise ValueError("bad protobuf wire type")
    return out


def _encode_field(number: int, kind: str, value: Any) -> bytes:
    if kind == "v":
        return _key(number, 0) + _varint(int(value))
    if kind == "s":
        data = str(value).encode("utf-8")
    else:
        if not isinstance(value, dict):
            raise ValueError(f"{kind} value must be a message")
        data = encode_message(kind, value)
        if not data:
            return b""
    return _key(number, 2) + _varint(len(data)) + data


def _put(out: dict[int, Any], field: int, value: Any, repeated: bool) -> None:
    if not repeated:
        out[field] = value
        return
    found = out.get(field)
    if isinstance(found, list):
        found.append(value)
    else:
        out[field] = [value]


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _varint(value: int) -> bytes:
    number = int(value)
    if number < 0:
        raise ValueError("negative protobuf varint")
    out = bytearray()
    while True:
        bits = number & 0x7F
        number >>= 7
        if number:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _read_varint(view: memoryview, index: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(10):
        if index >= len(view):
            raise ValueError("truncated protobuf")
        byte = view[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
    raise ValueError("protobuf varint too long")


def _advance(view: memoryview, index: int, length: int) -> int:
    if length < 0 or index + length > len(view):
        raise ValueError("truncated protobuf")
    return index + length
