"""sovrag eval: the commands wire the runners; runners are faked where a model would be needed."""

import json
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from sovrag import cli
from sovrag.auth import Clearance, Principal
from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.eval import cli as eval_cli
from sovrag.eval.report import SuiteResult
from sovrag.index.store import replace_chunks, upsert_document

runner = CliRunner()
DIM = 1024


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, test_database_url: str, tmp_path: Path) -> Path:
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setenv("PRINCIPALS_PATH", str(Path("data/principals.yaml").resolve()))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _seed(db: psycopg.Connection) -> None:
    upsert_document(
        db,
        DocumentMeta(
            id="policy",
            title="Policy",
            department="GC_WIDE",
            doc_type="POLICY",
            fiscal_year=None,
            lang="en",
            source_url="https://example.test/p",
            pages=(),
            classification="UNCLASSIFIED",
        ),
    )
    v = [0.0] * DIM
    v[0] = 1.0
    replace_chunks(db, "policy", [Chunk(0, "h", "the policy applies")], [v])


def _golden(root: Path) -> Path:
    g = root / "golden"
    g.mkdir()
    (g / "retrieval.jsonl").write_text(
        json.dumps(
            {
                "id": "r1",
                "question": "policy applies",
                "gold": [{"doc_id": "policy", "ordinal": 0}],
                "reference_answer": "It applies.",
                "notes": "n",
            }
        )
        + "\n"
    )
    (g / "unanswerable.jsonl").write_text(
        json.dumps({"id": "u1", "question": "q", "notes": "n"}) + "\n"
    )
    (g / "leak.jsonl").write_text(
        json.dumps(
            {
                "id": "l1",
                "question": "policy applies",
                "gold": [{"doc_id": "policy", "ordinal": 0}],
                "notes": "n",
            }
        )
        + "\n"
    )
    return g


class FakeEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for _ in texts:
            v = [0.0] * DIM
            v[0] = 1.0
            out.append(v)
        return out


def test_build_load_retrieval_and_leak_end_to_end(
    env: Path, db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db)
    golden = _golden(env)
    monkeypatch.setattr(
        eval_cli.Embedder, "from_settings", classmethod(lambda cls, s: FakeEmbedder())
    )
    fixture = env / "fixture"

    built = runner.invoke(
        cli.app, ["eval", "build-fixture", "--golden-dir", str(golden), "--out", str(fixture)]
    )
    assert built.exit_code == 0, built.output
    assert (fixture / "chunks.f32").exists()

    db.execute("truncate chunks, documents restart identity cascade")
    loaded = runner.invoke(cli.app, ["eval", "load-fixture", "--fixture-dir", str(fixture)])
    assert loaded.exit_code == 0, loaded.output
    assert db.execute("select count(*) from chunks").fetchone() == (1,)

    out = env / "r.json"
    ran = runner.invoke(
        cli.app,
        [
            "eval",
            "retrieval",
            "--golden-dir",
            str(golden),
            "--fixture-dir",
            str(fixture),
            "--out",
            str(out),
        ],
    )
    assert ran.exit_code == 0, ran.output
    report = json.loads(out.read_text())
    assert report["metrics"]["hybrid.hit@5"] == 1.0
    assert report["engine_backend"] == "llama.cpp"

    leak_out = env / "l.json"
    leak = runner.invoke(
        cli.app,
        [
            "eval",
            "leak",
            "--golden-dir",
            str(golden),
            "--fixture-dir",
            str(fixture),
            "--out",
            str(leak_out),
        ],
    )
    assert leak.exit_code == 0, leak.output
    assert json.loads(leak_out.read_text())["metrics"]["leaks"] == 0


