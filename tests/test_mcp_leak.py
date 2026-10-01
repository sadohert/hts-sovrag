"""The leak suite's questions through the MCP path.

The eval harness proves the search functions never return a forbidden chunk. This runs the
same 20 questions through the MCP server as each named principal, the way the Mattermost
plugin does: a user ID on the connection, the principal resolved per tool call, the `search`
tool. The forbidden set is the eval suite's independent oracle, not the SQL predicate. It
uses the committed fixture, so it needs Postgres and no model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2 as httpx
import psycopg
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from sovrag.auth import Principal, Resolution, load_principals
from sovrag.config import get_settings
from sovrag.eval.fixture import FIXTURE_DIR, load_fixture, load_question_embeddings
from sovrag.eval.golden import GOLDEN_DIR, LeakItem, load_leak
from sovrag.eval.leak import K, forbidden_doc_ids, run_leak_answers
from sovrag.generate.answer import Answer, Citation
from sovrag.mcp import server as server_mod
from sovrag.mcp.server import SovragMcp, build_app

BASE = "http://localhost:8001"
SECRET = "leak-check-secret"
PRINCIPALS = load_principals(Path("data/principals.yaml"))
USER_IDS = {name: f"u{i}".ljust(26, "0") for i, name in enumerate(sorted(PRINCIPALS))}
POSITIVE_CONTROL = "auditor"


class Resolver:
    """Each demo user ID resolves to its named principal, as the Mattermost resolver would."""

    def __init__(self, principals: Mapping[str, Principal] | None = None) -> None:
        self.principals = dict(principals or PRINCIPALS)

    def resolve(self, user_id: str) -> Resolution:
        name = next(n for n, uid in USER_IDS.items() if uid == user_id)
        who = self.principals[name]
        return Resolution(user_id, name, who, {"sovrag_clearance": who.clearance.value}, None)


class FixtureEmbedder:
    """Returns the fixture's embedding for a golden question, so no model is needed."""

    def __init__(self, items: Sequence[LeakItem], vectors: Mapping[str, list[float]]) -> None:
        self.by_question = {item.question: vectors[item.id] for item in items}

    def embed_one(self, text: str) -> list[float]:
        return self.by_question[text]


@pytest.fixture(scope="module")
def fixture_db(test_database_url: str) -> Iterator[tuple[psycopg.Connection, list[LeakItem]]]:
    """The committed index, loaded once for this module; every test here only reads it."""
    from sovrag.index.store import init_schema

    with psycopg.connect(test_database_url, autocommit=True) as conn:
        init_schema(conn)
        conn.execute("truncate chunks, documents restart identity cascade")
        load_fixture(conn, FIXTURE_DIR)
        yield conn, load_leak(GOLDEN_DIR / "leak.jsonl")


@pytest.fixture
def leak_env(
    fixture_db: tuple[psycopg.Connection, list[LeakItem]],
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    tmp_path: Path,
) -> tuple[psycopg.Connection, list[LeakItem]]:
    db, items = fixture_db
    embedder = FixtureEmbedder(items, load_question_embeddings(FIXTURE_DIR))
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MCP_IDENTITY", "mattermost")
    monkeypatch.setenv("MCP_SHARED_SECRET", SECRET)
    monkeypatch.setenv("MATTERMOST_URL", "http://mm.test")
    monkeypatch.setenv("MATTERMOST_BOT_TOKEN", "unused")
    monkeypatch.setattr(server_mod.Embedder, "from_settings", classmethod(lambda cls, s: embedder))
    return db, items


@asynccontextmanager
async def _as_user(built: SovragMcp, name: str) -> AsyncIterator[Client]:
    headers = {"Authorization": f"Bearer {SECRET}", "X-Mattermost-UserID": USER_IDS[name]}
    transport = httpx.ASGITransport(app=built.app)
    async with (
        httpx.AsyncClient(transport=transport, base_url=BASE, headers=headers) as hc,
        Client(streamable_http_client(f"{BASE}/mcp", http_client=hc)) as client,
    ):
        yield client


async def _search_all(
    built: SovragMcp,
    items: Sequence[LeakItem],
    names: Sequence[str],
    *,
    bound_as: Mapping[str, str] | None = None,
) -> dict[tuple[str, str], list[dict[str, object]]]:
    """(principal name, item id) -> hits, through the search tool with the suite's k."""
    hits: dict[tuple[str, str], list[dict[str, object]]] = {}
    async with built.server.session_manager.run():
        for name in names:
            async with _as_user(built, name) as client:
                for item in items:
                    result = await client.call_tool("search", {"question": item.question, "k": K})
                    assert not result.is_error, (name, item.id, result.content)
                    data = result.structured_content
                    assert data is not None
                    assert data["principal"] == (bound_as or {}).get(name, name)
                    hits[(name, item.id)] = list(data["hits"])  # type: ignore[arg-type]
    return hits


