# Hybrid Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `sovrag query` gains `--mode vector|keyword|hybrid`, where keyword ranks by Postgres full-text match and hybrid fuses the vector and keyword rankings with reciprocal rank fusion.

**Architecture:** Two new search functions sit beside the existing `search_vector` in `src/sovrag/retrieve/hybrid.py` and return the same frozen `Hit` dataclass. Fusion is a pure function in `src/sovrag/retrieve/fusion.py` over chunk ids. The CLI selects the function by a `Mode` enum and constructs the embedder only when a vector is needed.

**Tech Stack:** Python 3.14, psycopg 3, PostgreSQL 18 (`websearch_to_tsquery`, `ts_rank_cd`, the existing generated `tsv` column and GIN index), pgvector, typer, pytest.

**Spec:** `docs/superpowers/specs/2026-09-16-hybrid-retrieval-design.md`

## Global Constraints

- Every retrieval returns citations: `doc_id`, `chunk_id`, `heading_path`, `source_url` (CLAUDE.md invariant; already fields of `Hit`).
- No filtering in Python. Fusion re-orders candidates that SQL already returned; it never drops one for entitlement reasons.
- Type hints everywhere; `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` and `uv run pytest -q` must pass before each commit.
- Tests first. Run the test, watch it fail for the expected reason, then implement.
- Commit messages describe content only. No schedule or session references anywhere in the repo.
- Database tests use the `db` fixture from `tests/conftest.py`, which truncates `chunks` and `documents` with `restart identity`, so the first inserted chunk always has id 1.

---

### Task 1: Reciprocal rank fusion

**Files:**
- Create: `src/sovrag/retrieve/fusion.py`
- Test: `tests/test_fusion.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `rrf(rankings: Sequence[Sequence[int]], *, k: int = 60) -> list[tuple[int, float]]`, sorted by score descending then chunk id ascending. `DEFAULT_K = 60`.

- [ ] **Step 1: Write the failing tests**

```python
"""Reciprocal rank fusion merges ranked lists by position, ignoring incomparable scores."""

import pytest

from sovrag.retrieve.fusion import DEFAULT_K, rrf


def test_chunk_in_both_lists_outranks_chunk_in_one() -> None:
    fused = rrf([[1, 2, 3], [3, 4, 5]])
    assert fused[0][0] == 3


def test_ties_break_by_chunk_id() -> None:
    fused = rrf([[7], [2]])
    assert [chunk_id for chunk_id, _ in fused] == [2, 7]


def test_empty_list_contributes_nothing() -> None:
    assert rrf([[1, 2], []]) == rrf([[1, 2]])


def test_single_list_keeps_its_order_for_any_k() -> None:
    for k in (1, DEFAULT_K, 1000):
        assert [chunk_id for chunk_id, _ in rrf([[5, 9, 1]], k=k)] == [5, 9, 1]


def test_scores_follow_the_formula() -> None:
    fused = dict(rrf([[1, 2], [2]], k=60))
    assert fused[2] == pytest.approx(1 / 62 + 1 / 61)
    assert fused[1] == pytest.approx(1 / 61)


