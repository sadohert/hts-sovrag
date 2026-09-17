"""The principal model: clearance levels, departments, and the shared label vocabulary."""

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
