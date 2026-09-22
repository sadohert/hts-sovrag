"""MCP server: bearer and user-id gate, per-call binding, and both tools, in process."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2 as httpx
import psycopg
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from sovrag.auth import LEAST_PRIVILEGED, Clearance, Principal, Resolution, ResolutionError
from sovrag.config import get_settings
from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.generate.answer import Answer, Citation
from sovrag.generate.llm import GenerationError
from sovrag.index.store import replace_chunks, upsert_document
from sovrag.mcp import server as server_mod
from sovrag.mcp.server import ENTITLEMENT_ERROR, Identity, SovragMcp, build_app

DIM = 1024
BASE = "http://localhost:8001"
SECRET = "plugin-shared-secret"
UID_PUB = "p" * 26
UID_DND = "d" * 26
UID_AUD = "a" * 26
UID_GONE = "g" * 26
UID_DOWN = "x" * 26

PRINCIPALS = {
    UID_PUB: LEAST_PRIVILEGED,
    UID_DND: Principal("analyst_dnd", Clearance.PROTECTED_A, frozenset({"DND"})),
    UID_AUD: Principal(
        "auditor", Clearance.PROTECTED_B, frozenset({"CSE", "DND", "PS", "PSPC", "SSC", "TBS"})
    ),
}
VISIBLE = {UID_PUB: {"pub"}, UID_DND: {"pub", "dnd"}, UID_AUD: {"pub", "dnd", "oag"}}


class FakeResolver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, user_id: str) -> Resolution:
        self.calls.append(user_id)
        if user_id == UID_GONE:
            raise ResolutionError("unknown user")
        if user_id == UID_DOWN:
            raise ResolutionError("/api/v4/users: ConnectTimeout")
        who = PRINCIPALS[user_id]
        note = (
            "no sovrag_clearance value; bound to least privilege"
            if who == LEAST_PRIVILEGED
            else None
        )
        attrs = {} if note else {"sovrag_clearance": who.clearance.value}
        return Resolution(user_id, who.id, who, attrs, note)


class StubEmbedder:
    """Every seeded chunk is a unit axis; this query is equally close to all of them."""

    calls = 0

    def embed_one(self, text: str) -> list[float]:
        StubEmbedder.calls += 1
        v = [0.0] * DIM
        v[0] = v[1] = v[2] = 1.0
        return v


def _meta(doc_id: str, department: str, classification: str) -> DocumentMeta:
    return DocumentMeta(
        id=doc_id,
        title=f"Title of {doc_id}",
        department=department,
        doc_type="DP",
        fiscal_year="2024-25",
        lang="en",
        source_url=f"https://example.test/{doc_id}.html",
        pages=(),
        classification=classification,
    )


def _unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def _seed(db: psycopg.Connection) -> None:
    for axis, (doc_id, dept, cls) in enumerate(
        [
            ("pub", "GC_WIDE", "UNCLASSIFIED"),
            ("dnd", "DND", "PROTECTED_A"),
            ("oag", "GC_WIDE", "PROTECTED_B"),
        ]
    ):
        upsert_document(db, _meta(doc_id, dept, cls))
        replace_chunks(db, doc_id, [Chunk(0, "Budget", f"budget {doc_id}")], [_unit(axis)])


@pytest.fixture
def env(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str, tmp_path: Path, db: psycopg.Connection
) -> Iterator[Path]:
    """Test database seeded, audit log in tmp, embedder stubbed, MCP env cleared."""
    _seed(db)
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit))
    for name in ("MCP_IDENTITY", "PRINCIPAL", "MCP_SHARED_SECRET", "MCP_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MATTERMOST_URL", "http://mm.test")
    monkeypatch.setenv("MATTERMOST_BOT_TOKEN", "bot-token")
    monkeypatch.setattr(
        server_mod.Embedder, "from_settings", classmethod(lambda cls, s: StubEmbedder())
    )
    StubEmbedder.calls = 0
    yield audit


def _mattermost(monkeypatch: pytest.MonkeyPatch, resolver: FakeResolver | None = None) -> SovragMcp:
    monkeypatch.setenv("MCP_IDENTITY", "mattermost")
    monkeypatch.setenv("MCP_SHARED_SECRET", SECRET)
    return build_app(get_settings(), resolver=resolver or FakeResolver())


def _static(monkeypatch: pytest.MonkeyPatch, principal: str = "analyst_dnd") -> SovragMcp:
    monkeypatch.setenv("MCP_IDENTITY", "static")
    monkeypatch.setenv("PRINCIPAL", principal)
    return build_app(get_settings())


def _headers(user_id: str | None = UID_DND, bearer: str | None = SECRET) -> dict[str, str]:
    h: dict[str, str] = {}
    if bearer is not None:
        h["Authorization"] = f"Bearer {bearer}"
    if user_id is not None:
        h["X-Mattermost-UserID"] = user_id
    return h


@asynccontextmanager
async def _client(built: SovragMcp, headers: dict[str, str]) -> AsyncIterator[Client]:
    async with built.server.session_manager.run():
        transport = httpx.ASGITransport(app=built.app)
        async with (
            httpx.AsyncClient(transport=transport, base_url=BASE, headers=headers) as hc,
            Client(streamable_http_client(f"{BASE}/mcp", http_client=hc)) as client,
        ):
            yield client


async def _raw_initialize(
    built: SovragMcp, headers: dict[str, str], base: str = BASE
) -> httpx.Response:
    """One HTTP request, no MCP client: what the gate answers before the SDK sees anything."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    accept = {"Accept": "application/json, text/event-stream"}
    async with built.server.session_manager.run():
        transport = httpx.ASGITransport(app=built.app)
        async with httpx.AsyncClient(
            transport=transport, base_url=base, headers={**accept, **headers}
        ) as hc:
            return await hc.post("/mcp", json=body)


