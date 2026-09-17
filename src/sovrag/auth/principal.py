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
