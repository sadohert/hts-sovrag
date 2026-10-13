"""Benchmark the Mattermost path: time the three hops of a reply from timestamps that already
exist, so nothing is added to the path being measured.

  hop 1  the person's post create_at      ->  the `resolve` audit event (the tool call arrived)
  hop 2  the `resolve` event              ->  the `answer` audit event (sovrag's pipeline)
  hop 3  the `answer` event               ->  the reply post's last update_at (the agent writes)

The runner logs in as a demo user, posts a question in the direct message with the assistant,
polls until the reply has stopped changing, reads the audit file out of the mcp container and
joins the two by Mattermost user id within the run's window. One JSON file per configuration
under eval/results/, labelled with the models, the tier, the billing mode and the hardware.

Usage (see docs/serving-backends.md):
  uv run python scripts/bench_mattermost.py --configuration A-local --cold
  uv run python scripts/bench_mattermost.py --configuration A-local --warm --passes 2
"""

from __future__ import annotations

import json
import statistics
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
GOLDEN = ROOT / "eval" / "golden" / "retrieval.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"
QUESTION_IDS = ("r01", "r02", "r03", "r04", "r09")  # answerable as analyst_dnd
BOT_USERNAME = "sovrag"
TIERS = ("local", "model-apis", "dedicated")
BILLING = ("none", "per-token", "per-gpu-minute")
HOP_NAMES = ("hop1_s", "hop2_s", "hop3_s", "end_to_end_s")


class BenchError(Exception):
    pass


# --- timing ------------------------------------------------------------------------------


def ms_to_s(ms: int) -> float:
    """Mattermost timestamps are milliseconds since the epoch."""
    return ms / 1000.0


def iso_to_s(ts: str) -> float:
    """Audit timestamps are RFC 3339 with an explicit offset; the offset is honoured."""
    return datetime.fromisoformat(ts).timestamp()


@dataclass(frozen=True)
class Hops:
    hop1_s: float
    hop2_s: float
    hop3_s: float
    hop3_first_token_s: float | None
    end_to_end_s: float


def hops(
    *, post_ms: int, resolve_ts: str, answer_ts: str, reply_first_ms: int, reply_last_ms: int
) -> Hops:
    t0 = ms_to_s(post_ms)
    t1 = iso_to_s(resolve_ts)
    t2 = iso_to_s(answer_ts)
    t3_first = ms_to_s(reply_first_ms)
    t3 = ms_to_s(reply_last_ms)
    # A reply created before the answer event is a placeholder the plugin posted earlier; its
    # create_at then says nothing about when the first token of the real reply arrived.
    first_token = t3_first - t2 if t3_first >= t2 else None
    return Hops(
        hop1_s=t1 - t0,
        hop2_s=t2 - t1,
        hop3_s=t3 - t2,
        hop3_first_token_s=first_token,
        end_to_end_s=t3 - t0,
    )


# --- reply detection and the audit join --------------------------------------------------


def settled(samples: Sequence[tuple[float, int]], settle_s: float) -> bool:
    """`samples` are (poll time in seconds, the reply post's update_at) in poll order. The reply
    has settled when its latest update_at has held for at least `settle_s` of polling."""
    if not samples:
        return False
    last_t, last_update = samples[-1]
    since = last_t
    for t, update in reversed(samples):
        if update != last_update:
            break
        since = t
    return last_t - since >= settle_s


