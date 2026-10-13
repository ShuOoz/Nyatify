from __future__ import annotations

import pytest

import ai
from ai import AI, THINKING_TOKENS, AIError, _aiohttp_post_json
from config import settings as live_settings


@pytest.fixture(autouse=True)
def _no_connect_pause(monkeypatch):
    monkeypatch.setattr(ai, "CONNECT_PAUSE", 0)


class FakePost:
    def __init__(self, body: object) -> None:
        self.body = body
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.payloads: list[dict] = []

    async def __call__(self, url: str, headers: dict[str, str], payload: dict) -> object:
        self.urls.append(url)
        self.headers.append(headers)
        self.payloads.append(payload)
        body = self.body
        if isinstance(body, list):
            body = body.pop(0)
        if isinstance(body, Exception):
            raise body
        return body


def _client(body: object, **kwargs) -> tuple[AI, FakePost]:
    http = FakePost(body)
    client = AI(
        api_key=kwargs.get("api_key", "secret"),
        base_url=kwargs.get("base_url", "https://ai.example.test/chat"),
        model=kwargs.get("model", "local-chat"),
        post_json=http,
    )
    return client, http



async def test_ai(monkeypatch):
    await case_complete()
    with monkeypatch.context() as mp:
        await case_post_json_uses_proxy_only_when_set(mp)


async def case_complete():
    case_available_needs_url_and_model()
    await case_complete_posts_and_reads_common_reply_shapes()
    await case_complete_rejects_bad_input_and_empty_replies()
    await case_complete_deepseek_thinking_and_errors()

def case_available_needs_url_and_model():
    assert AI().available() is False
    assert AI(api_key="secret").available() is False
    assert AI(model="local-chat").available() is False
    assert AI(base_url="https://ai.example.test/chat").available() is False
    assert AI(base_url="https://ai.example.test/chat", model="local-chat").available() is True
    assert AI(base_url="  ", model="local-chat").available() is False
    assert AI(base_url="https://ai.example.test/chat/").base_url == (
        "https://ai.example.test/chat"
    )


async def case_complete_posts_and_reads_common_reply_shapes():
    messages = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Hi"},
    ]
    client, http = _client({"text": "  hello  "})
    assert await client.complete(messages, temperature=0, max_tokens=16) == "hello"
    assert http.urls == ["https://ai.example.test/chat"]
    assert http.headers[0]["Authorization"] == "Bearer secret"
    assert http.payloads[0]["messages"] == messages
    client, http = _client({"text": "ok"}, api_key=None)
    await client.complete([{"role": "user", "content": "Hi"}])
    assert "Authorization" not in http.headers[0]
    for body in (
        {"text": "hello"},
        {"content": "hello"},
        {"message": {"content": "hello"}},
        {"choices": [{"message": {"content": "hello"}}]},
        {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]},
        {"content": [{"type": "text", "text": "hello"}]},
    ):
        client, _http = _client(body)
        assert await client.complete([{"role": "user", "content": "Hi"}]) == "hello"


async def case_complete_rejects_bad_input_and_empty_replies():
    client, http = _client({"text": "no"}, base_url=None, model=None)
    with pytest.raises(AIError, match="not configured"):
        await client.complete([{"role": "user", "content": "Hi"}])
    assert http.urls == []
    client, http = _client({"text": "no"})
    with pytest.raises(AIError, match="at least one message"):
        await client.complete([])
    with pytest.raises(AIError, match="missing text"):
        await _client({"status": "ok"})[0].complete([{"role": "user", "content": "Hi"}])
    with pytest.raises(AIError, match="missing text"):
        await _client({"text": "   "})[0].complete([{"role": "user", "content": "Hi"}])
    with pytest.raises(AIError, match="missing text"):
        await _client(
            {"choices": [{"message": {"content": "", "reasoning_content": "KEEP"}}]}
        )[0].complete([{"role": "user", "content": "Hi"}])


