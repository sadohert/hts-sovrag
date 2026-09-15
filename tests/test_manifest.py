"""Manifest loading and the synthetic classification rule."""

from pathlib import Path

import pytest

from sovrag.corpus.manifest import DocumentMeta, classification_for, load_manifest

MANIFEST = Path(__file__).parents[1] / "data" / "manifest.yaml"


@pytest.mark.parametrize(
    ("doc_type", "expected"),
    [
        ("DP", "UNCLASSIFIED"),
        ("POLICY", "UNCLASSIFIED"),
        ("DRR", "PROTECTED_A"),
        ("OAG", "PROTECTED_B"),
    ],
)
def test_classification_is_a_function_of_doc_type(doc_type: str, expected: str) -> None:
    assert classification_for(doc_type) == expected


def test_unknown_doc_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="doc_type"):
        classification_for("MEMO")


def test_load_manifest_yields_document_meta_with_classification(tmp_path: Path) -> None:
    (tmp_path / "m.yaml").write_text(
        """
documents:
  - id: dnd-dp-2024-25
    title: "National Defence 2024-25 Departmental Plan"
    department: DND
    doc_type: DP
    fiscal_year: "2024-25"
    lang: en
    source_url: https://example.test/dnd.html
    pages:
      - https://example.test/dnd-1.html
  - id: policy-government-security
    title: "Policy on Government Security"
    department: GC_WIDE
    doc_type: POLICY
    lang: en
    source_url: https://example.test/pgs.html
"""
    )
    docs = load_manifest(tmp_path / "m.yaml")
    assert docs[0] == DocumentMeta(
        id="dnd-dp-2024-25",
        title="National Defence 2024-25 Departmental Plan",
        department="DND",
        doc_type="DP",
        fiscal_year="2024-25",
        lang="en",
        source_url="https://example.test/dnd.html",
        pages=("https://example.test/dnd-1.html",),
        classification="UNCLASSIFIED",
    )
    assert docs[1].fiscal_year is None
    assert docs[1].pages == ()
    assert docs[1].classification == "UNCLASSIFIED"


def test_manifest_rejects_unknown_department(tmp_path: Path) -> None:
    (tmp_path / "m.yaml").write_text(
        """
documents:
  - id: x
    title: x
    department: NASA
    doc_type: DP
    lang: en
    source_url: https://example.test/x.html
"""
    )
    with pytest.raises(ValueError, match="department"):
        load_manifest(tmp_path / "m.yaml")


def test_real_manifest_loads_and_ids_are_unique() -> None:
    docs = load_manifest(MANIFEST)
    assert len(docs) >= 20
    assert len({d.id for d in docs}) == len(docs)
    assert {d.department for d in docs} <= {"DND", "CSE", "PS", "PSPC", "SSC", "TBS", "GC_WIDE"}