def test_no_rankings_gives_no_results() -> None:
    assert rrf([]) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_fusion.py -q`
Expected: FAIL at import, `ModuleNotFoundError: No module named 'sovrag.retrieve.fusion'`.

- [ ] **Step 3: Write the implementation**

```python
"""Reciprocal rank fusion.

Vector similarity and full-text rank are not on comparable scales, so hybrid retrieval
fuses by position only: each list contributes 1 / (k + rank) for every chunk it holds,
ranks starting at 1. A chunk that is high in either list rises; one that is high in both
rises furthest. k = 60 is the constant from Cormack, Clarke and Buettcher (2009); it
keeps first place from dominating.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

DEFAULT_K = 60


def rrf(rankings: Sequence[Sequence[int]], *, k: int = DEFAULT_K) -> list[tuple[int, float]]:
    """Fuse ranked lists of chunk ids. Sorted by score descending, then chunk id ascending."""
    scores: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_fusion.py -q`
Expected: `6 passed`.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src
git add src/sovrag/retrieve/fusion.py tests/test_fusion.py
git commit -m "Add reciprocal rank fusion over chunk ids"
```

---

### Task 2: Keyword search

**Files:**
- Modify: `src/sovrag/retrieve/hybrid.py` (module docstring lines 1-5; add `search_keyword` after `search_vector`)
- Test: `tests/test_keyword_search.py`

**Interfaces:**
- Consumes: `Hit`, `_SELECT` from `hybrid.py`; `upsert_document`, `replace_chunks` from `sovrag.index.store`; `Chunk`, `DocumentMeta`.
- Produces: `search_keyword(conn: psycopg.Connection, query: str, *, k: int) -> list[Hit]`. Score is `ts_rank_cd`, a float greater than 0 for every returned row. Ordered by score descending, then chunk id ascending.

- [ ] **Step 1: Write the failing tests**

```python
"""Keyword retrieval: Postgres full-text match on the generated tsv column, ranked by ts_rank_cd."""

import psycopg

from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.index.store import replace_chunks, upsert_document
from sovrag.retrieve.hybrid import search_keyword

DIM = 1024


def _meta(doc_id: str, department: str = "DND") -> DocumentMeta:
    return DocumentMeta(
        id=doc_id,
        title=f"Title of {doc_id}",
        department=department,
        doc_type="DP",
        fiscal_year="2024-25",
        lang="en",
        source_url=f"https://example.test/{doc_id}.html",
        pages=(),
        classification="UNCLASSIFIED",
    )


def _unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def _seed(db: psycopg.Connection) -> None:
    """Chunk ids after truncate: a/0 -> 1, a/1 -> 2, b/0 -> 3."""
    upsert_document(db, _meta("a"))
    upsert_document(db, _meta("b", department="GC_WIDE"))
    replace_chunks(
        db,
        "a",
        [
            Chunk(0, "A > x", "Shared Services Canada delivers cloud brokering for departments."),
            Chunk(1, "A > y", "The PSPC procurement program manages contracting for the government."),
        ],
        [_unit(0), _unit(1)],
    )
    replace_chunks(
        db,
        "b",
        [Chunk(0, "B > z", "The Policy on Government Security applies to all departments.")],
        [_unit(2)],
    )


def test_exact_acronym_ranks_first(db: psycopg.Connection) -> None:
    _seed(db)
    hits = search_keyword(db, "PSPC procurement", k=5)
    assert hits[0].chunk_id == 2
    assert all(h.score > 0 for h in hits)


def test_filler_only_query_returns_nothing(db: psycopg.Connection) -> None:
    _seed(db)
    assert search_keyword(db, "the of and", k=5) == []


def test_hits_carry_citations(db: psycopg.Connection) -> None:
    _seed(db)
    hit = search_keyword(db, "cloud brokering", k=5)[0]
    assert (hit.doc_id, hit.chunk_id, hit.heading_path, hit.source_url) == (
        "a",
        1,
        "A > x",
        "https://example.test/a.html",
    )


def test_k_limits_results(db: psycopg.Connection) -> None:
    _seed(db)
    assert len(search_keyword(db, "departments", k=1)) == 1
    assert len(search_keyword(db, "departments", k=5)) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_keyword_search.py -q`
Expected: FAIL at import, `ImportError: cannot import name 'search_keyword'`.

- [ ] **Step 3: Write the implementation**

Replace the module docstring at the top of `src/sovrag/retrieve/hybrid.py` with:

```python
"""Retrieval over the pgvector store.

Three modes over the same Hit shape: vector (cosine over the HNSW index), keyword (Postgres
full-text match on the generated tsv column, ranked by ts_rank_cd) and hybrid (both, fused
by reciprocal rank fusion). The entitlement predicate comes next and is applied inside
these queries as a WHERE clause, never in Python.
"""
```

Append after `search_vector`:

```python
_TSQUERY = "websearch_to_tsquery('english', %(q)s)"


def search_keyword(conn: psycopg.Connection, query: str, *, k: int) -> list[Hit]:
    """Full-text match ranked by ts_rank_cd. A query of stop words only matches nothing."""
    sql = _SELECT.format(score=f"ts_rank_cd(c.tsv, {_TSQUERY})") + (
        f" where c.tsv @@ {_TSQUERY} order by score desc, c.id limit %(k)s"
    )
    rows = conn.execute(sql, {"q": query, "k": k}).fetchall()
    return [Hit(*row) for row in rows]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_keyword_search.py tests/test_vector_search.py -q`
Expected: `7 passed` (4 new, 3 existing).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src
git add src/sovrag/retrieve/hybrid.py tests/test_keyword_search.py
git commit -m "Add keyword search over the full-text index"
```

---

### Task 3: Hybrid search

**Files:**
- Modify: `src/sovrag/retrieve/hybrid.py` (imports; add `search_hybrid` after `search_keyword`)
- Test: `tests/test_hybrid_search.py`

**Interfaces:**
- Consumes: `search_vector`, `search_keyword`, `Hit` from `hybrid.py`; `rrf` from `fusion.py`.
- Produces: `search_hybrid(conn: psycopg.Connection, query: str, query_embedding: Sequence[float], *, k: int) -> list[Hit]`. Score is the fusion score. `CANDIDATE_MIN = 20`; candidate window is `max(4 * k, CANDIDATE_MIN)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Hybrid retrieval: vector and keyword rankings fused by position."""

