# MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `sovrag serve-mcp` exposes `search` and `answer` over Streamable HTTP, binding the principal from configuration (static mode) or from the authenticated Mattermost transport (mattermost mode), so the Agents plugin can call sovrag and every answer is filtered by the caller's entitlement.

**Architecture:** `sovrag.mcp.server.build_app(settings, resolver=None)` returns the MCP server and an ASGI app. In mattermost mode a pure-ASGI gate in front of the MCP app answers 401 to any HTTP request without the shared-secret bearer or a well-formed `X-Mattermost-UserID`; the tool handler then resolves the principal once per tool call from that header and writes a `resolve` audit event. In static mode the principal is resolved from the principals file at startup and the gate is absent. Both tools take the question and search options only; the principal is never a tool argument (ADR 0009).

**Tech Stack:** `mcp` 2.2.0 (`MCPServer`, `Context.headers`, `ToolError`, `streamable_http_app`, `TransportSecuritySettings`), Starlette, uvicorn (already a dependency), httpx2 `ASGITransport` and the SDK `Client` for in-process tests, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-17-mattermost-integration-design.md` (section "`sovrag.mcp.server` and `sovrag serve-mcp`", "Data flow", "Edge cases", "Tests", and item 5 under "Sequencing"). ADR 0009 is the decision this implements; no new ADR.

**Verified against the installed SDK before this plan was written:** a synchronous tool receives `ctx.headers` with lowercase keys (`x-mattermost-userid`) and runs in a worker thread; a `dict[str, object]` return becomes `structured_content`; `ToolError` reaches the client as `is_error=True` with the message in `content`; the SDK rejects a `Host` header not on its allow list with 421, and its default list is localhost with any port.

## Global Constraints

- The principal is bound by the server from configuration or from the verified transport. Nothing in a tool argument, message, or model output can name it (CLAUDE.md, ADR 0009).
- Three outcomes, never blurred (`.claude/rules/retrieval.md`): bad or missing bearer or user ID is HTTP 401 before anything runs; a resolution failure is a tool error and retrieves nothing; only a known user with nothing recognised is least privilege.
- Entitlement filtering stays in SQL: the tools call `search_hybrid` and `answer()` with the bound principal and never post-filter.
- Every hit returned carries `doc_id`, `chunk_id`, `heading_path`, `source_url` and `score`.
- The shared secret and the bot token never appear in logs, errors or tool results.
- Pin at the newest release checked at the time of the change: `mcp` 2.2.0 is the newest on PyPI (published 2026-09-07) and is already in `uv.lock` as the optional extra.
- Type hints everywhere; `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` and `uv run pytest -q` pass before each commit. Run `uv run ruff format docs` before committing this plan.
- Tests first. Run the test, watch it fail for the expected reason, then implement.
- Line length 100. No schedule, session or notes-folder references anywhere in the repo.
- End each commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Write import blocks by hand in sorted order; a local `ruff --fix` once sorted them differently from CI.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml`, `uv.lock` (modify) | `mcp>=2.2` becomes a core dependency; the `mcp` extra goes |
| `src/sovrag/config.py` (modify) | `mcp_identity`, `mcp_shared_secret`, `mcp_allowed_hosts` |
| `src/sovrag/mcp/__init__.py` (create) | Package docstring |
| `src/sovrag/mcp/server.py` (create) | `Identity`, `Resolver` protocol, gate, binder, tools, `build_app` |
| `src/sovrag/cli.py` (modify) | `serve-mcp` command |
| `compose.yaml` (modify) | The `mcp` service builds the single Dockerfile and requires `MCP_IDENTITY` |
| `.env.example` (modify) | The three MCP settings |
| `tests/test_mcp_server.py` (create) | Build-time refusals, 401 cases, static mode, per-principal document sets, resolve-once, resolver failure, answer, allowed hosts |
| `tests/test_cli.py` (modify) | `serve-mcp` refuses without an identity and hands the app to uvicorn |
| `tests/test_compose.py` (create) | The `mcp` service shape |
| `README.md`, `CLAUDE.md`, `docs/architecture.md` (modify) | Status and description |

## Branch Setup

Already done: worktree `.claude/worktrees/mcp-server` on branch `mcp-server` from `origin/main` at b57e2ef. Before Task 1:

```bash
uv sync --all-extras
docker start hts-sovrag-postgres-1 2>/dev/null || docker compose up -d postgres
uv run pytest -q
```

Expected: the suite passes with no skips.

---

### Task 1: `mcp` is a core dependency, and the three settings exist

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (regenerated), `src/sovrag/config.py`, `.env.example`

**Interfaces:**
- Produces: `Settings.mcp_identity: str = ""` (env `MCP_IDENTITY`), `Settings.mcp_shared_secret: str = ""` (`MCP_SHARED_SECRET`), `Settings.mcp_allowed_hosts: str = ""` (`MCP_ALLOWED_HOSTS`, comma-separated `Host` values, empty means the SDK's localhost default).

- [ ] **Step 1: Move the dependency**

In `pyproject.toml`, add `"mcp>=2.2",` to `dependencies` after `"uvicorn[standard]>=0.53",` and delete the line `mcp = ["mcp>=2.2"]` from `[project.optional-dependencies]`. The Compose image runs `uv sync --frozen --no-dev`, which installs no extras, so the server's dependency has to be a core one.

Run: `uv lock && uv sync --all-extras && uv run python -c "import mcp, importlib.metadata as m; print(m.version('mcp'))"`
Expected: `2.2.0`.

- [ ] **Step 2: Add the settings**

In `src/sovrag/config.py`, replace the block

```python
    # MCP server only. Bound at startup; never read from tool arguments.
    principal: str = ""
```

with

```python
    # MCP server. MCP_IDENTITY is "static" or "mattermost"; required, no default (ADR 0009).
    # In static mode PRINCIPAL is resolved from PRINCIPALS_PATH at startup. In mattermost mode
    # every request must carry the shared secret as a bearer. Neither value is ever read from a
    # tool argument. MCP_ALLOWED_HOSTS lists the Host header values the server accepts, comma
    # separated, "mcp:*" for the Compose service name; empty keeps the SDK's localhost default.
    mcp_identity: str = ""
    principal: str = ""
    mcp_shared_secret: str = ""
    mcp_allowed_hosts: str = ""
```

In `.env.example`, replace the block

```
# MCP server only (not implemented yet). The server binds the principal, never the agent:
# from this value at startup in static mode, or per tool call from an authenticated
# transport in mattermost mode. See ADR 0009.
PRINCIPAL=analyst_dnd
```

with

```
# MCP server (sovrag serve-mcp). MCP_IDENTITY is required: "static" binds PRINCIPAL at startup;
# "mattermost" binds each tool call to the Mattermost user the Agents plugin names, after the
# plugin proves itself with MCP_SHARED_SECRET as a bearer. The agent can never choose the
# principal (ADR 0009). MCP_ALLOWED_HOSTS lists accepted Host header values, comma separated.
MCP_IDENTITY=static
PRINCIPAL=analyst_dnd
MCP_SHARED_SECRET=
MCP_ALLOWED_HOSTS=
```

and change the comment on the Mattermost block from `(not implemented yet)` to nothing.

- [ ] **Step 3: Commit**

```bash
uv run ruff check . && uv run mypy src && uv run pytest -q
git add pyproject.toml uv.lock src/sovrag/config.py .env.example
git commit -m "Make mcp a core dependency and add the MCP server settings"
```

---

### Task 2: The server: gate, binder, tools, `build_app`

**Files:**
- Create: `src/sovrag/mcp/__init__.py`, `src/sovrag/mcp/server.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `Settings`; `resolve_principal`, `Principal`, `Resolution`, `ResolutionError`, `MattermostResolver` from `sovrag.auth`; `search_hybrid`, `entitlement_for`, `Mode` from `sovrag.retrieve`; `answer` and `GenerationError`, `MAX_K` from `sovrag.generate`; `Embedder`, `connect`; `write_event`.
- Produces, in `sovrag.mcp.server`:
  - `class Identity(StrEnum)`: `static`, `mattermost`.
  - `class Resolver(Protocol)`: `resolve(self, user_id: str) -> Resolution`.
  - `USER_ID_HEADER = "x-mattermost-userid"`, `ENTITLEMENT_ERROR = "entitlement could not be determined"`.
  - `@dataclass(frozen=True) class SovragMcp`: `server: MCPServer`, `app: ASGIApp`, `identity: Identity`.
  - `build_app(settings: Settings, *, resolver: Resolver | None = None) -> SovragMcp`; raises `ValueError` for a missing or unknown identity, a static `PRINCIPAL` that is empty or not in the file, or a mattermost mode without a shared secret.
  - Tools `search(question: str, k: int = 5)` and `answer(question: str)`, both returning `dict[str, object]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_server.py`:

```python
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
    built = _mattermost(monkeypatch)
    assert (await _raw_initialize(built, _headers(), base="http://mcp:8001")).status_code == 200
    assert (await _raw_initialize(built, _headers(), base=BASE)).status_code == 421


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
        assert {"doc_id", "chunk_id", "heading_path", "source_url", "title", "score"} <= set(hit)
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_mcp_server.py -q`
Expected: `ModuleNotFoundError: No module named 'sovrag.mcp'`.

- [ ] **Step 3: Implement the server**

Create `src/sovrag/mcp/__init__.py`:

```python
"""The MCP server the Mattermost Agents plugin calls. See server.py and ADR 0009."""
```

Create `src/sovrag/mcp/server.py`:

```python
"""MCP server: search and answer over Streamable HTTP, with the principal bound by the server.

Two identity modes, chosen by MCP_IDENTITY with no default and no fallback (ADR 0009):

- static: PRINCIPAL is resolved from the principals file when the app is built and every tool
  call runs as that principal.
- mattermost: an ASGI gate in front of the MCP app answers 401 to any request without the
  shared-secret bearer or a well-formed X-Mattermost-UserID. The tool handler then resolves
  that user's principal once per tool call (not per HTTP request, since one call is several)
  and writes a `resolve` audit event. A resolution failure is a tool error that retrieves
  nothing; it is never a downgrade.

Neither tool has a principal parameter. Entitlement filtering happens in SQL inside the search
functions and answer(); this module only binds and reports.
"""

from __future__ import annotations

import hmac
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

from sovrag.audit import write_event
from sovrag.auth import (
    MattermostResolver,
    Principal,
    Resolution,
    ResolutionError,
    resolve_principal,
)
from sovrag.config import Settings
from sovrag.generate import MAX_K, GenerationError
from sovrag.generate.answer import answer as generate_answer
from sovrag.index.embed import Embedder
from sovrag.index.store import connect
from sovrag.retrieve import Mode, entitlement_for, search_hybrid

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

USER_ID_HEADER = "x-mattermost-userid"
ENTITLEMENT_ERROR = "entitlement could not be determined"
_USER_ID = re.compile(r"^[a-z0-9]{26}$")
_NO_PRINCIPAL_ARG = (
    " The principal is bound by the server from the authenticated connection; there is no way"
    " to name or change it here."
)


class Identity(StrEnum):
    static = "static"
    mattermost = "mattermost"


class Resolver(Protocol):
    def resolve(self, user_id: str) -> Resolution: ...


@dataclass(frozen=True)
class SovragMcp:
    server: MCPServer
    app: ASGIApp
    identity: Identity


@dataclass(frozen=True)
class Binding:
    principal: Principal
    request_id: str


class _Gate:
    """Pure ASGI: every HTTP request must carry the bearer and a well-formed user id.

    Lifespan and other non-HTTP scopes pass straight through. The comparison is constant time
    so the secret cannot be guessed byte by byte from response timing."""

    def __init__(self, app: ASGIApp, secret: str) -> None:
        self._app = app
        self._secret = secret.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self._authorised(scope):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _authorised(self, scope: Scope) -> bool:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or not hmac.compare_digest(token.encode(), self._secret):
            return False
        return _USER_ID.match(headers.get(USER_ID_HEADER, "")) is not None


class _Binder:
    """Turns a tool call's context into the principal it runs as, once per call."""

    def __init__(
        self,
        identity: Identity,
        *,
        principal: Principal | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._identity = identity
        self._principal = principal
        self._resolver = resolver

    def bind(self, ctx: Context) -> Binding:
        request_id = uuid.uuid4().hex
        if self._identity is Identity.static:
            assert self._principal is not None
            return Binding(self._principal, request_id)
        assert self._resolver is not None
        user_id = (ctx.headers or {}).get(USER_ID_HEADER, "")
        if not _USER_ID.match(user_id):
            # The gate refuses these before the SDK runs; this only guards a misconfigured mount.
            raise ToolError(ENTITLEMENT_ERROR)
        try:
            resolution = self._resolver.resolve(user_id)
        except ResolutionError as exc:
            write_event("resolve", request_id=request_id, user_id=user_id, error=str(exc))
            raise ToolError(ENTITLEMENT_ERROR) from None
        write_event(
            "resolve",
            request_id=request_id,
            user_id=user_id,
            username=resolution.username,
            principal=resolution.principal.id,
            attributes=dict(resolution.attributes),
            note=resolution.note,
        )
        return Binding(resolution.principal, request_id)


def _identity(settings: Settings) -> Identity:
    try:
        return Identity(settings.mcp_identity)
    except ValueError:
        raise ValueError(
            f"MCP_IDENTITY must be 'static' or 'mattermost', got {settings.mcp_identity!r}"
        ) from None


def _binder(settings: Settings, identity: Identity, resolver: Resolver | None) -> _Binder:
    if identity is Identity.static:
        if not settings.principal:
            raise ValueError("PRINCIPAL is required in static mode")
        path = Path(settings.principals_path)
        try:
            principal = resolve_principal(settings.principal, path)
        except KeyError:
            raise ValueError(f"PRINCIPAL {settings.principal!r} is not in {path}") from None
        return _Binder(identity, principal=principal)
    if not settings.mcp_shared_secret:
        raise ValueError("MCP_SHARED_SECRET is required in mattermost mode")
    return _Binder(identity, resolver=resolver or MattermostResolver.from_settings(settings))


def _security(settings: Settings) -> TransportSecuritySettings | None:
    hosts = [h.strip() for h in settings.mcp_allowed_hosts.split(",") if h.strip()]
    if not hosts:
        return None
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[])


def build_app(settings: Settings, *, resolver: Resolver | None = None) -> SovragMcp:
    """Build the server and its ASGI app. Raises ValueError for any configuration that could
    bind the wrong principal, so the process refuses to start rather than run degraded."""
    identity = _identity(settings)
    binder = _binder(settings, identity, resolver)
    mcp = MCPServer(
        "sovrag",
        instructions=(
            "Permissions-aware retrieval over Canadian federal public documents. Every result"
            " is already filtered to what the calling user may see; relay answers and their"
            " citations verbatim." + _NO_PRINCIPAL_ARG
        ),
    )

    @mcp.tool(description="Retrieve the passages most relevant to a question." + _NO_PRINCIPAL_ARG)
    def search(question: str, ctx: Context, k: int = 5) -> dict[str, object]:
        binding = binder.bind(ctx)
        k = max(1, min(k, MAX_K))
        applied_filter = entitlement_for(binding.principal).render()
        query_vec = Embedder.from_settings(settings).embed_one(question)
        with connect(settings.database_url) as conn:
            hits = search_hybrid(conn, question, query_vec, k=k, principal=binding.principal)
        write_event(
            "search",
            request_id=binding.request_id,
            principal=binding.principal.id,
            k=k,
            applied_filter=applied_filter,
            chunk_ids=[h.chunk_id for h in hits],
        )
        return {
            "request_id": binding.request_id,
            "principal": binding.principal.id,
            "applied_filter": applied_filter,
            "hits": [
                {
                    "doc_id": h.doc_id,
                    "chunk_id": h.chunk_id,
                    "heading_path": h.heading_path,
                    "source_url": h.source_url,
                    "title": h.title,
                    "score": h.score,
                }
                for h in hits
            ],
        }

    @mcp.tool(
        description=(
            "Answer a question from the documents the calling user may see, with [n] citations,"
            " or refuse when they do not support an answer." + _NO_PRINCIPAL_ARG
        )
    )
    def answer(question: str, ctx: Context) -> dict[str, object]:
        binding = binder.bind(ctx)
        try:
            result = generate_answer(
                question,
                principal=binding.principal,
                mode=Mode.hybrid,
                k=5,
                request_id=binding.request_id,
            )
        except GenerationError as exc:
            raise ToolError(f"generation failed: {exc}") from None
        return asdict(result)

    mcp_app = mcp.streamable_http_app(json_response=True, transport_security=_security(settings))

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    inner = Starlette(routes=[Mount("/", app=mcp_app)], lifespan=lifespan)
    app: ASGIApp = inner
    if identity is Identity.mattermost:
        app = _Gate(inner, settings.mcp_shared_secret)
    return SovragMcp(server=mcp, app=app, identity=identity)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_mcp_server.py -q && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: all pass. Points to check if something fails:
- 421 instead of 200 in `test_the_gate_lets_a_good_request_through`: the base URL in the test must carry a port (`localhost:8001`), because the SDK's default allow list is `localhost:*`.
- `structured_content is None`: the tool's return annotation must be `dict[str, object]`, not bare `dict`.
- mypy on `search`/`answer` unused warnings: the decorated functions are registered by the decorator; if mypy complains they are unused, that is a ruff rule, not mypy, and the decorator use is the reference.
- pytest-asyncio warns about the event loop scope: add `asyncio_mode = "strict"` under `[tool.pytest.ini_options]` only if the marker form used here is rejected.

- [ ] **Step 5: Commit**

```bash
git add src/sovrag/mcp tests/test_mcp_server.py
git commit -m "Serve search and answer over MCP with the principal bound by the server"
```

---

### Task 3: `sovrag serve-mcp`

**Files:**
- Modify: `src/sovrag/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `sovrag serve-mcp [--host 127.0.0.1] [--port 8001]`; exit code 2 with the `ValueError` text on a configuration `build_app` refuses.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_serve_mcp_refuses_to_start_without_an_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_IDENTITY", raising=False)
    result = runner.invoke(cli.app, ["serve-mcp"])
    assert result.exit_code == 2
    assert "MCP_IDENTITY" in result.output


def test_serve_mcp_hands_the_built_app_to_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_IDENTITY", "static")
    monkeypatch.setenv("PRINCIPAL", "analyst_dnd")
    seen: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        cli.uvicorn, "run", lambda app, host, port, **kw: seen.append((app, host, port))
    )
    result = runner.invoke(cli.app, ["serve-mcp", "--host", "0.0.0.0", "--port", "9001"])
    assert result.exit_code == 0, result.output
    (app, host, port) = seen[0]
    assert callable(app) and host == "0.0.0.0" and port == 9001
    assert "static" in result.output and "analyst_dnd" in result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q -k serve_mcp`
Expected: `No such command 'serve-mcp'` (exit code 2, but without `MCP_IDENTITY` in the output) and `AttributeError: module 'sovrag.cli' has no attribute 'uvicorn'`.

- [ ] **Step 3: Implement**

In `src/sovrag/cli.py`, add `import uvicorn` after `import typer`, and `from sovrag.mcp.server import build_app` after the `sovrag.index.store` import. Replace the module docstring's first line with `"""sovrag CLI: ingest | query | ask | serve-mcp | serve."""`. After the `serve` command add:

```python
@app.command("serve-mcp")
def serve_mcp(host: str = "127.0.0.1", port: int = 8001) -> None:
    """Run the MCP server (Streamable HTTP at /mcp). MCP_IDENTITY chooses how the principal is
    bound; the server refuses to start with a configuration that could bind the wrong one."""
    try:
        built = build_app(get_settings())
    except ValueError as exc:
        typer.echo(f"serve-mcp: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    who = get_settings().principal if built.identity.value == "static" else "per tool call"
    typer.echo(
        f"serve-mcp: identity={built.identity.value} principal={who} at http://{host}:{port}/mcp"
    )
    uvicorn.run(built.app, host=host, port=port, log_level="info")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q && uv run mypy src && uv run ruff check .`
Expected: all pass. If mypy rejects `uvicorn.run(built.app, ...)` because `ASGIApp` is a `Callable` alias and uvicorn wants its own type, pass `built.app` through `typing.cast` to `Any` at that one call; the app is a valid ASGI callable.

- [ ] **Step 5: Commit**

```bash
git add src/sovrag/cli.py tests/test_cli.py
git commit -m "Add sovrag serve-mcp"
```

---

### Task 4: The Compose `mcp` service

**Files:**
- Modify: `compose.yaml`
- Test: `tests/test_compose.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_compose.py`:

```python
"""compose.yaml: the mcp service is the single Dockerfile with an identity it cannot guess."""

from pathlib import Path

import yaml

COMPOSE = yaml.safe_load((Path(__file__).parents[1] / "compose.yaml").read_text())


def test_mcp_service_builds_the_single_dockerfile_and_runs_serve_mcp() -> None:
    mcp = COMPOSE["services"]["mcp"]
    assert mcp["profiles"] == ["mcp"]
    assert mcp["build"] == {"context": "."}
    assert mcp["command"] == ["sovrag", "serve-mcp", "--host", "0.0.0.0", "--port", "8001"]
    assert not (Path(__file__).parents[1] / "Dockerfile.mcp").exists()


def test_mcp_identity_is_required_and_principal_has_no_default() -> None:
    env = COMPOSE["services"]["mcp"]["environment"]
    assert env["MCP_IDENTITY"].startswith("${MCP_IDENTITY:?")
    assert env["PRINCIPAL"] == "${PRINCIPAL:-}"
    assert env["MCP_SHARED_SECRET"] == "${MCP_SHARED_SECRET:-}"
    assert env["MCP_ALLOWED_HOSTS"] == "${MCP_ALLOWED_HOSTS:-mcp:*,localhost:*}"
    assert env["AUDIT_LOG_PATH"] == "/app/audit/audit.jsonl"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_compose.py -q`
Expected: `KeyError: 'command'` or an assertion on `build`.

- [ ] **Step 3: Replace the service**

In `compose.yaml`, replace the comment block and the `mcp:` service (from `# Not implemented yet. This is the service's earlier shape` through the end of its `models:` block) with:

```yaml
  # The MCP server the Mattermost Agents plugin calls (ADR 0009). Same image as app, different
  # command. MCP_IDENTITY has no default: "static" binds PRINCIPAL at startup; "mattermost"
  # binds each tool call to the user the plugin names, after the plugin proves itself with
  # MCP_SHARED_SECRET. MCP_ALLOWED_HOSTS must include the name the plugin dials.
  mcp:
    profiles: [mcp]
    build:
      context: .
    command: ["sovrag", "serve-mcp", "--host", "0.0.0.0", "--port", "8001"]
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql://sovrag:sovrag@postgres:5432/sovrag
      AUDIT_LOG_PATH: /app/audit/audit.jsonl
      MCP_IDENTITY: ${MCP_IDENTITY:?set MCP_IDENTITY to static or mattermost}
      PRINCIPAL: ${PRINCIPAL:-}
      MCP_SHARED_SECRET: ${MCP_SHARED_SECRET:-}
      MCP_ALLOWED_HOSTS: ${MCP_ALLOWED_HOSTS:-mcp:*,localhost:*}
      MATTERMOST_URL: ${MATTERMOST_URL:-}
      MATTERMOST_BOT_TOKEN: ${MATTERMOST_BOT_TOKEN:-}
    ports:
      - "8001:8001"
    volumes:
      - audit:/app/audit
    models:
      llm:
        endpoint_var: LLM_BASE_URL
        model_var: LLM_MODEL
      embed:
        endpoint_var: EMBED_BASE_URL
        model_var: EMBED_MODEL
```

Keep the existing `audit` volume and `models` top-level blocks. Update the file's line-2 comment if it still says the mcp profile is not implemented.

- [ ] **Step 4: Verify the config renders and the test passes**

Run: `MCP_IDENTITY=static docker compose --profile mcp config --quiet && uv run pytest tests/test_compose.py -q`
Expected: no output from compose, test passes. Also `docker compose --profile mcp config --quiet` with `MCP_IDENTITY` unset must fail with `set MCP_IDENTITY to static or mattermost`.

- [ ] **Step 5: Smoke the container in static mode**

```bash
MCP_IDENTITY=static PRINCIPAL=analyst_dnd docker compose --profile mcp up -d --build mcp
sleep 5
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8001/mcp \
  -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"c","version":"0"}}}'
docker compose --profile mcp logs mcp | tail -3
docker compose --profile mcp down mcp
```

Expected: `200`, and the log shows `serve-mcp: identity=static principal=analyst_dnd`. Record the result in the pull request body.

- [ ] **Step 6: Commit**

```bash
git add compose.yaml tests/test_compose.py
git commit -m "Give the mcp Compose service its command, its settings and a required identity"
```

---

### Task 5: Documentation

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `docs/architecture.md`

- [ ] **Step 1: README**

Change the `Status:` line to:

```markdown
Status: corpus indexed; vector, keyword and hybrid retrieval, each filtered by the principal's entitlement in SQL; grounded answers with citations and refusal from the CLI and over MCP.
```

After the quickstart block's closing fence and its paragraph, add:

```markdown
To expose the same answers to an agent, run the MCP server. The principal is bound by the server, never by the agent (ADR 0009):

```
uv run sovrag serve-mcp                          # MCP_IDENTITY=static PRINCIPAL=analyst_dnd from .env
MCP_IDENTITY=static PRINCIPAL=auditor docker compose --profile mcp up -d mcp
```

The Mattermost mode, where each direct message runs as the Mattermost user asking, is configured in the Mattermost walkthrough that ships with the demo profile.
```

- [ ] **Step 2: CLAUDE.md**

In Commands, add after the `sovrag ask` line:

```markdown
- uv run sovrag serve-mcp   # MCP_IDENTITY=static|mattermost; see .env.example
```

and change the last line to `- Not implemented yet: sovrag serve, sovrag eval, the mattermost profile.`

- [ ] **Step 3: docs/architecture.md**

Change the `Status:` line under Components to:

```markdown
Status: the corpus pipeline, all three retrieval modes with entitlement filtering in SQL, grounded generation with citations and refusal, and the MCP server exist. The HTTP API and the Mattermost Compose profile follow.
```

Add a section after "## Generation":

```markdown
## MCP server

`mcp/server.py` serves `search` and `answer` over Streamable HTTP for the Mattermost Agents plugin. Neither tool takes a principal. `MCP_IDENTITY` picks how it is bound, with no default: in static mode `PRINCIPAL` is resolved from the principals file when the server starts; in mattermost mode a gate in front of the MCP app answers 401 to any request without `MCP_SHARED_SECRET` as a bearer or a well-formed `X-Mattermost-UserID`, and the tool handler resolves that user's attributes once per tool call, writing a `resolve` audit event that shares a `request_id` with the `search` or `answer` event that follows. A resolution failure is a tool error and retrieves nothing. `search` runs hybrid retrieval with the bound principal and returns hits with their citation fields and the rendered filter; `answer` calls the same `answer()` the CLI uses and returns its result unchanged. See ADR 0009 and ADR 0010.
```

- [ ] **Step 4: Full check and commit**

```bash
uv run ruff format docs
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
git add README.md CLAUDE.md docs/architecture.md docs/superpowers/plans/2026-09-22-mcp-server.md
git commit -m "Describe the MCP server"
```

---

## Pull request

Push `mcp-server` and open a pull request against `main` titled "Serve search and answer over MCP with the principal bound by the server". The body covers the two identity modes, the gate, resolve-once-per-call, the allowed-hosts finding, the dependency move, the Compose service, and the container smoke result. Wait for `ci` and `claude-review` on the head commit; squash-merge with `gh pr merge --squash --delete-branch`.

## Self-review

- Spec coverage: settings table (Task 1; `MCP_ALLOWED_HOSTS` added because the SDK enforces a Host allow list); middleware steps 1 and 2 (Task 2, `_Gate`); handler step 3 with the `resolve` event and the tool error (Task 2, `_Binder`); static mode replaces 1 to 3 (Task 2); both tools without a principal parameter, descriptions say so (Task 2); `search` writes a `search` event with chunk ids (Task 2); `answer` calls the contract and returns the `Answer` unchanged (Task 2; the "generation absent" clause is moot since generation is on main); `serve-mcp` with host and port (Task 3); the `mcp` Compose service on the single Dockerfile (Task 4); spec tests: 401 cases, static mode, each demo principal's document set, resolver failure, resolve exactly once, answer JSON, shared `request_id`, refusal to start (Task 2 and Task 3).
- Names used across tasks: `build_app`, `SovragMcp`, `Identity`, `ENTITLEMENT_ERROR`, `USER_ID_HEADER`, `generate_answer`, `mcp_identity`, `mcp_shared_secret`, `mcp_allowed_hosts` are spelled the same everywhere.
