"""Corpus manifest: document metadata plus the synthetic classification rule.

Every document is public. Classification is assigned from doc_type so the entitlement model
has something to enforce; see README and ADR 0003.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEPARTMENTS = frozenset({"DND", "CSE", "PS", "PSPC", "SSC", "TBS", "GC_WIDE"})
CLASSIFICATION_BY_DOC_TYPE = {
    "DP": "UNCLASSIFIED",
    "POLICY": "UNCLASSIFIED",
    "DRR": "PROTECTED_A",
    "OAG": "PROTECTED_B",
}
CLASSIFICATION_RANK = {"UNCLASSIFIED": 0, "PROTECTED_A": 1, "PROTECTED_B": 2}


@dataclass(frozen=True)
class DocumentMeta:
    id: str
    title: str
    department: str
    doc_type: str
    fiscal_year: str | None
    lang: str
    source_url: str
    pages: tuple[str, ...]
    classification: str


def classification_for(doc_type: str) -> str:
    try:
        return CLASSIFICATION_BY_DOC_TYPE[doc_type]
    except KeyError:
        raise ValueError(f"unknown doc_type {doc_type!r}") from None


def load_manifest(path: Path) -> list[DocumentMeta]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    docs: list[DocumentMeta] = []
    for entry in raw["documents"]:
        if entry["department"] not in DEPARTMENTS:
            raise ValueError(f"{entry['id']}: unknown department {entry['department']!r}")
        docs.append(
            DocumentMeta(
                id=entry["id"],
                title=entry["title"],
                department=entry["department"],
                doc_type=entry["doc_type"],
                fiscal_year=entry.get("fiscal_year"),
                lang=entry.get("lang", "en"),
                source_url=entry["source_url"],
                pages=tuple(entry.get("pages") or ()),
                classification=classification_for(entry["doc_type"]),
            )
        )
    return docs
