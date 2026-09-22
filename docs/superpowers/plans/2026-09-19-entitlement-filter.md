# Entitlement Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every retrieval query carries a `WHERE` clause built from the caller's `Principal`, so a chunk the principal may not see never leaves the database, and every answer and audit line records the predicate that was applied.

**Architecture:** A new module `src/sovrag/retrieve/filters.py` turns a `Principal` into an `Entitlement`: a fixed SQL predicate with two bound arrays (the classification labels at or below the clearance, and the department memberships) and a `render()` for humans and the audit log. The three search functions in `src/sovrag/retrieve/hybrid.py` gain a required `principal` keyword and splice the predicate into their queries; `search_vector` also sets `hnsw.ef_search` and `hnsw.iterative_scan` with `SET LOCAL` inside a transaction so the filtered HNSW scan does not under-return. `answer()` passes its principal through and reports the rendered predicate as `applied_filter`. `sovrag query` resolves `--principal` the way `sovrag ask` already does, and both read the principals file path from settings.

**Tech Stack:** Python 3.14, psycopg 3, pgvector 0.8.6 (iterative scans exist from 0.8.0), pydantic-settings, typer, pytest, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-17-mattermost-integration-design.md` (sections "`sovrag.retrieve.filters`", "`sovrag.retrieve.hybrid`", "Tests", and item 3 under "Sequencing").

## Global Constraints

- Entitlement filtering happens in SQL, never in Python after retrieval (CLAUDE.md). The predicate is bound parameters, never string interpolation of principal values.
- A test must show that a principal without entitlement gets zero rows, not a shorter list (`.claude/rules/retrieval.md`).
- Every `Hit` keeps `doc_id`, `chunk_id`, `heading_path`, `source_url` and `score`.
- Classification labels are synthetic (ADR 0003). No copy may imply real protected data was used.
- `sovrag.auth` must not import from `sovrag.retrieve`, `sovrag.config` or `sovrag.cli`.
- Type hints everywhere; `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` and `uv run pytest -q` must pass before each commit. `ruff format` also formats the Python blocks in this plan and in the ADR, so run `uv run ruff format docs` before committing them.
- Tests first. Run the test, watch it fail for the expected reason, then implement.
- Line length 100 (ruff).
- Commit messages describe content only. No schedule or session references anywhere in the repo, and no paths outside the repo.
- End each commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Postgres tests need the Compose Postgres: `docker compose up -d postgres`. They skip when it is down, so check the pytest summary for `skipped` before treating a run as green.

## File Structure

| File | Responsibility |
|---|---|
| `docs/adr/0013-entitlement-predicate.md` (create) | The visibility rule, its SQL shape, and the iterative-scan decision |
| `src/sovrag/auth/principal.py` (modify) | `load_principals` turns YAML parse errors into `ValueError` |
| `src/sovrag/retrieve/filters.py` (create) | `PREDICATE`, `Entitlement`, `entitlement_for` |
| `src/sovrag/retrieve/hybrid.py` (modify) | `principal` keyword on the three search functions and `search`; predicate in every query; `SET LOCAL` in `search_vector` |
| `src/sovrag/retrieve/__init__.py` (modify) | Re-export `Entitlement` and `entitlement_for` |
| `src/sovrag/generate/answer.py` (modify) | Pass the principal to `search`; `applied_filter` is the rendered predicate |
| `src/sovrag/config.py` (modify) | `principals_path` setting |
| `src/sovrag/cli.py` (modify) | `query` resolves `--principal` and prints the filter; both commands read the path from settings |
| `.env.example` (modify) | `PRINCIPALS_PATH` |
| `tests/test_filters.py` (create) | Predicate, params, render; leak tests over the full classification-by-department grid; `SET LOCAL` scope |
| `tests/test_vector_search.py`, `tests/test_keyword_search.py`, `tests/test_hybrid_search.py` (modify) | Pass a principal |
| `tests/test_answer.py`, `tests/test_cli.py`, `tests/test_principal.py` (modify) | New behaviour of `answer`, `query`, and the loader |
| `README.md`, `CLAUDE.md`, `docs/architecture.md`, `.claude/rules/retrieval.md` (modify) | Status and description now that the filter exists |

## Branch Setup

Already done: worktree `.claude/worktrees/entitlement-filter` on branch `entitlement-filter` from `origin/main` at 79cff2f. Before Task 1:

```bash
uv sync --all-extras
docker compose up -d postgres
uv run pytest -q
```

Expected: the existing suite passes with no skips.

---

### Task 1: ADR 0013, the entitlement predicate

**Files:**
- Create: `docs/adr/0013-entitlement-predicate.md`
- Modify: `docs/architecture.md` (the "Decisions" list, after the ADR 0012 line)

**Interfaces:** none. The ADR names the rule the code in Tasks 3 and 4 implements.

- [ ] **Step 1: Write the ADR**

````markdown
# ADR 0013: The entitlement predicate is one bound WHERE clause inside every retrieval query

Status: accepted (2026-09-19)

## Context

The README promises a two-axis entitlement model, classification level and department,
enforced as a SQL pre-filter and never as a Python post-filter, with a leak-test suite
proving that a principal can never retrieve a chunk they are not entitled to. ADR 0012 gave
the principal its two axes. This decision fixes the rule that maps a principal to a set of
visible documents, and where that rule runs.

Two facts about the store shape the decision. Classification and department live on
`documents`, and every retrieval query already joins `chunks` to `documents` for the
citation fields, so the predicate has its columns at hand. And vector search is an HNSW index
scan: pgvector applies a `WHERE` clause after the scan, so a principal who may see few
documents can receive fewer than `k` rows unless the scan is told to continue.

## Decision

**The visibility rule.** A document is visible to a principal when its classification is at
or below the principal's clearance, and one of three things holds: the document is
`UNCLASSIFIED`, or its department is `GC_WIDE`, or its department is one of the principal's
memberships. In words: unclassified material is open to everyone, and protected material is
need-to-know by department, with government-wide material open to anyone cleared for it.

**One predicate, two bound arrays.** The rule is a single SQL fragment, fixed at import
time:

```sql
d.classification = any(%(cls)s)
and (d.classification = 'UNCLASSIFIED' or d.department = 'GC_WIDE' or d.department = any(%(depts)s))
```

`cls` is the list of labels at or below the clearance and `depts` the sorted memberships.
Both are bound parameters. No principal value is ever interpolated into SQL, so the fragment
is the same string for every principal and a reviewer can read it once.

**Applied inside every query.** `search_vector`, `search_keyword` and `search_hybrid` each
require a `principal` keyword and put the predicate in their `WHERE` clause. Hybrid
retrieval passes the principal to both arms and fuses only what they return; fusion never
filters. There is no retrieval path without a principal, and the type system enforces it: a
call without one is a `TypeError`.

**The HNSW scan continues past the filter.** `search_vector` runs inside a transaction and
sets `hnsw.iterative_scan = relaxed_order` and `hnsw.ef_search` with `SET LOCAL`, so the
settings end with the query and never leak to the connection. With iterative scanning,
pgvector keeps scanning the index until `k` rows pass the `WHERE` clause or
`hnsw.max_scan_tuples` (default 20,000, more than the corpus) is reached. Relaxed order can
return the `k` rows slightly out of distance order, so the query sorts them again by score
in an outer `SELECT`; that is `k` rows, not the index.

**The rendered predicate travels with the answer.** `Entitlement.render()` substitutes the
values for a human, for example `classification in (UNCLASSIFIED, PROTECTED_A) and
(classification = UNCLASSIFIED or department in (GC_WIDE, DND))`. `answer()` reports it as
`Answer.applied_filter` and writes it to the audit line, and `sovrag query` prints it, so
every result says which rule produced it.

## Consequences

- Against the indexed corpus, `analyst_dnd` sees 16 of 27 documents and is forbidden 11: the
  other departments' Departmental Results Reports and the three Auditor General reports.
- The leak tests seed one document per classification-and-department cell and assert the
  exact visible set for each shipped principal, and that a forbidden document is zero rows
  even when it is the nearest neighbour or the only keyword match.
- `sovrag query --principal` now resolves the id and rejects an unknown one as a usage error,
  the same as `sovrag ask`. The path to the principals file moves into settings
  (`PRINCIPALS_PATH`), as ADR 0012 anticipated.
- Callers that stub `search` in tests must accept the `principal` keyword.
- `hnsw.ef_search` is now set on every vector query, not only when `k` exceeds the default,
  because `SET LOCAL` costs nothing and one code path is easier to verify than two.
````

- [ ] **Step 2: Add the decision to the architecture doc**

In `docs/architecture.md`, under "## Decisions", after the line for ADR 0012, add:

```markdown
- ADR 0013: the entitlement predicate is one bound WHERE clause inside every retrieval query; HNSW scans continue past the filter with `SET LOCAL hnsw.iterative_scan`.
```

- [ ] **Step 3: Format and commit**

```bash
uv run ruff format docs
uv run ruff format --check .
git add docs/adr/0013-entitlement-predicate.md docs/architecture.md docs/superpowers/plans/2026-09-19-entitlement-filter.md
git commit -m "Decide the entitlement predicate and where it runs (ADR 0013)"
```

---

### Task 2: The principals loader reports a broken file as a `ValueError`

**Files:**
- Modify: `src/sovrag/auth/principal.py:100-102`
- Test: `tests/test_principal.py`

**Interfaces:**
- Consumes: `load_principals(path)`, `_UniqueKeyLoader`.
- Produces: `load_principals` raises `ValueError` for any file YAML cannot parse or that has an unhashable key, in addition to the shape errors it already reports.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_principal.py`:

```python
@pytest.mark.parametrize(
    "body",
    [
        "principals:\n  a:\n    clearance: [UNCLASSIFIED\n",  # unterminated flow sequence
        "principals:\n\t a:\n    clearance: UNCLASSIFIED\n",  # tab indentation
        "principals:\n  ? [a, b]\n  : {clearance: UNCLASSIFIED}\n",  # unhashable key
    ],
)
def test_a_file_yaml_cannot_parse_is_a_valueerror_naming_the_path(
    tmp_path: Path, body: str
) -> None:
    path = _write(tmp_path, body)
    with pytest.raises(ValueError, match=rf"{path.name}: not a valid principals file"):
        load_principals(path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_principal.py -q -k yaml_cannot_parse`
Expected: three failures, each raising `yaml.YAMLError` (a `ScannerError` or `ParserError`) or `TypeError`, not `ValueError`.

- [ ] **Step 3: Wrap the parse**

In `src/sovrag/auth/principal.py`, replace the first line of `load_principals`'s body:

```python
    raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
```

with:

```python
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, TypeError) as exc:
        raise ValueError(f"{path}: not a valid principals file: {exc}") from exc
```

`yaml.YAMLError` covers scanner, parser and constructor errors. `TypeError` is what `_UniqueKeyLoader.construct_mapping` raises when it checks `key in seen` for an unhashable key.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_principal.py -q`
Expected: all pass, including the duplicate-key tests, which still raise `ValueError` (the `ValueError` from `construct_mapping` is not caught by the new `except`).

- [ ] **Step 5: Commit**

```bash
git add src/sovrag/auth/principal.py tests/test_principal.py
git commit -m "Report a principals file YAML cannot parse as a ValueError"
```

---

### Task 3: `Entitlement` and `entitlement_for`

**Files:**
- Create: `src/sovrag/retrieve/filters.py`
- Modify: `src/sovrag/retrieve/__init__.py`
- Test: `tests/test_filters.py`

**Interfaces:**
- Consumes: `Principal`, `Clearance`, `GC_WIDE` from `sovrag.auth`.
- Produces, importable from `sovrag.retrieve`:
  - `PREDICATE: str`, the fixed SQL fragment with `%(cls)s` and `%(depts)s` placeholders.
  - `@dataclass(frozen=True) class Entitlement` with `clearance: Clearance`, `departments: frozenset[str]`, `sql: str`, `params: dict[str, list[str]]`, and `render() -> str`.
  - `entitlement_for(principal: Principal) -> Entitlement`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_filters.py`:

```python
"""The entitlement predicate: bound parameters per principal, and leak tests against the store."""

from __future__ import annotations

import pytest

from sovrag.auth import LEAST_PRIVILEGED, Clearance, Principal, load_principals
from sovrag.retrieve import Entitlement, entitlement_for

SHIPPED = load_principals()

PUBLIC_FILTER = (
    "classification in (UNCLASSIFIED)"
    " and (classification = UNCLASSIFIED or department in (GC_WIDE))"
)
ANALYST_DND_FILTER = (
    "classification in (UNCLASSIFIED, PROTECTED_A)"
    " and (classification = UNCLASSIFIED or department in (GC_WIDE, DND))"
)


@pytest.mark.parametrize(
    ("principal_id", "cls", "depts"),
    [
        ("public", ["UNCLASSIFIED"], []),
        ("analyst_dnd", ["UNCLASSIFIED", "PROTECTED_A"], ["DND"]),
        ("analyst_ps", ["UNCLASSIFIED", "PROTECTED_A"], ["PS"]),
        ("cio_ssc", ["UNCLASSIFIED", "PROTECTED_A"], ["SSC", "TBS"]),
        (
            "auditor",
            ["UNCLASSIFIED", "PROTECTED_A", "PROTECTED_B"],
            ["CSE", "DND", "PS", "PSPC", "SSC", "TBS"],
        ),
    ],
)
def test_shipped_principals_bind_their_labels_and_departments(
    principal_id: str, cls: list[str], depts: list[str]
) -> None:
    ent = entitlement_for(SHIPPED[principal_id])
    assert isinstance(ent, Entitlement)
    assert ent.params == {"cls": cls, "depts": depts}
    assert ent.clearance is SHIPPED[principal_id].clearance
    assert ent.departments == SHIPPED[principal_id].departments


def test_least_privileged_binds_the_same_as_public() -> None:
    assert entitlement_for(LEAST_PRIVILEGED).params == entitlement_for(SHIPPED["public"]).params


def test_rendered_predicate_reads_as_the_spec_example() -> None:
    assert entitlement_for(SHIPPED["analyst_dnd"]).render() == ANALYST_DND_FILTER
    assert entitlement_for(SHIPPED["public"]).render() == PUBLIC_FILTER


def test_sql_is_fixed_and_binds_values_instead_of_interpolating() -> None:
    who = Principal("x", Clearance.PROTECTED_B, frozenset({"CSE", "PSPC"}))
    ent = entitlement_for(who)
    assert ent.sql == entitlement_for(SHIPPED["public"]).sql
    assert "%(cls)s" in ent.sql and "%(depts)s" in ent.sql
    for value in ("CSE", "PSPC", "PROTECTED_B", "x"):
        assert value not in ent.sql
    assert "'GC_WIDE'" in ent.sql and "'UNCLASSIFIED'" in ent.sql


def test_entitlement_is_frozen() -> None:
    ent = entitlement_for(SHIPPED["public"])
    with pytest.raises(AttributeError):
        ent.sql = "true"  # type: ignore[misc]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_filters.py -q`
Expected: `ImportError: cannot import name 'Entitlement' from 'sovrag.retrieve'`.

- [ ] **Step 3: Implement the module**

Create `src/sovrag/retrieve/filters.py`:

```python
"""The entitlement predicate: one bound SQL WHERE clause built from a Principal.

A document is visible when its classification is at or below the principal's clearance,
and it is unclassified, government-wide, or in one of the principal's departments.
Unclassified material is open to everyone; protected material is need-to-know by
department. The fragment is fixed at import time and the values are bound parameters, so
no principal value is ever interpolated into SQL (ADR 0013).
"""

from __future__ import annotations

from dataclasses import dataclass

from sovrag.auth import GC_WIDE, Clearance, Principal

PREDICATE = (
    "d.classification = any(%(cls)s)"
    f" and (d.classification = '{Clearance.UNCLASSIFIED.value}'"
    f" or d.department = '{GC_WIDE}' or d.department = any(%(depts)s))"
)


@dataclass(frozen=True)
class Entitlement:
    """What a principal may see, as SQL. `params` binds `%(cls)s` and `%(depts)s` in `sql`."""

    clearance: Clearance
    departments: frozenset[str]
    sql: str
    params: dict[str, list[str]]

    def render(self) -> str:
        """The predicate with values substituted, for people and the audit log."""
        labels = ", ".join(self.params["cls"])
        departments = ", ".join([GC_WIDE, *self.params["depts"]])
        return (
            f"classification in ({labels})"
            f" and (classification = {Clearance.UNCLASSIFIED.value}"
            f" or department in ({departments}))"
        )


def entitlement_for(principal: Principal) -> Entitlement:
    labels = [c.value for c in Clearance if c.rank <= principal.clearance.rank]
    return Entitlement(
        clearance=principal.clearance,
        departments=principal.departments,
        sql=PREDICATE,
        params={"cls": labels, "depts": sorted(principal.departments)},
    )
```

Replace `src/sovrag/retrieve/__init__.py` with:

```python
"""Retrieval: vector, keyword and hybrid search over the pgvector store, the Mode enum, and
the entitlement predicate every search applies."""

from sovrag.retrieve.filters import PREDICATE, Entitlement, entitlement_for
from sovrag.retrieve.hybrid import Hit, Mode, search, search_hybrid, search_keyword, search_vector

__all__ = [
    "PREDICATE",
    "Entitlement",
    "Hit",
    "Mode",
    "entitlement_for",
    "search",
    "search_hybrid",
    "search_keyword",
    "search_vector",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_filters.py -q && uv run mypy src && uv run ruff check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/sovrag/retrieve/filters.py src/sovrag/retrieve/__init__.py tests/test_filters.py
git commit -m "Build the entitlement predicate from a principal as bound SQL parameters"
```

---

### Task 4: Every search function requires a principal and applies the predicate

**Files:**
- Modify: `src/sovrag/retrieve/hybrid.py`
- Modify: `tests/test_vector_search.py`, `tests/test_keyword_search.py`, `tests/test_hybrid_search.py`
- Test: `tests/test_filters.py` (leak tests appended)

**Interfaces:**
- Consumes: `entitlement_for` from Task 3.
- Produces:
  - `search_vector(conn, query_embedding, *, k: int, principal: Principal) -> list[Hit]`
  - `search_keyword(conn, query, *, k: int, principal: Principal) -> list[Hit]`
  - `search_hybrid(conn, query, query_embedding, *, k: int, principal: Principal) -> list[Hit]`
  - `search(conn, question, *, mode: Mode, k: int, embed_query, principal: Principal) -> list[Hit]`
  - `HNSW_EF_SEARCH_MAX = 1000`

- [ ] **Step 1: Make the existing search tests pass a principal**

The seeded documents in the three files are `UNCLASSIFIED`, so the least privileged principal sees all of them and every existing assertion stands. In each of `tests/test_vector_search.py`, `tests/test_keyword_search.py` and `tests/test_hybrid_search.py`:

Add the import after the existing `from sovrag.corpus.chunk import Chunk` line:

```python
from sovrag.auth import LEAST_PRIVILEGED
```

Then add `, principal=LEAST_PRIVILEGED` to every call of `search_vector`, `search_keyword`, `search_hybrid` and `search` in those files. Examples of the resulting calls:

```python
hits = search_vector(db, query, k=2, principal=LEAST_PRIVILEGED)
hits = search_keyword(db, "PSPC procurement", k=5, principal=LEAST_PRIVILEGED)
hits = search_hybrid(db, "cloud brokering", _mix(1.0, 0.5, 0.25), k=3, principal=LEAST_PRIVILEGED)
kw = search(
    db,
    "cloud brokering",
    mode=Mode.keyword,
    k=3,
    embed_query=embed_query,
    principal=LEAST_PRIVILEGED,
)
```

Use `grep -n "search_vector(\|search_keyword(\|search_hybrid(\|search(" tests/test_vector_search.py tests/test_keyword_search.py tests/test_hybrid_search.py` to find every call; the count must match the number of edits.

- [ ] **Step 2: Write the leak tests**

Replace the import block at the top of `tests/test_filters.py` with:

```python
from __future__ import annotations

import psycopg
import pytest

from sovrag.auth import DEPARTMENTS, LEAST_PRIVILEGED, Clearance, Principal, load_principals
from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.index.store import replace_chunks, upsert_document
from sovrag.retrieve import (
    Entitlement,
    Mode,
    entitlement_for,
    search,
    search_hybrid,
    search_keyword,
    search_vector,
)
```

Then append to the file:

```python
# Leak tests: one document per classification-and-department cell, one chunk each, whose
# embedding is a distinct unit axis and whose text carries a distinct marker word.

DIM = 1024
GRID = [(cls, dept) for cls in Clearance for dept in sorted(DEPARTMENTS)]


def _doc_id(cls: Clearance, dept: str) -> str:
    return f"{cls.value}-{dept}".lower()


def _unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def _every_axis() -> list[float]:
    """Equally close to every seeded chunk, so k covers the whole visible set."""
    v = [0.0] * DIM
    for axis in range(len(GRID)):
        v[axis] = 1.0
    return v


def _seed_grid(db: psycopg.Connection) -> None:
    for n, (cls, dept) in enumerate(GRID):
        doc_id = _doc_id(cls, dept)
        upsert_document(
            db,
            DocumentMeta(
                id=doc_id,
                title=f"Title of {doc_id}",
                department=dept,
                doc_type="DP",
                fiscal_year="2024-25",
                lang="en",
                source_url=f"https://example.test/{doc_id}.html",
                pages=(),
                classification=cls.value,
            ),
        )
        replace_chunks(db, doc_id, [Chunk(0, "Budget", f"budget marker{n}")], [_unit(n)])


UNCLASSIFIED_ALL = {_doc_id(Clearance.UNCLASSIFIED, d) for d in DEPARTMENTS}
VISIBLE: dict[str, set[str]] = {
    "public": UNCLASSIFIED_ALL,
    "analyst_dnd": UNCLASSIFIED_ALL | {"protected_a-gc_wide", "protected_a-dnd"},
    "analyst_ps": UNCLASSIFIED_ALL | {"protected_a-gc_wide", "protected_a-ps"},
    "cio_ssc": UNCLASSIFIED_ALL | {"protected_a-gc_wide", "protected_a-ssc", "protected_a-tbs"},
    "auditor": {_doc_id(cls, dept) for cls, dept in GRID},
}


def test_the_grid_and_the_expected_sets_are_the_right_size() -> None:
    assert len(GRID) == 21
    assert {len(v) for v in VISIBLE.values()} == {7, 9, 10, 21}


@pytest.mark.parametrize("principal_id", sorted(VISIBLE))
def test_vector_search_returns_exactly_the_visible_documents(
    db: psycopg.Connection, principal_id: str
) -> None:
    _seed_grid(db)
    hits = search_vector(db, _every_axis(), k=30, principal=SHIPPED[principal_id])
    assert {h.doc_id for h in hits} == VISIBLE[principal_id]
    assert len(hits) == len(VISIBLE[principal_id])


@pytest.mark.parametrize("principal_id", sorted(VISIBLE))
def test_keyword_search_returns_exactly_the_visible_documents(
    db: psycopg.Connection, principal_id: str
) -> None:
    _seed_grid(db)
    hits = search_keyword(db, "budget", k=30, principal=SHIPPED[principal_id])
    assert {h.doc_id for h in hits} == VISIBLE[principal_id]
    assert len(hits) == len(VISIBLE[principal_id])


@pytest.mark.parametrize("principal_id", sorted(VISIBLE))
def test_hybrid_search_returns_exactly_the_visible_documents(
    db: psycopg.Connection, principal_id: str
) -> None:
    _seed_grid(db)
    hits = search_hybrid(db, "budget", _every_axis(), k=30, principal=SHIPPED[principal_id])
    assert {h.doc_id for h in hits} == VISIBLE[principal_id]
    assert len(hits) == len(VISIBLE[principal_id])


def _forbidden(principal_id: str) -> list[tuple[int, str]]:
    return [
        (n, _doc_id(cls, dept))
        for n, (cls, dept) in enumerate(GRID)
        if _doc_id(cls, dept) not in VISIBLE[principal_id]
    ]


@pytest.mark.parametrize("principal_id", ["public", "analyst_dnd", "cio_ssc"])
def test_a_forbidden_document_is_zero_rows_even_as_the_nearest_neighbour(
    db: psycopg.Connection, principal_id: str
) -> None:
    _seed_grid(db)
    who = SHIPPED[principal_id]
    forbidden = _forbidden(principal_id)
    assert forbidden
    for axis, doc_id in forbidden:
        by_vector = search_vector(db, _unit(axis), k=1, principal=who)
        assert doc_id not in {h.doc_id for h in by_vector}
        assert search_keyword(db, f"marker{axis}", k=5, principal=who) == []
        by_hybrid = search_hybrid(db, f"marker{axis}", _unit(axis), k=5, principal=who)
        assert doc_id not in {h.doc_id for h in by_hybrid}


def test_hybrid_never_returns_what_either_arm_filtered(db: psycopg.Connection) -> None:
    _seed_grid(db)
    who = SHIPPED["analyst_dnd"]
    hits = search_hybrid(db, "budget", _every_axis(), k=30, principal=who)
    assert {h.doc_id for h in hits} == VISIBLE["analyst_dnd"]
    assert all(h.classification != "PROTECTED_B" for h in hits)


def test_the_dispatcher_passes_the_principal(db: psycopg.Connection) -> None:
    _seed_grid(db)
    hits = search(
        db,
        "budget",
        mode=Mode.hybrid,
        k=30,
        embed_query=lambda q: _every_axis(),
        principal=SHIPPED["public"],
    )
    assert {h.doc_id for h in hits} == UNCLASSIFIED_ALL


def test_search_functions_require_a_principal(db: psycopg.Connection) -> None:
    with pytest.raises(TypeError, match="principal"):
        search_vector(db, _unit(0), k=1)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="principal"):
        search_keyword(db, "budget", k=1)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="principal"):
        search_hybrid(db, "budget", _unit(0), k=1)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="principal"):
        search(db, "budget", mode=Mode.keyword, k=1, embed_query=lambda q: _unit(0))  # type: ignore[call-arg]


def test_hnsw_settings_do_not_outlive_the_query(db: psycopg.Connection) -> None:
    _seed_grid(db)
    search_vector(db, _every_axis(), k=50, principal=SHIPPED["auditor"])
    assert db.execute("show hnsw.ef_search").fetchone() == ("40",)
    assert db.execute("show hnsw.iterative_scan").fetchone() == ("off",)


def test_hits_keep_their_citation_fields(db: psycopg.Connection) -> None:
    _seed_grid(db)
    (hit,) = search_vector(db, _unit(0), k=1, principal=SHIPPED["public"])
    assert hit.doc_id == "unclassified-cse"
    assert hit.heading_path == "Budget"
    assert hit.source_url == "https://example.test/unclassified-cse.html"
    assert hit.chunk_id > 0 and hit.score > 0
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_filters.py tests/test_vector_search.py tests/test_keyword_search.py tests/test_hybrid_search.py -q`
Expected: every call that passes `principal=` fails with `TypeError: ... got an unexpected keyword argument 'principal'`; `test_search_functions_require_a_principal` fails because no `TypeError` is raised.

- [ ] **Step 4: Implement the filtered queries**

Replace the module docstring, `_SELECT`, the `HNSW_EF_SEARCH_DEFAULT` comment and the four functions in `src/sovrag/retrieve/hybrid.py` so the file reads:

```python
"""Retrieval over the pgvector store.

Three modes over the same Hit shape: vector (cosine over the HNSW index), keyword (Postgres
full-text match on the generated tsv column, ranked by ts_rank_cd) and hybrid (both, fused
by reciprocal rank fusion). Every query carries the caller's entitlement predicate as a
WHERE clause, so a chunk the principal may not see never leaves the database (ADR 0013).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

import psycopg
from pgvector import Vector

from sovrag.auth import Principal
from sovrag.retrieve.filters import entitlement_for
from sovrag.retrieve.fusion import rrf


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    doc_id: str
    ordinal: int
    heading_path: str
    text: str
    title: str
    department: str
    classification: str
    doc_type: str
    source_url: str
    score: float


class Mode(StrEnum):
    vector = "vector"
    keyword = "keyword"
    hybrid = "hybrid"


_SELECT = """
select c.id, c.doc_id, c.ordinal, c.heading_path, c.text,
       d.title, d.department, d.classification, d.doc_type, d.source_url,
       {score} as score
from chunks c
join documents d on d.id = c.doc_id
where {entitlement}
"""


# pgvector's HNSW scan returns at most hnsw.ef_search candidates (default 40) before the
# WHERE clause is applied. search_vector raises ef_search to k and turns on iterative
# scanning for the duration of its transaction, so a filtered query still returns k rows.
HNSW_EF_SEARCH_DEFAULT = 40
HNSW_EF_SEARCH_MAX = 1000


def search_vector(
    conn: psycopg.Connection,
    query_embedding: Sequence[float],
    *,
    k: int,
    principal: Principal,
) -> list[Hit]:
    entitlement = entitlement_for(principal)
    nearest = (
        _SELECT.format(score="1 - (c.embedding <=> %(q)s)", entitlement=entitlement.sql)
        + " order by c.embedding <=> %(q)s limit %(k)s"
    )
    # relaxed_order may hand back the k rows slightly out of distance order; sort those k.
    sql = f"with nearest as materialized ({nearest}) select * from nearest order by score desc, id"
    params = {"q": Vector(list(query_embedding)), "k": k, **entitlement.params}
    ef_search = max(HNSW_EF_SEARCH_DEFAULT, min(int(k), HNSW_EF_SEARCH_MAX))
    with conn.transaction():
        conn.execute(f"set local hnsw.ef_search = {ef_search}")
        conn.execute("set local hnsw.iterative_scan = relaxed_order")
        rows = conn.execute(sql, params).fetchall()
    return [Hit(*row) for row in rows]


_TSQUERY = "websearch_to_tsquery('english', %(q)s)"


def search_keyword(
    conn: psycopg.Connection, query: str, *, k: int, principal: Principal
) -> list[Hit]:
    """Full-text match ranked by ts_rank_cd. Every term must match: a query of stop words
    only, or any question with one term absent from a chunk, returns no rows."""
    entitlement = entitlement_for(principal)
    sql = (
        _SELECT.format(score=f"ts_rank_cd(c.tsv, {_TSQUERY})", entitlement=entitlement.sql)
        + f" and c.tsv @@ {_TSQUERY} order by score desc, c.id limit %(k)s"
    )
    params = {"q": query, "k": k, **entitlement.params}
    rows = conn.execute(sql, params).fetchall()
    return [Hit(*row) for row in rows]


CANDIDATE_MIN = 20


def search_hybrid(
    conn: psycopg.Connection,
    query: str,
    query_embedding: Sequence[float],
    *,
    k: int,
    principal: Principal,
) -> list[Hit]:
    """Fuse vector and keyword rankings by position; the score reported is the fusion score."""
    window = max(4 * k, CANDIDATE_MIN)
    by_vector = search_vector(conn, query_embedding, k=window, principal=principal)
    by_keyword = search_keyword(conn, query, k=window, principal=principal)
    # Both arms applied the entitlement predicate in SQL; fusion only orders what they returned.
    candidates = {h.chunk_id: h for h in [*by_vector, *by_keyword]}
    fused = rrf([[h.chunk_id for h in by_vector], [h.chunk_id for h in by_keyword]])
    return [replace(candidates[chunk_id], score=score) for chunk_id, score in fused[:k]]


def search(
    conn: psycopg.Connection,
    question: str,
    *,
    mode: Mode,
    k: int,
    embed_query: Callable[[str], Sequence[float]],
    principal: Principal,
) -> list[Hit]:
    """Dispatch on mode. embed_query is called only when a vector is needed."""
    if mode is Mode.keyword:
        return search_keyword(conn, question, k=k, principal=principal)
    query_vec = embed_query(question)
    if mode is Mode.vector:
        return search_vector(conn, query_vec, k=k, principal=principal)
    return search_hybrid(conn, question, query_vec, k=k, principal=principal)
```

Notes for the implementer:
- `conn.transaction()` opens a transaction block on the autocommit connections `connect()` and the `db` fixture create, so `SET LOCAL` is legal and ends with the block. Inside a caller's open transaction it becomes a savepoint, and the settings last until that outer transaction ends, which is still per call.
- `SET LOCAL` cannot take a bound parameter, which is why `ef_search` is formatted in; it is an integer computed from `k`, never caller text.
- The outer `select * from nearest order by score desc, id` re-sorts only the `k` rows the CTE returned. The `id` column is `c.id` from the inner select.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_filters.py tests/test_vector_search.py tests/test_keyword_search.py tests/test_hybrid_search.py -q && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: all pass. If `test_hnsw_settings_do_not_outlive_the_query` fails with `("relaxed_order",)`, the transaction block is not being used; check that `conn.transaction()` wraps all three statements.

- [ ] **Step 6: Run the whole suite and note what breaks**

Run: `uv run pytest -q`
Expected: failures only in `tests/test_answer.py` and `tests/test_cli.py` (`search() missing 1 required keyword-only argument: 'principal'`). Those are Tasks 5 and 6. Do not fix them here.

- [ ] **Step 7: Commit**

```bash
git add src/sovrag/retrieve/hybrid.py tests/test_filters.py tests/test_vector_search.py tests/test_keyword_search.py tests/test_hybrid_search.py
git commit -m "Apply the entitlement predicate inside every search query"
```

---

### Task 5: `answer()` passes the principal through and reports the applied filter

**Files:**
- Modify: `src/sovrag/generate/answer.py`
- Test: `tests/test_answer.py`

**Interfaces:**
- Consumes: `search(..., principal=)` from Task 4, `entitlement_for` from Task 3.
- Produces: `Answer.applied_filter` and the `answer` audit event's `applied_filter` are `entitlement_for(principal).render()`, never `None`.

- [ ] **Step 1: Update the tests**

In `tests/test_answer.py`, after the `WHO = ...` line add:

```python
WHO_FILTER = (
    "classification in (UNCLASSIFIED, PROTECTED_A)"
    " and (classification = UNCLASSIFIED or department in (GC_WIDE, DND))"
)
```

