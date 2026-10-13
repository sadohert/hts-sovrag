"""Leak suite: an independent oracle for the visibility rule, and zero forbidden chunks."""

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from sovrag.auth import Clearance, Principal, load_principals
from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.eval.golden import GoldChunk, LeakItem
from sovrag.eval.leak import forbidden_doc_ids, run_leak, run_leak_answers, visible
from sovrag.generate.answer import Answer, Citation
from sovrag.index.store import replace_chunks, upsert_document
from sovrag.retrieve.filters import entitlement_for

DIM = 1024
AUDITOR = Principal("auditor", Clearance.PROTECTED_B, frozenset({"DND", "PS"}))
DND = Principal("analyst_dnd", Clearance.PROTECTED_A, frozenset({"DND"}))
PUBLIC = Principal("public", Clearance.UNCLASSIFIED, frozenset())


def _meta(doc_id: str, department: str, doc_type: str, classification: str) -> DocumentMeta:
    return DocumentMeta(
        id=doc_id,
        title=doc_id,
        department=department,
        doc_type=doc_type,
        fiscal_year=None,
        lang="en",
        source_url=f"https://example.test/{doc_id}",
        pages=(),
        classification=classification,
    )


def _unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def _seed(db: psycopg.Connection) -> None:
    upsert_document(db, _meta("dnd-drr", "DND", "DRR", "PROTECTED_A"))
    upsert_document(db, _meta("ps-drr", "PS", "DRR", "PROTECTED_A"))
    upsert_document(db, _meta("oag", "GC_WIDE", "OAG", "PROTECTED_B"))
    upsert_document(db, _meta("policy", "GC_WIDE", "POLICY", "UNCLASSIFIED"))
    # Each disjunct of `visible` gets its own document, isolated from the others:
    upsert_document(db, _meta("ps-dp", "PS", "DP", "UNCLASSIFIED"))  # classification alone
    upsert_document(db, _meta("gc-drr", "GC_WIDE", "DRR", "PROTECTED_A"))  # GC_WIDE alone
    upsert_document(
        db, _meta("dnd-oag", "DND", "OAG", "PROTECTED_B")
    )  # department membership alone
    replace_chunks(db, "dnd-drr", [Chunk(0, "h", "defence results achieved")], [_unit(0)])
    replace_chunks(db, "ps-drr", [Chunk(0, "h", "public safety results achieved")], [_unit(1)])
    replace_chunks(db, "oag", [Chunk(0, "h", "the audit found contracting gaps")], [_unit(2)])
    replace_chunks(db, "policy", [Chunk(0, "h", "the policy applies to departments")], [_unit(3)])
    replace_chunks(db, "ps-dp", [Chunk(0, "h", "public safety plan priorities")], [_unit(4)])
    replace_chunks(db, "gc-drr", [Chunk(0, "h", "government wide results reporting")], [_unit(5)])
    replace_chunks(db, "dnd-oag", [Chunk(0, "h", "defence audit findings")], [_unit(6)])


def test_visible_states_the_rule() -> None:
    assert visible(DND, "UNCLASSIFIED", "PS")
    assert visible(DND, "PROTECTED_A", "DND")
    assert not visible(DND, "PROTECTED_A", "PS")
    assert not visible(DND, "PROTECTED_B", "GC_WIDE")
    assert visible(AUDITOR, "PROTECTED_B", "GC_WIDE")
    assert not visible(PUBLIC, "PROTECTED_A", "DND")


def test_forbidden_set_agrees_with_the_sql_predicate_for_every_named_principal(
    db: psycopg.Connection,
) -> None:
    _seed(db)
    for principal in load_principals(Path("data/principals.yaml")).values():
        ent = entitlement_for(principal)
        visible_ids = {
            row[0]
            for row in db.execute(
                f"select d.id from documents d where {ent.sql}", ent.params
            ).fetchall()
        }
        all_ids = {row[0] for row in db.execute("select id from documents").fetchall()}
        assert forbidden_doc_ids(db, principal) == frozenset(all_ids - visible_ids), principal.id


