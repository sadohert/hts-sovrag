"""CLI: ingest from a manifest, then query with citations. Model access is faked."""

from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from sovrag import cli
from sovrag.corpus.manifest import load_manifest

REAL = Path(__file__).parent / "fixtures" / "real"
DIM = 1024
runner = CliRunner()


class FakeEmbedder:
    """Deterministic: the vector is a bag of a few keywords, so queries land on the right chunk."""

    dim = DIM
    KEYS = ("identity", "shared services", "operations", "authorities")

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * DIM
            low = t.lower()
            for i, key in enumerate(self.KEYS):
                v[i] = float(low.count(key))
            v[len(self.KEYS)] = 0.01  # never a zero vector
            out.append(v)
        return out


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, test_database_url: str, tmp_path: Path) -> Path:
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setattr(cli.Embedder, "from_settings", classmethod(lambda cls, s: FakeEmbedder()))
    raw = tmp_path / "raw"
    raw.mkdir()
    for name in ("directive-identity-management", "ssc-dp-2025-26"):
        (raw / f"{name}.html").write_bytes((REAL / f"{name}.html").read_bytes())
    (tmp_path / "manifest.yaml").write_text(
        """
documents:
  - id: directive-identity-management
    title: "Directive on Identity Management"
    department: GC_WIDE
    doc_type: POLICY
    lang: en
    source_url: https://example.test/dim.html
  - id: ssc-dp-2025-26
    title: "Shared Services Canada 2025-26 Departmental Plan"
    department: SSC
    doc_type: DP
    fiscal_year: "2025-26"
    lang: en
    source_url: https://example.test/ssc.html
"""
    )
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute("drop table if exists chunks, documents cascade")
    return tmp_path


def test_ingest_reports_each_document_and_total(env: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["ingest", "--manifest", str(env / "manifest.yaml"), "--raw-dir", str(env / "raw")],
    )
    assert result.exit_code == 0, result.output
    assert "directive-identity-management" in result.output
    assert "ssc-dp-2025-26" in result.output
    assert "2 documents" in result.output
    assert len(load_manifest(env / "manifest.yaml")) == 2


def test_query_prints_ranked_hits_with_citations(env: Path) -> None:
    runner.invoke(
        cli.app,
        ["ingest", "--manifest", str(env / "manifest.yaml"), "--raw-dir", str(env / "raw")],
    )
    result = runner.invoke(
        cli.app, ["query", "authorities for identity management", "--principal", "analyst_dnd"]
    )
    assert result.exit_code == 0, result.output
    assert "directive-identity-management" in result.output
    assert "https://example.test/dim.html" in result.output
    assert "Authorities" in result.output  # heading path of the top hit
    assert result.output.count("[") >= 5  # five numbered citations


def test_query_json_output_is_machine_readable(env: Path) -> None:
    import json

    runner.invoke(
        cli.app,
        ["ingest", "--manifest", str(env / "manifest.yaml"), "--raw-dir", str(env / "raw")],
    )
    result = runner.invoke(
        cli.app, ["query", "shared services operations", "--principal", "analyst_dnd", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["principal"] == "analyst_dnd"
    assert payload["mode"] == "vector"
    hit = payload["hits"][0]
    assert {"doc_id", "chunk_id", "heading_path", "source_url", "score", "text"} <= set(hit)


def _ingest(env: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["ingest", "--manifest", str(env / "manifest.yaml"), "--raw-dir", str(env / "raw")],
    )
    assert result.exit_code == 0, result.output


def test_keyword_mode_never_builds_the_embedder(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    _ingest(env)

    def boom(cls: type, settings: object) -> None:
        raise AssertionError("embedder constructed in keyword mode")

    monkeypatch.setattr(cli.Embedder, "from_settings", classmethod(boom))
    result = runner.invoke(
        cli.app,
        [
            "query",
            "identity management",
            "--principal",
            "analyst_dnd",
            "--mode",
            "keyword",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "keyword"
    assert payload["hits"][0]["doc_id"] == "directive-identity-management"
    assert all(h["score"] > 0 for h in payload["hits"])


def test_hybrid_mode_returns_fused_hits(env: Path) -> None:
    import json

    _ingest(env)
    result = runner.invoke(
        cli.app,
        [
            "query",
            "authorities for identity management",
            "--principal",
            "analyst_dnd",
            "--mode",
            "hybrid",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "hybrid"
    assert len(payload["hits"]) == 5
    assert payload["hits"][0]["doc_id"] == "directive-identity-management"


def test_unknown_mode_is_rejected() -> None:
    result = runner.invoke(cli.app, ["query", "x", "--principal", "analyst_dnd", "--mode", "bm25"])
    assert result.exit_code != 0
    assert "bm25" in result.output