def test_check_exits_one_below_threshold(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    thresholds = env / "thresholds.json"
    thresholds.write_text(
        json.dumps({"retrieval": {"hybrid.hit@5": 0.9}, "leak": {"positive_control": 1}})
    )
    monkeypatch.setattr(eval_cli, "THRESHOLDS_PATH", thresholds)
    monkeypatch.setattr(eval_cli, "load_retrieval", lambda path, **kw: [])
    monkeypatch.setattr(eval_cli, "load_question_embeddings", lambda d: {})
    monkeypatch.setattr(
        eval_cli, "connect", lambda url: __import__("contextlib").nullcontext(object())
    )
    monkeypatch.setattr(
        eval_cli, "run_retrieval", lambda *a, **kw: SuiteResult({"hybrid.hit@5": 0.5}, [])
    )
    result = runner.invoke(cli.app, ["eval", "retrieval", "--check", "--out", str(env / "o.json")])
    assert result.exit_code == 1
    assert "hybrid.hit@5: 0.500 < 0.900" in result.output


def test_leak_check_gates_on_leaks_and_positive_control(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thresholds = env / "thresholds.json"
    thresholds.write_text(json.dumps({"retrieval": {}, "leak": {"positive_control": 18}}))
    monkeypatch.setattr(eval_cli, "THRESHOLDS_PATH", thresholds)
    monkeypatch.setattr(eval_cli, "load_leak", lambda path, **kw: [])
    monkeypatch.setattr(eval_cli, "load_question_embeddings", lambda d: {})
    monkeypatch.setattr(
        eval_cli,
        "load_principals",
        lambda path: {"auditor": Principal("auditor", Clearance.PROTECTED_B, frozenset({"DND"}))},
    )
    monkeypatch.setattr(
        eval_cli, "connect", lambda url: __import__("contextlib").nullcontext(object())
    )

    # Case A: forbidden chunks were returned; the positive control does not save the run.
    monkeypatch.setattr(
        eval_cli,
        "run_leak",
        lambda *a, **kw: SuiteResult({"leaks": 1.0, "checks": 3.0, "positive_control": 20.0}, []),
    )
    result = runner.invoke(cli.app, ["eval", "leak", "--check", "--out", str(env / "a.json")])
    assert result.exit_code == 1
    assert "forbidden chunks returned" in result.output

    # Case B: no leaks, but the positive control fell below its floor.
    monkeypatch.setattr(
        eval_cli,
        "run_leak",
        lambda *a, **kw: SuiteResult({"leaks": 0.0, "checks": 3.0, "positive_control": 10.0}, []),
    )
    result = runner.invoke(cli.app, ["eval", "leak", "--check", "--out", str(env / "b.json")])
    assert result.exit_code == 1
    assert "positive_control" in result.output

    # Case C: no leaks and the positive control meets its floor.
    monkeypatch.setattr(
        eval_cli,
        "run_leak",
        lambda *a, **kw: SuiteResult({"leaks": 0.0, "checks": 3.0, "positive_control": 20.0}, []),
    )
    result = runner.invoke(cli.app, ["eval", "leak", "--check", "--out", str(env / "c.json")])
    assert result.exit_code == 0
    assert "thresholds met" in result.output


def test_generation_refuses_same_model(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUDGE_MODEL", "ai/qwen3.5:9b-q4_K_M")
    monkeypatch.setenv("LLM_MODEL", "ai/qwen3.5:9b-q4_K_M")
    golden = _golden(env)
    monkeypatch.setattr(
        eval_cli, "connect", lambda url: __import__("contextlib").nullcontext(object())
    )
    result = runner.invoke(cli.app, ["eval", "generation", "--golden-dir", str(golden)])
    assert result.exit_code == 2
    assert "judge" in result.output


def test_agreement_reads_a_result_file(env: Path) -> None:
    path = env / "g.json"
    path.write_text(
        json.dumps(
            {
                "rows": [
                    {"verdict": {"faithful": True}, "human_check": True},
                    {"verdict": {"faithful": True}, "human_check": False},
                ]
            }
        )
    )
    result = runner.invoke(cli.app, ["eval", "agreement", str(path)])
    assert result.exit_code == 0
    assert "0.500" in result.output
