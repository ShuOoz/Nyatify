"""News card layout. Does not send, and does not call the model."""

from __future__ import annotations

import io
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from fetch import FetchedItem, excerpt, http_url
from news.follows import FEATURE_GAME, FEATURE_OTHER
from news.source.match.common import (
    END_TAG,
    MATCH_DETAIL_HEADING,
    MATCH_IDLE_LINE,
    NEXT_TAG,
    SKIP_TAG,
    START_TAG,
    is_match_item,
    match_stage,
)
from news.source.post.common import CardPicture, embed_image_url
from news.source.registry import card_kind
from announce import ACCENT_COLOR

_MATCH_COLOR = discord.Color.from_str("#228B22")
_BILIBILI_COLOR = discord.Color.from_str("#3DDCFF")
_WEIBO_COLOR = discord.Color.from_str("#C25454")

MATCH_RECAP_CHARS = 400
TITLE_CHARS = 256
_LINK_MARK = "˙"
LINE_WIDTH = 56
TITLE_WIDTH = 48  # title font wraps sooner than the body

_SLOGAN = re.compile(r"「[^」]*」")
_SKIP_CHARS = "：: "


def _nl(text: str) -> str:
    return text.replace("\r\n", "\n")


def _plain(text: str) -> str:
    return text.replace("：", "").replace(":", "").replace(" ", "")


def match_write_phase(item: FetchedItem) -> bool:
    return END_TAG in item.tags


def match_recap_notes(item: FetchedItem) -> str:
    if not match_write_phase(item):
        return ""
    raw = _nl(item.text)
    at = raw.find(MATCH_DETAIL_HEADING)
    if at < 0:
        return ""
    note = raw[at + len(MATCH_DETAIL_HEADING) :].strip()
    return "" if note in {MATCH_IDLE_LINE, MATCH_DETAIL_HEADING} else note


def match_event_id(item: FetchedItem) -> str | None:
    """Stable id for one Discord card across preview, kickoff, and full time."""
    if not is_match_item(item) or not item.item_id:
        return None
    return item.item_id.rsplit(":", 1)[0]


def game_title(account: str | None, title: str) -> str:
    """Account | event on one embed line. Drops slogans the model stuffed in."""
    title = " ".join(_SLOGAN.sub("", " ".join(title.split())).split())
    name = (account or "").strip()
    if not name:
        return title
    rest = _event_after_account(title, name)
    return f"{name} | {rest}" if rest else name


def _event_after_account(title: str, account: str) -> str:
    wanted = _plain(account)
    if not wanted:
        return title.strip()
    if "|" in title:
        left, right = title.split("|", 1)
        if _plain(left) == wanted:
            return right.strip()
    n = 0
    for index, ch in enumerate(title):
        if ch in _SKIP_CHARS:
            continue
        if n >= len(wanted) or ch != wanted[n]:
            return title.strip()
        n += 1
        if n == len(wanted):
            return title[index + 1 :].lstrip(" |：:").strip()
    return title.strip()


def parse_title_summary(text: str) -> tuple[str, str] | None:
    data = _json_object(text)
    if data is None or not isinstance(data.get("title"), str):
        return None
    title = excerpt(" ".join(data["title"].split()), TITLE_CHARS)
    if isinstance(data.get("about"), str):
        summary = _game_card_body(data)
    else:
        raw = data.get("summary")
        if not isinstance(raw, str):
            return None
        summary = _fold_summary(raw)
    return (title, summary) if summary else None


def apply_match_recap(text: str, recap: str) -> str:
    recap = excerpt(_nl(recap).strip(), MATCH_RECAP_CHARS, breaks=True)
    raw = _nl(text)
    at = raw.find(MATCH_DETAIL_HEADING)
    if at < 0:
        return f"{raw}\n{recap}" if recap else raw
    start = at + len(MATCH_DETAIL_HEADING)
    if start < len(raw) and raw[start] == "\n":
        start += 1
    return raw[:start] + recap


