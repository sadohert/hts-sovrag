# Principal Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `sovrag.auth` defines who is asking: a `Principal` with a clearance and a set of departments, the shared label vocabulary, and five shipped principals loaded from `data/principals.yaml`.

**Architecture:** One module, `src/sovrag/auth/principal.py`, owns the vocabulary (`Clearance`, `CLASSIFICATION_RANK`, `DEPARTMENTS`), the frozen `Principal` dataclass, `LEAST_PRIVILEGED`, and the YAML loader. `src/sovrag/auth/__init__.py` re-exports the public names. `src/sovrag/corpus/manifest.py` stops defining the vocabulary and imports it, so there is one source. Nothing in retrieval or the CLI changes here; the entitlement filter that consumes `Principal` is a later pull request.

**Tech Stack:** Python 3.14, dataclasses, `enum.StrEnum`, PyYAML, pytest, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-17-mattermost-integration-design.md` (section "`sovrag.auth`" and the first item under "Sequencing").

## Global Constraints

- Classification labels are synthetic (ADR 0003). No copy may imply real protected data was used.
- `sovrag.auth` must not import from `sovrag.corpus`, `sovrag.retrieve` or `sovrag.cli`. Other packages import from it, never the reverse.
- Type hints everywhere; `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` and `uv run pytest -q` must pass before each commit.
- Tests first. Run the test, watch it fail for the expected reason, then implement.
- Line length 100 (ruff).
- Commit messages describe content only. No schedule or session references anywhere in the repo, and no paths outside the repo.
- End each commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- No changes to `src/sovrag/retrieve/`, `src/sovrag/cli.py` or `src/sovrag/generate/` in this plan.

## File Structure

| File | Responsibility |
|---|---|
| `src/sovrag/auth/principal.py` (create) | Vocabulary, `Clearance`, `Principal`, `LEAST_PRIVILEGED`, `load_principals`, `resolve_principal` |
| `src/sovrag/auth/__init__.py` (modify, currently empty) | Re-export the public names |
| `src/sovrag/corpus/manifest.py` (modify) | Import `DEPARTMENTS` from `sovrag.auth`; drop its own `DEPARTMENTS` and `CLASSIFICATION_RANK` |
| `data/principals.yaml` (create) | The five shipped principals |
| `Dockerfile` (modify) | Copy `data/principals.yaml` into the runtime image |
| `docs/architecture.md` (modify) | A "Principals" section |
| `tests/test_principal.py` (create) | All tests for this plan |

## Branch Setup

The spec and this plan are committed on `mattermost-integration`. Create the working branch from it so the pull request carries spec, plan and code together:

```bash
git worktree add ../principal-model -b principal-model mattermost-integration
cd ../principal-model
uv sync --all-extras
uv run pytest -q
```

Expected: the existing suite passes (Postgres tests skip if the Compose Postgres is not running; start it with `docker compose up -d postgres` to run them).

---

### Task 1: Vocabulary, `Clearance` and `Principal`

**Files:**
- Create: `src/sovrag/auth/principal.py`
- Modify: `src/sovrag/auth/__init__.py`
- Modify: `src/sovrag/corpus/manifest.py:14-21`
- Test: `tests/test_principal.py`

**Interfaces:**
- Consumes: nothing.
- Produces, all importable from `sovrag.auth`:
  - `GC_WIDE: str = "GC_WIDE"`
  - `DEPARTMENTS: frozenset[str]` = `{"DND", "CSE", "PS", "PSPC", "SSC", "TBS", "GC_WIDE"}`
  - `CLASSIFICATION_RANK: dict[str, int]` = `{"UNCLASSIFIED": 0, "PROTECTED_A": 1, "PROTECTED_B": 2}`
  - `class Clearance(StrEnum)` with members `UNCLASSIFIED`, `PROTECTED_A`, `PROTECTED_B` and a property `rank -> int`
  - `@dataclass(frozen=True) class Principal` with fields `id: str`, `clearance: Clearance`, `departments: frozenset[str]`
  - `ANONYMOUS_ID: str = "anonymous"`
  - `LEAST_PRIVILEGED: Principal` = `Principal("anonymous", Clearance.UNCLASSIFIED, frozenset())`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_principal.py`:

```python
"""The principal model: clearance levels, departments, and the shared label vocabulary."""

import dataclasses

import pytest

from sovrag import auth
from sovrag.auth import (
    ANONYMOUS_ID,
    CLASSIFICATION_RANK,
    DEPARTMENTS,
    GC_WIDE,
    LEAST_PRIVILEGED,
    Clearance,
    Principal,
)
from sovrag.corpus import manifest


def test_clearance_ranks_order_the_three_labels() -> None:
    assert Clearance.UNCLASSIFIED.rank < Clearance.PROTECTED_A.rank < Clearance.PROTECTED_B.rank


def test_rank_table_covers_exactly_the_clearance_labels() -> None:
    assert set(CLASSIFICATION_RANK) == {c.value for c in Clearance}


def test_clearance_is_built_from_its_label() -> None:
    assert Clearance("PROTECTED_A") is Clearance.PROTECTED_A
    with pytest.raises(ValueError):
        Clearance("SECRET")


def test_every_doc_type_classification_is_a_clearance_label() -> None:
    for label in manifest.CLASSIFICATION_BY_DOC_TYPE.values():
        assert Clearance(label).value == label


def test_manifest_uses_the_auth_department_vocabulary() -> None:
    assert manifest.DEPARTMENTS is auth.DEPARTMENTS
    assert GC_WIDE in DEPARTMENTS


def test_manifest_no_longer_defines_its_own_rank_table() -> None:
    assert not hasattr(manifest, "CLASSIFICATION_RANK")


def test_principal_is_frozen_and_hashable() -> None:
    p = Principal(id="analyst_dnd", clearance=Clearance.PROTECTED_A, departments=frozenset({"DND"}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.id = "someone_else"  # type: ignore[misc]
    assert p in {p}


def test_least_privileged_sees_the_minimum() -> None:
    assert LEAST_PRIVILEGED.id == ANONYMOUS_ID == "anonymous"
    assert LEAST_PRIVILEGED.clearance is Clearance.UNCLASSIFIED
    assert LEAST_PRIVILEGED.departments == frozenset()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_principal.py -q`
Expected: collection error, `ImportError: cannot import name 'ANONYMOUS_ID' from 'sovrag.auth'`.

- [ ] **Step 3: Write the implementation**

Create `src/sovrag/auth/principal.py`:

```python
"""Principals: who is asking, and what they may see.

Two axes, both synthetic (ADR 0003): a clearance level and a set of departments. This module is
the single source for the label vocabulary. The corpus manifest imports it from here, and so
will the entitlement filter. It imports nothing from the rest of sovrag.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

GC_WIDE = "GC_WIDE"
DEPARTMENTS = frozenset({"DND", "CSE", "PS", "PSPC", "SSC", "TBS", GC_WIDE})
CLASSIFICATION_RANK = {"UNCLASSIFIED": 0, "PROTECTED_A": 1, "PROTECTED_B": 2}
ANONYMOUS_ID = "anonymous"


class Clearance(StrEnum):
    UNCLASSIFIED = "UNCLASSIFIED"
    PROTECTED_A = "PROTECTED_A"
    PROTECTED_B = "PROTECTED_B"

    @property
    def rank(self) -> int:
        return CLASSIFICATION_RANK[self.value]


@dataclass(frozen=True)
class Principal:
    id: str
    clearance: Clearance
    departments: frozenset[str]


LEAST_PRIVILEGED = Principal(
    id=ANONYMOUS_ID, clearance=Clearance.UNCLASSIFIED, departments=frozenset()
)
```

Replace the (empty) `src/sovrag/auth/__init__.py` with:

```python
"""Identity and the entitlement vocabulary. See principal.py."""

from sovrag.auth.principal import (
    ANONYMOUS_ID,
    CLASSIFICATION_RANK,
    DEPARTMENTS,
    GC_WIDE,
    LEAST_PRIVILEGED,
    Clearance,
    Principal,
)

__all__ = [
    "ANONYMOUS_ID",
    "CLASSIFICATION_RANK",
    "DEPARTMENTS",
    "GC_WIDE",
    "LEAST_PRIVILEGED",
    "Clearance",
    "Principal",
]
```

In `src/sovrag/corpus/manifest.py`, delete these two definitions:

```python
DEPARTMENTS = frozenset({"DND", "CSE", "PS", "PSPC", "SSC", "TBS", "GC_WIDE"})
```

```python
CLASSIFICATION_RANK = {"UNCLASSIFIED": 0, "PROTECTED_A": 1, "PROTECTED_B": 2}
```

and add the import below `import yaml`, so the import block reads:

```python
from dataclasses import dataclass
from pathlib import Path

import yaml

from sovrag.auth import DEPARTMENTS
```

`CLASSIFICATION_BY_DOC_TYPE`, `DocumentMeta`, `classification_for` and `load_manifest` stay exactly as they are. `load_manifest` already uses the name `DEPARTMENTS`, which now resolves to the imported one.

