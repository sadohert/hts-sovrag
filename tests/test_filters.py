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
