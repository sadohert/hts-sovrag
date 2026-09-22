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