- [ ] **Step 4: Confirm nothing else used the removed name**

Run: `grep -rn "CLASSIFICATION_RANK" src tests scripts`
Expected: matches only in `src/sovrag/auth/principal.py`, `src/sovrag/auth/__init__.py` and `tests/test_principal.py`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_principal.py tests/test_manifest.py -q`
Expected: all pass.

- [ ] **Step 6: Run the full checks**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q`
Expected: no ruff findings, `Success: no issues found`, all tests pass. If `ruff format --check` reports a file, run `uv run ruff format src tests`, review the diff (layout only), and re-run the checks.

- [ ] **Step 7: Commit**

```bash
git add src/sovrag/auth/principal.py src/sovrag/auth/__init__.py src/sovrag/corpus/manifest.py tests/test_principal.py
git commit -m "Add the principal model and move the label vocabulary into sovrag.auth

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Loading principals and the five shipped principals

**Files:**
- Modify: `src/sovrag/auth/principal.py`
- Modify: `src/sovrag/auth/__init__.py`
- Create: `data/principals.yaml`
- Test: `tests/test_principal.py` (append)

**Interfaces:**
- Consumes from Task 1: `Clearance`, `Principal`, `DEPARTMENTS`, `GC_WIDE`, `ANONYMOUS_ID`.
- Produces, importable from `sovrag.auth`:
  - `DEFAULT_PRINCIPALS_PATH: Path = Path("data/principals.yaml")`
  - `load_principals(path: Path = DEFAULT_PRINCIPALS_PATH) -> dict[str, Principal]`. Raises `ValueError` naming the principal for an unknown clearance, an unknown department (including `GC_WIDE`, which is a document label, not a membership), or the reserved id `anonymous`.
  - `resolve_principal(principal_id: str, path: Path = DEFAULT_PRINCIPALS_PATH) -> Principal`. Raises `KeyError(principal_id)` for an unknown id. Callers such as the CLI turn that into a usage error; nothing downgrades silently.
  - `data/principals.yaml` with ids `public`, `analyst_dnd`, `analyst_ps`, `cio_ssc`, `auditor`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_principal.py`. First extend the imports at the top of the file so they read:

```python
import dataclasses
from pathlib import Path

import pytest

from sovrag import auth
from sovrag.auth import (
    ANONYMOUS_ID,
    CLASSIFICATION_RANK,
    DEFAULT_PRINCIPALS_PATH,
    DEPARTMENTS,
    GC_WIDE,
    LEAST_PRIVILEGED,
    Clearance,
    Principal,
    load_principals,
    resolve_principal,
)
from sovrag.corpus import manifest
```

Then add at the end of the file:

```python
def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "principals.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_principals_builds_principals_from_yaml(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "principals:\n"
        "  reader:\n"
        "    clearance: UNCLASSIFIED\n"
        "    departments: []\n"
        "  analyst:\n"
        "    clearance: PROTECTED_A\n"
        "    departments: [DND, PS]\n",
    )
    assert load_principals(path) == {
        "reader": Principal("reader", Clearance.UNCLASSIFIED, frozenset()),
        "analyst": Principal("analyst", Clearance.PROTECTED_A, frozenset({"DND", "PS"})),
    }


def test_departments_key_may_be_omitted(tmp_path: Path) -> None:
    path = _write(tmp_path, "principals:\n  reader:\n    clearance: UNCLASSIFIED\n")
    assert load_principals(path)["reader"].departments == frozenset()


def test_unknown_clearance_is_rejected_and_names_the_principal(tmp_path: Path) -> None:
    path = _write(tmp_path, "principals:\n  spy:\n    clearance: SECRET\n")
    with pytest.raises(ValueError, match=r"spy: unknown clearance 'SECRET'"):
        load_principals(path)


def test_unknown_department_is_rejected_and_names_the_principal(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "principals:\n  a:\n    clearance: PROTECTED_A\n    departments: [DND, NASA]\n"
    )
    with pytest.raises(ValueError, match=r"a: unknown departments \['NASA'\]"):
        load_principals(path)


def test_gc_wide_is_a_document_label_not_a_membership(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "principals:\n  a:\n    clearance: PROTECTED_A\n    departments: [GC_WIDE]\n"
    )
    with pytest.raises(ValueError, match=r"a: unknown departments \['GC_WIDE'\]"):
        load_principals(path)


def test_the_anonymous_id_is_reserved(tmp_path: Path) -> None:
    path = _write(tmp_path, "principals:\n  anonymous:\n    clearance: PROTECTED_B\n")
    with pytest.raises(ValueError, match="reserved"):
        load_principals(path)


def test_resolve_principal_returns_the_named_principal(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "principals:\n  a:\n    clearance: PROTECTED_B\n    departments: [TBS]\n"
    )
    assert resolve_principal("a", path) == Principal("a", Clearance.PROTECTED_B, frozenset({"TBS"}))


def test_resolve_principal_raises_keyerror_for_an_unknown_id(tmp_path: Path) -> None:
    path = _write(tmp_path, "principals:\n  a:\n    clearance: UNCLASSIFIED\n")
    with pytest.raises(KeyError, match="nobody"):
        resolve_principal("nobody", path)


def test_shipped_principals_cover_the_entitlement_grid() -> None:
    all_five = frozenset({"DND", "PS", "PSPC", "SSC", "TBS"})
    assert load_principals(DEFAULT_PRINCIPALS_PATH) == {
        "public": Principal("public", Clearance.UNCLASSIFIED, frozenset()),
        "analyst_dnd": Principal("analyst_dnd", Clearance.PROTECTED_A, frozenset({"DND"})),
        "analyst_ps": Principal("analyst_ps", Clearance.PROTECTED_A, frozenset({"PS"})),
        "cio_ssc": Principal("cio_ssc", Clearance.PROTECTED_A, frozenset({"SSC", "TBS"})),
        "auditor": Principal("auditor", Clearance.PROTECTED_B, all_five),
    }
```