def news_embed(
    item: FetchedItem, topic: str, *, title: str | None = None, summary: str | None = None,
    face: CardPicture | None = None,
) -> discord.Embed:
    heading = (title or item.title or "News").strip() or "News"
    if topic == FEATURE_GAME and not is_match_item(item):
        heading = game_title(item.source_name, heading)
    if is_match_item(item):
        body = _nl(item.text)
        if summary:
            body = apply_match_recap(body, summary)
        body = body.strip()
    else:
        body = excerpt(
            item.text if summary is None else summary,
            breaks=summary is not None,
        )
        shown = heading[:TITLE_CHARS]
        if shown and not _plain_title(shown):
            if _only_repeats(shown, body):
                body = ""
            if not shown.endswith(_LINK_MARK):
                heading = f"{shown[: TITLE_CHARS - len(_LINK_MARK)]}{_LINK_MARK}"
        elif _only_repeats(shown, body):
            body = ""
    kind = card_kind(item)
    label = topic.capitalize()
    footer = label if kind == topic else f"{label} {kind.capitalize()}"
    if item.source_name:
        footer = f"{footer} · {item.source_name}"
    embed = discord.Embed(
        title=heading[:TITLE_CHARS], url=http_url(item.url),
        description=body or None, color=_card_color(item),
    )
    image = embed_image_url(item)
    if image:
        embed.set_image(url=image)
    embed.set_footer(text=footer)
    _other_author(embed, item, topic, face)
    return embed


def _plain_title(text: str) -> bool:
    """True when the line has a letter or number."""
    return any(unicodedata.category(ch)[0] in {"L", "N"} for ch in text)


def _other_author(
    embed: discord.Embed, item: FetchedItem, topic: str, face: CardPicture | None,
) -> None:
    """Name and face on an other card. Neither is a link."""
    if topic != FEATURE_OTHER:
        return
    name = _author_name(item.source_name)
    if not name:
        return
    if face is not None:
        icon = f"attachment://{face.filename}"
    else:
        icon = None if item.platform in {"weibo", "wb"} else _remote_icon(item.author_icon)
    embed.set_author(name=name, icon_url=icon or None)


def _only_repeats(title: str, body: str) -> bool:
    """True when the body is the title, or that title written again."""
    head = " ".join(title.split())
    flat = " ".join(body.split())
    if not head or not flat:
        return False
    if flat == head:
        return True
    if not flat.startswith(head):
        return False
    return all(not part.strip() for part in flat.split(head))


def _author_name(name: str | None) -> str:
    """One line, inside Discord's 256-character author limit."""
    flat = " ".join((name or "").split())
    return flat[:256]


def _remote_icon(url: str | None) -> str | None:
    """HTTPS face URL Discord can fetch. Anything else is left off the card."""
    found = http_url(url)
    if not found:
        return None
    if found.startswith("http://"):
        found = "https://" + found[len("http://") :]
    parts = found.split("/", 3)
    if len(parts) < 3 or not parts[2]:
        return None
    return found


def picture_file(cover: CardPicture) -> discord.File:
    """A new file each call. One BytesIO cannot be sent twice."""
    return discord.File(io.BytesIO(cover.data), filename=cover.filename)


def picture_files(*covers: CardPicture | None) -> list[discord.File]:
    return [picture_file(cover) for cover in covers if cover is not None]


async def news_card(
    item: FetchedItem, topic: str, *, title: str | None = None, summary: str | None = None,
) -> tuple[discord.Embed, list[discord.File]]:
    """Embed plus fresh attachment files. Call once per send. One file cannot be sent twice."""
    from news.source.match.pandascore.banners import banner_picture
    from news.source.post.common import author_face, card_picture

    face = await author_face(item) if topic == FEATURE_OTHER else None
    embed = news_embed(item, topic, title=title, summary=summary, face=face)
    cover = banner_picture(item) or await card_picture(item)
    if cover is not None:
        embed.set_image(url=f"attachment://{cover.filename}")
    return embed, picture_files(face, cover)


def _card_color(item: FetchedItem) -> discord.Color:
    if is_match_item(item):
        return _MATCH_COLOR
    if item.platform in {"bilibili", "bili"}:
        return _BILIBILI_COLOR
    if item.platform in {"weibo", "wb"}:
        return _WEIBO_COLOR
    return ACCENT_COLOR


def embed_fingerprint(embed: discord.Embed) -> str:
    image = embed.image.url if embed.image else ""
    return "\n".join((embed.title or "", embed.description or "", embed.url or "", image or ""))


def match_notice_extra(
    item: FetchedItem, embed: discord.Embed, summary: str | None
) -> dict[str, Any]:
    extra: dict[str, Any] = {"card": embed_fingerprint(embed), "account": item.account}
    if stage := match_stage(item):
        extra["stage"] = stage
    if notes := match_recap_notes(item):
        extra["notes"] = notes
    if summary:
        extra["summary"] = summary
    return extra


# A second club must not walk a shared match card backwards.
_MATCH_REWIND = {
    (START_TAG, NEXT_TAG),
    (END_TAG, NEXT_TAG),
    (END_TAG, START_TAG),
    (END_TAG, SKIP_TAG),
    (SKIP_TAG, NEXT_TAG),
}


