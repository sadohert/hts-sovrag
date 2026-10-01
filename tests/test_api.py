"""HTTP API in process: configuration refusal, binding, the query contract, health."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2 as httpx
import pytest

from sovrag.api import app as app_mod
from sovrag.api.app import build_app
from sovrag.auth import Principal
from sovrag.config import Settings
from sovrag.generate.answer import Answer, Citation
from sovrag.generate.llm import GenerationError

SECRET = "gateway-shared-secret"
PRINCIPALS = Path("data/principals.yaml").resolve()


def _settings(**overrides: str) -> Settings:
    base = {
        "principals_path": str(PRINCIPALS),
        "audit_log_path": "/dev/null",
        "api_identity": "",
        "api_shared_secret": "",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]


def _answer(
    question: str, *, principal: Principal, mode: object, k: int, request_id: str | None
) -> Answer:
    return Answer(
        question=question,
        principal=principal.id,
        mode=str(mode),
        request_id=request_id,
        text="The plan is X [1].",
        citations=(Citation(1, "d", 7, "t", "h", "https://example.test/d"),),
        refused=False,
        model="gen",
        retrieved_chunk_ids=(7, 8),
        applied_filter="f",
    )


class Recorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = fail

    def __call__(
        self, question: str, *, principal: Principal, mode: object, k: int, request_id: str | None
    ) -> Answer:
        self.calls.append(
            {
                "question": question,
                "principal": principal.id,
                "mode": str(mode),
                "k": k,
                "request_id": request_id,
            }
        )
        if self.fail:
            raise GenerationError("generation failed: model unreachable")
        return _answer(question, principal=principal, mode=mode, k=k, request_id=request_id)


@asynccontextmanager
async def _client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        yield client


GATEWAY = {"Authorization": f"Bearer {SECRET}", "X-Sovrag-Principal": "auditor"}


# configuration


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({}, "API_IDENTITY"),
        ({"api_identity": "mattermost"}, "API_IDENTITY"),
        ({"api_identity": "static", "principal": ""}, "PRINCIPAL"),
        ({"api_identity": "static", "principal": "nobody"}, "nobody"),
        ({"api_identity": "gateway"}, "API_SHARED_SECRET"),
    ],
)
def test_bad_configuration_refuses_to_build(overrides: dict[str, str], needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        build_app(_settings(**overrides))


# binding


@pytest.mark.asyncio
async def test_static_mode_binds_the_configured_principal_and_ignores_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = Recorder()
    monkeypatch.setattr(app_mod, "generate_answer", rec)
    async with _client(_settings(api_identity="static", principal="analyst_dnd")) as c:
        r = await c.post("/query", json={"question": "q"}, headers=GATEWAY)
    assert r.status_code == 200, r.text
    assert rec.calls[0]["principal"] == "analyst_dnd"


@pytest.mark.asyncio
async def test_gateway_mode_binds_the_header_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    monkeypatch.setattr(app_mod, "generate_answer", rec)
    async with _client(_settings(api_identity="gateway", api_shared_secret=SECRET)) as c:
        ok = await c.post("/query", json={"question": "q"}, headers=GATEWAY)
        bad = await c.post(
            "/query", json={"question": "q"}, headers={**GATEWAY, "X-Sovrag-Principal": "nobody"}
        )
        none = await c.post("/query", json={"question": "q"})
    assert ok.status_code == 200 and rec.calls[0]["principal"] == "auditor"
    assert bad.status_code == 401 and none.status_code == 401
    assert len(rec.calls) == 1


# the query contract


@pytest.mark.asyncio
async def test_response_is_the_answer_with_a_server_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = Recorder()
    monkeypatch.setattr(app_mod, "generate_answer", rec)
    async with _client(_settings(api_identity="static", principal="auditor")) as c:
        r = await c.post(
            "/query",
            json={"question": " What is the plan? "},
            headers={"X-Request-ID": "client-chosen"},
        )
    body = r.json()
    rid = body["request_id"]
    assert rid and rid != "client-chosen"
    assert r.headers["x-request-id"] == rid
    assert rec.calls[0] == {
        "question": "What is the plan?",
        "principal": "auditor",
        "mode": "hybrid",
        "k": 5,
        "request_id": rid,
    }
    assert body["citations"][0]["chunk_id"] == 7 and body["retrieved_chunk_ids"] == [7, 8]
    assert body["applied_filter"] == "f" and body["refused"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("k", "expected"), [(0, 1), (50, 10), (3, 3)])
async def test_k_is_clamped(monkeypatch: pytest.MonkeyPatch, k: int, expected: int) -> None:
    rec = Recorder()
    monkeypatch.setattr(app_mod, "generate_answer", rec)
    async with _client(_settings(api_identity="static", principal="auditor")) as c:
        r = await c.post("/query", json={"question": "q", "k": k, "mode": "vector"})
    assert r.status_code == 200
    assert rec.calls[0]["k"] == expected and rec.calls[0]["mode"] == "vector"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"question": "q", "principal": "auditor"},
        {"question": ""},
        {"question": "   "},
        {"question": "x" * 2001},
        {"question": "q", "k": "five"},
        {"question": "q", "k": "7"},
        {"question": "q", "k": True},
        {"question": "q", "mode": "bm25"},
        {},
    ],
)
async def test_bad_bodies_are_422_and_never_reach_the_generator(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    rec = Recorder()
    monkeypatch.setattr(app_mod, "generate_answer", rec)
    async with _client(_settings(api_identity="static", principal="auditor")) as c:
        r = await c.post("/query", json=body)
    assert r.status_code == 422
    assert rec.calls == []


@pytest.mark.asyncio
async def test_malformed_json_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    monkeypatch.setattr(app_mod, "generate_answer", rec)
    async with _client(_settings(api_identity="static", principal="auditor")) as c:
        r = await c.post(
            "/query", content=b"{not json", headers={"content-type": "application/json"}
        )
    assert r.status_code == 422 and rec.calls == []


@pytest.mark.asyncio
async def test_generation_failure_is_502_with_the_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = Recorder(fail=True)
    monkeypatch.setattr(app_mod, "generate_answer", rec)
    async with _client(_settings(api_identity="static", principal="auditor")) as c:
        r = await c.post("/query", json={"question": "q"})
    assert r.status_code == 502
    assert r.json() == {
        "detail": "generation failed: model unreachable",
        "request_id": rec.calls[0]["request_id"],
    }
    assert r.headers["x-request-id"] == rec.calls[0]["request_id"]


@pytest.mark.asyncio
async def test_empty_generator_reply_gets_the_prefix_once(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> Answer:
        raise GenerationError("empty reply from the generator")

    monkeypatch.setattr(app_mod, "generate_answer", boom)
    async with _client(_settings(api_identity="static", principal="auditor")) as c:
        r = await c.post("/query", json={"question": "q"})
    assert r.status_code == 502
    body = r.json()
    assert body["detail"] == "generation failed: empty reply from the generator"
    assert body["request_id"] == r.headers["x-request-id"]


@pytest.mark.asyncio
async def test_trailing_slash_is_404_not_a_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    monkeypatch.setattr(app_mod, "generate_answer", rec)
    async with _client(_settings(api_identity="static", principal="auditor")) as c:
        r = await c.post("/query/", json={"question": "q"})
    assert r.status_code == 404 and rec.calls == []


@pytest.mark.asyncio
async def test_head_healthz_is_200_without_a_bearer_in_both_modes() -> None:
    async with _client(_settings(api_identity="gateway", api_shared_secret=SECRET)) as c:
        assert (await c.head("/healthz")).status_code == 200
    async with _client(_settings(api_identity="static", principal="analyst_dnd")) as c:
        assert (await c.head("/healthz")).status_code == 200


# health


@pytest.mark.asyncio
async def test_healthz_reports_mode_and_needs_no_bearer_in_gateway_mode() -> None:
    async with _client(_settings(api_identity="gateway", api_shared_secret=SECRET)) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "identity": "gateway", "principal": None}
    async with _client(_settings(api_identity="static", principal="analyst_dnd")) as c:
        r = await c.get("/healthz")
    assert r.json() == {"status": "ok", "identity": "static", "principal": "analyst_dnd"}
