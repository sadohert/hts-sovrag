"""One envelope for every suite's result, and the thresholds CI gates on.

Model tags carry the quantization and the engine labels name the runtime, so a result is
never read as "the model" but as "this model, at this precision, on this backend, at this
commit".
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sovrag.config import Settings

RESULTS_DIR = Path("eval/results")
THRESHOLDS_PATH = Path("eval/thresholds.json")


@dataclass(frozen=True)
class SuiteResult:
    metrics: dict[str, float]
    rows: list[dict[str, Any]]


@dataclass(frozen=True)
class Report:
    suite: str
    ran_at: str
    commit: str
    embed_model: str
    llm_model: str
    judge_model: str
    engine: str
    engine_backend: str
    metrics: dict[str, float]
    rows: list[dict[str, Any]]


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
    except OSError, subprocess.CalledProcessError:
        return "unknown"
    return out.stdout.strip() or "unknown"


def make_report(suite: str, settings: Settings, result: SuiteResult) -> Report:
    return Report(
        suite=suite,
        ran_at=datetime.now(UTC).isoformat(),
        commit=git_commit(),
        embed_model=settings.embed_model,
        llm_model=settings.llm_model,
        judge_model=settings.judge_model,
        engine=settings.engine,
        engine_backend=settings.engine_backend,
        metrics=dict(result.metrics),
        rows=list(result.rows),
    )


def write_report(report: Report, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n", "utf-8")
    return path


def default_path(suite: str, results_dir: Path = RESULTS_DIR, today: date | None = None) -> Path:
    day = today or datetime.now(UTC).date()
    return results_dir / f"{suite}-{day.isoformat()}.json"


def load_thresholds(path: Path) -> dict[str, dict[str, float]]:
    raw = json.loads(path.read_text("utf-8"))
    return {suite: {k: float(v) for k, v in gates.items()} for suite, gates in raw.items()}


def check_thresholds(metrics: Mapping[str, float], thresholds: Mapping[str, float]) -> list[str]:
    misses: list[str] = []
    for key, floor in thresholds.items():
        if key not in metrics:
            misses.append(f"{key}: not reported")
        elif metrics[key] < floor:
            misses.append(f"{key}: {metrics[key]:.3f} < {floor:.3f}")
    return misses