def older_match_card(saved: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """True when this version must not replace the card already in the channel."""
    old, new = str(saved.get("stage") or ""), str(incoming.get("stage") or "")
    if (old, new) in _MATCH_REWIND:
        return True
    if old != new:
        return False
    owner = str(saved.get("account") or "")
    return bool(owner) and incoming.get("account") != owner and incoming.get("card") != saved.get("card")


def saved_match_summary(extra: dict[str, Any], stage: str, notes: str) -> str | None:
    """Summary already stored for this stage and these notes."""
    raw = extra.get("summary")
    same = extra.get("stage") == stage and str(extra.get("notes") or "") == notes
    return raw if same and isinstance(raw, str) and raw.strip() else None


def _json_object(text: str) -> dict[str, Any] | None:
    blob = text.strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:json)?\s*", "", blob, count=1)
        blob = re.sub(r"\s*```$", "", blob).strip()
    start, end = blob.find("{"), blob.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(blob[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _one_line(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _prose(value: object, *, grant: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    lines = [line for line in _fold_summary(value).split("\n") if line]
    return excerpt("\n".join(_drop_grant_repeat(lines) if grant else lines), breaks=True)


def _drop_grant_repeat(lines: list[str]) -> list[str]:
    """A sentence under the grant must not start by saying the grant again."""
    if len(lines) < 2:
        return lines
    grant = lines[0].strip()
    if not grant:
        return lines
    kept = [lines[0]]
    for line in lines[1:]:
        body = line.strip()
        if body.startswith(grant):
            body = body[len(grant) :].lstrip(" ，,：:、")
        if body:
            kept.append(body)
    return kept


def _fit_lines(text: str, limit: int) -> str:
    """Keep whole lines. A cut ends on the last line that fits, then an ellipsis."""
    if len(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        extra = len(line) if not kept else len(line) + 1
        if used + extra + 2 > limit:
            break
        kept.append(line)
        used += extra
    if not kept:
        return excerpt(text, limit, breaks=True)
    return "\n".join(kept) + "\n…"


def _game_card_body(data: dict[str, object]) -> str:
    clock = _one_line(data.get("time"))
    about = _one_line(data.get("about"))
    detail = _prose(data.get("detail"))
    reward = _prose(data.get("reward"), grant=True)
    blocks: list[str] = []
    if clock:
        blocks.append(f"**Time**\n{clock}")
    about_lines = [part for part in (about, detail) if part]
    if about_lines:
        blocks.append("**About**\n" + "\n".join(about_lines))
    if reward:
        blocks.append(f"**Reward**\n{reward}")
    return "\n\n".join(blocks)


def _fold_summary(text: str) -> str:
    lines = [" ".join(part.split()) for part in _nl(text).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


STREAM_FP_PREFIX = "stream:"
_CN = timezone(timedelta(hours=8))
_CLOCK = re.compile(
    r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"[^\d]{0,8}(\d{1,2})\s*[:：]\s*(\d{2})"
)


def stream_when(quote: str, posted: datetime | None = None) -> datetime | None:
    """Watch clock in China time, copied from a fact quote."""
    match = _CLOCK.search(quote)
    if match is None:
        return None
    year_s, month, day, hour, minute = match.groups()
    posted_cn = (posted or datetime.now(timezone.utc)).astimezone(_CN)
    year = _show_year(year_s, month, day, posted_cn)
    try:
        return datetime(year, int(month), int(day), int(hour), int(minute), tzinfo=_CN)
    except ValueError:
        return None


def _show_year(year_s: str | None, month: str, day: str, posted: datetime) -> int:
    """A month and day with no year is this year, or next year once that day has passed."""
    if year_s:
        return int(year_s)
    year = posted.year
    try:
        shown = datetime(year, int(month), int(day), tzinfo=posted.tzinfo).date()
    except ValueError:
        return year
    if shown < posted.date():
        return year + 1
    return year


def stream_fingerprint(
    quotes: dict[str, str],
    occurred_at: datetime | None,
    *,
    keep: bool,
    kind: str,
    watch: str = "",
) -> str | None:
    """Same watch clock, posted before the show day. A reservation clock wins."""
    if not keep or kind != "stream":
        return None
    quoted = next((text for text in quotes.values() if stream_when(text, occurred_at)), "")
    quote = watch if watch and stream_when(watch, occurred_at) else quoted
    clock = stream_when(quote, occurred_at) if quote else None
    if clock is None or occurred_at is None:
        return None
    if occurred_at.astimezone(_CN).date() >= clock.date():
        return None
    return (
        f"{STREAM_FP_PREFIX}{clock.year}-{clock.month}-{clock.day}-"
        f"{clock.hour:02d}:{clock.minute:02d}"
    )
