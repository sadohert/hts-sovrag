"""Principals: who is asking, and what they may see.

Two axes, both synthetic (ADR 0003): a clearance level and a set of departments. This module is
the single source for the label vocabulary. The corpus manifest imports it from here, and so
will the entitlement filter. It imports nothing from the rest of sovrag.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

GC_WIDE = "GC_WIDE"
DEPARTMENTS = frozenset({"DND", "CSE", "PS", "PSPC", "SSC", "TBS", GC_WIDE})
CLASSIFICATION_RANK = {"UNCLASSIFIED": 0, "PROTECTED_A": 1, "PROTECTED_B": 2}
ANONYMOUS_ID = "anonymous"
DEFAULT_PRINCIPALS_PATH = Path("data/principals.yaml")


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
            raise ValueError(f"{principal_id}: unknown clearance {entry['clearance']!r}") from None
        departments = frozenset(entry.get("departments") or ())
        unknown = departments - memberships
        if unknown:
            raise ValueError(f"{principal_id}: unknown departments {sorted(unknown)!r}")
        principals[principal_id] = Principal(principal_id, clearance, departments)
    return principals


def resolve_principal(principal_id: str, path: Path = DEFAULT_PRINCIPALS_PATH) -> Principal:
    """Look up one principal by id. Raises KeyError for an unknown id."""
    return load_principals(path)[principal_id]
