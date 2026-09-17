"""Embedding client: OpenAI-compatible /embeddings, batched, dimension-checked."""

import json

import httpx2 as httpx
import pytest

from sovrag.index.embed import Embedder


def _server(dim: int, seen: list[dict]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/embeddings")
        body = json.loads(request.content)
        seen.append(body)
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        data = [
            {"object": "embedding", "index": i, "embedding": [float(i)] * dim}
            for i, _ in enumerate(inputs)
        ]
        return httpx.Response(200, json={"object": "list", "data": data, "model": body["model"]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_embeds_texts_in_order_with_configured_model() -> None:
    seen: list[dict] = []
    emb = Embedder(base_url="http://m/engines/v1", model="m", dim=4, http_client=_server(4, seen))
    vectors = emb.embed(["a", "b", "c"])
    assert vectors == [[0.0] * 4, [1.0] * 4, [2.0] * 4]
    assert seen[0]["model"] == "m"
    assert seen[0]["input"] == ["a", "b", "c"]


def test_batches_requests_to_batch_size() -> None:
    seen: list[dict] = []
    emb = Embedder(
        base_url="http://m/engines/v1", model="m", dim=2, http_client=_server(2, seen), batch_size=2
    )
    vectors = emb.embed(["a", "b", "c", "d", "e"])
    assert len(vectors) == 5
    assert [len(b["input"]) for b in seen] == [2, 2, 1]


def test_rejects_vectors_of_the_wrong_dimension() -> None:
    emb = Embedder(base_url="http://m/engines/v1", model="m", dim=8, http_client=_server(4, []))
    with pytest.raises(ValueError, match="dimension"):
        emb.embed(["a"])


def test_empty_input_makes_no_request() -> None:
    seen: list[dict] = []
    emb = Embedder(base_url="http://m/engines/v1", model="m", dim=4, http_client=_server(4, seen))
    assert emb.embed([]) == []
    assert seen == []