In `_wire`, make `fake_search` check the principal:

```python
    def fake_search(conn: object, q: str, **kw: object) -> list[Hit]:
        assert kw["principal"] is WHO
        if kw["mode"] is not Mode.keyword:
            kw["embed_query"](q)  # type: ignore[operator]
        return list(hits[: kw["k"]])  # type: ignore[misc]
```

In `test_cited_answer_builds_citations_for_cited_hits_only`, change `applied_filter=None,` to `applied_filter=WHO_FILTER,` and add after the `assert line["refused"] is False and line["reason"] is None` line:

```python
    assert line["applied_filter"] == WHO_FILTER
```

In `test_k_is_clamped_to_max_k`, the local `fake_search` also receives `principal`; it uses `**kw`, so no edit is needed. In the test that sets `search` to a lambda (`lambda conn, q, **kw: [_hit(1)][: kw["k"]]`), no edit is needed either.

Add a test at the end of the file:

```python
def test_refusal_still_reports_the_applied_filter(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    _wire(monkeypatch, [], ["unused"])
    result = answer("q", principal=WHO)
    assert result.refused and result.applied_filter == WHO_FILTER
    (line,) = _audit_lines(audit_path)
    assert line["applied_filter"] == WHO_FILTER
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_answer.py -q`
Expected: `KeyError: 'principal'` from `fake_search`, and `applied_filter` mismatches.

- [ ] **Step 3: Implement**

In `src/sovrag/generate/answer.py`:

Change the docstring's last two sentences to:

```python
The principal is passed in by the caller, which is responsible for binding it (ADR 0009).
Entitlement filtering happens inside the search functions (ADR 0013); this module only
reports the predicate that was applied.
```

Change the import line `from sovrag.retrieve import Hit, Mode, search` to:

```python
from sovrag.retrieve import Hit, Mode, entitlement_for, search
```

In `answer()`, after `k = max(1, min(k, MAX_K))` add:

```python
    applied_filter = entitlement_for(principal).render()
```

In the `search(` call, replace the comment line `# entitlement: pass principal here once the filter lands` with:

```python
principal = (principal,)
```

In both `audit(...)` and the `Answer(...)` construction inside `finish`, change `applied_filter=None,` to `applied_filter=applied_filter,`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_answer.py -q && uv run mypy src && uv run ruff check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/sovrag/generate/answer.py tests/test_answer.py
git commit -m "Pass the principal into retrieval and report the applied filter on every answer"
```

---

### Task 6: `sovrag query` resolves its principal; the principals path is a setting

**Files:**
- Modify: `src/sovrag/config.py`
- Modify: `src/sovrag/cli.py`
- Modify: `.env.example`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `resolve_principal(principal_id, path)` from `sovrag.auth`; `search(..., principal=)`; `entitlement_for`.
- Produces: `Settings.principals_path: str = "data/principals.yaml"` (env `PRINCIPALS_PATH`); `sovrag query` and `sovrag ask` both resolve `--principal` against it and reject an unknown id as a usage error; `sovrag query` prints `filter: <rendered predicate>` and its JSON carries `applied_filter`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
ANALYST_DND_FILTER = (
    "classification in (UNCLASSIFIED, PROTECTED_A)"
    " and (classification = UNCLASSIFIED or department in (GC_WIDE, DND))"
)


def test_query_unknown_principal_is_a_usage_error(env: Path) -> None:
    result = runner.invoke(cli.app, ["query", "x", "--principal", "nobody"])
    assert result.exit_code == 2
    assert "unknown principal" in result.output


def test_query_prints_the_applied_filter(env: Path) -> None:
    _ingest(env)
    result = runner.invoke(cli.app, ["query", "identity", "--principal", "analyst_dnd"])
    assert result.exit_code == 0, result.output
    assert f"filter: {ANALYST_DND_FILTER}" in result.output


def test_query_json_carries_the_applied_filter(env: Path) -> None:
    import json

    _ingest(env)
    result = runner.invoke(cli.app, ["query", "identity", "--principal", "analyst_dnd", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["applied_filter"] == ANALYST_DND_FILTER


def test_principals_file_comes_from_settings(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ingest(env)
    principals = env / "principals.yaml"
    principals.write_text("principals:\n  tester:\n    clearance: UNCLASSIFIED\n")
    monkeypatch.setenv("PRINCIPALS_PATH", str(principals))
    ok = runner.invoke(cli.app, ["query", "identity", "--principal", "tester"])
    assert ok.exit_code == 0, ok.output
    missing = runner.invoke(cli.app, ["query", "identity", "--principal", "analyst_dnd"])
    assert missing.exit_code == 2
    assert "unknown principal" in missing.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q`
Expected: the existing `query` tests fail with `TypeError: search() missing 1 required keyword-only argument: 'principal'`; the new ones fail on exit code or missing output.

- [ ] **Step 3: Add the setting**

In `src/sovrag/config.py`, after the `audit_log_path` line add:

```python
    # Named principals for the CLI and the MCP server's static mode (ADR 0012).
    principals_path: str = "data/principals.yaml"
```

In `.env.example`, after the `AUDIT_LOG_PATH` block add:

```
# Named principals (YAML). Both `sovrag query` and `sovrag ask` resolve --principal here.
PRINCIPALS_PATH=data/principals.yaml
```

- [ ] **Step 4: Resolve in both commands**

In `src/sovrag/cli.py`:

Change the imports `from sovrag.auth import resolve_principal` and `from sovrag.retrieve import Mode, search` to:

```python
from sovrag.auth import Principal, resolve_principal
from sovrag.config import Settings, get_settings
from sovrag.retrieve import Mode, entitlement_for, search
```

and delete the now-duplicate `from sovrag.config import get_settings` line.

Add a helper above the `ingest` command:

```python
def _bind(principal_id: str, settings: Settings) -> Principal:
    """Resolve --principal against the configured file; an unknown id is a usage error."""
    try:
        return resolve_principal(principal_id, Path(settings.principals_path))
    except KeyError as exc:
        raise typer.BadParameter(
            f"unknown principal: {principal_id}", param_hint="--principal"
        ) from exc
```

In `query`, replace the body from `settings = get_settings()` through the end of the `with connect(...)` block with:

```python
    settings = get_settings()
    who = _bind(principal, settings)
    applied_filter = entitlement_for(who).render()
    with connect(settings.database_url) as conn:
        hits = search(
            conn,
            question,
            mode=mode,
            k=k,
            embed_query=lambda q: Embedder.from_settings(settings).embed_one(q),
            principal=who,
        )
```

Add `"applied_filter": applied_filter,` to the JSON `payload` after the `"mode"` entry, and change the text header line to:

```python
    typer.echo(
        f"question: {question}\nprincipal: {who.id}  mode: {mode.value}  k: {k}\n"
        f"filter: {applied_filter}\n"
    )
```

In `ask`, replace the `try: who = resolve_principal(principal) ... raise typer.BadParameter(...)` block with:

```python
    settings = get_settings()
    who = _bind(principal, settings)
```

Change the `--principal` help text on both commands to `"Principal id in the file PRINCIPALS_PATH names"`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: all pass. `test_ask_unknown_principal_is_a_usage_error` still passes through `_bind`.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass, none skipped.

- [ ] **Step 7: Commit**

```bash
git add src/sovrag/config.py src/sovrag/cli.py .env.example tests/test_cli.py
git commit -m "Resolve the principal for sovrag query and read the principals file from settings"
```

---

### Task 7: Documentation says what the code now does

**Files:**
- Modify: `README.md` (the status line), `CLAUDE.md` (Invariants and Commands), `docs/architecture.md` (Status, Principals, Retrieval modes), `.claude/rules/retrieval.md` (no change needed; verify)

**Interfaces:** none.

- [ ] **Step 1: README status line**

Replace the line beginning `Status:` with:

```markdown
Status: corpus indexed; vector, keyword and hybrid retrieval, each filtered by the principal's entitlement in SQL; grounded answers with citations and refusal from the CLI.
```

Read the rest of `README.md` for any sentence that says filtering is not yet applied and fix it the same way; `grep -n "yet\|not implemented" README.md` finds them.

- [ ] **Step 2: CLAUDE.md**

In the first invariant, change `(src/sovrag/retrieve/filters.py, once it lands)` to `(src/sovrag/retrieve/filters.py)`. In Commands, change the last line to:

```markdown
- Not implemented yet: sovrag serve, sovrag eval, the mcp profile.
```

- [ ] **Step 3: docs/architecture.md**

Change the `Status:` line under Components to:

```markdown
Status: the corpus pipeline, all three retrieval modes with entitlement filtering in SQL, and grounded generation with citations and refusal exist. The HTTP API and the MCP server follow.
```

In "## Principals", replace the last paragraph's final two sentences (`The entitlement filter that applies a principal to retrieval is not implemented yet, and until it lands ...`) with:

```markdown
Both `sovrag query` and `sovrag ask` resolve `--principal` against the file `PRINCIPALS_PATH` names and reject an unknown id as a usage error.
```

Add a new section after "## Retrieval modes":

```markdown
## Entitlement filter

`retrieve/filters.py` turns a principal into an `Entitlement`: one fixed SQL fragment and two bound arrays, the classification labels at or below the clearance and the department memberships. A document is visible when its classification is at or below the clearance and it is unclassified, government-wide, or in one of the principal's departments. All three search functions require a principal and put the predicate in their `WHERE` clause; hybrid passes it to both arms and fuses only what they return. Vector search runs in a transaction with `SET LOCAL hnsw.iterative_scan = relaxed_order` so the HNSW scan continues past filtered-out rows until `k` visible ones are found, then re-sorts those `k` by score. The rendered predicate is reported as `applied_filter` on every answer and audit line. Against the indexed corpus `analyst_dnd` sees 16 of 27 documents. See ADR 0013.
```

- [ ] **Step 4: Verify the retrieval rule needs no change**

`.claude/rules/retrieval.md` already states the filter is a SQL WHERE clause and that a test must show zero rows. Leave it.

- [ ] **Step 5: Full check and commit**

```bash
uv run ruff format docs
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
git add README.md CLAUDE.md docs/architecture.md
git commit -m "Describe entitlement filtering now that every retrieval query applies it"
```

---

## Pull request

Push `entitlement-filter` and open a pull request against `main` titled "Filter every retrieval query by the principal's entitlement". The body summarises ADR 0013, the leak tests, the `sovrag query` change, and the settings move; it ends with the generated-with line. Wait for the `ci` and `claude-review` checks on the head commit; the review must post a "## Code review" comment. Squash-merge and delete the branch.

## Self-review

- Spec coverage: `Entitlement` fields and `render()` (Task 3); `entitlement_for` (Task 3); required `principal` on the three functions and `search_hybrid` passing it to both arms (Task 4); the two `SET LOCAL`s (Task 4); leak tests seeding one document per cell, exact set per principal, zero rows for forbidden documents, rejection without `principal`, hybrid never returning a filtered chunk (Task 4); CLI resolution for `sovrag query` (Task 6); the marked call site in `generate/answer.py` (Task 5); `DEFAULT_PRINCIPALS_PATH` into settings (Task 6); the loader gap (Task 2); ADR before code (Task 1).
- Names used across tasks: `entitlement_for`, `Entitlement.render`, `Entitlement.params`, `Entitlement.sql`, `PREDICATE`, `HNSW_EF_SEARCH_MAX`, `_bind`, `Settings.principals_path`, `WHO_FILTER`, `ANALYST_DND_FILTER` are spelled the same everywhere.
