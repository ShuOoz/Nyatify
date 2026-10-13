"""HTTP chat client. No Discord, no vendor SDK, no card reply shapes."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from config import proxy_url

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 90
CONNECT_TRIES = 3
CONNECT_PAUSE = 0.4
# DeepSeek thinking and the answer share max_tokens. reasoning_effort caps
# the thought. This page is thought plus answer, not the host 64K default.
THINKING_TOKENS = 16_384
USER_AGENT = (
    "Mozilla/5.0 (compatible; Nyatify/0.1; +https://github.com/nyatify)"
)

PostJson = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[Any]]


class AIError(Exception):
    """The model host could not complete the request."""


class AI:
    """A phone line: `available()` and `complete(messages)`."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        post_json: PostJson | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip() or None
        self.base_url = (base_url or "").strip().rstrip("/") or None
        self.model = (model or "").strip() or None
        self._post_json = post_json

    def available(self) -> bool:
        return bool(self.base_url and self.model)

    async def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        **options: Any,
    ) -> str:
        if not self.available():
            raise AIError("AI is not configured (need AI_BASE_URL and AI_MODEL).")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _messages(messages),
            **options,
        }
        # DeepSeek V4 thinks first, and that thought shares max_tokens with the
        # answer. Keep thinking on. Use the 16K page so a low thought still
        # leaves room for the JSON. reasoning_content is not the reply.
        if _uses_deepseek(self.base_url, self.model) and "thinking" not in options:
            payload["thinking"] = {"type": "enabled"}
        if _thinking_enabled(payload):
            payload["max_tokens"] = THINKING_TOKENS
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = await self._post(self.base_url, headers, payload)
        text = _reply_text(data)
        if text is None:
            raise AIError("Model reply was missing text.")
        if not text:
            raise AIError("Model reply was empty.")
        return text

    async def _post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> Any:
        poster = self._post_json or _aiohttp_post_json
        saved: Exception | None = None
        for attempt in range(CONNECT_TRIES):
            try:
                return await poster(url, headers, payload)
            except AIError:
                raise
            except Exception as exc:
                saved = exc
                if attempt + 1 == CONNECT_TRIES:
                    break
                log.warning("Model host connection failed; retrying")
                await asyncio.sleep(CONNECT_PAUSE)
        raise AIError("Could not reach the model host.") from saved


def _uses_deepseek(base_url: str | None, model: str | None) -> bool:
    haystack = f"{base_url or ''} {model or ''}".lower()
    return "deepseek" in haystack


def _thinking_enabled(payload: Mapping[str, Any]) -> bool:
    thinking = payload.get("thinking")
    if isinstance(thinking, Mapping):
        return thinking.get("type") == "enabled"
    return False


def _nonempty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _messages(messages: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            raise AIError("Each message needs string role and content.")
        out.append({"role": role, "content": content})
    if not out:
        raise AIError("Need at least one message.")
    return out


def _reply_text(data: Any) -> str | None:
    """Pull the model text out of a few common JSON shapes."""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return None
    for key in ("text", "output_text", "response"):
        text = _nonempty_text(data.get(key))
        if text is not None:
            return text
    content = data.get("content")
    text = _nonempty_text(content)
    if text is not None:
        return text
    if isinstance(content, dict):
        nested = _reply_text(content)
        if nested is not None:
            return nested
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                nested = _reply_text(item)
                if nested:
                    parts.append(nested)
        if parts:
            return "\n".join(parts)
    for key in ("message", "choices", "candidates", "parts"):
        nested = _first_nested(data.get(key))
        if nested is not None:
            return nested
    return None


def _first_nested(value: Any) -> str | None:
    if isinstance(value, list):
        if not value:
            return None
        return _reply_text(value[0])
    return _reply_text(value)


async def _aiohttp_post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> Any:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
    kwargs: dict[str, Any] = {"headers": headers, "json": payload}
    proxy = proxy_url("ai_proxy")
    if proxy:
        kwargs["proxy"] = proxy
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, **kwargs) as response:
            body = await response.text()
            if response.status >= 400:
                raise AIError(f"HTTP {response.status} from model host")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise AIError("Model host did not return JSON.") from exc
