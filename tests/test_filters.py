"""The entitlement predicate: bound parameters per principal, and leak tests against the store."""

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
