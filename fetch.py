"""Get pages and feeds from the web. No Discord, no platforms."""

from __future__ import annotations

import json
import logging
import random
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from config import BASE_DIR, write_text_atomic

log = logging.getLogger(__name__)

DEFAULT_BOOKMARKS_PATH = BASE_DIR / "data" / "news_bookmarks.json"
EXCERPT_CHARS = 280
WRITE_TEXT_CHARS = 4000
POLL_MIN_SECONDS = 30.0
SEEN_CAP = 30
SEEN_HARD_CAP = 500
USER_AGENT = "Mozilla/5.0 (compatible; Nyatify/0.1; +https://github.com/nyatify)"
_HTTP_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

GetText = Callable[[str, dict[str, str] | None], Awaitable[str]]


def redact_fetch_url(url: str) -> str:
    """Drop query/fragment so access keys are not logged or shown in Discord."""
    parts = urlsplit(url)
    if not parts.scheme:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def public_fetch_message(text: str) -> str:
    return _HTTP_URL_RE.sub(lambda match: redact_fetch_url(match.group(0)), text)


class FetchError(Exception):
    """A source could not be read."""

    def __init__(self, message: str = "", *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body[:1000]

    def __str__(self) -> str:
        return public_fetch_message(super().__str__())


@dataclass(frozen=True)
class FetchedItem:
    item_id: str
    title: str
    text: str
    url: str
    image_url: str | None
    platform: str
    account: str
    tags: tuple[str, ...] = ()
    source_name: str | None = None
    occurred_at: datetime | None = None
    image_width: int | None = None
    image_height: int | None = None
    author_icon: str | None = None


def http_url(url: str | None) -> str | None:
    return url if url and url.lower().startswith(("http://", "https://")) else None


def excerpt(text: str, limit: int = EXCERPT_CHARS, *, breaks: bool = False) -> str:
    cleaned = text.replace("\r\n", "\n").strip() if breaks else " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def format_fetched_item(item: FetchedItem, *, peek: int | None = None) -> str:
    text = item.text
    limit = WRITE_TEXT_CHARS if peek is None else peek
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    lines = [f"Title: {item.title or '(none)'}"]
    account = (item.source_name or "").strip()
    if not account:
        raw = (item.account or "").strip()
        if raw and not raw.lower().startswith(("http://", "https://")):
            account = raw
    if account:
        lines.append(f"Account: {account}")
    if text:
        lines.append(f"Text: {text}")
    if item.tags:
        lines.append("Tags: " + ", ".join(item.tags))
    return "\n".join(lines)


def poll_delay(
    base: float, jitter: float, rng: random.Random, *, min_seconds: float = POLL_MIN_SECONDS,
) -> float:
    """Wait at least `base` seconds, then up to `jitter` extra."""
    gap = max(min_seconds, float(base))
    extra = max(0.0, float(jitter))
    return gap + rng.uniform(0, extra) if extra else gap


def seen_key(source: str, account: str) -> str:
    return f"{source}:{account}"


def _bookmark_key(key: str) -> str:
    if key.startswith("bili:") and not key.startswith("bilibili:"):
        return "bilibili:" + key[5:]
    return key


def _merge_ids(*groups: Sequence[str], cap: int | None = SEEN_CAP) -> list[str]:
    """Keep the first group whole (current window / seed), then trim older ids."""
    merged: list[str] = []
    seen: set[str] = set()
    for index, group in enumerate(groups):
        for item_id in group:
            text = str(item_id) if item_id else ""
            if text and text not in seen:
                seen.add(text)
                merged.append(text)
        if index == 0 and cap is not None:
            cap = max(cap, len(merged))
    return merged if cap is None else merged[:cap]


def _merge_delivered(
    left: dict[str, list[str]], right: dict[str, list[str]]
) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {key: list(refs) for key, refs in left.items()}
    for item_id, refs in right.items():
        if not item_id:
            continue
        existing = merged.get(item_id, [])
        merged[item_id] = list(dict.fromkeys([*existing, *refs]))
    return {item_id: refs for item_id, refs in merged.items() if refs}


def _delivered_from_raw(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, list[str]] = {}
    for item_id, refs in raw.items():
        if not item_id or not isinstance(refs, list):
            continue
        clean = [str(ref) for ref in refs if ref]
        if clean:
            parsed[str(item_id)] = list(dict.fromkeys(clean))
    return parsed


def _line_from_raw(raw: Any) -> tuple[str, str]:
    """Catch-up id and clock. Empty when this account has not been followed yet."""
    if not isinstance(raw, dict):
        return "", ""
    floor = raw.get("floor")
    since = raw.get("since")
    floor_text = floor.strip() if isinstance(floor, str) else ""
    since_text = since.strip() if isinstance(since, str) else ""
    if not floor_text.isdigit() or not since_text:
        return "", ""
    return floor_text, since_text


def _words_from_raw(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    words = raw.get("words")
    if not isinstance(words, dict):
        return {}
    return {str(item_id): str(fp) for item_id, fp in words.items() if item_id and fp}


def _bookmark_entry(
    ids: Sequence[str] | None = None, delivered: dict[str, list[str]] | None = None,
    line: tuple[str, str] = ("", ""), words: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    entry: dict[str, Any] = {}
    if ids:
        entry["ids"] = list(ids)
    if delivered:
        entry["delivered"] = delivered
    floor, since = line
    if floor and since:
        entry["floor"] = floor
        entry["since"] = since
    if words:
        entry["words"] = dict(words)
    return entry or None


def _canonical_bookmarks(data: dict[str, Any]) -> dict[str, Any]:
    collected_ids: dict[str, list[str]] = {}
    collected_delivered: dict[str, dict[str, list[str]]] = {}
    collected_line: dict[str, tuple[str, str]] = {}
    collected_words: dict[str, dict[str, str]] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        canon = _bookmark_key(key)
        raw_ids = value.get("ids")
        if isinstance(raw_ids, list):
            ids = _merge_ids(collected_ids.get(canon, ()), raw_ids, cap=None)
            if ids:
                collected_ids[canon] = ids
        delivered = _delivered_from_raw(value.get("delivered"))
        if delivered:
            collected_delivered[canon] = _merge_delivered(
                collected_delivered.get(canon, {}), delivered
            )
        line = _line_from_raw(value)
        if line[0] and canon not in collected_line:
            collected_line[canon] = line
        words = _words_from_raw(value)
        if words:
            collected_words.setdefault(canon, {}).update(words)
    out: dict[str, Any] = {}
    keys = set(collected_ids) | set(collected_delivered) | set(collected_line) | set(collected_words)
    for key in sorted(keys):
        delivered = collected_delivered.get(key, {})
        entry = _bookmark_entry(
            collected_ids.get(key), delivered, collected_line.get(key, ("", "")),
            collected_words.get(key),
        )
        if entry is not None:
            out[key] = entry
    return out


class BookmarkStore:
    """Remember which items were already seen per source + account."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_BOOKMARKS_PATH
        self._data: dict[str, Any] | None = None
        self._readable = True

    def is_seeded(self, source: str, account: str) -> bool:
        return bool(self._seen_id_list(source, account))

    def seen_ids(self, source: str, account: str) -> set[str]:
        return set(self._seen_id_list(source, account))

    def words(self, source: str, account: str) -> dict[str, str]:
        return _words_from_raw(self._entry(source, account))

    def note_words(self, source: str, account: str, item_id: str, fingerprint: str) -> None:
        item_key = str(item_id) if item_id else ""
        if not item_key or not fingerprint:
            return
        words = self.words(source, account)
        words[item_key] = fingerprint
        self._put(
            source, account, self._seen_id_list(source, account),
            self._delivered_map(source, account), words=words,
        )

    def remember(self, source: str, account: str, ids: Sequence[str]) -> None:
        incoming = [str(item_id) for item_id in ids if item_id]
        old = self._seen_id_list(source, account)
        # A small add (one posted id) must not drop older ids that are still in
        # the live feed. Only a full window/seed may trim leftovers to the cap.
        if old and len(incoming) < SEEN_CAP:
            total = len(incoming) + len(old)
        else:
            total = max(len(incoming), len(old))
        cap = min(SEEN_HARD_CAP, max(SEEN_CAP, total))
        merged = _merge_ids(incoming, old, cap=cap)
        dropped = set(old) - set(merged)
        delivered = {
            item_id: refs
            for item_id, refs in self._delivered_map(source, account).items()
            if item_id not in dropped
        }
        self._put(source, account, merged, delivered)

    def catch_line(self, source: str, account: str) -> tuple[int | None, datetime | None]:
        """Newest id already up when this account was first followed, and that clock."""
        floor, since = self._line(source, account)
        if not floor:
            return None, None
        try:
            stamp = datetime.fromisoformat(since)
        except ValueError:
            return None, None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return int(floor), stamp

    def remember_line(self, source: str, account: str, floor: str, since: datetime) -> None:
        """The first catch-up mark stays. A later page does not move it."""
        if self._line(source, account)[0] or not str(floor).isdigit():
            return
        stamp = since if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)
        self._put(
            source, account, self._seen_id_list(source, account),
            self._delivered_map(source, account), (str(floor), stamp.isoformat()),
        )

    def delivered_refs(self, source: str, account: str, item_id: str) -> set[str]:
        return set(self._delivered_map(source, account).get(str(item_id), ()))

    def delivered_item_ids(self, source: str, account: str) -> set[str]:
        return set(self._delivered_map(source, account))

    def mark_delivered(
        self, source: str, account: str, item_id: str, refs: Sequence[str],
    ) -> None:
        item_key = str(item_id) if item_id else ""
        incoming = [str(ref) for ref in refs if ref]
        if not item_key or not incoming:
            return
        delivered = _merge_delivered(self._delivered_map(source, account), {item_key: incoming})
        self._put(source, account, self._seen_id_list(source, account), delivered)

    def keep_only(self, follows: Iterable[tuple[str, str]]) -> int:
        """Drop accounts that nobody follows anymore. Return how many keys went."""
        data = self._load()
        wanted = {seen_key(source, account) for source, account in follows}
        kept = {key: value for key, value in data.items() if key in wanted}
        dropped = len(data) - len(kept)
        if not dropped:
            return 0
        self._save(kept)
        return dropped

    def move(self, source: str, old_account: str, new_account: str) -> None:
        """Put old_account's bookmarks on new_account and drop the old key."""
        if not new_account or old_account == new_account:
            return
        data = self._load()
        old_key = seen_key(source, old_account)
        new_key = seen_key(source, new_account)
        old_entry = data.get(old_key)
        if not isinstance(old_entry, dict):
            return
        new_entry = data.get(new_key)
        new_entry = new_entry if isinstance(new_entry, dict) else {}
        ids = _merge_ids(
            new_entry.get("ids") if isinstance(new_entry.get("ids"), list) else (),
            old_entry.get("ids") if isinstance(old_entry.get("ids"), list) else (),
            cap=SEEN_HARD_CAP,
        )
        delivered = _merge_delivered(
            _delivered_from_raw(new_entry.get("delivered")),
            _delivered_from_raw(old_entry.get("delivered")),
        )
        line = _line_from_raw(new_entry) 
        if not line[0]:
            line = _line_from_raw(old_entry)
        data.pop(old_key, None)
        words = {**_words_from_raw(old_entry), **_words_from_raw(new_entry)}
        self._put(source, new_account, ids, delivered, line, words)

    def _put(
        self, source: str, account: str, ids: Sequence[str],
        delivered: dict[str, list[str]], line: tuple[str, str] | None = None,
        words: dict[str, str] | None = None,
    ) -> None:
        data = self._load()
        if not self._readable:
            log.error("Not saving bookmarks; %s could not be read.", self.path)
            return
        key = seen_key(source, account)
        kept = self._line(source, account) if line is None else line
        if words is None:
            current = data.get(key)
            words = _words_from_raw(current if isinstance(current, dict) else {})
        keep = {str(item) for item in ids} | set(delivered)
        words = {item_id: fp for item_id, fp in words.items() if item_id in keep}
        entry = _bookmark_entry(ids, delivered, kept, words)
        if entry is None:
            data.pop(key, None)
        else:
            data[key] = entry
        self._save(data)

    def _line(self, source: str, account: str) -> tuple[str, str]:
        return _line_from_raw(self._entry(source, account))

    def _entry(self, source: str, account: str) -> dict[str, Any]:
        entry = self._load().get(seen_key(source, account))
        return entry if isinstance(entry, dict) else {}

    def _seen_id_list(self, source: str, account: str) -> list[str]:
        raw = self._entry(source, account).get("ids")
        return [str(item) for item in raw if item] if isinstance(raw, list) else []

    def _delivered_map(self, source: str, account: str) -> dict[str, list[str]]:
        return _delivered_from_raw(self._entry(source, account).get("delivered"))

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            data = self._read()
            if self._readable:
                self._data = data
            return data
        return self._data

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            self._readable = True
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("Could not read %s; bookmarks were not replaced.", self.path)
            self._readable = False
            return {}
        self._readable = True
        if not isinstance(data, dict):
            return {}
        cleaned = _canonical_bookmarks(data)
        if cleaned != data:
            self._save(cleaned)
        return cleaned

    def _save(self, data: dict[str, Any]) -> None:
        if not self._readable:
            log.error("Not saving bookmarks; %s could not be read.", self.path)
            return
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
        write_text_atomic(self.path, payload + "\n")
        self._data = data


async def aiohttp_get(url: str, headers: dict[str, str] | None) -> str:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            if response.status >= 400:
                try:
                    body = await response.text()
                except Exception:
                    body = ""
                raise FetchError(
                    f"HTTP {response.status} from {redact_fetch_url(url)}",
                    status=response.status, body=body,
                )
            return await response.text()


def json_headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


def load_json(body: str, *, what: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise FetchError(f"{what} is not valid JSON") from exc


def json_rows(payload: Any, key: str) -> list[dict[str, Any]]:
    rows = payload.get(key) if isinstance(payload, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


async def get_text(
    url: str, headers: dict[str, str] | None = None, *, getter: GetText | None = None,
) -> str:
    used = getter or aiohttp_get
    try:
        return await used(url, headers)
    except FetchError:
        raise
    except Exception as exc:
        detail = public_fetch_message(str(exc)).strip()
        where = redact_fetch_url(url)
        message = f"Could not fetch {where}: {detail}" if detail else f"Could not fetch {where}"
        raise FetchError(message) from exc