def test_clean_index_reports_no_leaks_and_counts_control(db: psycopg.Connection) -> None:
    _seed(db)
    items = [
        LeakItem("l1", "results achieved", (GoldChunk("ps-drr", 0),), "n"),
        LeakItem("l2", "audit found", (GoldChunk("oag", 0),), "n"),
    ]
    result = run_leak(
        db,
        items,
        {"l1": _unit(1), "l2": _unit(2)},
        {"dnd": DND, "public": PUBLIC},
        control=AUDITOR,
    )
    assert result.metrics["leaks"] == 0
    assert result.metrics["checks"] == 2 * 2 * 3
    assert result.metrics["positive_control"] == 2
    assert not [r for r in result.rows if "doc_id" in r]


def test_a_forbidden_chunk_in_results_is_reported(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break the predicate on purpose: swap the search for one that ignores the principal."""
    from sovrag.eval import leak as leak_mod
    from sovrag.retrieve import search

    _seed(db)
    monkeypatch.setattr(
        leak_mod, "search", lambda conn, q, **kw: search(conn, q, **{**kw, "principal": AUDITOR})
    )
    items = [LeakItem("l1", "results achieved", (GoldChunk("ps-drr", 0),), "n")]
    result = run_leak(db, items, {"l1": _unit(1)}, {"dnd": DND}, control=AUDITOR)
    assert result.metrics["leaks"] >= 1
    leaked = [r for r in result.rows if "doc_id" in r]
    assert {(r["principal"], r["doc_id"]) for r in leaked} >= {("dnd", "ps-drr")}


def test_answer_check_counts_refusals() -> None:
    def fake_answer(question: str, principal: Principal) -> Answer:
        refused = principal.id != "auditor"
        cites = () if refused else (Citation(1, "oag", 7, "t", "h", "https://example.test/oag"),)
        return Answer(
            question=question,
            principal=principal.id,
            mode="hybrid",
            request_id=None,
            text="x",
            citations=cites,
            refused=refused,
            model="m",
            retrieved_chunk_ids=(),
            applied_filter=None,
        )

    items = [LeakItem("l1", "q", (GoldChunk("oag", 0),), "n")]
    fn: Callable[[str, Principal], Answer] = fake_answer
    result = run_leak_answers(items, {"dnd": DND, "auditor": AUDITOR}, fn)
    assert result.metrics == {
        "dnd.asked": 1,
        "dnd.refused": 1,
        "dnd.failed": 0,
        "auditor.asked": 1,
        "auditor.refused": 0,
        "auditor.failed": 0,
    }
    by_principal = {r["principal"]: r for r in result.rows}
    assert by_principal["dnd"] == {
        "id": "l1",
        "principal": "dnd",
        "refused": True,
        "answer": "x",
        "citations": [],
    }
    assert by_principal["auditor"] == {
        "id": "l1",
        "principal": "auditor",
        "refused": False,
        "answer": "x",
        "citations": [7],
    }


def test_answer_check_records_a_generation_error_and_continues() -> None:
    """One hosted request timing out must not discard the other fifty-nine answers."""
    from sovrag.generate.llm import GenerationError

    def fake_answer(question: str, principal: Principal) -> Answer:
        if question == "q2":
            raise GenerationError("generation failed: Request timed out.")
        return Answer(
            question=question,
            principal=principal.id,
            mode="hybrid",
            request_id=None,
            text="x",
            citations=(),
            refused=True,
            model="m",
            retrieved_chunk_ids=(),
            applied_filter=None,
        )

    items = [
        LeakItem(i, q, (GoldChunk("oag", 0),), "n")
        for i, q in (("l1", "q1"), ("l2", "q2"), ("l3", "q3"))
    ]
    fn: Callable[[str, Principal], Answer] = fake_answer
    result = run_leak_answers(items, {"dnd": DND}, fn)
    assert result.metrics["dnd.asked"] == 3.0
    assert result.metrics["dnd.refused"] == 2.0
    assert result.metrics["dnd.failed"] == 1.0
    failed = [r for r in result.rows if r["id"] == "l2"]
    assert failed and failed[0]["refused"] is None
    assert "timed out" in str(failed[0]["error"])