import psycopg

from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.index.store import replace_chunks, upsert_document
from sovrag.retrieve.hybrid import search_hybrid, search_vector

DIM = 1024


def _meta(doc_id: str, department: str = "DND") -> DocumentMeta:
    return DocumentMeta(
        id=doc_id,
        title=f"Title of {doc_id}",
        department=department,
        doc_type="DP",
        fiscal_year="2024-25",
        lang="en",
        source_url=f"https://example.test/{doc_id}.html",
        pages=(),
        classification="UNCLASSIFIED",
    )


def _unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def _mix(*weights: float) -> list[float]:
    """A query vector with distinct cosine distances to axes 0, 1, 2, so vector order is fixed."""
    v = [0.0] * DIM
    for axis, w in enumerate(weights):
        v[axis] = w
    return v


def _seed(db: psycopg.Connection) -> None:
    """Chunk ids after truncate: a/0 -> 1 (axis 0), a/1 -> 2 (axis 1), b/0 -> 3 (axis 2)."""
    upsert_document(db, _meta("a"))
    upsert_document(db, _meta("b", department="GC_WIDE"))
    replace_chunks(
        db,
        "a",
        [
            Chunk(0, "A > x", "Shared Services Canada delivers cloud brokering for departments."),
            Chunk(1, "A > y", "The PSPC procurement program manages contracting for the government."),
        ],
        [_unit(0), _unit(1)],
    )
    replace_chunks(
        db,
        "b",
        [Chunk(0, "B > z", "The Policy on Government Security applies to all departments.")],
        [_unit(2)],
    )


def test_chunk_strong_in_both_signals_ranks_first(db: psycopg.Connection) -> None:
    _seed(db)
    hits = search_hybrid(db, "cloud brokering", _mix(1.0, 0.5, 0.25), k=3)
    assert hits[0].chunk_id == 1


def test_keyword_signal_lifts_a_weaker_vector_match(db: psycopg.Connection) -> None:
    _seed(db)
    # Vector order alone: chunk 2, then 1, then 3. Keyword matches only chunk 1.
    hits = search_hybrid(db, "cloud brokering", _mix(0.5, 1.0, 0.25), k=3)
    assert [h.chunk_id for h in hits] == [1, 2, 3]


def test_keyword_empty_degrades_to_vector_ranking(db: psycopg.Connection) -> None:
    _seed(db)
    vec = _mix(0.25, 0.5, 1.0)
    hybrid_ids = [h.chunk_id for h in search_hybrid(db, "the of and", vec, k=3)]
    vector_ids = [h.chunk_id for h in search_vector(db, vec, k=3)]
    assert hybrid_ids == vector_ids == [3, 2, 1]


def test_hybrid_score_is_the_fusion_score(db: psycopg.Connection) -> None:
    _seed(db)
    top = search_hybrid(db, "cloud brokering", _mix(1.0, 0.5, 0.25), k=1)[0]
    assert abs(top.score - (1 / 61 + 1 / 61)) < 1e-9


def test_k_limits_results(db: psycopg.Connection) -> None:
    _seed(db)
    assert len(search_hybrid(db, "departments", _mix(1.0, 0.5, 0.25), k=2)) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_hybrid_search.py -q`
Expected: FAIL at import, `ImportError: cannot import name 'search_hybrid'`.

- [ ] **Step 3: Write the implementation**

Add to the imports at the top of `src/sovrag/retrieve/hybrid.py`:

```python
from dataclasses import dataclass, replace