`test_shipped_principals_cover_the_entitlement_grid` reads `data/principals.yaml` relative to the working directory, the same way `tests/test_manifest.py::test_real_manifest_loads_and_ids_are_unique` reads the real manifest. Run pytest from the repo root.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_principal.py -q`
Expected: collection error, `ImportError: cannot import name 'DEFAULT_PRINCIPALS_PATH' from 'sovrag.auth'`.

- [ ] **Step 3: Write the implementation**

In `src/sovrag/auth/principal.py`, change the import block to:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
```

add this constant below `ANONYMOUS_ID`:

```python
DEFAULT_PRINCIPALS_PATH = Path("data/principals.yaml")
```

and add at the end of the file:

```python
def load_principals(path: Path = DEFAULT_PRINCIPALS_PATH) -> dict[str, Principal]:
    """Read named principals from YAML. A bad entry is an error, never a quiet downgrade."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    memberships = DEPARTMENTS - {GC_WIDE}
    principals: dict[str, Principal] = {}
    for principal_id, entry in raw["principals"].items():
        if principal_id == ANONYMOUS_ID:
            raise ValueError(f"{ANONYMOUS_ID!r} is reserved for the least privileged principal")
        try:
            clearance = Clearance(entry["clearance"])
        except ValueError:
            raise ValueError(
                f"{principal_id}: unknown clearance {entry['clearance']!r}"
            ) from None
        departments = frozenset(entry.get("departments") or ())
        unknown = departments - memberships
        if unknown:
            raise ValueError(f"{principal_id}: unknown departments {sorted(unknown)!r}")
        principals[principal_id] = Principal(principal_id, clearance, departments)
    return principals


def resolve_principal(principal_id: str, path: Path = DEFAULT_PRINCIPALS_PATH) -> Principal:
    """Look up one principal by id. Raises KeyError for an unknown id."""
    return load_principals(path)[principal_id]
```

Replace `src/sovrag/auth/__init__.py` with:

```python
"""Identity and the entitlement vocabulary. See principal.py."""

from sovrag.auth.principal import (
    ANONYMOUS_ID,
    CLASSIFICATION_RANK,
    DEFAULT_PRINCIPALS_PATH,
    DEPARTMENTS,
    GC_WIDE,
    LEAST_PRIVILEGED,
    Clearance,
    Principal,
    load_principals,
    resolve_principal,
)

__all__ = [
    "ANONYMOUS_ID",
    "CLASSIFICATION_RANK",
    "DEFAULT_PRINCIPALS_PATH",
    "DEPARTMENTS",
    "GC_WIDE",
    "LEAST_PRIVILEGED",
    "Clearance",
    "Principal",
    "load_principals",
    "resolve_principal",
]
```

Create `data/principals.yaml`:

```yaml
# Named principals for the CLI and the tests. One per row of the entitlement grid.
# Clearance labels are synthetic and every document in the corpus is public; see ADR 0003.
# `anonymous` is reserved for the least privileged principal and may not be defined here.
principals:
  public:
    clearance: UNCLASSIFIED
    departments: []
  analyst_dnd:
    clearance: PROTECTED_A
    departments: [DND]
  analyst_ps:
    clearance: PROTECTED_A
    departments: [PS]
  cio_ssc:
    clearance: PROTECTED_A
    departments: [SSC, TBS]
  auditor:
    clearance: PROTECTED_B
    departments: [DND, PS, PSPC, SSC, TBS]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_principal.py -q`