async def case_complete_deepseek_thinking_and_errors():
    client, http = _client(
        {"text": "KEEP"},
        base_url="https://api.deepseek.com/chat/completions",
        model="deepseek-flash",
    )
    assert await client.complete([{"role": "user", "content": "Hi"}]) == "KEEP"
    assert http.payloads[0]["thinking"] == {"type": "enabled"}
    assert http.payloads[0]["max_tokens"] == THINKING_TOKENS
    await client.complete(
        [{"role": "user", "content": "Hi"}], max_tokens=16
    )
    assert http.payloads[1]["thinking"] == {"type": "enabled"}
    assert http.payloads[1]["max_tokens"] == THINKING_TOKENS
    await client.complete(
        [{"role": "user", "content": "Hi"}],
        thinking={"type": "disabled"},
        max_tokens=16,
    )
    assert http.payloads[2]["thinking"] == {"type": "disabled"}
    assert http.payloads[2]["max_tokens"] == 16
    await client.complete(
        [{"role": "user", "content": "Hi"}], thinking={"type": "enabled"}, max_tokens=32
    )
    assert http.payloads[3]["thinking"] == {"type": "enabled"}
    assert http.payloads[3]["max_tokens"] == THINKING_TOKENS
    await client.complete(
        [{"role": "user", "content": "Hi"}],
        reasoning_effort="low",
        max_tokens=16,
    )
    assert http.payloads[4]["thinking"] == {"type": "enabled"}
    assert http.payloads[4]["reasoning_effort"] == "low"
    assert http.payloads[4]["max_tokens"] == THINKING_TOKENS
    with pytest.raises(AIError, match="missing text"):
        await _client(
            {"choices": [{"message": {"content": "", "reasoning_content": "KEEP"}}]},
            base_url="https://api.deepseek.com/chat/completions",
            model="deepseek-flash",
        )[0].complete([{"role": "user", "content": "Hi"}], max_tokens=16)
    other, http = _client({"text": "KEEP"})
    await other.complete([{"role": "user", "content": "Hi"}])
    assert "thinking" not in http.payloads[0]
    with pytest.raises(AIError, match="HTTP 401"):
        await _client(AIError("HTTP 401 from model host"))[0].complete(
            [{"role": "user", "content": "Hi"}]
        )
    down, http = _client(RuntimeError("down"))
    with pytest.raises(AIError, match="Could not reach"):
        await down.complete([{"role": "user", "content": "Hi"}])
    assert len(http.urls) == 3

    class _ThenOk(FakePost):
        async def __call__(self, url: str, _headers: dict[str, str], _payload: dict) -> object:
            self.urls.append(url)
            if len(self.urls) == 1:
                raise RuntimeError("reset")
            return {"text": "KEEP"}

    flaky = _ThenOk(None)
    client = AI(
        api_key="secret",
        base_url="https://ai.example.test/chat",
        model="local-chat",
        post_json=flaky,
    )
    assert await client.complete([{"role": "user", "content": "Hi"}]) == "KEEP"
    assert len(flaky.urls) == 2


async def case_post_json_uses_proxy_only_when_set(monkeypatch):
    sent = []

    class _Response:
        status = 200

        async def text(self):
            return '{"text":"ok"}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Session:
        def post(self, url, **kwargs):
            sent.append(kwargs)
            return _Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr("aiohttp.ClientSession", lambda *args, **kwargs: _Session())
    monkeypatch.setattr(live_settings, "ai_proxy", None)
    await _aiohttp_post_json("https://ai.example.test/chat", {}, {"model": "local"})
    monkeypatch.setattr(live_settings, "ai_proxy", "http://127.0.0.1:7890")
    await _aiohttp_post_json("https://ai.example.test/chat", {}, {"model": "local"})
    assert "proxy" not in sent[0]
    assert sent[1]["proxy"] == "http://127.0.0.1:7890"