from sovrag.retrieve.fusion import rrf
```

(`dataclass` is already imported; extend that line to include `replace`.)

Append after `search_keyword`:

```python
CANDIDATE_MIN = 20


def search_hybrid(
    conn: psycopg.Connection, query: str, query_embedding: Sequence[float], *, k: int
) -> list[Hit]:
    """Fuse vector and keyword rankings by position; the score reported is the fusion score."""
    window = max(4 * k, CANDIDATE_MIN)
    by_vector = search_vector(conn, query_embedding, k=window)
    by_keyword = search_keyword(conn, query, k=window)
    candidates = {h.chunk_id: h for h in [*by_vector, *by_keyword]}
    fused = rrf([[h.chunk_id for h in by_vector], [h.chunk_id for h in by_keyword]])
    return [replace(candidates[chunk_id], score=score) for chunk_id, score in fused[:k]]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_hybrid_search.py tests/test_keyword_search.py tests/test_vector_search.py tests/test_fusion.py -q`
Expected: `18 passed`.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src
git add src/sovrag/retrieve/hybrid.py tests/test_hybrid_search.py
git commit -m "Add hybrid search fusing vector and keyword rankings"
```

---

### Task 4: CLI mode selection

**Files:**
- Modify: `src/sovrag/cli.py` (imports at lines 1-18; `query` command at lines 55-88)
- Test: `tests/test_cli.py` (append three tests)

**Interfaces:**
- Consumes: `search_vector`, `search_keyword`, `search_hybrid` from `sovrag.retrieve.hybrid`; `Embedder.from_settings`.
- Produces: `Mode` enum (`vector`, `keyword`, `hybrid`) in `cli.py`; `--mode` option; JSON payload `mode` is the enum value string.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def _ingest(env: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["ingest", "--manifest", str(env / "manifest.yaml"), "--raw-dir", str(env / "raw")],
    )
    assert result.exit_code == 0, result.output


