"""Result files carry the model labels; thresholds compare and report misses."""

import json
from datetime import date
from pathlib import Path

from sovrag.config import Settings
from sovrag.eval.report import (
    SuiteResult,
    check_thresholds,
    default_path,
    load_thresholds,
    make_report,
    write_report,
)


def test_report_carries_models_engine_and_metrics(tmp_path: Path) -> None:
    settings = Settings(
        llm_model="ai/qwen3.5:9b-q4_K_M",
        judge_model="ai/gemma4:12b-q4_K_M",
        embed_model="hf.co/gpustack/bge-m3-gguf:Q8_0",
        engine="docker-model-runner",
        engine_backend="llama.cpp",
    )
    report = make_report("retrieval", settings, SuiteResult({"hybrid.hit@5": 0.9}, [{"id": "r01"}]))
    path = write_report(report, tmp_path / "r.json")
    data = json.loads(path.read_text())
    assert data["suite"] == "retrieval"
    assert data["llm_model"] == "ai/qwen3.5:9b-q4_K_M"
    assert data["judge_model"] == "ai/gemma4:12b-q4_K_M"
    assert data["embed_model"] == "hf.co/gpustack/bge-m3-gguf:Q8_0"
    assert data["engine"] == "docker-model-runner"
    assert data["engine_backend"] == "llama.cpp"
    assert data["metrics"] == {"hybrid.hit@5": 0.9}
    assert data["rows"] == [{"id": "r01"}]
    assert data["commit"] and data["ran_at"]


def test_default_path_is_suite_and_date() -> None:
    assert default_path("leak", Path("eval/results"), date(2026, 9, 22)) == Path(
        "eval/results/leak-2026-09-22.json"
    )


def test_thresholds_report_each_miss(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"retrieval": {"hybrid.hit@5": 0.8, "vector.mrr": 0.7}}))
    thresholds = load_thresholds(path)["retrieval"]
    assert check_thresholds({"hybrid.hit@5": 0.9, "vector.mrr": 0.7}, thresholds) == []
    assert check_thresholds({"hybrid.hit@5": 0.75}, thresholds) == [
        "hybrid.hit@5: 0.750 < 0.800",
        "vector.mrr: not reported",
    ]
