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