def test_keyword_mode_never_builds_the_embedder(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    _ingest(env)

    def boom(cls: type, settings: object) -> None:
        raise AssertionError("embedder constructed in keyword mode")

    monkeypatch.setattr(cli.Embedder, "from_settings", classmethod(boom))
    result = runner.invoke(
        cli.app,
        ["query", "identity management", "--principal", "analyst_dnd", "--mode", "keyword", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "keyword"
    assert payload["hits"][0]["doc_id"] == "directive-identity-management"
    assert all(h["score"] > 0 for h in payload["hits"])


def test_hybrid_mode_returns_fused_hits(env: Path) -> None:
    import json

    _ingest(env)
    result = runner.invoke(
        cli.app,
        ["query", "authorities for identity management", "--principal", "analyst_dnd", "--mode", "hybrid", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "hybrid"
    assert len(payload["hits"]) == 5
    assert payload["hits"][0]["doc_id"] == "directive-identity-management"


def test_unknown_mode_is_rejected(env: Path) -> None:
    result = runner.invoke(cli.app, ["query", "x", "--principal", "analyst_dnd", "--mode", "bm25"])
    assert result.exit_code != 0
    assert "bm25" in result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q`
Expected: the three new tests FAIL. Keyword and hybrid fail with exit code 2 and `only --mode vector is implemented yet` in the output; the unknown-mode test fails because the current code also exits non-zero but with "only --mode vector", so the `"bm25" in result.output` assertion is what fails. Confirm each failure message before moving on.

- [ ] **Step 3: Write the implementation**

Change the import line in `src/sovrag/cli.py`:

```python
from sovrag.retrieve.hybrid import search_hybrid, search_keyword, search_vector
```

Add after the imports, before `app = typer.Typer(...)`:

```python
from enum import Enum


class Mode(str, Enum):
    vector = "vector"
    keyword = "keyword"
    hybrid = "hybrid"
```

(Put `from enum import Enum` with the other standard-library imports; ruff's isort rule will insist.)

Replace the body of `query` from the `mode` parameter through the `with connect(...)` block:

```python
@app.command()
def query(
    question: str,
    principal: Annotated[str, typer.Option(help="Principal id from principals.yaml")],
    mode: Annotated[Mode, typer.Option(help="vector | keyword | hybrid")] = Mode.vector,
    k: Annotated[int, typer.Option()] = 5,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text")] = False,
) -> None:
    """Retrieve the top-k chunks for a question, as a principal. Generation is not wired in yet."""
    settings = get_settings()
    with connect(settings.database_url) as conn:
        if mode is Mode.keyword:
            hits = search_keyword(conn, question, k=k)
        else:
            query_vec = Embedder.from_settings(settings).embed([question])[0]
            if mode is Mode.vector:
                hits = search_vector(conn, query_vec, k=k)
            else:
                hits = search_hybrid(conn, question, query_vec, k=k)
```

Then in the JSON payload and the text header, use `mode.value` instead of `mode`:

```python
            "mode": mode.value,
```

```python
    typer.echo(f"question: {question}\nprincipal: {principal}  mode: {mode.value}  k: {k}\n")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q`
Expected: `6 passed`.

- [ ] **Step 5: Run the whole suite, lint, type-check, commit**

```bash
uv run pytest -q          # expected: 84 passed
uv run ruff check . && uv run ruff format --check . && uv run mypy src
git add src/sovrag/cli.py tests/test_cli.py
git commit -m "Select retrieval mode from the CLI: vector, keyword or hybrid"
```

---

### Task 5: Decision record, docs, and the eyeball comparison

**Files:**
- Create: `docs/adr/0007-keyword-ranking-not-bm25.md`
- Modify: `docs/architecture.md:20` (mermaid label), `docs/architecture.md:37` (status line), `docs/architecture.md:81-85` (decisions list)
- Modify: `README.md:5` (status line)
- Modify: `CLAUDE.md` Commands section (the `query` line and the "Not implemented yet" line)

**Interfaces:**
- Consumes: the CLI from Task 4 against the locally ingested corpus (`docker compose up -d postgres`, corpus already ingested).
- Produces: documentation only.

- [ ] **Step 1: Write ADR 0007**

```markdown
# ADR 0007: Keyword ranking uses Postgres ts_rank_cd, not BM25; hybrid fuses by rank

Status: accepted (2026-09-16)

## Context

Vector search handles paraphrase but is loose about exact tokens, and this corpus is full of exact tokens: acronyms, program names, fiscal years. A keyword signal is needed, and the schema already carries a generated `tsvector` column with a GIN index. The question is how to rank keyword matches and how to combine the two signals.

## Decision

Keyword mode parses the question with `websearch_to_tsquery('english', ...)`, matches with `@@`, and ranks with `ts_rank_cd`. The CLI mode is named `keyword`, not `bm25`, because `ts_rank_cd` is not BM25: it weights term frequency and proximity but has no corpus-wide inverse document frequency term and no BM25 length normalisation. Naming it BM25 would invite a fair correction.

Hybrid mode runs vector and keyword search with a candidate window of `max(4k, 20)` each and fuses by reciprocal rank fusion with k = 60. Fusion uses positions only, because cosine similarity and `ts_rank_cd` are not on comparable scales. It happens in Python over rows SQL already returned; it never filters.

## Consequences

- True BM25 in Postgres needs an extension (for example `pg_search` from ParadeDB), which means a different database image for the whole stack. That trade is deferred until the retrieval eval shows the keyword signal is the weak link.
- The retrieval eval will report hit@5, recall@10 and MRR for all three modes, so the value of fusion is measured rather than assumed.
- Keyword mode never loads the embedding model, which makes it the cheapest mode to run and a useful control in the eval.
```

- [ ] **Step 2: Update the architecture doc**

Line 20, change the retrieve node label to:

```
    QE --> RT[retrieve/hybrid.py<br/>vector, keyword, RRF]
```

Line 37, replace the status sentence with:

```
Status: the corpus pipeline and all three retrieval modes exist. Generation, entitlement filtering and the HTTP API follow.
```

Append to the Decisions list:

```
- ADR 0007: keyword ranking is Postgres `ts_rank_cd`, not BM25; hybrid fuses vector and keyword rankings by reciprocal rank fusion.
```

Add a short section after "## Corpus" and before "## Decisions":

```markdown
## Retrieval modes

`vector` orders chunks by cosine distance over the HNSW index. `keyword` parses the question with `websearch_to_tsquery` and ranks full-text matches with `ts_rank_cd`; a question of stop words only returns nothing. `hybrid` takes the top `max(4k, 20)` from each and fuses by reciprocal rank fusion (k = 60), so a chunk near the top of either list rises and one near the top of both rises furthest. All three return the same `Hit` with citations. See ADR 0007.
```

- [ ] **Step 3: Update README and CLAUDE.md**

`README.md` line 5:

```
Status: corpus indexed; vector, keyword and hybrid retrieval with citations from the CLI. No generation, no entitlement filtering yet.
```

`CLAUDE.md` Commands section, change the query line to:

```
- uv run sovrag query "..." --principal analyst_dnd --mode hybrid   # or vector | keyword
```

and the last line to:

```
- Not implemented yet: sovrag serve, sovrag eval, the mcp profile.
```

- [ ] **Step 4: Run the eyeball set in all three modes**

Run each of the ten questions below with `--json` in each mode and note the top-1 `doc_id`. Record the results as a table in the PR description: question, expected document, top-1 for vector, keyword, hybrid.

```bash
for m in vector keyword hybrid; do
  uv run sovrag query "What are Shared Services Canada's planned results for cloud services?" --principal analyst_dnd --mode $m --json | python3 -c "import sys,json; d=json.load(sys.stdin); print('$m', d['hits'][0]['doc_id'])"
done
```

Questions and expected documents:

1. What are Shared Services Canada's planned results for cloud services? -> an `ssc-dp-*` or `ssc-drr-*` document
2. What did the Auditor General find about cybersecurity of personal information in the cloud? -> an `oag-*` report
3. What does the Directive on Identity Management require of departments? -> `directive-identity-management`
4. How much did National Defence plan to spend in 2024-25? -> `dnd-dp-2024-25`
5. What are Public Safety Canada's core responsibilities? -> a `ps-dp-*` or `ps-drr-*` document
6. What is the Policy on Government Security's requirement for security screening? -> `policy-government-security`
7. What results did PSPC report on procurement modernisation? -> a `pspc-drr-*` document
8. What is Treasury Board Secretariat's role in digital government? -> a `tbs-dp-*` or `tbs-drr-*` document
9. What are the departmental results for the Canadian Armed Forces readiness? -> a `dnd-drr-*` document
10. Which policy instrument covers security event management? -> `directive-security-management` or `policy-government-security`

(Document ids are in `data/manifest.yaml`; adjust the expected id if the manifest names differ.)

- [ ] **Step 5: Verify, commit, open the PR**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src
git add docs/adr/0007-keyword-ranking-not-bm25.md docs/architecture.md README.md CLAUDE.md
git commit -m "Record the keyword ranking decision and document the three retrieval modes"
git push -u origin hybrid-retrieval
gh pr create --base main --title "Keyword and hybrid retrieval modes" --body-file <(cat <<'EOF'
## What changed
- `--mode vector|keyword|hybrid` on `sovrag query`. Keyword is Postgres full-text (`websearch_to_tsquery` + `ts_rank_cd`) over the existing tsv column; hybrid fuses vector and keyword rankings with reciprocal rank fusion (k = 60). All modes return the same Hit with citations.
- Keyword mode never loads the embedding model.
- ADR 0007 explains why the mode is named keyword rather than BM25.

## Eyeball set, top-1 document by mode
| # | Question | Expected | vector | keyword | hybrid |
|---|---|---|---|---|---|
(fill from Step 4)

## Verification
- `uv run pytest -q`: 84 passed
- ruff, mypy clean
EOF
)
```

---

## Self-review

- Spec coverage: `search_keyword` (Task 2), `search_hybrid` and candidate window (Task 3), `rrf` in its own module with deterministic ties (Task 1), CLI enum and embedder-only-when-needed (Task 4), filler-only degradation (Tasks 2 and 3 tests), ADR 0007, architecture and README updates, eyeball table (Task 5). No spec item without a task.
- Placeholders: none. Every code step carries the code.
- Type consistency: `rrf(rankings, *, k)` returns `list[tuple[int, float]]` and Task 3 consumes it as such; `search_keyword(conn, query, *, k)` and `search_hybrid(conn, query, query_embedding, *, k)` are used with the same signatures in Task 4; `Mode` values match the CLI test strings.
