# Grounded Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `sovrag ask` answers from retrieved chunks only, cites every claim with `[n]` markers tied to chunks, refuses when the context lacks the answer, and writes one audit line per call.

**Architecture:** Four small modules under `src/sovrag/generate/` (client, prompts, citation parsing, orchestration) plus a shared audit writer, all pure or fake-able so the tests need no model. `Mode` moves into the retrieval package with a `search` dispatcher used by both the CLI and the answer path. The `Principal` type comes from `sovrag.auth`.

**Tech Stack:** Python 3.14, openai SDK 3.x (chat completions, `extra_body` for the thinking flag), httpx2 MockTransport in tests, typer, pytest, PyYAML.

**Spec:** `docs/superpowers/specs/2026-09-17-grounded-generation-design.md`

## Global Constraints

- The contract in the spec is fixed: `Citation`, `Answer` fields and order, `answer(question, *, principal: Principal, mode: Mode = Mode.hybrid, k: int = 5, request_id: str | None = None)`.
- `SENTINEL = "INSUFFICIENT_CONTEXT"`; `REFUSAL_TEXT = "The retrieved documents do not contain enough information to answer this question."`; `MAX_K = 10`.
- Every `[n]` in returned text has a `Citation`; `refused` implies no citations and `REFUSAL_TEXT`.
- Audit `reason` is one of `None`, `"no_hits"`, `"sentinel"`, `"uncited"`.
- Generation: temperature 0; `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`; only `GenerationError` escapes the client.
- Type hints everywhere; `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run pytest -q` pass before each commit.
- Tests first: watch each new test fail for the expected reason, then implement.
- Commit messages: content-only subject, blank line, trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. No schedule, session or plan references in repo text or commits.
- Database tests use the `db` / `test_database_url` fixtures in `tests/conftest.py` (local Postgres). Audit tests point `AUDIT_LOG_PATH` at a temp file via `monkeypatch.setenv`.

---

### Task 1: Move `Mode` into the retrieval package and add a `search` dispatcher

**Files:**
- Modify: `src/sovrag/retrieve/hybrid.py` (add `Mode`, `search`)
- Modify: `src/sovrag/retrieve/__init__.py` (re-exports)
- Modify: `src/sovrag/cli.py` (remove the local `Mode`, import from retrieve, use `search`)
- Test: `tests/test_hybrid_search.py` (append), `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `search_vector`, `search_keyword`, `search_hybrid` as they exist.
- Produces: `Mode(StrEnum)` with `vector`, `keyword`, `hybrid`; `search(conn, question, *, mode: Mode, k: int, embed_query: Callable[[str], Sequence[float]]) -> list[Hit]` which calls `embed_query` only for vector and hybrid; `sovrag.retrieve` exports `Hit, Mode, search, search_hybrid, search_keyword, search_vector`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hybrid_search.py`:

```python
def test_search_dispatches_by_mode_and_embeds_only_when_needed(db: psycopg.Connection) -> None:
    from sovrag.retrieve import Mode, search

    _seed(db)
    calls: list[str] = []

    def embed_query(q: str) -> list[float]:
        calls.append(q)
        return _mix(1.0, 0.5, 0.25)

    kw = search(db, "cloud brokering", mode=Mode.keyword, k=3, embed_query=embed_query)
    assert [h.chunk_id for h in kw] == [1]
    assert calls == []

    vec = search(db, "cloud brokering", mode=Mode.vector, k=3, embed_query=embed_query)
    assert [h.chunk_id for h in vec] == [1, 2, 3]
    assert calls == ["cloud brokering"]

    hyb = search(db, "cloud brokering", mode=Mode.hybrid, k=3, embed_query=embed_query)
    assert hyb[0].chunk_id == 1
    assert calls == ["cloud brokering", "cloud brokering"]
```

Append to `tests/test_cli.py`:

```python
def test_mode_is_the_retrieve_package_enum() -> None:
    from sovrag.retrieve import Mode

    assert cli.Mode is Mode
    assert Mode("keyword") is Mode.keyword
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_hybrid_search.py::test_search_dispatches_by_mode_and_embeds_only_when_needed tests/test_cli.py::test_mode_is_the_retrieve_package_enum -q`
Expected: both FAIL with `ImportError: cannot import name 'Mode' from 'sovrag.retrieve'`.

- [ ] **Step 3: Implement**

In `src/sovrag/retrieve/hybrid.py`, add to the imports:

```python
from collections.abc import Callable, Sequence
from enum import StrEnum
```

(`Sequence` is already imported; extend that line.) After the `Hit` dataclass add:

```python
class Mode(StrEnum):
    vector = "vector"
    keyword = "keyword"
    hybrid = "hybrid"
```

At the end of the module add:

```python
def search(
    conn: psycopg.Connection,
    question: str,
    *,
    mode: Mode,
    k: int,
    embed_query: Callable[[str], Sequence[float]],
) -> list[Hit]:
    """Dispatch on mode. embed_query is called only when a vector is needed."""
    if mode is Mode.keyword:
        return search_keyword(conn, question, k=k)
    query_vec = embed_query(question)
    if mode is Mode.vector:
        return search_vector(conn, query_vec, k=k)
    return search_hybrid(conn, question, query_vec, k=k)
```

Replace the contents of `src/sovrag/retrieve/__init__.py` with:

```python
"""Retrieval: vector, keyword and hybrid search over the pgvector store, plus the Mode enum."""

from sovrag.retrieve.hybrid import Hit, Mode, search, search_hybrid, search_keyword, search_vector

__all__ = ["Hit", "Mode", "search", "search_hybrid", "search_keyword", "search_vector"]
```

In `src/sovrag/cli.py`: delete `from enum import StrEnum` and the local `class Mode(StrEnum)` block; change the retrieve import to `from sovrag.retrieve import Mode, search`; replace the body of `query` from `settings = get_settings()` through the end of the `with` block with:

```python
    settings = get_settings()
    with connect(settings.database_url) as conn:
        hits = search(
            conn,
            question,
            mode=mode,
            k=k,
            embed_query=lambda q: Embedder.from_settings(settings).embed([q])[0],
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_hybrid_search.py tests/test_cli.py -q`
Expected: all pass, including the existing `test_keyword_mode_never_builds_the_embedder`.

- [ ] **Step 5: Gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
git add src/sovrag/retrieve/hybrid.py src/sovrag/retrieve/__init__.py src/sovrag/cli.py tests/test_hybrid_search.py tests/test_cli.py
git commit -m "Move Mode into the retrieval package and add a search dispatcher"
```

---

### Task 2: Principals (only if `sovrag.auth` is still empty on this branch)

Check first: `ls src/sovrag/auth/`. If it already contains `principal.py` (delivered by the `principal-model` branch, PR 8), skip this task entirely and report SKIPPED. That package exports `Clearance` (with a `.rank` property), `Principal`, `LEAST_PRIVILEGED`, `load_principals`, `resolve_principal`, `CLASSIFICATION_RANK`, `DEPARTMENTS`, `GC_WIDE`, `ANONYMOUS_ID` and `DEFAULT_PRINCIPALS_PATH`; later tasks only import `Clearance`, `Principal` and `resolve_principal`, which exist under both this task and PR 8.

**Files:**
- Create: `src/sovrag/auth/principals.py`, `data/principals.yaml`
- Modify: `src/sovrag/auth/__init__.py`, `src/sovrag/corpus/manifest.py:21` (import the rank table)
- Test: `tests/test_principals.py`

**Interfaces:**
- Produces: `Clearance(StrEnum)`; `CLASSIFICATION_RANK: dict[str, int]`; `rank(clearance) -> int`; `Principal(id, clearance, departments: frozenset[str])`; `LEAST_PRIVILEGED`; `load_principals(path) -> dict[str, Principal]`; `resolve_principal(principal_id, path) -> Principal` raising `KeyError`.

- [ ] **Step 1: Write the failing tests**

```python
"""Principals: who is asking and what they may see. Loaded from data/principals.yaml."""

from pathlib import Path

import pytest

from sovrag.auth import (
    CLASSIFICATION_RANK,
    LEAST_PRIVILEGED,
    Clearance,
    Principal,
    load_principals,
    rank,
    resolve_principal,
)

REPO_FILE = Path("data/principals.yaml")


def test_clearances_are_ordered() -> None:
    assert rank(Clearance.UNCLASSIFIED) < rank(Clearance.PROTECTED_A) < rank(Clearance.PROTECTED_B)
    assert CLASSIFICATION_RANK == {"UNCLASSIFIED": 0, "PROTECTED_A": 1, "PROTECTED_B": 2}


def test_repo_file_defines_the_five_principals() -> None:
    principals = load_principals(REPO_FILE)
    assert set(principals) == {"public", "analyst_dnd", "analyst_ps", "cio_ssc", "auditor"}
    assert principals["analyst_dnd"] == Principal(
        id="analyst_dnd", clearance=Clearance.PROTECTED_A, departments=frozenset({"DND"})
    )
    assert principals["public"].departments == frozenset()
    assert principals["auditor"].clearance is Clearance.PROTECTED_B


def test_resolve_unknown_id_raises_key_error() -> None:
    with pytest.raises(KeyError, match="unknown principal"):
        resolve_principal("nobody", REPO_FILE)


def test_least_privileged_sees_nothing_protected() -> None:
    assert LEAST_PRIVILEGED.clearance is Clearance.UNCLASSIFIED
    assert LEAST_PRIVILEGED.departments == frozenset()