def _events(audit: Path) -> list[dict]:
    if not audit.exists():
        return []
    return [json.loads(line) for line in audit.read_text().splitlines()]


# Build-time refusals: the server never starts in a state that could bind the wrong principal.


def test_identity_is_required(env: Path) -> None:
    with pytest.raises(ValueError, match="MCP_IDENTITY"):
        build_app(get_settings())


def test_an_unknown_identity_is_refused(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_IDENTITY", "anonymous")
    with pytest.raises(ValueError, match="MCP_IDENTITY"):
        build_app(get_settings())


@pytest.mark.parametrize("principal", ["", "nobody"])
def test_static_mode_refuses_a_missing_or_unknown_principal(
    env: Path, monkeypatch: pytest.MonkeyPatch, principal: str
) -> None:
    monkeypatch.setenv("MCP_IDENTITY", "static")
    monkeypatch.setenv("PRINCIPAL", principal)
    with pytest.raises(ValueError, match="PRINCIPAL"):
        build_app(get_settings())


def test_mattermost_mode_refuses_without_a_shared_secret(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_IDENTITY", "mattermost")
    with pytest.raises(ValueError, match="MCP_SHARED_SECRET"):
        build_app(get_settings(), resolver=FakeResolver())


def test_static_mode_never_builds_a_resolver(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATTERMOST_URL")
    monkeypatch.delenv("MATTERMOST_BOT_TOKEN")
    assert _static(monkeypatch).identity is Identity.static


# The gate: 401 before any retrieval, never a downgrade.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        _headers(bearer=None),
        _headers(bearer=""),
        _headers(bearer="wrong"),
        _headers(bearer=SECRET + "x"),
        _headers(user_id=None),
        _headers(user_id=""),
        _headers(user_id="d" * 25),
        _headers(user_id="D" * 26),
        _headers(user_id="../" + "d" * 23),
    ],
    ids=[
        "no-bearer",
        "empty-bearer",
        "wrong-bearer",
        "longer-bearer",
        "no-user",
        "empty-user",
        "short-user",
        "upper-user",
        "path-user",
    ],
)
async def test_a_bad_bearer_or_user_id_is_401_before_anything_runs(
    env: Path, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    resolver = FakeResolver()
    built = _mattermost(monkeypatch, resolver)
    response = await _raw_initialize(built, headers)
    assert response.status_code == 401
    assert "mcp-session-id" not in response.headers
    assert resolver.calls == [] and StubEmbedder.calls == 0 and _events(env) == []


@pytest.mark.asyncio
async def test_the_gate_lets_a_good_request_through(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _mattermost(monkeypatch)
    response = await _raw_initialize(built, _headers())
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "sovrag"


@pytest.mark.asyncio
async def test_allowed_hosts_setting_governs_the_host_header(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp:*")
    allowed = await _raw_initialize(_mattermost(monkeypatch), _headers(), base="http://mcp:8001")
    assert allowed.status_code == 200
    # A fresh app for the second request: the SDK's session manager runs once per instance.
    refused = await _raw_initialize(_mattermost(monkeypatch), _headers(), base=BASE)
    assert refused.status_code == 421


# Static mode: the principal from configuration, the header ignored.


@pytest.mark.asyncio
async def test_static_mode_binds_the_configured_principal_and_ignores_headers(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _static(monkeypatch, "analyst_dnd")
    async with _client(built, _headers(user_id=UID_AUD, bearer=None)) as client:
        result = await client.call_tool("search", {"question": "budget"})
    assert not result.is_error
    data = result.structured_content
    assert data is not None and data["principal"] == "analyst_dnd"
    assert {h["doc_id"] for h in data["hits"]} == VISIBLE[UID_DND]
    assert data["applied_filter"].startswith("classification in (UNCLASSIFIED, PROTECTED_A)")
    events = _events(env)
    assert [e["event"] for e in events] == ["search"]
    assert events[0]["principal"] == "analyst_dnd" and events[0]["request_id"] == data["request_id"]


# Mattermost mode: each user gets exactly their document set.


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", sorted(VISIBLE), ids=["auditor", "analyst_dnd", "public"])
async def test_search_returns_exactly_the_documents_the_user_may_see(
    env: Path, monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    built = _mattermost(monkeypatch)
    async with _client(built, _headers(user_id=user_id)) as client:
        result = await client.call_tool("search", {"question": "budget", "k": 10})
    data = result.structured_content
    assert data is not None and {h["doc_id"] for h in data["hits"]} == VISIBLE[user_id]
    assert data["principal"] == PRINCIPALS[user_id].id
    for hit in data["hits"]:
        assert {
            "doc_id",
            "chunk_id",
            "heading_path",
            "source_url",
            "title",
            "text",
            "score",
        } <= set(hit)
        assert hit["text"] == f"budget {hit['doc_id']}"
    resolve, search = _events(env)
    assert resolve["event"] == "resolve" and search["event"] == "search"
    assert resolve["user_id"] == user_id and resolve["principal"] == PRINCIPALS[user_id].id
    assert search["chunk_ids"] and len(search["chunk_ids"]) == len(data["hits"])
    assert resolve["request_id"] == search["request_id"] == data["request_id"]


@pytest.mark.asyncio
async def test_a_user_with_nothing_recognised_is_least_privilege_and_the_note_is_logged(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _mattermost(monkeypatch)
    async with _client(built, _headers(user_id=UID_PUB)) as client:
        result = await client.call_tool("search", {"question": "budget"})
    assert (
        result.structured_content is not None
        and result.structured_content["principal"] == "anonymous"
    )
    resolve = _events(env)[0]
    assert resolve["principal"] == "anonymous" and "least privilege" in resolve["note"]


@pytest.mark.asyncio
async def test_one_tool_call_resolves_once_however_many_http_requests_it_took(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = FakeResolver()
    built = _mattermost(monkeypatch, resolver)
    async with _client(built, _headers()) as client:
        await client.list_tools()
        await client.list_tools()
        await client.call_tool("search", {"question": "budget"})
    assert resolver.calls == [UID_DND]
    assert [e["event"] for e in _events(env)] == ["resolve", "search"]


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [UID_GONE, UID_DOWN], ids=["unknown-user", "mattermost-down"])
async def test_a_resolution_failure_is_a_tool_error_and_retrieves_nothing(
    env: Path, monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    built = _mattermost(monkeypatch)
    async with _client(built, _headers(user_id=user_id)) as client:
        result = await client.call_tool("search", {"question": "budget"})
    assert result.is_error
    assert any(ENTITLEMENT_ERROR in getattr(block, "text", "") for block in result.content)
    assert StubEmbedder.calls == 0
    (resolve,) = _events(env)
    assert resolve["event"] == "resolve" and resolve["user_id"] == user_id
    assert "error" in resolve and "principal" not in resolve


@pytest.mark.asyncio
async def test_neither_tool_accepts_a_principal_argument(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _mattermost(monkeypatch)
    async with _client(built, _headers()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"search", "answer"}
    assert set(tools["search"].input_schema["properties"]) == {"question", "k"}
    assert set(tools["answer"].input_schema["properties"]) == {"question"}
    for tool in tools.values():
        assert "principal" in (tool.description or "").lower()


@pytest.mark.asyncio
async def test_k_is_clamped_to_max_k(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    built = _mattermost(monkeypatch)
    async with _client(built, _headers(user_id=UID_AUD)) as client:
        await client.call_tool("search", {"question": "budget", "k": 500})
    assert _events(env)[1]["k"] == 10


# The answer tool: generation is the existing entry point, called with the bound principal.


def _fake_answer(seen: list[dict[str, object]], refused: bool = False) -> object:
    def fake(
        question: str, *, principal: Principal, mode: object, k: int, request_id: str | None
    ) -> Answer:
        seen.append(
            {
                "question": question,
                "principal": principal,
                "mode": mode,
                "k": k,
                "request_id": request_id,
            }
        )
        if refused:
            text, citations = "I can't answer that from the documents available to you.", ()
        else:
            text, citations = (
                "Yes [1].",
                (Citation(1, "dnd", 2, "Title of dnd", "Budget", "https://example.test/dnd.html"),),
            )
        return Answer(
            question=question,
            principal=principal.id,
            mode=str(mode),
            request_id=request_id,
            text=text,
            citations=citations,
            refused=refused,
            model="m",
            retrieved_chunk_ids=(2,),
            applied_filter="filter",
        )

    return fake


@pytest.mark.asyncio
async def test_answer_calls_generation_with_the_bound_principal_and_returns_it_unchanged(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(server_mod, "generate_answer", _fake_answer(seen))
    built = _mattermost(monkeypatch)
    async with _client(built, _headers(user_id=UID_DND)) as client:
        result = await client.call_tool("answer", {"question": "Is there a budget?"})
    assert not result.is_error and result.structured_content is not None
    data = result.structured_content
    assert data["text"] == "Yes [1]." and data["refused"] is False
    assert data["citations"][0]["source_url"] == "https://example.test/dnd.html"
    assert data["applied_filter"] == "filter" and data["principal"] == "analyst_dnd"
    (call,) = seen
    assert call["principal"] == PRINCIPALS[UID_DND] and call["k"] == 5
    resolve = _events(env)[0]
    assert data["request_id"] == resolve["request_id"] == call["request_id"]


@pytest.mark.asyncio
async def test_a_refusal_comes_back_as_a_result_not_an_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "generate_answer", _fake_answer([], refused=True))
    built = _mattermost(monkeypatch)
    async with _client(built, _headers()) as client:
        result = await client.call_tool("answer", {"question": "q"})
    assert not result.is_error
    assert result.structured_content is not None and result.structured_content["refused"] is True
    assert result.structured_content["citations"] == []


@pytest.mark.asyncio
async def test_a_generation_failure_is_a_tool_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(question: str, **kw: object) -> Answer:
        raise GenerationError("model unreachable")

    monkeypatch.setattr(server_mod, "generate_answer", boom)
    built = _mattermost(monkeypatch)
    async with _client(built, _headers()) as client:
        result = await client.call_tool("answer", {"question": "q"})
    assert result.is_error
    assert any("generation failed" in getattr(block, "text", "") for block in result.content)


@pytest.mark.asyncio
async def test_the_secret_and_token_never_reach_a_tool_result_or_the_audit_log(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _mattermost(monkeypatch)
    async with _client(built, _headers(user_id=UID_GONE)) as client:
        result = await client.call_tool("search", {"question": "budget"})
    text = json.dumps([getattr(b, "text", "") for b in result.content]) + env.read_text()
    assert SECRET not in text and "bot-token" not in text
