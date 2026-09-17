"""Principals: who is asking, and what they may see.

Two axes, both synthetic (ADR 0003): a clearance level and a set of departments. This module is
the single source for the label vocabulary. The corpus manifest imports it from here, and so
will the entitlement filter. It imports nothing from the rest of sovrag.

A Principal validates itself, so one that exists is valid whatever built it, and the YAML file
loads strictly: every malformed input is a ValueError that names the principal. See ADR 0012.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

GC_WIDE = "GC_WIDE"
DEPARTMENTS = frozenset({"DND", "CSE", "PS", "PSPC", "SSC", "TBS", GC_WIDE})
# GC_WIDE labels documents that every department may read. Nobody is a member of it.
MEMBERSHIPS = DEPARTMENTS - {GC_WIDE}
ANONYMOUS_ID = "anonymous"
DEFAULT_PRINCIPALS_PATH = Path("data/principals.yaml")


class Clearance(StrEnum):
    """Ordered from least to most sensitive. The order here is the rank."""

    UNCLASSIFIED = "UNCLASSIFIED"
    PROTECTED_A = "PROTECTED_A"
    PROTECTED_B = "PROTECTED_B"

    @property
    def rank(self) -> int:
        return CLASSIFICATION_RANK[self.value]


# Derived from the enum order so there is one source, and read-only so no importer can reorder it.
CLASSIFICATION_RANK: Mapping[str, int] = MappingProxyType(
    {clearance.value: rank for rank, clearance in enumerate(Clearance)}
)


def _reserved(principal_id: str) -> str:
    return (
        f"{principal_id!r}: the id {ANONYMOUS_ID!r} is reserved for the least privileged principal"
    )


@dataclass(frozen=True)
class Principal:
    id: str
    clearance: Clearance
    departments: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError(f"principal id must be a non-empty string, got {self.id!r}")
        if not isinstance(self.clearance, Clearance):
            raise ValueError(f"{self.id}: clearance must be a Clearance, got {self.clearance!r}")
        if not isinstance(self.departments, frozenset):
            kind = type(self.departments).__name__
            raise ValueError(f"{self.id}: departments must be a frozenset, got {kind}")
        unknown = self.departments - MEMBERSHIPS
        if unknown:
            raise ValueError(f"{self.id}: unknown departments {sorted(unknown, key=str)!r}")
        if self.id.lower() == ANONYMOUS_ID:
            least_privileged = (
                self.id == ANONYMOUS_ID
                and self.clearance is Clearance.UNCLASSIFIED
                and not self.departments
            )
            if not least_privileged:
                raise ValueError(_reserved(self.id))


LEAST_PRIVILEGED = Principal(
    id=ANONYMOUS_ID, clearance=Clearance.UNCLASSIFIED, departments=frozenset()
)


class _UniqueKeyLoader(yaml.SafeLoader):
    """A SafeLoader that refuses a repeated key instead of quietly keeping the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                line = key_node.start_mark.line + 1
                raise ValueError(f"duplicate key {key!r} at line {line}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_principals(path: Path = DEFAULT_PRINCIPALS_PATH) -> dict[str, Principal]:
    """Read named principals from YAML. A bad file is an error, never a quiet downgrade."""
    raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    entries = raw.get("principals") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        raise ValueError(f"{path}: expected a top-level 'principals' mapping")

    principals: dict[str, Principal] = {}
    for principal_id, entry in entries.items():
        if not isinstance(principal_id, str):
            raise ValueError(
                f"principal id {principal_id!r} must be a string; quote it in the YAML"
            )
        if principal_id.lower() == ANONYMOUS_ID:
            raise ValueError(_reserved(principal_id))
        if not isinstance(entry, dict):
            raise ValueError(f"{principal_id}: entry must be a mapping with a clearance")
        if "clearance" not in entry:
            raise ValueError(f"{principal_id}: missing clearance")
        try:
            clearance = Clearance(entry["clearance"])
        except ValueError:
            raise ValueError(f"{principal_id}: unknown clearance {entry['clearance']!r}") from None
        listed = entry.get("departments")
        if listed is None:
            listed = []
        if not isinstance(listed, list) or not all(isinstance(d, str) for d in listed):
            raise ValueError(f"{principal_id}: departments must be a list of strings")
        principals[principal_id] = Principal(principal_id, clearance, frozenset(listed))
    return principals


def resolve_principal(principal_id: str, path: Path = DEFAULT_PRINCIPALS_PATH) -> Principal:
    """Look up one principal by id. KeyError means the id is not in the file, and nothing else:
    a malformed file raises ValueError from load_principals."""
    return load_principals(path)[principal_id]