def test_loader_reads_departments_as_a_frozenset(tmp_path: Path) -> None:
    f = tmp_path / "p.yaml"
    f.write_text(
        "principals:\n  - id: x\n    clearance: PROTECTED_A\n    departments: [SSC, TBS]\n"
    )
    assert load_principals(f)["x"].departments == frozenset({"SSC", "TBS"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_principals.py -q`
Expected: FAIL at import, `ImportError: cannot import name 'CLASSIFICATION_RANK' from 'sovrag.auth'`.

- [ ] **Step 3: Implement**

`src/sovrag/auth/principals.py`:

```python
"""Principals: who is asking, and what they may see.

A principal has an id, a clearance and a set of departments. On the CLI the id names an
entry in data/principals.yaml. Other transports build a Principal from attributes they
verify themselves; the id is then a value, not a name. The entitlement predicate is built
from clearance and departments in retrieve/filters.py (SQL only, never a Python post-filter).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml


class Clearance(StrEnum):
    UNCLASSIFIED = "UNCLASSIFIED"
    PROTECTED_A = "PROTECTED_A"
    PROTECTED_B = "PROTECTED_B"


CLASSIFICATION_RANK: dict[str, int] = {c.value: i for i, c in enumerate(Clearance)}


def rank(clearance: Clearance | str) -> int:
    return CLASSIFICATION_RANK[str(clearance)]


@dataclass(frozen=True)
class Principal:
    id: str
    clearance: Clearance
    departments: frozenset[str]


LEAST_PRIVILEGED = Principal(
    id="anonymous", clearance=Clearance.UNCLASSIFIED, departments=frozenset()
)

DEFAULT_PATH = Path("data/principals.yaml")


def load_principals(path: Path = DEFAULT_PATH) -> dict[str, Principal]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, Principal] = {}
    for item in raw.get("principals", []):
        p = Principal(
            id=str(item["id"]),
            clearance=Clearance(item["clearance"]),
            departments=frozenset(str(d) for d in item.get("departments", [])),
        )
        out[p.id] = p
    return out


def resolve_principal(principal_id: str, path: Path = DEFAULT_PATH) -> Principal:
    """Raise KeyError for an unknown id. The CLI turns that into a usage error."""
    try:
        return load_principals(path)[principal_id]
    except KeyError:
        raise KeyError(f"unknown principal: {principal_id}") from None
```

`src/sovrag/auth/__init__.py`:

```python
"""Principals and clearances. See principals.py."""

from sovrag.auth.principals import (
    CLASSIFICATION_RANK,
    DEFAULT_PATH,
    LEAST_PRIVILEGED,
    Clearance,
    Principal,
    load_principals,
    rank,
    resolve_principal,
)

__all__ = [
    "CLASSIFICATION_RANK",
    "DEFAULT_PATH",
    "LEAST_PRIVILEGED",
    "Clearance",
    "Principal",
    "load_principals",
    "rank",
    "resolve_principal",
]
```

`data/principals.yaml`:

```yaml
# Principals for the CLI and the eval harness. Labels are synthetic (ADR 0003).
principals:
  - id: public
    clearance: UNCLASSIFIED
    departments: []
  - id: analyst_dnd
    clearance: PROTECTED_A
    departments: [DND]
  - id: analyst_ps
    clearance: PROTECTED_A
    departments: [PS]
  - id: cio_ssc
    clearance: PROTECTED_A
    departments: [SSC, TBS]
  - id: auditor
    clearance: PROTECTED_B
    departments: [DND, PS, PSPC, SSC, TBS]
```

In `src/sovrag/corpus/manifest.py` replace the line `CLASSIFICATION_RANK = {"UNCLASSIFIED": 0, "PROTECTED_A": 1, "PROTECTED_B": 2}` with an import at the top of the module: `from sovrag.auth import CLASSIFICATION_RANK` (keep the name exported from manifest so existing imports keep working; add `# noqa: F401` only if ruff reports it unused).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_principals.py tests/test_manifest.py -q`
Expected: all pass.

- [ ] **Step 5: Gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
git add src/sovrag/auth data/principals.yaml src/sovrag/corpus/manifest.py tests/test_principals.py
git commit -m "Add principals: clearance, departments and the principals file"
```

---

### Task 3: Audit writer

**Files:**
- Create: `src/sovrag/audit.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Produces: `write_event(event: str, **fields: object) -> None`. Appends one JSON line `{"ts": <UTC ISO 8601>, "event": event, ...fields}` to `Settings().audit_log_path`, creating the parent directory.

- [ ] **Step 1: Write the failing tests**

```python
"""Audit log: one JSON line per event, appended to AUDIT_LOG_PATH."""

import json
from pathlib import Path

import pytest

from sovrag.audit import write_event


def test_appends_json_lines_with_timestamp_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "logs" / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(target))
    write_event("answer", principal="analyst_dnd", chunk_ids=[1, 2])
    write_event("search", principal="public")
    lines = [json.loads(line) for line in target.read_text().splitlines()]
    assert [line["event"] for line in lines] == ["answer", "search"]
    assert lines[0]["principal"] == "analyst_dnd"
    assert lines[0]["chunk_ids"] == [1, 2]
    assert lines[0]["ts"].endswith("+00:00")


def test_creates_the_parent_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "deep" / "er" / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(target))
    write_event("resolve")
    assert target.exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_audit.py -q`
Expected: FAIL at import, `ModuleNotFoundError: No module named 'sovrag.audit'`.

- [ ] **Step 3: Implement**

```python
"""Append-only JSONL audit log shared by the CLI, HTTP and MCP paths.

One line per event. Every retrieval and answer is logged with the principal, the entitlement
predicate applied and the chunk ids returned, on every path, so the two enforcement layers
can be checked against each other after the fact.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sovrag.config import get_settings


def write_event(event: str, **fields: object) -> None:
    target = Path(get_settings().audit_log_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_audit.py -q`
Expected: `2 passed`.

- [ ] **Step 5: Gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
git add src/sovrag/audit.py tests/test_audit.py
git commit -m "Add the shared JSONL audit writer"
```

---

### Task 4: Prompts

**Files:**
- Create: `src/sovrag/generate/prompts.py`
- Test: `tests/test_prompts.py`

**Interfaces:**
- Consumes: `Hit` from `sovrag.retrieve`.
- Produces: `SENTINEL`, `REFUSAL_TEXT`, `format_sources(hits) -> str`, `build_messages(question, hits, *, firm: bool = False) -> list[dict[str, str]]`.

- [ ] **Step 1: Write the failing tests**

```python
"""Prompt construction: numbered sources, citation instruction, sentinel, firm retry."""

from sovrag.generate.prompts import REFUSAL_TEXT, SENTINEL, build_messages, format_sources
from sovrag.retrieve import Hit


def _hit(n: int) -> Hit:
    return Hit(
        chunk_id=100 + n,
        doc_id=f"doc-{n}",
        ordinal=n,
        heading_path=f"Part {n} > Section",
        text=f"Body of chunk {n}.",
        title=f"Title {n}",
        department="DND",
        classification="UNCLASSIFIED",
        doc_type="DP",
        source_url=f"https://example.test/{n}",
        score=1.0,
    )


def test_sources_are_numbered_in_order_with_title_and_heading() -> None:
    text = format_sources([_hit(1), _hit(2)])
    assert text.index("[1] Title 1 > Part 1 > Section\nBody of chunk 1.") < text.index(
        "[2] Title 2"
    )


def test_messages_carry_system_rules_and_the_question() -> None:
    messages = build_messages("What is X?", [_hit(1)])
    assert [m["role"] for m in messages] == ["system", "user"]
    system, user = messages[0]["content"], messages[1]["content"]
    assert "[n]" in system or "[1]" in system
    assert SENTINEL in system
    assert user.endswith("Question: What is X?")
    assert "[1] Title 1" in user


def test_firm_adds_one_sentence_and_changes_nothing_else() -> None:
    soft = build_messages("q", [_hit(1)])
    firm = build_messages("q", [_hit(1)], firm=True)
    assert firm[1] == soft[1]
    assert firm[0]["content"].startswith(soft[0]["content"])
    assert "no citations" in firm[0]["content"]


def test_refusal_text_is_a_plain_sentence() -> None:
    assert REFUSAL_TEXT.endswith(".")
    assert SENTINEL not in REFUSAL_TEXT
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_prompts.py -q`
Expected: FAIL at import, `ModuleNotFoundError: No module named 'sovrag.generate.prompts'`.

- [ ] **Step 3: Implement**

```python
"""Prompt construction for grounded answers.

The model sees numbered sources and the question, nothing else. It must cite with [n] and
must reply with the sentinel alone when the sources do not contain the answer; the code
turns the sentinel into a fixed refusal. See ADR 0008.
"""

from __future__ import annotations

from collections.abc import Sequence

from sovrag.retrieve import Hit

SENTINEL = "INSUFFICIENT_CONTEXT"
REFUSAL_TEXT = "The retrieved documents do not contain enough information to answer this question."

SYSTEM = (
    "You answer questions about Government of Canada documents using only the numbered "
    "sources provided. Cite every claim with the source number in square brackets, such as "
    "[1] or [2][3]. Do not use knowledge from outside the sources. Keep the answer short and "
    f"factual. If the sources do not contain the answer, reply with exactly {SENTINEL} and "
    "nothing else."
)
FIRM = (
    "Your previous reply had no citations. Every sentence must end with at least one [n] "
    "citation to a source."
)


def format_sources(hits: Sequence[Hit]) -> str:
    return "\n\n".join(
        f"[{n}] {h.title} > {h.heading_path}\n{h.text}" for n, h in enumerate(hits, start=1)
    )


def build_messages(
    question: str, hits: Sequence[Hit], *, firm: bool = False
) -> list[dict[str, str]]:
    system = f"{SYSTEM} {FIRM}" if firm else SYSTEM
    user = f"Sources:\n\n{format_sources(hits)}\n\nQuestion: {question}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_prompts.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
git add src/sovrag/generate/prompts.py tests/test_prompts.py
git commit -m "Add grounded-answer prompts with citation and sentinel rules"
```

---

### Task 5: Citation parsing and refusal detection

**Files:**
- Create: `src/sovrag/generate/citations.py`
- Test: `tests/test_citations.py`

**Interfaces:**
- Consumes: `SENTINEL` from `prompts.py`.
- Produces: `is_refusal(text: str) -> bool`; `parse(text: str, k: int) -> tuple[str, tuple[int, ...]]` returning cleaned text (out-of-range markers removed, `[1, 3]` normalised to `[1][3]`, stray spaces before punctuation removed) and the sorted de-duplicated in-range indices.

- [ ] **Step 1: Write the failing tests**

```python
"""Citation markers: find [n], drop out-of-range ones, detect the refusal sentinel."""

from sovrag.generate.citations import is_refusal, parse
from sovrag.generate.prompts import SENTINEL


def test_finds_single_and_adjacent_markers() -> None:
    assert parse("A [1]. B [1][3].", 5) == ("A [1]. B [1][3].", (1, 3))


def test_normalises_comma_lists() -> None:
    assert parse("x [1, 3]", 5) == ("x [1][3]", (1, 3))


def test_drops_out_of_range_markers_and_tidies_punctuation() -> None:
    assert parse("A [1]. B [7].", 5) == ("A [1]. B.", (1,))
    assert parse("Only [0] here.", 5) == ("Only here.", ())


def test_deduplicates_and_sorts() -> None:
    assert parse("[3] then [1] then [3]", 5)[1] == (1, 3)


def test_no_markers_gives_empty_tuple() -> None:
    assert parse("Plain answer.", 5) == ("Plain answer.", ())


def test_refusal_detection() -> None:
    assert is_refusal(SENTINEL)
    assert is_refusal(f"  {SENTINEL}\n")
    assert is_refusal(f"I am sorry. {SENTINEL}")
    assert not is_refusal("The plan says [1].")
    assert not is_refusal("")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_citations.py -q`
Expected: FAIL at import, `ModuleNotFoundError: No module named 'sovrag.generate.citations'`.

- [ ] **Step 3: Implement**

```python
"""Citation markers in generated text.

The model cites sources as [n]. Only markers that point at a provided source are kept; the
rest are removed so the returned text never carries a citation without a Citation object.
"""

from __future__ import annotations

import re

from sovrag.generate.prompts import SENTINEL

_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([.,;:!?])")
_DOUBLE_SPACE = re.compile(r"[ \t]{2,}")


def is_refusal(text: str) -> bool:
    return SENTINEL in text


def parse(text: str, k: int) -> tuple[str, tuple[int, ...]]:
    cited: set[int] = set()

    def replace(match: re.Match[str]) -> str:
        keep = [n for n in (int(x) for x in match.group(1).split(",")) if 1 <= n <= k]
        cited.update(keep)
        return "".join(f"[{n}]" for n in keep)

    cleaned = _MARKER.sub(replace, text)
    cleaned = _SPACE_BEFORE_PUNCT.sub(r"\1", cleaned)
    cleaned = _DOUBLE_SPACE.sub(" ", cleaned).strip()
    return cleaned, tuple(sorted(cited))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_citations.py -q`
Expected: `6 passed`.

- [ ] **Step 5: Gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
git add src/sovrag/generate/citations.py tests/test_citations.py
git commit -m "Parse citation markers and detect the refusal sentinel"
```

---

### Task 6: Generator client

**Files:**
- Create: `src/sovrag/generate/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `Settings` (`llm_base_url`, `llm_model`, `llm_api_key`).
- Produces: `GenerationError(RuntimeError)`; `Generator(*, base_url, model, api_key="not-needed", max_tokens=600, max_retries=2, http_client=None)` with `from_settings(settings)` and `complete(messages: Sequence[dict[str, str]], *, max_tokens: int | None = None) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_llm.py -q`
Expected: FAIL at import, `ModuleNotFoundError: No module named 'sovrag.generate.llm'`.

- [ ] **Step 3: Implement**

```python
"""Chat completions through the OpenAI-compatible client.

Docker Model Runner serves Qwen 3.5 by default (ADR 0006); vLLM or any other OpenAI-compatible
server is a base-URL swap. Qwen 3.x thinks before answering unless told not to, and with a
small token budget the thinking consumes the whole reply, so the thinking flag is always sent.
Temperature 0 keeps answers reproducible for the eval.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

import httpx2
from openai import OpenAI, OpenAIError
from openai.types.chat import ChatCompletionMessageParam

from sovrag.config import Settings

THINKING_OFF = {"chat_template_kwargs": {"enable_thinking": False}}


class GenerationError(RuntimeError):
    """The generator could not produce a reply. The message says why."""


class Generator:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "not-needed",
        max_tokens: int = 600,
        max_retries: int = 2,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = OpenAI(
            base_url=base_url, api_key=api_key, http_client=http_client, max_retries=max_retries
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Generator:
        return cls(
            base_url=settings.llm_base_url, model=settings.llm_model, api_key=settings.llm_api_key
        )

    def complete(self, messages: Sequence[dict[str, str]], *, max_tokens: int | None = None) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=cast("list[ChatCompletionMessageParam]", list(messages)),
                temperature=0,
                max_tokens=max_tokens or self.max_tokens,
                extra_body=THINKING_OFF,
            )
        except OpenAIError as exc:
            raise GenerationError(f"generation failed: {exc}") from exc
        content = response.choices[0].message.content if response.choices else None
        if content is None or not content.strip():
            raise GenerationError("empty reply from the generator")
        return content.strip()
```

If mypy rejects the `cast` string form, use `cast(list[ChatCompletionMessageParam], list(messages))`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_llm.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
git add src/sovrag/generate/llm.py tests/test_llm.py
git commit -m "Add the generator client with thinking off and temperature zero"
```

---

### Task 7: The answer function

**Files:**
- Create: `src/sovrag/generate/answer.py`
- Modify: `src/sovrag/generate/__init__.py` (re-export `Answer`, `Citation`, `answer`)
- Test: `tests/test_answer.py`

**Interfaces:**
- Consumes: `Principal` (Task 2 or the merged branch), `Mode`, `Hit`, `search` (Task 1), `write_event` (Task 3), `build_messages`, `REFUSAL_TEXT` (Task 4), `is_refusal`, `parse` (Task 5), `Generator`, `GenerationError` (Task 6), `Embedder`, `connect`.
- Produces: `MAX_K = 10`; `Citation`; `Answer`; `answer(question, *, principal, mode=Mode.hybrid, k=5, request_id=None) -> Answer`. Module-level names `Generator`, `Embedder`, `connect`, `search` are looked up at call time so tests can monkeypatch them.

- [ ] **Step 1: Write the failing tests**

```python
"""answer(): retrieve, prompt, cite, refuse, audit. Model and database are faked."""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from sovrag.auth import Clearance, Principal
from sovrag.generate import answer as answer_mod
from sovrag.generate.answer import MAX_K, Answer, Citation, answer
from sovrag.generate.prompts import REFUSAL_TEXT, SENTINEL
from sovrag.retrieve import Hit, Mode

WHO = Principal(id="analyst_dnd", clearance=Clearance.PROTECTED_A, departments=frozenset({"DND"}))


def _hit(n: int) -> Hit:
    return Hit(
        chunk_id=100 + n,
        doc_id=f"doc-{n}",
        ordinal=n,
        heading_path=f"Part {n}",
        text=f"Body {n}.",
        title=f"Title {n}",
        department="DND",
        classification="UNCLASSIFIED",
        doc_type="DP",
        source_url=f"https://example.test/{n}",
        score=1.0,
    )


class FakeGenerator:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]], *, max_tokens: int | None = None) -> str:
        self.calls.append(list(messages))
        return self.replies.pop(0)


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(target))
    return target


