"""Generator client: OpenAI-compatible chat completions with thinking off and temperature 0."""

import json

import httpx2 as httpx
import pytest

from sovrag.generate.llm import GenerationError, Generator


def _server(reply: str | None, status: int, seen: list[dict]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        seen.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "boom"}})
        message = {"role": "assistant", "content": reply}
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _gen(
    reply: str | None = "ok [1]", status: int = 200, seen: list[dict] | None = None
) -> Generator:
    return Generator(
        base_url="http://m/engines/v1",
        model="m",
        http_client=_server(reply, status, seen if seen is not None else []),
        max_retries=0,
    )


MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_returns_the_reply_and_sends_temperature_zero_with_thinking_off() -> None:
    seen: list[dict] = []
    assert _gen(seen=seen).complete(MESSAGES) == "ok [1]"
    body = seen[0]
    assert body["model"] == "m"
    assert body["temperature"] == 0
    assert body["max_tokens"] == 600
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"] == MESSAGES


def test_max_tokens_override() -> None:
    seen: list[dict] = []
    _gen(seen=seen).complete(MESSAGES, max_tokens=50)
    assert seen[0]["max_tokens"] == 50


def test_server_error_becomes_generation_error() -> None:
    with pytest.raises(GenerationError, match="generation failed"):
        _gen(status=500).complete(MESSAGES)


def test_empty_reply_becomes_generation_error() -> None:
    with pytest.raises(GenerationError, match="empty"):
        _gen(reply="   ").complete(MESSAGES)
    with pytest.raises(GenerationError, match="empty"):
        _gen(reply=None).complete(MESSAGES)