@pytest.mark.asyncio
async def test_no_principal_retrieves_a_forbidden_chunk_through_the_mcp_search_tool(
    leak_env: tuple[psycopg.Connection, list[LeakItem]],
) -> None:
    db, items = leak_env
    built = build_app(get_settings(), resolver=Resolver())
    hits = await _search_all(built, items, sorted(PRINCIPALS))

    leaks = [
        (name, item_id, h["doc_id"])
        for (name, item_id), found in hits.items()
        for h in found
        if h["doc_id"] in forbidden_doc_ids(db, PRINCIPALS[name])
    ]
    assert leaks == []
    assert len(hits) == len(items) * len(PRINCIPALS)
    assert all(found for (name, _), found in hits.items() if name == POSITIVE_CONTROL)


@pytest.mark.asyncio
async def test_the_positive_control_still_finds_the_gold_chunks(
    leak_env: tuple[psycopg.Connection, list[LeakItem]],
) -> None:
    """Zero leaks would be trivially true of a server that returned nothing."""
    db, items = leak_env
    built = build_app(get_settings(), resolver=Resolver())
    hits = await _search_all(built, items, [POSITIVE_CONTROL])
    ordinal_of = dict(db.execute("select id, ordinal from chunks").fetchall())
    found = 0
    for item in items:
        gold = {(g.doc_id, g.ordinal) for g in item.gold}
        ranked = {
            (h["doc_id"], ordinal_of[h["chunk_id"]]) for h in hits[(POSITIVE_CONTROL, item.id)]
        }
        found += bool(gold & ranked)
    assert found >= 18, f"positive control found gold for {found} of {len(items)} questions"


@pytest.mark.asyncio
async def test_a_resolver_that_over_grants_is_caught_by_the_oracle(
    leak_env: tuple[psycopg.Connection, list[LeakItem]],
) -> None:
    """The check can fail: bind everyone to the auditor and the oracle reports leaks."""
    db, items = leak_env
    everyone_is_auditor = {name: PRINCIPALS[POSITIVE_CONTROL] for name in PRINCIPALS}
    built = build_app(get_settings(), resolver=Resolver(everyone_is_auditor))
    hits = await _search_all(built, items[:5], ["public"], bound_as={"public": POSITIVE_CONTROL})
    leaked = {
        h["doc_id"]
        for found in hits.values()
        for h in found
        if h["doc_id"] in forbidden_doc_ids(db, PRINCIPALS["public"])
    }
    assert leaked


@pytest.mark.asyncio
async def test_answer_path_runs_the_suite_as_each_principal(
    leak_env: tuple[psycopg.Connection, list[LeakItem]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_leak_answers through the answer tool: the generator is faked, so this checks that
    every question reaches generation as the user who asked. The
    real-model run is local only; see docs/mattermost-demo.md."""
    _, items = leak_env
    seen: list[tuple[str, str]] = []

    def fake(
        question: str, *, principal: Principal, mode: object, k: int, request_id: str | None
    ) -> Answer:
        seen.append((principal.id, question))
        refused = principal.id != POSITIVE_CONTROL
        cites = () if refused else (Citation(1, "d", 1, "t", "h", "https://example.test/d"),)
        return Answer(
            question, principal.id, "hybrid", request_id, "x", cites, refused, "m", (), "f"
        )

    monkeypatch.setattr(server_mod, "generate_answer", fake)
    built = build_app(get_settings(), resolver=Resolver())
    names = ["public", POSITIVE_CONTROL]

    async with built.server.session_manager.run():
        answers: dict[tuple[str, str], Answer] = {}
        for name in names:
            async with _as_user(built, name) as client:
                for item in items[:3]:
                    result = await client.call_tool("answer", {"question": item.question})
                    data = result.structured_content
                    assert data is not None
                    # The tool returns only what is relayed (text, citations, refused,
                    # request id); the principal it ran as is asserted from `seen` below.
                    answers[(name, item.question)] = Answer(
                        question=item.question,
                        principal=name,
                        mode="hybrid",
                        request_id=str(data["request_id"]),
                        text=str(data["text"]),
                        citations=(),
                        refused=bool(data["refused"]),
                        model="m",
                        retrieved_chunk_ids=(),
                        applied_filter="",
                    )

    def through_mcp(question: str, principal: Principal) -> Answer:
        return answers[(principal.id, question)]

    result = run_leak_answers(items[:3], {n: PRINCIPALS[n] for n in names}, through_mcp)
    assert result.metrics == {
        "public.asked": 3,
        "public.refused": 3,
        f"{POSITIVE_CONTROL}.asked": 3,
        f"{POSITIVE_CONTROL}.refused": 0,
    }
    assert sorted(seen) == sorted((n, i.question) for n in names for i in items[:3])