def _wire(monkeypatch: pytest.MonkeyPatch, hits: list[Hit], replies: list[str]) -> FakeGenerator:
    gen = FakeGenerator(replies)
    monkeypatch.setattr(answer_mod.Generator, "from_settings", classmethod(lambda cls, s: gen))
    monkeypatch.setattr(answer_mod, "connect", lambda url: nullcontext(object()))
    monkeypatch.setattr(answer_mod, "search", lambda conn, q, **kw: list(hits[: kw["k"]]))
    return gen


def _audit_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_cited_answer_builds_citations_for_cited_hits_only(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    _wire(monkeypatch, [_hit(1), _hit(2), _hit(3)], ["Fact [3]. More [1]."])
    result = answer("q", principal=WHO, mode=Mode.vector, k=3, request_id="r1")
    assert result == Answer(
        question="q",
        principal="analyst_dnd",
        mode="vector",
        request_id="r1",
        text="Fact [3]. More [1].",
        citations=(
            Citation(1, "doc-1", 101, "Title 1", "Part 1", "https://example.test/1"),
            Citation(3, "doc-3", 103, "Title 3", "Part 3", "https://example.test/3"),
        ),
        refused=False,
        model=result.model,
        retrieved_chunk_ids=(101, 102, 103),
        applied_filter=None,
    )
    (line,) = _audit_lines(audit_path)
    assert line["event"] == "answer"
    assert line["request_id"] == "r1"
    assert line["cited_chunk_ids"] == [101, 103]
    assert line["retrieved_chunk_ids"] == [101, 102, 103]
    assert line["refused"] is False and line["reason"] is None


def test_sentinel_refuses_with_no_citations(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    _wire(monkeypatch, [_hit(1)], [SENTINEL])
    result = answer("q", principal=WHO)
    assert result.refused and result.text == REFUSAL_TEXT and result.citations == ()
    assert _audit_lines(audit_path)[0]["reason"] == "sentinel"


def test_uncited_reply_is_retried_firmly_then_accepted(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    gen = _wire(monkeypatch, [_hit(1)], ["No markers here.", "Now cited [1]."])
    result = answer("q", principal=WHO)
    assert not result.refused and result.text == "Now cited [1]."
    assert len(gen.calls) == 2
    assert "no citations" in gen.calls[1][0]["content"]
    assert "no citations" not in gen.calls[0][0]["content"]


def test_uncited_twice_refuses_with_reason_uncited(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    _wire(monkeypatch, [_hit(1)], ["Nope.", "Still nope [9]."])
    result = answer("q", principal=WHO)
    assert result.refused and result.citations == ()
    assert _audit_lines(audit_path)[0]["reason"] == "uncited"


def test_no_hits_refuses_without_calling_the_model(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    gen = _wire(monkeypatch, [], ["should not be used"])
    result = answer("q", principal=WHO)
    assert result.refused and result.retrieved_chunk_ids == ()
    assert gen.calls == []
    assert _audit_lines(audit_path)[0]["reason"] == "no_hits"


def test_k_is_clamped_to_max_k(monkeypatch: pytest.MonkeyPatch, audit_path: Path) -> None:
    seen: list[int] = []
    gen = FakeGenerator(["ok [1]"])
    monkeypatch.setattr(answer_mod.Generator, "from_settings", classmethod(lambda cls, s: gen))
    monkeypatch.setattr(answer_mod, "connect", lambda url: nullcontext(object()))

    def fake_search(conn: object, q: str, **kw: object) -> list[Hit]:
        seen.append(int(kw["k"]))  # type: ignore[call-overload]
        return [_hit(1)]

    monkeypatch.setattr(answer_mod, "search", fake_search)
    answer("q", principal=WHO, k=50)
    assert seen == [MAX_K]
    assert _audit_lines(audit_path)[0]["k"] == MAX_K


def test_keyword_mode_never_builds_the_embedder(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    _wire(monkeypatch, [_hit(1)], ["ok [1]"])

    def boom(cls: type, s: object) -> None:
        raise AssertionError("embedder constructed in keyword mode")

    monkeypatch.setattr(answer_mod.Embedder, "from_settings", classmethod(boom))
    assert not answer("q", principal=WHO, mode=Mode.keyword).refused
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_answer.py -q`
Expected: FAIL at import, `ModuleNotFoundError: No module named 'sovrag.generate.answer'`.

- [ ] **Step 3: Implement**

```python
"""Grounded answers: retrieve, prompt, cite, refuse, audit.

This is the one entry point for answering. The CLI, the HTTP API and the MCP server all call
it, so the citation rule, the refusal rule and the audit line are enforced in one place.
The principal is passed in by the caller, which is responsible for binding it (ADR on
principal binding). Entitlement filtering will happen inside the search functions.
"""

from __future__ import annotations

from dataclasses import dataclass

from sovrag.audit import write_event
from sovrag.auth import Principal
from sovrag.config import get_settings
from sovrag.generate.citations import is_refusal, parse
from sovrag.generate.llm import Generator
from sovrag.generate.prompts import REFUSAL_TEXT, build_messages
from sovrag.index.embed import Embedder
from sovrag.index.store import connect
from sovrag.retrieve import Hit, Mode, search

MAX_K = 10


@dataclass(frozen=True)
class Citation:
    n: int
    doc_id: str
    chunk_id: int
    title: str
    heading_path: str
    source_url: str


@dataclass(frozen=True)
class Answer:
    question: str
    principal: str
    mode: str
    request_id: str | None
    text: str
    citations: tuple[Citation, ...]
    refused: bool
    model: str
    retrieved_chunk_ids: tuple[int, ...]
    applied_filter: str | None


def answer(
    question: str,
    *,
    principal: Principal,
    mode: Mode = Mode.hybrid,
    k: int = 5,
    request_id: str | None = None,
) -> Answer:
    settings = get_settings()
    k = max(1, min(k, MAX_K))
    with connect(settings.database_url) as conn:
        hits: list[Hit] = search(
            conn,
            question,
            mode=mode,
            k=k,
            # entitlement: pass principal here once the filter lands
            embed_query=lambda q: Embedder.from_settings(settings).embed([q])[0],
        )

    def finish(
        text: str, citations: tuple[Citation, ...], *, refused: bool, reason: str | None
    ) -> Answer:
        result = Answer(
            question=question,
            principal=principal.id,
            mode=mode.value,
            request_id=request_id,
            text=text,
            citations=citations,
            refused=refused,
            model=settings.llm_model,
            retrieved_chunk_ids=tuple(h.chunk_id for h in hits),
            applied_filter=None,
        )
        write_event(
            "answer",
            request_id=request_id,
            principal=principal.id,
            mode=mode.value,
            k=k,
            model=settings.llm_model,
            retrieved_chunk_ids=list(result.retrieved_chunk_ids),
            cited_chunk_ids=[c.chunk_id for c in citations],
            refused=refused,
            reason=reason,
            applied_filter=None,
        )
        return result

    def refuse(reason: str) -> Answer:
        return finish(REFUSAL_TEXT, (), refused=True, reason=reason)

    if not hits:
        return refuse("no_hits")

    generator = Generator.from_settings(settings)
    text = generator.complete(build_messages(question, hits))
    if is_refusal(text):
        return refuse("sentinel")
    cleaned, cited = parse(text, len(hits))
    if not cited:
        text = generator.complete(build_messages(question, hits, firm=True))
        if is_refusal(text):
            return refuse("sentinel")
        cleaned, cited = parse(text, len(hits))
        if not cited:
            return refuse("uncited")

    citations = tuple(
        Citation(
            n=n,
            doc_id=hits[n - 1].doc_id,
            chunk_id=hits[n - 1].chunk_id,
            title=hits[n - 1].title,
            heading_path=hits[n - 1].heading_path,
            source_url=hits[n - 1].source_url,
        )
        for n in cited
    )
    return finish(cleaned, citations, refused=False, reason=None)
```

`src/sovrag/generate/__init__.py`:

```python
"""Generation: grounded answers with citations and refusal."""

from sovrag.generate.answer import MAX_K, Answer, Citation, answer
from sovrag.generate.llm import GenerationError, Generator

__all__ = ["MAX_K", "Answer", "Citation", "GenerationError", "Generator", "answer"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_answer.py -q`
Expected: `7 passed`.

- [ ] **Step 5: Gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
git add src/sovrag/generate/answer.py src/sovrag/generate/__init__.py tests/test_answer.py
git commit -m "Add answer(): grounded generation with citations, refusal and audit"
```

---

### Task 8: The `ask` command

**Files:**
- Modify: `src/sovrag/cli.py` (imports; new `ask` command after `query`; module docstring)
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `answer`, `Answer`, `Citation`, `MAX_K`, `GenerationError` from `sovrag.generate`; `resolve_principal` from `sovrag.auth`.
- Produces: `sovrag ask QUESTION --principal ID [--mode vector|keyword|hybrid] [--k 1..10] [--json] [--request-id ID]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def _fake_answer(refused: bool = False) -> object:
    from sovrag.generate import Answer, Citation

    if refused:
        return Answer(
            question="q",
            principal="analyst_dnd",
            mode="hybrid",
            request_id=None,
            text="The retrieved documents do not contain enough information to answer this question.",
            citations=(),
            refused=True,
            model="m",
            retrieved_chunk_ids=(1,),
            applied_filter=None,
        )
    return Answer(
        question="q",
        principal="analyst_dnd",
        mode="hybrid",
        request_id="r7",
        text="SSC plans cloud brokering [1].",
        citations=(
            Citation(1, "ssc-dp-2025-26", 42, "SSC Plan", "Plans > Cloud", "https://x/ssc"),
        ),
        refused=False,
        model="m",
        retrieved_chunk_ids=(42, 43),
        applied_filter=None,
    )


def test_ask_prints_answer_and_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "answer", lambda q, **kw: _fake_answer())
    result = runner.invoke(cli.app, ["ask", "q", "--principal", "analyst_dnd"])
    assert result.exit_code == 0, result.output
    assert "SSC plans cloud brokering [1]." in result.output
    assert "Sources:" in result.output
    assert "[1] SSC Plan, Plans > Cloud, https://x/ssc" in result.output


def test_ask_json_round_trips_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setattr(cli, "answer", lambda q, **kw: _fake_answer())
    result = runner.invoke(
        cli.app, ["ask", "q", "--principal", "analyst_dnd", "--json", "--request-id", "r7"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["refused"] is False
    assert payload["request_id"] == "r7"
    assert payload["citations"][0]["chunk_id"] == 42
    assert payload["retrieved_chunk_ids"] == [42, 43]


def test_ask_refusal_prints_no_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "answer", lambda q, **kw: _fake_answer(refused=True))
    result = runner.invoke(cli.app, ["ask", "q", "--principal", "analyst_dnd"])
    assert result.exit_code == 0
    assert "do not contain enough information" in result.output
    assert "Sources:" not in result.output


def test_ask_unknown_principal_is_a_usage_error() -> None:
    result = runner.invoke(cli.app, ["ask", "q", "--principal", "nobody"])
    assert result.exit_code == 2
    assert "unknown principal" in result.output


def test_ask_generation_error_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from sovrag.generate import GenerationError

    def boom(q: str, **kw: object) -> None:
        raise GenerationError("generation failed: connection refused")

    monkeypatch.setattr(cli, "answer", boom)
    result = runner.invoke(cli.app, ["ask", "q", "--principal", "analyst_dnd"])
    assert result.exit_code == 1
    assert "connection refused" in result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q -k ask`
Expected: FAIL. The first three and the last fail with `AttributeError: module 'sovrag.cli' has no attribute 'answer'`; the unknown-principal test fails because the `ask` command does not exist (exit code 2 with "No such command"), so the assertion on "unknown principal" is what fails. Confirm each.

- [ ] **Step 3: Implement**

In `src/sovrag/cli.py` add imports:

```python
from sovrag.auth import resolve_principal
from sovrag.generate import MAX_K, GenerationError, answer
```

Change the module docstring's first line to `"""sovrag CLI: ingest | query | ask | serve."""`. After the `query` command add:

```python
@app.command()
def ask(
    question: str,
    principal: Annotated[str, typer.Option(help="Principal id from data/principals.yaml")],
    mode: Annotated[Mode, typer.Option(help="vector | keyword | hybrid")] = Mode.hybrid,
    k: Annotated[int, typer.Option(min=1, max=MAX_K)] = 5,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text")] = False,
    request_id: Annotated[
        str | None, typer.Option(help="Caller's id, echoed into the audit log")
    ] = None,
) -> None:
    """Answer a question from retrieved chunks only, with citations; refuse when unsupported."""
    try:
        who = resolve_principal(principal)
    except KeyError as exc:
        raise typer.BadParameter(
            f"unknown principal: {principal}", param_hint="--principal"
        ) from exc
    try:
        result = answer(question, principal=who, mode=mode, k=k, request_id=request_id)
    except GenerationError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_out:
        typer.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return
    typer.echo(result.text)
    if result.citations:
        typer.echo("\nSources:")
        for c in result.citations:
            typer.echo(f"[{c.n}] {c.title}, {c.heading_path}, {c.source_url}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q`
Expected: all pass (the five new `ask` tests plus the existing ones).

- [ ] **Step 5: Gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
git add src/sovrag/cli.py tests/test_cli.py
git commit -m "Add the ask command: grounded answers with sources from the CLI"
```

---

### Task 9: Decision record, docs, and the live check

**Files:**
- Create: `docs/adr/0008-grounding-and-refusal.md`
- Modify: `docs/architecture.md` (query-path node label, status sentence, decisions list, a "## Generation" section before "## Decisions"), `README.md` (status line), `CLAUDE.md` (Commands and the "Not implemented yet" line)

**Interfaces:**
- Consumes: the working CLI against the local corpus and Docker Model Runner (both running; keyword needs no model, vector and hybrid embed via Model Runner, `ask` also calls the generator).
- Produces: documentation and a results table for the PR.

- [ ] **Step 1: Write ADR 0008**

```markdown
# ADR 0008: Grounded answers with a refusal sentinel and mandatory citations

Status: accepted (2026-09-17)

## Context

The generator must answer only from retrieved chunks, cite every claim to a chunk, and refuse when the chunks do not contain the answer. The generator is a 9B-parameter quantised model served locally (ADR 0006), so the mechanism has to be robust to imperfect instruction following.

## Decision

- The prompt lists the retrieved chunks as numbered sources with title and heading path, and instructs the model to cite with `[n]` and to reply with the single token `INSUFFICIENT_CONTEXT` when the sources lack the answer. The code maps that sentinel to one fixed refusal sentence with no citations. A sentinel was chosen over structured JSON output because it survives a small model's formatting lapses and keeps the prompt short.
- Citation markers are validated by code: markers outside the provided range are removed, and a `Citation` object is built for each marker kept. The returned text never carries a marker without a citation.
- An answer with no in-range markers is retried once with a firmer instruction. If the retry still cites nothing, the answer is refused and the audit line records `uncited`. An uncited answer presented as grounded is the one output this system must not produce; withholding an occasional correct but uncited answer is the accepted cost.
- Temperature 0, a 600-token budget, and Qwen's thinking mode off on every call, so results are reproducible for the eval.
- `k` is capped at 10 so ten chunks of at most 340 tokens plus the instructions stay well inside the 8192-token context.
- Every call writes one audit line: principal, mode, k, model, retrieved and cited chunk ids, refused, reason, applied filter. The same writer serves the HTTP and MCP paths.

## Consequences

- Refusal quality depends on the model honouring the sentinel. The generation eval will measure refusal precision on questions the corpus cannot answer, and faithfulness on those it can, with a different judge model.
- The uncited-retry doubles latency on the rare uncited path. Acceptable at demo scale; the eval will report how often it triggers.
- Structured output remains an option if a later model supports it reliably; the parsing layer is the only thing that would change.
```

- [ ] **Step 2: Update the architecture doc, README and CLAUDE.md**

In `docs/architecture.md`: change the node `G[generate/llm.py<br/>grounded answer, citations]` to `G[generate/answer.py<br/>grounded answer, [n] citations, refusal]`; change the status sentence to `Status: the corpus pipeline, all three retrieval modes, and grounded generation with citations and refusal exist. Entitlement filtering and the HTTP API follow.`; add before "## Decisions":

```markdown
## Generation

`generate/answer.py` is the single entry point for answering. It retrieves with the chosen mode, builds a prompt that lists the chunks as numbered sources, and makes one chat completion call at temperature 0 with the model's thinking mode off. The model must cite with `[n]` or reply with a sentinel; code validates the markers, builds a `Citation` for each, and turns the sentinel into a fixed refusal. An answer with no citations is retried once with a firmer instruction, then refused. Every call appends one line to the JSONL audit log. See ADR 0008.
```

and append to the Decisions list: `- ADR 0008: grounded answers with a refusal sentinel, code-validated citations, and a retry-then-refuse rule for uncited replies.`

`README.md` status line: `Status: corpus indexed; vector, keyword and hybrid retrieval; grounded answers with citations and refusal from the CLI. No entitlement filtering yet.`

`CLAUDE.md` Commands: add `- uv run sovrag ask "..." --principal analyst_dnd   # answer with [n] citations, or refuse` after the query line, and change the last line to `- Not implemented yet: sovrag serve, sovrag eval, the mcp profile, entitlement filtering.`

- [ ] **Step 3: Live check with the real model**

Run each question below with `uv run sovrag ask "<question>" --principal analyst_dnd --json` and record: refused, the top citation's doc_id, and whether the answer text reads as grounded (one or two sentences, cites, no invented facts). Questions 1 to 10 are the retrieval eyeball set (expected documents in `data/manifest.yaml`); 11 to 13 cannot be answered from the corpus and should refuse.

1. What are Shared Services Canada's planned results for cloud services?
2. What did the Auditor General find about cybersecurity of personal information in the cloud?
3. What does the Directive on Identity Management require of departments?
4. How much did National Defence plan to spend in 2024-25?
5. What are Public Safety Canada's core responsibilities?
6. What is the Policy on Government Security's requirement for security screening?
7. What results did PSPC report on procurement modernisation?
8. What is Treasury Board Secretariat's role in digital government?
9. What are the departmental results for the Canadian Armed Forces readiness?
10. Which policy instrument covers security event management?
11. What is the population of Ottawa?
12. Who won the 2024 Stanley Cup?
13. What is the recipe for butter tarts?

Record the table (question, refused, top citation doc_id, grounded yes/no, notes) in the report under `## PR body`, with the PR description text: what changed, the table, and follow-ups for the generation eval.

- [ ] **Step 4: Gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
git add docs/adr/0008-grounding-and-refusal.md docs/architecture.md README.md CLAUDE.md
git commit -m "Record the grounding decision and document generation"
```

---

## Self-review

- Spec coverage: contract dataclasses and `answer` signature (Task 7); `Mode` move and dispatcher (Task 1); `Principal` dependency handled (Task 2 conditional); audit writer (Task 3); prompts with sentinel and firm retry (Task 4); marker parsing rules (Task 5); client with thinking off, temperature 0, `GenerationError` (Task 6); no-hits, sentinel, uncited-retry, clamp, keyword-no-embedder (Task 7 tests); CLI text and JSON, usage error, exit 1 (Task 8); ADR 0008, docs, live check with three unanswerable questions (Task 9).
- Placeholders: none.
- Type consistency: `search(conn, question, *, mode, k, embed_query)` defined in Task 1 and used identically in Task 7; `build_messages(question, hits, *, firm)` in Tasks 4 and 7; `parse(text, k) -> (str, tuple[int, ...])` in Tasks 5 and 7; `Generator.complete(messages, *, max_tokens)` in Tasks 6 and 7 and the fake in Task 7 tests; `Answer` field order in Task 7 matches the positional construction in Task 8 tests (keyword arguments used there, so order-safe); `Citation` positional order `(n, doc_id, chunk_id, title, heading_path, source_url)` in Tasks 7 and 8.