Expected: all pass.

- [ ] **Step 5: Run the full checks**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q`
Expected: no ruff findings, `Success: no issues found`, all tests pass. If `ruff format --check` reports a file, run `uv run ruff format src tests`, review the diff (layout only), and re-run the checks.

- [ ] **Step 6: Commit**

```bash
git add src/sovrag/auth/principal.py src/sovrag/auth/__init__.py data/principals.yaml tests/test_principal.py
git commit -m "Load named principals from data/principals.yaml and ship the five-principal grid

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Image, architecture doc and pull request

**Files:**
- Modify: `Dockerfile` (the `COPY ... data/manifest.yaml` line in the runtime stage)
- Modify: `docs/architecture.md` (new section between "Data model" and "Deployment")

**Interfaces:**
- Consumes from Task 2: `data/principals.yaml`, `resolve_principal`.
- Produces: a runtime image that contains `data/principals.yaml`; documentation of the principal model.

- [ ] **Step 1: Copy the principals file into the image**

In `Dockerfile`, replace:

```dockerfile
COPY --chown=sovrag:sovrag data/manifest.yaml ./data/manifest.yaml
```

with:

```dockerfile
COPY --chown=sovrag:sovrag data/manifest.yaml data/principals.yaml ./data/
```

- [ ] **Step 2: Verify the image resolves a shipped principal**

Run:

```bash
docker build -t sovrag:principal-model .
docker run --rm sovrag:principal-model python -c "from sovrag.auth import resolve_principal; print(resolve_principal('analyst_dnd'))"
```

Expected output:

```
Principal(id='analyst_dnd', clearance=<Clearance.PROTECTED_A: 'PROTECTED_A'>, departments=frozenset({'DND'}))
```

- [ ] **Step 3: Document the principal model**

In `docs/architecture.md`, insert this section immediately before the `## Deployment` heading:

```markdown
## Principals

A principal is who is asking: an id, a clearance (`UNCLASSIFIED`, `PROTECTED_A` or `PROTECTED_B`) and a set of departments. The type and the label vocabulary live in `src/sovrag/auth/principal.py`, which the corpus manifest imports so documents and principals share one vocabulary. `GC_WIDE` is a label on documents, never a membership a principal can hold.

`data/principals.yaml` ships five named principals, one per row of the entitlement grid: `public`, `analyst_dnd`, `analyst_ps`, `cio_ssc` and `auditor`. An unknown id, clearance or department is an error; nothing downgrades quietly. `LEAST_PRIVILEGED` (id `anonymous`, unclassified, no departments) is reserved for callers that authenticate but carry no recognised entitlement.

Like the classification labels, clearances are synthetic (ADR 0003). The entitlement filter that applies a principal to retrieval is not implemented yet.
```

- [ ] **Step 4: Run the full checks**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q`
Expected: all pass. The README status line stays as it is: it already says there is no entitlement filtering yet, which remains true.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docs/architecture.md
git commit -m "Ship principals.yaml in the image and document the principal model

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 6: Push and open the pull request**

```bash
git push -u origin principal-model
gh pr create --base main --head principal-model \
  --title "Principal model: clearance, departments and the shipped principals" \
  --body "$(cat <<'EOF'
Adds `sovrag.auth`: the `Principal` type (id, clearance, departments), the shared label vocabulary moved out of the corpus manifest, `LEAST_PRIVILEGED`, and a YAML loader with five shipped principals covering the entitlement grid.

Also carries the design spec for the Mattermost integration and this pull request's plan, since both the entitlement filter and the generation layer build on `Principal`.

No retrieval, CLI or generation changes. The entitlement filter follows in its own pull request.

- Unknown id, clearance or department is an error, never a quiet downgrade.
- `GC_WIDE` is a document label and is rejected as a principal's department.
- `anonymous` is reserved for the least privileged principal.
- The runtime image now includes `data/principals.yaml`.

Checks: ruff, ruff format, mypy strict and pytest pass locally.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: the pull request URL is printed, and CI (`ci.yml`) starts.

- [ ] **Step 7: Confirm CI is green**

Run: `gh pr checks principal-model --watch`
Expected: all checks pass. If the review workflow leaves comments, address them with the `superpowers:receiving-code-review` skill before merging.