def join_audit(
    lines: Iterable[str], *, user_id: str, after_ms: int, before_ms: int
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The first `resolve` event for `user_id` inside the window whose request id also has an
    `answer` event, with that answer. None when the run left no such pair."""
    events = [json.loads(line) for line in lines if line.strip()]
    lo, hi = ms_to_s(after_ms), ms_to_s(before_ms)
    answers = {e["request_id"]: e for e in events if e.get("event") == "answer"}
    for e in events:
        if e.get("event") != "resolve" or e.get("user_id") != user_id:
            continue
        if not lo <= iso_to_s(e["ts"]) <= hi:
            continue
        answer = answers.get(e.get("request_id"))
        if answer is not None:
            return e, answer
    return None


# --- labels and records -------------------------------------------------------------------


@dataclass(frozen=True)
class Labels:
    configuration: str
    plugin_model: str
    plugin_precision: str
    generator_model: str
    generator_precision: str
    tier: str
    billing: str
    engine: str
    engine_backend: str
    hardware: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}, not {self.tier!r}")
        if self.billing not in BILLING:
            raise ValueError(f"billing must be one of {BILLING}, not {self.billing!r}")


@dataclass
class Run:
    question_id: str
    question: str
    state: str  # "cold" or "warm"
    post_ms: int
    request_id: str | None
    refused: bool | None
    resolve_ts: str | None
    answer_ts: str | None
    reply_first_ms: int | None
    reply_last_ms: int | None
    hops: Hops | None
    error: str | None


def summarise(runs: Sequence[Run]) -> dict[str, dict[str, float | int]]:
    """Per state: how many runs succeeded and failed, then mean and median of every hop over
    the successful ones. A state with no successful run is absent."""
    out: dict[str, dict[str, float | int]] = {}
    for state in ("cold", "warm"):
        ok = [r.hops for r in runs if r.state == state and r.hops is not None]
        if not ok:
            continue
        entry: dict[str, float | int] = {
            "n": len(ok),
            "failed": sum(1 for r in runs if r.state == state and r.hops is None),
        }
        for name in HOP_NAMES:
            values = [getattr(h, name) for h in ok]
            entry[f"{name}_mean"] = round(statistics.fmean(values), 2)
            entry[f"{name}_median"] = round(statistics.median(values), 2)
        first = [h.hop3_first_token_s for h in ok if h.hop3_first_token_s is not None]
        if first:
            entry["hop3_first_token_s_median"] = round(statistics.median(first), 2)
        out[state] = entry
    return out


def table_row(labels: Labels, summary: dict[str, dict[str, float | int]]) -> str:
    """One markdown row for docs/serving-backends.md: warm medians, the cold end-to-end, n."""
    warm = summary.get("warm")
    cold = summary.get("cold")

    def cell(entry: dict[str, float | int] | None, key: str) -> str:
        return f"{entry[key]:.1f}" if entry is not None and key in entry else "n/a"

    return (
        f"| {labels.configuration}"
        f" | {labels.plugin_model} ({labels.plugin_precision})"
        f" | {labels.generator_model} ({labels.generator_precision})"
        f" | {labels.tier} | {labels.billing}"
        f" | {cell(warm, 'hop1_s_median')} | {cell(warm, 'hop2_s_median')}"
        f" | {cell(warm, 'hop3_s_median')} | {cell(warm, 'end_to_end_s_median')}"
        f" | {cell(cold, 'end_to_end_s_median')} | {warm['n'] if warm else 0} |"
    )


# --- the machine ---------------------------------------------------------------------------


def models_loaded(docker_model_ps: str) -> list[str]:
    """Model names from `docker model ps`; empty when only the header is printed."""
    lines = [line for line in docker_model_ps.splitlines() if line.strip()]
    return [line.split()[0] for line in lines[1:]]


def hardware(run: Callable[[list[str]], str]) -> dict[str, str]:
    """Labels read from the machine at run time, never typed. A command that fails is `unknown`."""

    def q(cmd: list[str]) -> str:
        try:
            return run(cmd).strip()
        except OSError, subprocess.CalledProcessError:
            return "unknown"

    memsize = q(["sysctl", "-n", "hw.memsize"])
    memory_gb = str(int(memsize) // 1024**3) if memsize.isdigit() else "unknown"
    model_runner = q(["docker", "model", "version"])
    if model_runner != "unknown":
        versions = [
            w for line in model_runner.splitlines() if "Version" in line for w in line.split()
        ]
        model_runner = versions[-1] if versions else "unknown"
    return {
        "machine": q(["sysctl", "-n", "hw.model"]),
        "chip": q(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "memory_gb": memory_gb,
        "os": q(["sw_vers", "-productVersion"]),
        "docker": q(["docker", "version", "--format", "{{.Server.Version}}"]),
        "model_runner": model_runner,
    }


def run_command(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


# --- questions -----------------------------------------------------------------------------


def load_questions(path: Path, ids: Sequence[str]) -> list[tuple[str, str]]:
    """(id, question) for each id, in the order given, from the retrieval golden set."""
    by_id: dict[str, str] = {}
    for line in path.read_text("utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            by_id[row["id"]] = row["question"]
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise BenchError(f"{path}: no question with id {', '.join(missing)}")
    return [(i, by_id[i]) for i in ids]
