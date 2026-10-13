# Serving-Backend Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Time the three hops of a Mattermost reply from timestamps that already exist, switch the plugin model and then the sovrag generator to Baseten by configuration only, and record a labelled benchmark table with a write-up on the serving-backend tradeoff.

**Architecture:** A benchmark runner in `scripts/` posts a question as a demo user, polls for the assistant's reply, and joins the Mattermost post timestamps with the `resolve` and `answer` audit events to compute hops 1, 2 and 3. The seed script learns an API key and a service name so the Agents plugin can point at Baseten, and a Compose override moves sovrag's own generator there. Retrieval, entitlement SQL, identity binding and the audit log do not change.

**Tech Stack:** Python 3.14, httpx2, pytest, Docker Compose v5 (`!reset` merge tag), Mattermost REST API v4, Baseten Model APIs (OpenAI-compatible at `https://inference.baseten.co/v1`).

**Spec:** `docs/superpowers/specs/2026-10-11-serving-backend-benchmark-design.md`

## Global Constraints

- Nothing under `src/sovrag/retrieve/`, `src/sovrag/auth/`, `src/sovrag/api/gate.py`, the `_Gate` in `src/sovrag/mcp/server.py`, or `src/sovrag/audit.py` changes.
- No new dependency. The runner uses `httpx2`, which is already a dependency.
- Secrets: `BASETEN_API_KEY` and `AGENT_LLM_API_KEY` are blank in `.env.example`, never printed by any script, never written into a results file, never committed. `.env` is git-ignored already.
- Env names, a decision taken while planning: the override reads `BASETEN_BASE_URL`, `BASETEN_MODEL` and `BASETEN_API_KEY` rather than `LLM_*`, because a developer's `.env` sets `LLM_BASE_URL` to the host-side Model Runner address and the container must not inherit it. The spec's intent (configuration only, from `.env`, never committed) is unchanged.
- Every results file is labelled: plugin model and precision, generator model and precision, tier (`local`, `model-apis`, `dedicated`), billing (`none`, `per-token`, `per-gpu-minute`), engine, engine backend, hardware.
- Fewer than five warm runs is not a row. Cold is the first run after `docker model unload --all` with `docker model ps` confirmed empty.
- Prose in every doc: no em dashes; no "delve", "leverage", "seamless", "journey"; every number marked measured, observed or estimated.
- Commit messages carry the attribution trailers used on this repository and never refer to anything outside the repository.
- Versions in docs are read at run time or checked upstream, never typed from memory.
- Before bringing the local stack up for a measurement task, stop and ask: the machine's memory is the person's call.

## Review Focus

1. The plugin posts a placeholder reply before it calls the tool, so the reply's `create_at` precedes the `answer` event: first-token time must be `None` and hop 3 must still be measured from the `answer` event to the last edit. Pinned in Task 1 (`test_first_token_is_none_when_the_reply_predates_the_answer`).
2. The plugin calls `search` before `answer`, leaving two `resolve` events for the user in the window: the join must pick the one whose `request_id` has an `answer` event. Pinned in Task 1 (`test_join_skips_a_resolve_without_an_answer`).
3. A run that times out is recorded as failed and counted, not dropped, and the results file is still written. Pinned in Task 2 (`test_a_timeout_is_a_failed_run_not_a_missing_one`).
4. Audit timestamps carry `+00:00`; the conversion must respect the offset, not treat the string as local time. Pinned in Task 1 (`test_iso_to_s_respects_the_utc_offset`).
5. A reply left in the channel by an earlier question must not be taken as this question's reply. Pinned in Task 2 (`test_a_reply_older_than_the_question_is_ignored`).

---

## File structure

| File | Responsibility |
|---|---|
| `scripts/bench_mattermost.py` | the runner: pure timing functions at the top, Mattermost client and orchestration below, `main` |
| `tests/test_bench_mattermost.py` | unit tests for the pure functions and the orchestration against a fake server |
| `scripts/seed_mattermost.py` | gains `--llm-api-key` and `--llm-service-name`, env-driven defaults |
| `tests/test_seed_mattermost.py` | the new arguments and that the key is never printed |
| `compose.baseten.yaml` | override: generator on Baseten for `app` and `mcp` |
| `tests/test_compose.py` | the override parses and sets what it must |
| `.env.example` | the new variables, blank |
| `docs/adr/0017-inference-placement.md` | the decision |
| `docs/serving-backends.md` | method, table, tradeoff, cost |
| `README.md`, `CLAUDE.md`, `docs/architecture.md` | pointers |
| `eval/results/bench-mattermost-*.json` | one per configuration, generated |

---

### Task 1: The runner's pure core

**Files:**
- Create: `scripts/bench_mattermost.py`
- Test: `tests/test_bench_mattermost.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (used by Task 2 and the measurement tasks):
  - `ms_to_s(ms: int) -> float`, `iso_to_s(ts: str) -> float`
  - `Hops` frozen dataclass: `hop1_s, hop2_s, hop3_s: float`, `hop3_first_token_s: float | None`, `end_to_end_s: float`
  - `hops(*, post_ms: int, resolve_ts: str, answer_ts: str, reply_first_ms: int, reply_last_ms: int) -> Hops`
  - `settled(samples: Sequence[tuple[float, int]], settle_s: float) -> bool`
  - `join_audit(lines: Iterable[str], *, user_id: str, after_ms: int, before_ms: int) -> tuple[dict[str, Any], dict[str, Any]] | None`
  - `Labels` frozen dataclass with fields `configuration, plugin_model, plugin_precision, generator_model, generator_precision, tier, billing, engine, engine_backend: str` and `hardware: dict[str, str]`; `TIERS = ("local", "model-apis", "dedicated")`, `BILLING = ("none", "per-token", "per-gpu-minute")`; invalid tier or billing raises `ValueError`
  - `Run` dataclass: `question_id, question, state: str`; `post_ms: int`; `request_id: str | None`; `refused: bool | None`; `resolve_ts, answer_ts: str | None`; `reply_first_ms, reply_last_ms: int | None`; `hops: Hops | None`; `error: str | None`
  - `summarise(runs: Sequence[Run]) -> dict[str, dict[str, float | int]]`
  - `table_row(labels: Labels, summary: dict[str, dict[str, float | int]]) -> str`
  - `models_loaded(docker_model_ps: str) -> list[str]`
  - `hardware(run: Callable[[list[str]], str]) -> dict[str, str]`
  - `QUESTION_IDS = ("r01", "r02", "r03", "r04", "r09")`, `load_questions(path: Path, ids: Sequence[str]) -> list[tuple[str, str]]`

- [ ] **Step 1: Write the failing tests for timestamps and hops**

```python
"""The Mattermost benchmark runner: hop arithmetic, the settle rule, the audit join, labels,
summaries, and the orchestration against a fake server. Nothing here talks to a network."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx2 as httpx
import pytest


def _load() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "bench_mattermost.py"
    spec = importlib.util.spec_from_file_location("bench_mattermost", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = _load()

T0_MS = 1_760_000_000_000  # an arbitrary epoch millisecond
T0_S = T0_MS / 1000.0


def _iso(seconds_after_t0: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(T0_S + seconds_after_t0, UTC).isoformat()


def test_ms_to_s_and_iso_to_s_agree_on_the_same_instant() -> None:
    assert bench.ms_to_s(T0_MS) == pytest.approx(T0_S)
    assert bench.iso_to_s(_iso(0)) == pytest.approx(T0_S)


def test_iso_to_s_respects_the_utc_offset() -> None:
    """The audit writer stamps `datetime.now(UTC).isoformat()`, which ends in +00:00. A naive
    parse would read it as local time and shift every hop by the machine's offset."""
    assert bench.iso_to_s("1970-01-01T01:00:00+00:00") == pytest.approx(3600.0)
    assert bench.iso_to_s("1970-01-01T01:00:00+01:00") == pytest.approx(0.0)


def test_hops_are_the_differences_between_the_four_marks() -> None:
    h = bench.hops(
        post_ms=T0_MS,
        resolve_ts=_iso(12.0),
        answer_ts=_iso(16.5),
        reply_first_ms=T0_MS + 17_000,
        reply_last_ms=T0_MS + 40_000,
    )
    assert h.hop1_s == pytest.approx(12.0)
    assert h.hop2_s == pytest.approx(4.5)
    assert h.hop3_s == pytest.approx(23.5)
    assert h.hop3_first_token_s == pytest.approx(0.5)
    assert h.end_to_end_s == pytest.approx(40.0)


def test_first_token_is_none_when_the_reply_predates_the_answer() -> None:
    """The plugin may post a placeholder before it calls the tool. Then the reply's create_at
    says nothing about hop 3's first token, and hop 3 still runs from the answer to the last edit."""
    h = bench.hops(
        post_ms=T0_MS,
        resolve_ts=_iso(12.0),
        answer_ts=_iso(16.5),
        reply_first_ms=T0_MS + 11_000,
        reply_last_ms=T0_MS + 40_000,
    )
    assert h.hop3_first_token_s is None
    assert h.hop3_s == pytest.approx(23.5)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_bench_mattermost.py -q`
Expected: FAIL at import, `FileNotFoundError` or `AttributeError` because `scripts/bench_mattermost.py` does not exist.

- [ ] **Step 3: Write the module header and the timing functions**

Create `scripts/bench_mattermost.py`:

```python
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

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx2 as httpx

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_bench_mattermost.py -q`
Expected: 4 passed.

- [ ] **Step 5: Write the failing tests for the settle rule and the audit join**

Append to `tests/test_bench_mattermost.py`:

```python
def test_settled_needs_the_last_update_to_hold_for_the_settle_interval() -> None:
    assert bench.settled([], 3.0) is False
    assert bench.settled([(0.0, 10), (1.0, 10), (2.0, 10)], 3.0) is False
    assert bench.settled([(0.0, 10), (1.0, 10), (3.0, 10)], 3.0) is True
    # An edit resets the clock: the value 11 has only been seen for two seconds.
    assert bench.settled([(0.0, 10), (1.0, 10), (3.0, 10), (4.0, 11), (6.0, 11)], 3.0) is False
    assert bench.settled([(0.0, 10), (4.0, 11), (7.0, 11)], 3.0) is True


def _audit_lines() -> list[str]:
    rows = [
        {"ts": _iso(-30.0), "event": "resolve", "request_id": "old", "user_id": "u1"},
        {"ts": _iso(-29.0), "event": "answer", "request_id": "old", "refused": False},
        {"ts": _iso(12.0), "event": "resolve", "request_id": "s1", "user_id": "u1"},
        {"ts": _iso(12.5), "event": "search", "request_id": "s1"},
        {"ts": _iso(13.0), "event": "resolve", "request_id": "a1", "user_id": "u1"},
        {"ts": _iso(13.2), "event": "resolve", "request_id": "x1", "user_id": "u2"},
        {"ts": _iso(16.5), "event": "answer", "request_id": "a1", "refused": True},
    ]
    return [json.dumps(r) for r in rows] + [""]


def test_join_finds_the_resolve_in_the_window_and_its_answer() -> None:
    joined = bench.join_audit(
        _audit_lines(), user_id="u1", after_ms=T0_MS, before_ms=T0_MS + 40_000
    )
    assert joined is not None
    resolve, answer = joined
    assert resolve["request_id"] == "a1" and answer["refused"] is True


def test_join_skips_a_resolve_without_an_answer() -> None:
    """The plugin may call `search` before `answer`; that resolve has no answer event and must
    not be taken as the run's tool call."""
    joined = bench.join_audit(
        _audit_lines(), user_id="u1", after_ms=T0_MS, before_ms=T0_MS + 40_000
    )
    assert joined is not None and joined[0]["request_id"] != "s1"


def test_join_ignores_other_users_and_events_outside_the_window() -> None:
    assert (
        bench.join_audit(_audit_lines(), user_id="u2", after_ms=T0_MS, before_ms=T0_MS + 40_000)
        is None
    )
    assert (
        bench.join_audit(_audit_lines(), user_id="u1", after_ms=T0_MS + 20_000, before_ms=T0_MS + 40_000)
        is None
    )
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `uv run pytest tests/test_bench_mattermost.py -q`
Expected: 4 failed with `AttributeError: module 'bench_mattermost' has no attribute 'settled'` (and `join_audit`).

- [ ] **Step 7: Implement the settle rule and the join**

Append to `scripts/bench_mattermost.py` after `hops`:

```python
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
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_bench_mattermost.py -q`
Expected: 8 passed.

- [ ] **Step 9: Write the failing tests for labels, runs, summaries, the table row, loaded models, hardware and questions**

Append to `tests/test_bench_mattermost.py`:

```python
def _labels(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "configuration": "A-local",
        "plugin_model": "ai/qwen3.5:9b-q4_K_M",
        "plugin_precision": "Q4_K_M",
        "generator_model": "ai/qwen3.5:9b-q4_K_M",
        "generator_precision": "Q4_K_M",
        "tier": "local",
        "billing": "none",
        "engine": "docker-model-runner",
        "engine_backend": "llama.cpp",
        "hardware": {"chip": "Apple M3 Max"},
    }
    values.update(overrides)
    return bench.Labels(**values)


def test_labels_reject_an_unknown_tier_or_billing_mode() -> None:
    with pytest.raises(ValueError):
        _labels(tier="cloud")
    with pytest.raises(ValueError):
        _labels(billing="monthly")
    assert _labels(tier="model-apis", billing="per-token").tier == "model-apis"


def _run(state: str, hop1: float, hop2: float, hop3: float, *, failed: bool = False) -> Any:
    h = None if failed else bench.Hops(hop1, hop2, hop3, 0.4, hop1 + hop2 + hop3)
    return bench.Run(
        question_id="r01",
        question="q",
        state=state,
        post_ms=T0_MS,
        request_id=None if failed else "req",
        refused=None if failed else False,
        resolve_ts=None,
        answer_ts=None,
        reply_first_ms=None,
        reply_last_ms=None,
        hops=h,
        error="timeout" if failed else None,
    )


def test_summary_reports_n_failed_mean_and_median_per_state() -> None:
    runs = [
        _run("cold", 30.0, 10.0, 20.0),
        _run("warm", 10.0, 4.0, 20.0),
        _run("warm", 12.0, 4.0, 24.0),
        _run("warm", 14.0, 5.0, 25.0),
        _run("warm", 0.0, 0.0, 0.0, failed=True),
    ]
    s = bench.summarise(runs)
    assert s["cold"]["n"] == 1 and s["cold"]["end_to_end_s_median"] == 60.0
    assert s["warm"]["n"] == 3 and s["warm"]["failed"] == 1
    assert s["warm"]["hop1_s_mean"] == 12.0 and s["warm"]["hop1_s_median"] == 12.0
    assert s["warm"]["hop2_s_median"] == 4.0 and s["warm"]["hop3_s_median"] == 24.0
    assert s["warm"]["hop3_first_token_s_median"] == 0.4
    assert "cold" not in bench.summarise(runs[1:])


def test_table_row_names_every_label_and_the_medians() -> None:
    s = bench.summarise([_run("warm", 10.0, 4.0, 20.0)] * 5 + [_run("cold", 30.0, 10.0, 20.0)])
    row = bench.table_row(_labels(), s)
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    assert cells == [
        "A-local",
        "ai/qwen3.5:9b-q4_K_M (Q4_K_M)",
        "ai/qwen3.5:9b-q4_K_M (Q4_K_M)",
        "local",
        "none",
        "10.0",
        "4.0",
        "20.0",
        "34.0",
        "60.0",
        "5",
    ]


def test_table_row_says_na_without_a_cold_run() -> None:
    row = bench.table_row(_labels(tier="model-apis", billing="per-token"), bench.summarise([_run("warm", 1.0, 1.0, 1.0)]))
    assert "| n/a |" in row


def test_models_loaded_reads_docker_model_ps() -> None:
    header = "MODEL NAME  BACKEND  MODE  UNTIL  \n"
    assert bench.models_loaded(header) == []
    assert bench.models_loaded(header + "ai/qwen3.5:9b-q4_K_M  llama.cpp  chat  4 minutes\n") == [
        "ai/qwen3.5:9b-q4_K_M"
    ]


def test_hardware_reads_each_value_and_says_unknown_when_a_command_fails() -> None:
    answers = {
        "hw.model": "Mac15,9\n",
        "machdep.cpu.brand_string": "Apple M3 Max\n",
        "hw.memsize": str(48 * 1024**3) + "\n",
        "-productVersion": "26.6.2\n",
        "{{.Server.Version}}": "29.8.2\n",
    }

    def run(cmd: list[str]) -> str:
        if cmd[:2] == ["docker", "model"]:
            raise OSError("no model runner")
        for key, value in answers.items():
            if key in cmd:
                return value
        raise AssertionError(cmd)

    hw = bench.hardware(run)
    assert hw == {
        "machine": "Mac15,9",
        "chip": "Apple M3 Max",
        "memory_gb": "48",
        "os": "26.6.2",
        "docker": "29.8.2",
        "model_runner": "unknown",
    }


def test_load_questions_keeps_the_requested_order(tmp_path: Path) -> None:
    golden = tmp_path / "retrieval.jsonl"
    golden.write_text(
        "\n".join(
            json.dumps({"id": i, "question": f"Q {i}", "gold": [], "reference_answer": "", "notes": ""})
            for i in ("r01", "r02", "r09", "r03")
        )
        + "\n"
    )
    assert bench.load_questions(golden, ("r09", "r01")) == [("r09", "Q r09"), ("r01", "Q r01")]
    with pytest.raises(bench.BenchError):
        bench.load_questions(golden, ("r99",))
```

- [ ] **Step 10: Run the tests to verify they fail**

Run: `uv run pytest tests/test_bench_mattermost.py -q`
Expected: 7 failed with `AttributeError` for `Labels`, `Run`, `summarise`, `table_row`, `models_loaded`, `hardware`, `load_questions`.

- [ ] **Step 11: Implement labels, runs, summaries, the table row, loaded models, hardware and questions**

Append to `scripts/bench_mattermost.py`:

```python
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
        except (OSError, subprocess.CalledProcessError):
            return "unknown"

    memsize = q(["sysctl", "-n", "hw.memsize"])
    memory_gb = str(int(memsize) // 1024**3) if memsize.isdigit() else "unknown"
    model_runner = q(["docker", "model", "version"])
    if model_runner != "unknown":
        versions = [w for line in model_runner.splitlines() if "Version" in line for w in line.split()]
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
```

- [ ] **Step 12: Run the tests to verify they pass**

Run: `uv run pytest tests/test_bench_mattermost.py -q`
Expected: 15 passed.

- [ ] **Step 13: Lint, type-check and commit**

Run: `uv run ruff format scripts/bench_mattermost.py tests/test_bench_mattermost.py && uv run ruff check --no-cache scripts tests && uv run mypy scripts/bench_mattermost.py`
Expected: files formatted, `All checks passed!`, `Success: no issues found`.

```bash
git add scripts/bench_mattermost.py tests/test_bench_mattermost.py
git commit -m "Benchmark runner: hop arithmetic, settle rule, audit join and labels"
```

---

### Task 2: The runner's orchestration and CLI

**Files:**
- Modify: `scripts/bench_mattermost.py` (append)
- Test: `tests/test_bench_mattermost.py` (append)

**Interfaces:**
- Consumes from Task 1: everything listed under Produces.
- Produces:
  - `class Mattermost` with `login(username, password) -> str` (returns the user id), `user_id(username) -> str`, `direct_channel(a, b) -> str`, `post(channel_id, message) -> dict[str, Any]`, `posts_since(channel_id, since_ms) -> list[dict[str, Any]]`
  - `ask(mm, *, channel_id, bot_user_id, question, settle_s, timeout_s, poll_s, clock, sleep) -> tuple[int, int, int]` returning `(post_ms, reply_first_ms, reply_last_ms)`, raising `BenchError("timeout ...")`
  - `run_one(mm, *, channel_id, bot_user_id, user_id, question_id, question, state, audit, settle_s, timeout_s, poll_s, clock, sleep) -> Run` where `audit: Callable[[], list[str]]`
  - `report(labels, runs, *, ran_at, commit) -> dict[str, Any]`, `merge_runs(existing: dict[str, Any] | None, new: list[Run]) -> list[Run]`, `result_path(configuration, today) -> Path`
  - `main(argv) -> int`

- [ ] **Step 1: Write the failing tests for the fake-server orchestration**

Append to `tests/test_bench_mattermost.py`:

```python
class FakeMattermost:
    """Enough of the REST API for one question: login, user lookup, the direct channel, the
    post, and the channel's posts as the assistant's reply streams in over successive polls."""

    def __init__(self, *, reply_updates: list[int], reply_create_ms: int, stale_reply: bool = False) -> None:
        self.reply_updates = reply_updates  # update_at seen on poll 1, 2, ...
        self.reply_create_ms = reply_create_ms
        self.stale_reply = stale_reply
        self.polls = 0
        self.posted: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/api/v4/users/login":
            return httpx.Response(200, json={"id": "u1", "username": "analyst_dnd"}, headers={"Token": "tok"})
        if path == "/api/v4/users/username/sovrag":
            return httpx.Response(200, json={"id": "bot1"})
        if path == "/api/v4/channels/direct":
            return httpx.Response(201, json={"id": "dm1"})
        if path == "/api/v4/posts" and method == "POST":
            self.posted.append(json.loads(request.content)["message"])
            return httpx.Response(201, json={"id": "p1", "create_at": T0_MS, "user_id": "u1"})
        if path == "/api/v4/channels/dm1/posts":
            self.polls += 1
            posts: dict[str, Any] = {}
            if self.stale_reply:
                posts["old"] = {"id": "old", "user_id": "bot1", "create_at": T0_MS - 60_000, "update_at": T0_MS - 50_000, "message": "stale"}
            if self.polls <= len(self.reply_updates):
                posts["r1"] = {"id": "r1", "user_id": "bot1", "create_at": self.reply_create_ms, "update_at": self.reply_updates[self.polls - 1], "message": "x" * self.polls}
            elif self.reply_updates:
                posts["r1"] = {"id": "r1", "user_id": "bot1", "create_at": self.reply_create_ms, "update_at": self.reply_updates[-1], "message": "done"}
            return httpx.Response(200, json={"order": list(posts), "posts": posts})
        return httpx.Response(500, json={"message": f"unexpected {method} {path}"})


class FakeClock:
    def __init__(self) -> None:
        self.now = T0_S

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _client(server: FakeMattermost) -> Any:
    mm = bench.Mattermost("http://mm.test", transport=httpx.MockTransport(server))
    assert mm.login("analyst_dnd", "pw") == "u1"
    return mm


def test_ask_returns_the_post_time_and_the_reply_marks_once_settled() -> None:
    server = FakeMattermost(reply_updates=[T0_MS + 17_000, T0_MS + 30_000, T0_MS + 40_000], reply_create_ms=T0_MS + 17_000)
    clock = FakeClock()
    marks = bench.ask(
        _client(server),
        channel_id="dm1",
        bot_user_id="bot1",
        question="How much for NORAD?",
        settle_s=3.0,
        timeout_s=120.0,
        poll_s=1.0,
        clock=clock,
        sleep=clock.sleep,
    )
    assert marks == (T0_MS, T0_MS + 17_000, T0_MS + 40_000)
    assert server.posted == ["How much for NORAD?"]


def test_a_reply_older_than_the_question_is_ignored() -> None:
    server = FakeMattermost(reply_updates=[], reply_create_ms=0, stale_reply=True)
    clock = FakeClock()
    with pytest.raises(bench.BenchError, match="timeout"):
        bench.ask(
            _client(server),
            channel_id="dm1",
            bot_user_id="bot1",
            question="q",
            settle_s=3.0,
            timeout_s=10.0,
            poll_s=1.0,
            clock=clock,
            sleep=clock.sleep,
        )


def test_run_one_joins_the_audit_and_computes_hops() -> None:
    server = FakeMattermost(reply_updates=[T0_MS + 17_000, T0_MS + 40_000, T0_MS + 40_000], reply_create_ms=T0_MS + 17_000)
    clock = FakeClock()
    run = bench.run_one(
        _client(server),
        channel_id="dm1",
        bot_user_id="bot1",
        user_id="u1",
        question_id="r01",
        question="q",
        state="warm",
        audit=_audit_lines,
        settle_s=3.0,
        timeout_s=120.0,
        poll_s=1.0,
        clock=clock,
        sleep=clock.sleep,
    )
    assert run.error is None and run.request_id == "a1" and run.refused is True
    assert run.hops is not None and run.hops.hop1_s == pytest.approx(13.0)
    assert run.hops.hop2_s == pytest.approx(3.5) and run.hops.hop3_s == pytest.approx(23.5)


def test_a_timeout_is_a_failed_run_not_a_missing_one() -> None:
    server = FakeMattermost(reply_updates=[], reply_create_ms=0)
    clock = FakeClock()
    run = bench.run_one(
        _client(server),
        channel_id="dm1",
        bot_user_id="bot1",
        user_id="u1",
        question_id="r01",
        question="q",
        state="warm",
        audit=_audit_lines,
        settle_s=3.0,
        timeout_s=5.0,
        poll_s=1.0,
        clock=clock,
        sleep=clock.sleep,
    )
    assert run.hops is None and run.error is not None and "timeout" in run.error
    assert bench.summarise([run]) == {}


def test_a_run_without_audit_lines_is_failed_with_a_reason() -> None:
    server = FakeMattermost(reply_updates=[T0_MS + 17_000, T0_MS + 40_000], reply_create_ms=T0_MS + 17_000)
    clock = FakeClock()
    run = bench.run_one(
        _client(server),
        channel_id="dm1",
        bot_user_id="bot1",
        user_id="u9",
        question_id="r01",
        question="q",
        state="warm",
        audit=_audit_lines,
        settle_s=3.0,
        timeout_s=120.0,
        poll_s=1.0,
        clock=clock,
        sleep=clock.sleep,
    )
    assert run.hops is None and run.error == "no resolve/answer pair in the audit log for this run"


def test_report_merges_earlier_runs_and_never_carries_a_key(tmp_path: Path) -> None:
    earlier = bench.report(_labels(), [_run("cold", 30.0, 10.0, 20.0)], ran_at="2026-10-13T00:00:00+00:00", commit="abc1234")
    runs = bench.merge_runs(earlier, [_run("warm", 10.0, 4.0, 20.0)])
    assert [r.state for r in runs] == ["cold", "warm"]
    rep = bench.report(_labels(), runs, ran_at="2026-10-13T01:00:00+00:00", commit="abc1234")
    assert rep["summary"]["cold"]["n"] == 1 and rep["summary"]["warm"]["n"] == 1
    assert rep["labels"]["tier"] == "local" and rep["commit"] == "abc1234"
    text = json.dumps(rep)
    assert "api_key" not in text.lower() and "authorization" not in text.lower()
    assert bench.result_path("A-local", bench.date(2026, 10, 13)).name == "bench-mattermost-A-local-2026-10-13.json"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_bench_mattermost.py -q`
Expected: 7 failed with `AttributeError: module 'bench_mattermost' has no attribute 'Mattermost'` (and `ask`, `run_one`, `report`, `merge_runs`, `result_path`).

- [ ] **Step 3: Implement the client, the orchestration and the report**

Append to `scripts/bench_mattermost.py`:

```python
# --- Mattermost ----------------------------------------------------------------------------


class Mattermost:
    """A thin REST client. Every call raises BenchError on a 4xx or 5xx."""

    def __init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=60, transport=transport)
        self.token: str | None = None

    def call(self, method: str, path: str, *, json: Any = None, params: Any = None) -> httpx.Response:
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self._client.request(method, path, json=json, params=params, headers=headers)
        if response.status_code not in (200, 201):
            raise BenchError(f"{method} {path}: HTTP {response.status_code} {response.text[:200]}")
        return response

    def login(self, username: str, password: str) -> str:
        response = self.call("POST", "/api/v4/users/login", json={"login_id": username, "password": password})
        self.token = response.headers.get("Token")
        return str(response.json()["id"])

    def user_id(self, username: str) -> str:
        return str(self.call("GET", f"/api/v4/users/username/{username}").json()["id"])

    def direct_channel(self, a: str, b: str) -> str:
        return str(self.call("POST", "/api/v4/channels/direct", json=[a, b]).json()["id"])

    def post(self, channel_id: str, message: str) -> dict[str, Any]:
        body = self.call("POST", "/api/v4/posts", json={"channel_id": channel_id, "message": message}).json()
        return dict(body)

    def posts_since(self, channel_id: str, since_ms: int) -> list[dict[str, Any]]:
        body = self.call("GET", f"/api/v4/channels/{channel_id}/posts", params={"since": since_ms}).json()
        posts = body.get("posts", {})
        return [dict(posts[i]) for i in body.get("order", []) if i in posts]


# --- one run -------------------------------------------------------------------------------


def ask(
    mm: Mattermost,
    *,
    channel_id: str,
    bot_user_id: str,
    question: str,
    settle_s: float,
    timeout_s: float,
    poll_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[int, int, int]:
    """Post the question and poll until the assistant's reply has settled. Returns the post's
    create_at and the reply's create_at and last update_at, all in milliseconds."""
    post = mm.post(channel_id, question)
    post_ms = int(post["create_at"])
    started = clock()
    samples: list[tuple[float, int]] = []
    reply: dict[str, Any] | None = None
    while clock() - started < timeout_s:
        sleep(poll_s)
        replies = [
            p
            for p in mm.posts_since(channel_id, post_ms - 1)
            if p.get("user_id") == bot_user_id and int(p.get("create_at", 0)) >= post_ms
        ]
        if replies:
            reply = min(replies, key=lambda p: int(p["create_at"]))
            samples.append((clock(), int(reply["update_at"])))
            if reply.get("message") and settled(samples, settle_s):
                return post_ms, int(reply["create_at"]), int(reply["update_at"])
    raise BenchError(f"timeout after {timeout_s:.0f}s waiting for the reply to settle")


def run_one(
    mm: Mattermost,
    *,
    channel_id: str,
    bot_user_id: str,
    user_id: str,
    question_id: str,
    question: str,
    state: str,
    audit: Callable[[], list[str]],
    settle_s: float,
    timeout_s: float,
    poll_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> Run:
    run = Run(
        question_id=question_id,
        question=question,
        state=state,
        post_ms=0,
        request_id=None,
        refused=None,
        resolve_ts=None,
        answer_ts=None,
        reply_first_ms=None,
        reply_last_ms=None,
        hops=None,
        error=None,
    )
    try:
        post_ms, first_ms, last_ms = ask(
            mm,
            channel_id=channel_id,
            bot_user_id=bot_user_id,
            question=question,
            settle_s=settle_s,
            timeout_s=timeout_s,
            poll_s=poll_s,
            clock=clock,
            sleep=sleep,
        )
    except BenchError as exc:
        run.error = str(exc)
        return run
    run.post_ms, run.reply_first_ms, run.reply_last_ms = post_ms, first_ms, last_ms
    joined = join_audit(audit(), user_id=user_id, after_ms=post_ms, before_ms=last_ms + 1000)
    if joined is None:
        run.error = "no resolve/answer pair in the audit log for this run"
        return run
    resolve, answer = joined
    run.request_id = str(resolve["request_id"])
    run.refused = bool(answer.get("refused"))
    run.resolve_ts, run.answer_ts = str(resolve["ts"]), str(answer["ts"])
    run.hops = hops(
        post_ms=post_ms,
        resolve_ts=run.resolve_ts,
        answer_ts=run.answer_ts,
        reply_first_ms=first_ms,
        reply_last_ms=last_ms,
    )
    return run


# --- the results file ----------------------------------------------------------------------


def result_path(configuration: str, today: date) -> Path:
    return RESULTS_DIR / f"bench-mattermost-{configuration}-{today.isoformat()}.json"


def report(labels: Labels, runs: Sequence[Run], *, ran_at: str, commit: str) -> dict[str, Any]:
    return {
        "suite": "bench-mattermost",
        "ran_at": ran_at,
        "commit": commit,
        "labels": asdict(labels),
        "runs": [asdict(r) for r in runs],
        "summary": summarise(runs),
    }


def merge_runs(existing: dict[str, Any] | None, new: Sequence[Run]) -> list[Run]:
    """Earlier runs from the same configuration's file, then the new ones."""
    old: list[Run] = []
    for r in (existing or {}).get("runs", []):
        h = r.get("hops")
        old.append(Run(**{**r, "hops": Hops(**h) if h else None}))
    return old + list(new)


def git_commit() -> str:
    try:
        return run_command(["git", "rev-parse", "--short", "HEAD"]).strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_bench_mattermost.py -q`
Expected: 22 passed.

- [ ] **Step 5: Write the failing test for `main`**

Append to `tests/test_bench_mattermost.py`:

```python
def test_main_cold_refuses_when_a_model_is_still_loaded(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(bench, "run_command", lambda cmd: "MODEL NAME  BACKEND\nai/qwen3.5:9b-q4_K_M  llama.cpp\n" if cmd[:3] == ["docker", "model", "ps"] else "")
    code = bench.main(["--configuration", "A-local", "--cold", "--url", "http://mm.test"])
    assert code == 1
    assert "docker model unload --all" in capsys.readouterr().err


def test_main_writes_the_results_file_and_prints_the_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    server = FakeMattermost(reply_updates=[T0_MS + 17_000, T0_MS + 40_000, T0_MS + 40_000], reply_create_ms=T0_MS + 17_000)
    clock = FakeClock()
    golden = tmp_path / "retrieval.jsonl"
    golden.write_text(json.dumps({"id": "r01", "question": "q", "gold": [], "reference_answer": "", "notes": ""}) + "\n")
    monkeypatch.setattr(bench, "GOLDEN", golden)
    monkeypatch.setattr(bench, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(bench, "QUESTION_IDS", ("r01",))
    monkeypatch.setattr(bench, "run_command", lambda cmd: "\n".join(_audit_lines()) if cmd[:2] == ["docker", "compose"] else "x")
    monkeypatch.setattr(bench, "_transport", lambda: httpx.MockTransport(server))
    monkeypatch.setattr(bench, "_clock", lambda: clock)
    code = bench.main(["--configuration", "A-local", "--warm", "--passes", "2", "--url", "http://mm.test", "--password", "pw"])
    assert code == 0
    files = list(tmp_path.glob("bench-mattermost-A-local-*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["summary"]["warm"]["n"] == 2 and data["labels"]["configuration"] == "A-local"
    assert "| A-local |" in capsys.readouterr().out
```

- [ ] **Step 6: Run the test to verify it fails**

Run: `uv run pytest tests/test_bench_mattermost.py -q -k main`
Expected: 2 failed with `AttributeError: module 'bench_mattermost' has no attribute 'main'`.

- [ ] **Step 7: Implement `main`**

Append to `scripts/bench_mattermost.py`:

```python
# --- command line --------------------------------------------------------------------------


def _transport() -> httpx.BaseTransport | None:
    return None


def _clock() -> Any:
    class Wall:
        def __call__(self) -> float:
            return time.time()

        def sleep(self, seconds: float) -> None:
            time.sleep(seconds)

    return Wall()


def _audit_from_container() -> list[str]:
    out = run_command(["docker", "compose", "--profile", "mcp", "exec", "-T", "mcp", "cat", "/app/audit/audit.jsonl"])
    return out.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--configuration", required=True, help="A-local, B-plugin-baseten, C-both-baseten, D-dedicated")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--cold", action="store_true", help="one run of the first question; refuses if a model is loaded")
    mode.add_argument("--warm", action="store_true", help="every question, --passes times")
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument("--url", default=os.environ.get("MATTERMOST_SITE_URL", "http://localhost:8065"))
    parser.add_argument("--username", default="analyst_dnd")
    parser.add_argument("--password", default=os.environ.get("MATTERMOST_DEMO_PASSWORD", "Passw0rd!demo"))
    parser.add_argument("--audit", type=Path, default=None, help="read this file instead of the mcp container")
    parser.add_argument("--settle-s", type=float, default=3.0)
    parser.add_argument("--timeout-s", type=float, default=240.0)
    parser.add_argument("--poll-s", type=float, default=1.0)
    parser.add_argument("--plugin-model", default=os.environ.get("AGENT_LLM_MODEL") or os.environ.get("LLM_MODEL", "ai/qwen3.5:9b-q4_K_M"))
    parser.add_argument("--plugin-precision", default="Q4_K_M")
    parser.add_argument("--generator-model", default=os.environ.get("LLM_MODEL", "ai/qwen3.5:9b-q4_K_M"))
    parser.add_argument("--generator-precision", default="Q4_K_M")
    parser.add_argument("--tier", choices=TIERS, default="local")
    parser.add_argument("--billing", choices=BILLING, default="none")
    parser.add_argument("--engine", default=os.environ.get("ENGINE", "docker-model-runner"))
    parser.add_argument("--engine-backend", default=os.environ.get("ENGINE_BACKEND", "llama.cpp"))
    args = parser.parse_args(argv)

    if args.cold:
        loaded = models_loaded(run_command(["docker", "model", "ps"]))
        if loaded:
            print(f"bench: {', '.join(loaded)} still loaded; run `docker model unload --all` first", file=sys.stderr)
            return 1

    labels = Labels(
        configuration=args.configuration,
        plugin_model=args.plugin_model,
        plugin_precision=args.plugin_precision,
        generator_model=args.generator_model,
        generator_precision=args.generator_precision,
        tier=args.tier,
        billing=args.billing,
        engine=args.engine,
        engine_backend=args.engine_backend,
        hardware=hardware(run_command),
    )
    questions = load_questions(GOLDEN, QUESTION_IDS[:1] if args.cold else QUESTION_IDS)
    passes = 1 if args.cold else args.passes
    state = "cold" if args.cold else "warm"
    audit: Callable[[], list[str]] = (
        (lambda: args.audit.read_text("utf-8").splitlines()) if args.audit else _audit_from_container
    )
    clock = _clock()

    mm = Mattermost(args.url, transport=_transport())
    try:
        user_id = mm.login(args.username, args.password)
        bot_user_id = mm.user_id(BOT_USERNAME)
        channel_id = mm.direct_channel(user_id, bot_user_id)
    except BenchError as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 1

    new: list[Run] = []
    for _ in range(passes):
        for question_id, question in questions:
            run = run_one(
                mm,
                channel_id=channel_id,
                bot_user_id=bot_user_id,
                user_id=user_id,
                question_id=question_id,
                question=question,
                state=state,
                audit=audit,
                settle_s=args.settle_s,
                timeout_s=args.timeout_s,
                poll_s=args.poll_s,
                clock=clock,
                sleep=clock.sleep,
            )
            new.append(run)
            outcome = f"{run.hops.end_to_end_s:.1f}s" if run.hops else f"failed: {run.error}"
            print(f"  {state} {question_id}: {outcome}")

    path = result_path(args.configuration, date.today())
    existing = json.loads(path.read_text("utf-8")) if path.exists() else None
    runs = merge_runs(existing, new)
    data = report(labels, runs, ran_at=datetime.now(UTC).isoformat(), commit=git_commit())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"\nwrote {path}")
    print(table_row(labels, data["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_bench_mattermost.py -q`
Expected: 24 passed.

- [ ] **Step 9: Lint, type-check and commit**

Run: `uv run ruff format scripts/bench_mattermost.py tests/test_bench_mattermost.py && uv run ruff check --no-cache scripts tests && uv run mypy scripts/bench_mattermost.py && uv run pytest -q`
Expected: clean, and the full suite green.

```bash
git add scripts/bench_mattermost.py tests/test_bench_mattermost.py
git commit -m "Benchmark runner: post, poll, join the audit log, write the results file"
```

---

### Task 3: The seed script points the plugin at any OpenAI-compatible endpoint

**Files:**
- Modify: `scripts/seed_mattermost.py` (`_plugin_config`, `seed`, `main`)
- Modify: `.env.example`
- Test: `tests/test_seed_mattermost.py`

**Interfaces:**
- Produces: `_plugin_config(shared_secret, llm_url, llm_model, mcp_url, *, llm_api_key="not-needed", llm_service_name="Docker Model Runner")`; `seed(..., llm_api_key: str = "not-needed", llm_service_name: str = "Docker Model Runner", ...)`; CLI `--llm-api-key`, `--llm-service-name`; env `AGENT_LLM_URL`, `AGENT_LLM_MODEL`, `AGENT_LLM_API_KEY`, `AGENT_LLM_SERVICE_NAME`. The service `id` stays `model-runner`, so the existing bot keeps its service and the service is updated in place.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_seed_mattermost.py`:

```python
def test_plugin_service_takes_the_key_and_name_it_is_given() -> None:
    cfg = seed._plugin_config(
        "s3cret", "https://inference.baseten.co/v1", "Qwen/Qwen3.5-9B", "http://mcp:8002/mcp",
        llm_api_key="bt-key", llm_service_name="Baseten Model APIs",
    )
    (service,) = cfg["services"]
    assert service["id"] == seed.SERVICE_ID  # unchanged: the bot keeps pointing at it
    assert service["name"] == "Baseten Model APIs"
    assert service["apiURL"] == "https://inference.baseten.co/v1"
    assert service["apiKey"] == "bt-key"
    assert service["defaultModel"] == "Qwen/Qwen3.5-9B"


def test_plugin_service_defaults_to_model_runner_without_a_key() -> None:
    cfg = seed._plugin_config("s", "http://model-runner.docker.internal/engines/v1", "m", "u")
    (service,) = cfg["services"]
    assert service["name"] == "Docker Model Runner" and service["apiKey"] == "not-needed"


def test_main_reads_the_agent_llm_env_and_never_prints_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_LLM_URL", "https://inference.baseten.co/v1")
    monkeypatch.setenv("AGENT_LLM_MODEL", "Qwen/Qwen3.5-9B")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "bt-secret-key")
    monkeypatch.setenv("AGENT_LLM_SERVICE_NAME", "Baseten Model APIs")
    seen: dict[str, Any] = {}

    def fake_seed(mm: Any, **kw: Any) -> Any:
        seen.update(kw)
        return seed.Result(shared_secret="s", bot_user_id="b", bot_token=None, user_ids={})

    monkeypatch.setattr(seed, "seed", fake_seed)
    code = seed.main(["--dry-run", "--shared-secret", "s3cret"])
    out = capsys.readouterr()
    assert code == 0
    assert seen["llm_url"] == "https://inference.baseten.co/v1"
    assert seen["llm_model"] == "Qwen/Qwen3.5-9B"
    assert seen["llm_api_key"] == "bt-secret-key"
    assert seen["llm_service_name"] == "Baseten Model APIs"
    assert "bt-secret-key" not in out.out and "bt-secret-key" not in out.err


def test_main_defaults_the_plugin_to_model_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENT_LLM_URL", "AGENT_LLM_MODEL", "AGENT_LLM_API_KEY", "AGENT_LLM_SERVICE_NAME", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    seen: dict[str, Any] = {}

    def fake_seed(mm: Any, **kw: Any) -> Any:
        seen.update(kw)
        return seed.Result(shared_secret="s", bot_user_id="b", bot_token=None, user_ids={})

    monkeypatch.setattr(seed, "seed", fake_seed)
    assert seed.main(["--dry-run", "--shared-secret", "s3cret"]) == 0
    assert seen["llm_url"] == "http://model-runner.docker.internal/engines/v1"
    assert seen["llm_model"] == "ai/qwen3.5:9b-q4_K_M"
    assert seen["llm_api_key"] == "not-needed"
    assert seen["llm_service_name"] == "Docker Model Runner"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_seed_mattermost.py -q -k "plugin_service or agent_llm or defaults_the_plugin"`
Expected: 4 failed: `TypeError: _plugin_config() got an unexpected keyword argument 'llm_api_key'` and `KeyError: 'llm_api_key'`.

- [ ] **Step 3: Implement**

In `scripts/seed_mattermost.py`, change `_plugin_config`:

```python
def _plugin_config(
    shared_secret: str,
    llm_url: str,
    llm_model: str,
    mcp_url: str,
    *,
    llm_api_key: str = "not-needed",
    llm_service_name: str = "Docker Model Runner",
) -> dict[str, Any]:
    return {
        "services": [
            {
                "id": SERVICE_ID,  # fixed, so the bot keeps its service when the endpoint changes
                "name": llm_service_name,
                "type": "openaicompatible",
                "apiURL": llm_url,
                "apiKey": llm_api_key,
                "defaultModel": llm_model,
                "useResponsesAPI": False,
                "streamingTimeoutSeconds": 300,
            }
        ],
```

(the rest of the function unchanged). In `seed(...)`, add two keyword parameters after `llm_model: str,`:

```python
    llm_api_key: str = "not-needed",
    llm_service_name: str = "Docker Model Runner",
```

and pass them through:

```python
    _ensure_plugin_config(
        mm,
        _plugin_config(
            shared_secret,
            llm_url,
            llm_model,
            mcp_url,
            llm_api_key=llm_api_key,
            llm_service_name=llm_service_name,
        ),
    )
```

In `main`, replace the two `--llm-*` arguments with:

```python
    parser.add_argument(
        "--llm-url",
        default=os.environ.get("AGENT_LLM_URL") or "http://model-runner.docker.internal/engines/v1",
        help="The plugin's model endpoint (OpenAI-compatible). AGENT_LLM_URL.",
    )
    parser.add_argument(
        "--llm-model",
        default=os.environ.get("AGENT_LLM_MODEL") or os.environ.get("LLM_MODEL") or "ai/qwen3.5:9b-q4_K_M",
        help="The plugin's model id. AGENT_LLM_MODEL, then LLM_MODEL.",
    )
    parser.add_argument(
        "--llm-api-key",
        default=os.environ.get("AGENT_LLM_API_KEY") or "not-needed",
        help="The plugin's key for that endpoint. AGENT_LLM_API_KEY. Never printed.",
    )
    parser.add_argument(
        "--llm-service-name",
        default=os.environ.get("AGENT_LLM_SERVICE_NAME") or "Docker Model Runner",
        help="How the service is named in the plugin's settings. AGENT_LLM_SERVICE_NAME.",
    )
```

and in the `seed(` call add `llm_api_key=args.llm_api_key, llm_service_name=args.llm_service_name,` after `llm_model=args.llm_model,`.

In `.env.example`, after the `MATTERMOST_BOT_TOKEN` block, add:

```
# The Agents plugin's own model (the seed script writes it into the plugin's settings). Defaults
# are Model Runner inside Compose. To point the plugin at Baseten Model APIs set:
#   AGENT_LLM_URL=https://inference.baseten.co/v1
#   AGENT_LLM_MODEL=<a model id from the catalog>
#   AGENT_LLM_API_KEY=<your Baseten key>
#   AGENT_LLM_SERVICE_NAME=Baseten Model APIs
# then re-run the seed script. Keys never leave .env. See docs/serving-backends.md.
AGENT_LLM_URL=
AGENT_LLM_MODEL=
AGENT_LLM_API_KEY=
AGENT_LLM_SERVICE_NAME=
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_seed_mattermost.py -q`
Expected: all passed (the existing dry-run and idempotence tests still pass because the defaults are unchanged).

- [ ] **Step 5: Lint, type-check and commit**

Run: `uv run ruff format scripts tests && uv run ruff check --no-cache scripts tests && uv run mypy scripts/seed_mattermost.py`
Expected: clean.

```bash
git add scripts/seed_mattermost.py tests/test_seed_mattermost.py .env.example
git commit -m "Seed script: the plugin's model endpoint, key and name come from AGENT_LLM_* env"
```

---

### Task 4: The Compose override for the generator on Baseten

**Files:**
- Create: `compose.baseten.yaml`
- Modify: `.env.example`
- Test: `tests/test_compose.py`

**Interfaces:**
- Produces: `compose.baseten.yaml`, used as `docker compose -f compose.yaml -f compose.baseten.yaml --profile mattermost --profile mcp up -d`; env `BASETEN_BASE_URL` (default `https://inference.baseten.co/v1`), `BASETEN_MODEL` (required), `BASETEN_API_KEY` (required), `ENGINE` (default `baseten-model-apis`), `ENGINE_BACKEND` (default `managed`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_compose.py`:

```python
class _ResetLoader(yaml.SafeLoader):
    """Compose's `!reset` merge tag, read as the plain value it wraps."""


_ResetLoader.add_constructor("!reset", lambda loader, node: loader.construct_mapping(node))

OVERRIDE = yaml.load((Path(__file__).parents[1] / "compose.baseten.yaml").read_text(), Loader=_ResetLoader)


def test_baseten_override_moves_only_the_generator_and_keeps_embeddings_local() -> None:
    for name in ("app", "mcp"):
        svc = OVERRIDE["services"][name]
        assert svc["models"] == {}  # the Model Runner bindings are reset; env below replaces them
        env = svc["environment"]
        assert env["LLM_BASE_URL"] == "${BASETEN_BASE_URL:-https://inference.baseten.co/v1}"
        assert env["LLM_MODEL"].startswith("${BASETEN_MODEL:?")
        assert env["LLM_API_KEY"].startswith("${BASETEN_API_KEY:?")
        assert env["EMBED_BASE_URL"] == "http://model-runner.docker.internal/engines/v1"
        assert env["EMBED_MODEL"] == COMPOSE["models"]["embed"]["model"]
        assert env["ENGINE"] == "${ENGINE:-baseten-model-apis}"
        assert env["ENGINE_BACKEND"] == "${ENGINE_BACKEND:-managed}"
    assert set(OVERRIDE["services"]) == {"app", "mcp"}
    assert set(OVERRIDE) == {"services"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_compose.py -q`
Expected: FAIL at import with `FileNotFoundError: compose.baseten.yaml`.

- [ ] **Step 3: Write the override and the env entries**

Create `compose.baseten.yaml`:

```yaml
# sovrag's generator on Baseten; everything else unchanged. Use it as an override:
#   docker compose -f compose.yaml -f compose.baseten.yaml --profile mattermost --profile mcp up -d
# The `!reset` drops the Model Runner bindings on app and mcp (Compose injects LLM_* and
# EMBED_* from `models:`), then environment sets the generator from .env and pins embeddings
# back to Model Runner, because the committed eval fixture is BGE-M3 (ADR 0017). Retrieval, the
# entitlement predicate, identity binding and the audit log do not change.
services:
  app:
    models: !reset {}
    environment:
      LLM_BASE_URL: ${BASETEN_BASE_URL:-https://inference.baseten.co/v1}
      LLM_MODEL: ${BASETEN_MODEL:?set BASETEN_MODEL to a model id from the catalog}
      LLM_API_KEY: ${BASETEN_API_KEY:?set BASETEN_API_KEY in .env}
      EMBED_BASE_URL: http://model-runner.docker.internal/engines/v1
      EMBED_MODEL: hf.co/gpustack/bge-m3-gguf:Q8_0
      ENGINE: ${ENGINE:-baseten-model-apis}
      ENGINE_BACKEND: ${ENGINE_BACKEND:-managed}
  mcp:
    models: !reset {}
    environment:
      LLM_BASE_URL: ${BASETEN_BASE_URL:-https://inference.baseten.co/v1}
      LLM_MODEL: ${BASETEN_MODEL:?set BASETEN_MODEL to a model id from the catalog}
      LLM_API_KEY: ${BASETEN_API_KEY:?set BASETEN_API_KEY in .env}
      EMBED_BASE_URL: http://model-runner.docker.internal/engines/v1
      EMBED_MODEL: hf.co/gpustack/bge-m3-gguf:Q8_0
      ENGINE: ${ENGINE:-baseten-model-apis}
      ENGINE_BACKEND: ${ENGINE_BACKEND:-managed}
```

In `.env.example`, after the `AGENT_LLM_*` block from Task 3, add:

```
# sovrag's own generator on Baseten Model APIs, via the override file:
#   docker compose -f compose.yaml -f compose.baseten.yaml --profile mattermost --profile mcp up -d
# For host-side runs (`uv run sovrag eval generation`) set LLM_BASE_URL, LLM_MODEL and LLM_API_KEY
# to the same values on the command line. Embeddings and the judge stay on Model Runner.
BASETEN_BASE_URL=https://inference.baseten.co/v1
BASETEN_MODEL=
BASETEN_API_KEY=
LLM_API_KEY=not-needed
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_compose.py -q`
Expected: all passed.

- [ ] **Step 5: Verify the merge with Compose itself**

Run: `BASETEN_MODEL=x BASETEN_API_KEY=y docker compose -f compose.yaml -f compose.baseten.yaml --profile mcp config | grep -A1 -E "LLM_BASE_URL|EMBED_MODEL|ENGINE:|model_runner|models:"`
Expected: for `app` and `mcp`, `LLM_BASE_URL: https://inference.baseten.co/v1`, `EMBED_MODEL: hf.co/gpustack/bge-m3-gguf:Q8_0`, `ENGINE: baseten-model-apis`, and no `models:` block under either service. If `!reset` leaves the `models:` block in place, the plan is wrong here: rule on `models: !override {}` instead, re-run, and ledger it.

Run: `docker compose -f compose.yaml -f compose.baseten.yaml --profile mcp config >/dev/null; echo $?` with `BASETEN_MODEL` unset.
Expected: a non-zero exit and the message `set BASETEN_MODEL to a model id from the catalog`.

- [ ] **Step 6: Commit**

```bash
git add compose.baseten.yaml .env.example tests/test_compose.py
git commit -m "Compose override: sovrag's generator on Baseten, embeddings and judge stay local"
```

---

### Task 5: ADR 0017 and the docs skeleton

**Files:**
- Create: `docs/adr/0017-inference-placement.md`
- Create: `docs/serving-backends.md`
- Modify: `README.md` (after the observability paragraph in "Serve it"), `CLAUDE.md` (Commands), `docs/architecture.md` (decisions list and the Compose section)

- [ ] **Step 1: Write ADR 0017**

Create `docs/adr/0017-inference-placement.md`:

```markdown
# ADR 0017: Inference placement is a configuration and data-classification decision

Status: accepted (2026-10-13)

## Context

Every model call goes through two OpenAI-compatible clients whose base URL, model name and API
key come from the environment (ADR 0005). The Mattermost Agents plugin calls its own model the
same way, from one service entry the seed script writes. A reply in Mattermost was observed at
around 40 s on a laptop while the direct path measured 4.1 s, and the difference was attributed
to the plugin's loop on a laptop-sized model without ever being timed.

## Decision

- Where inference runs is configuration, and the benchmark proves it: the plugin's model moves
  with four `AGENT_LLM_*` variables and a re-seed; sovrag's generator moves with
  `compose.baseten.yaml` and three `BASETEN_*` variables. No code path changes.
- Retrieval, the entitlement predicate, identity binding and the audit log are local in every
  configuration. Embeddings stay on Docker Model Runner because the committed eval fixture holds
  BGE-M3 vectors and the thresholds were set against them. The judge stays local so quality
  scores are comparable across rows.
- Moving inference to a hosted provider means the provider sees the question and the entitled
  chunks, which is exactly what the all-local configuration avoids. Placement of inference is
  therefore a data-classification decision: the question and the chunks a principal may see go
  wherever the generator runs, while what a principal may see is decided at home.
- Every measurement is labelled with the plugin model and precision, the generator model and
  precision, the tier (`local`, `model-apis`, `dedicated`), the billing mode (`none`,
  `per-token`, `per-gpu-minute`), the engine, the engine backend and the hardware, read at run
  time. Hops are measured from timestamps that already exist: the person's post, the `resolve`
  and `answer` audit events, and the reply's last edit. Nothing is added to the measured path.
- Baseten has two product shapes and the write-up covers both: Model APIs (shared, always
  warm, per-token, a curated list) and dedicated deployments (any model, scale-to-zero, a cold
  start measured in minutes, per GPU-minute). Model APIs is the measured backend; the dedicated
  tier is measured once if an evening allows, and otherwise characterised from documentation
  and marked as such.

## Consequences

- `docs/serving-backends.md` holds the table and the method; every number there is marked
  measured, observed or estimated.
- The benchmark runner lives in `scripts/`, outside the product, and reads the audit file; a
  change to the audit line's `ts`, `event`, `user_id` or `request_id` fields is a benchmark
  change.
- Keys live in `.env` only. The seed script and the runner never print them, and results files
  carry labels, not credentials.
- A hosted generator sees public documents with synthetic labels (ADR 0003). The sovereignty
  paragraph describes what would happen with real markings, not what happened here.
```

- [ ] **Step 2: Write the serving-backends doc skeleton**

Create `docs/serving-backends.md`:

```markdown
# Serving backends: where the time goes, and what moving inference costs

Every model call in sovrag goes through an OpenAI-compatible client configured from the
environment, so the serving backend is a configuration choice. This page records what that
choice costs in latency and in sovereignty, from measurements, with every number marked
measured, observed or estimated. Method and labels: ADR 0017.

## What is measured

A question asked in Mattermost passes through three hops:

1. **The agent decides.** The Agents plugin's model reads the conversation and decides to call
   the `answer` tool. From the person's post to the `resolve` audit event.
2. **sovrag answers.** Embed, hybrid retrieval under the entitlement filter, generate. From the
   `resolve` event to the `answer` event.
3. **The agent writes.** The plugin's model writes the reply from the tool result, token by
   token. From the `answer` event to the reply's last edit.

The runner (`scripts/bench_mattermost.py`) posts a question as `analyst_dnd`, waits for the
reply to settle, and joins the Mattermost timestamps with the audit events by user id and
request id. Hop 1 includes Mattermost delivering the post to the plugin and hop 3 the final
edit; both are milliseconds. Cold is the first run after `docker model unload --all`. Warm rows
are ten runs: five questions from the retrieval golden set (`r01` to `r04`, `r09`), two passes.
Fewer than five warm runs is not a row.

```
uv run python scripts/bench_mattermost.py --configuration A-local --cold
uv run python scripts/bench_mattermost.py --configuration A-local --warm --passes 2
```

## Results

Hardware for local rows is read at run time and recorded in each results file under
`eval/results/bench-mattermost-*.json`. Values are warm medians in seconds; `cold` is the
cold run's end-to-end time; `n` is warm runs.

| Configuration | Plugin model | sovrag generator | Tier | Billing | Hop 1 | Hop 2 | Hop 3 | End to end | Cold | n |
|---|---|---|---|---|---|---|---|---|---|---|

Quality, where the generator changed (the generation eval, judged locally by Gemma 4 12B
Q4_K_M, and the leak suite's answer run):

| Configuration | Faithful | Correct | False refusals | Unanswerable refused | Leak answers refused |
|---|---|---|---|---|---|
| A-local (2026-09-22 baseline) | 1.000 (29 judged) | 0.966 | 1/30 | 9/10 | public 15/20, analyst_dnd 12/20, analyst_ps 13/20 |

## Configurations

| Configuration | Plugin model (hops 1 and 3) | sovrag generator (hop 2) | How |
|---|---|---|---|
| A-local | Qwen 3.5 9B Q4_K_M on Docker Model Runner | the same | `docker compose --profile mattermost --profile mcp up -d`, seed with defaults |
| B-plugin-baseten | a Baseten Model APIs model | Qwen 3.5 9B Q4_K_M on Docker Model Runner | set `AGENT_LLM_*`, re-seed |
| C-both-baseten | a Baseten Model APIs model | the same Baseten model | B, then `docker compose -f compose.yaml -f compose.baseten.yaml ...` |
| D-dedicated | a dedicated Baseten deployment, scale-to-zero | as stated in the row | optional |

Retrieval, the entitlement predicate, identity binding, the audit log, embeddings and the
judge are local in every configuration.

## The serving-backend tradeoff

Moving inference to a hosted provider means the provider sees the question and the entitled
chunks, which is exactly what the all-local configuration avoids. Placement of inference
becomes a data-classification decision: retrieval, entitlements and audit stay local, and the
question plus whatever the principal may see travels to wherever the generator runs. For public
documents that is a latency decision. For documents with real markings it is the decision.

Two product shapes on the hosted side:

- **Shared, always warm** (Baseten Model APIs): a curated list of open-weights models behind one
  OpenAI-compatible URL, billed per million tokens, no deployment to manage and no cold start.
- **Dedicated** (a Baseten deployment from the model library or your own weights): any model,
  your hardware choice, scale-to-zero when idle, a cold start measured in minutes while the
  container pulls, loads weights and initialises the engine, billed per GPU-minute while a
  replica is up.

The all-local shape pays in a different currency: a laptop-sized model at a laptop's decode
speed, and a reload whenever the model has been idle long enough to be unloaded.

## Cost observed

(filled in by the measurement tasks: account balance before and after, tokens per run)

## What is not measured

Prefill and decode inside hops 1 and 3 are not separated; a logging proxy between the plugin
and its model would be the next instrument. Hop 2 is not split into embed, retrieve and
generate here; the generation eval's latency figure covers the direct path. The dedicated tier,
where it is not run, is characterised from Baseten's documentation and marked as such.
```

- [ ] **Step 3: Add the pointers**

In `README.md`, after the observability paragraph in "Serve it", add:

```markdown
Where the models run is configuration: `docs/serving-backends.md` measures the three hops of a Mattermost reply on this laptop and with the plugin's model, then sovrag's generator, moved to Baseten, and says what moving inference off the machine means for sovereignty. See ADR 0017.
```

In `CLAUDE.md`, under Commands, add:

```
- uv run python scripts/bench_mattermost.py --configuration A-local --warm   # hop timings; see docs/serving-backends.md
```

In `docs/architecture.md`, in the decisions list after the ADR 0016 line, add:

```markdown
- ADR 0017: where inference runs is configuration and a data-classification decision; retrieval, entitlements, audit, embeddings and the judge stay local in every configuration; measurements are labelled and taken from existing timestamps.
```

and in the Compose section after the observability sentence, add:

```markdown
`compose.baseten.yaml` is an override that moves sovrag's generator to Baseten Model APIs and pins embeddings back to Model Runner; the seed script's `AGENT_LLM_*` variables move the plugin's model the same way. See ADR 0017 and `docs/serving-backends.md`.
```

- [ ] **Step 4: Check the prose rules and commit**

Run: `grep -n "—\|delve\|leverage\|seamless\|journey" docs/adr/0017-inference-placement.md docs/serving-backends.md README.md docs/architecture.md`
Expected: no output.

```bash
git add docs/adr/0017-inference-placement.md docs/serving-backends.md README.md CLAUDE.md docs/architecture.md
git commit -m "ADR 0017 and the serving-backends page: method, configurations, the tradeoff"
```

---

### Task 6: Measurement A, the local baseline (live)

**Files:**
- Create: `eval/results/bench-mattermost-A-local-<date>.json` (generated)
- Modify: `docs/serving-backends.md` (one results row, the cold note)

This task needs the whole local stack and the models. **Stop and ask before step 1.**

- [ ] **Step 1: Bring the stack up (with the person's go)**

Run: `docker compose --profile mattermost --profile mcp up -d --wait && uv run python scripts/seed_mattermost.py | tail -4`
Expected: every service healthy; the seed reports the agent unchanged or updated and prints the four `.env` lines (the token line says unchanged). `.env` already holds `MCP_IDENTITY=mattermost`, the secret and the token from the earlier demo.

- [ ] **Step 2: Cold run**

Run: `docker model unload --all && docker model ps && uv run python scripts/bench_mattermost.py --configuration A-local --cold`
Expected: `docker model ps` prints only its header; the runner prints `cold r01: <seconds>s`, `wrote eval/results/bench-mattermost-A-local-<date>.json`, and a table row whose Cold cell is a number. A `failed: timeout` line means the plugin took longer than 240 s: re-run with `--timeout-s 480` and note it.

- [ ] **Step 3: Warm runs**

Run: `uv run python scripts/bench_mattermost.py --configuration A-local --warm --passes 2`
Expected: ten `warm rNN: <seconds>s` lines, the same file rewritten, `summary.warm.n` is 10 (check with `jq .summary eval/results/bench-mattermost-A-local-*.json`). If any run failed, re-run `--warm --passes 1` until `n` is at least 10 and record the failures count from `summary.warm.failed` in the doc.

- [ ] **Step 4: Record the row**

Paste the printed table row under the Results table header in `docs/serving-backends.md`. Under it add one line: `A-local hardware: <chip>, <memory_gb> GB, macOS <os>, Docker <docker>, Model Runner <model_runner>` with the values from the file's `labels.hardware`. Add a sentence to "What is measured" if the plugin posted a placeholder (`hop3_first_token_s` absent in the summary) saying so.

- [ ] **Step 5: Commit**

```bash
git add eval/results/bench-mattermost-A-local-*.json docs/serving-backends.md
git commit -m "Measure the local baseline: cold and ten warm runs through Mattermost"
```

---

### Task 7: Baseten compatibility check and measurement B (live)

**Files:**
- Create: `eval/results/bench-mattermost-B-plugin-baseten-<date>.json` (generated)
- Modify: `docs/serving-backends.md`, `docs/adr/0017-inference-placement.md` only if a code change was needed

Needs `BASETEN_API_KEY` in `.env` (the person adds it) and the stack from Task 6.

- [ ] **Step 1: List the catalog and pick by the rule**

Run: `set -a; . ./.env; set +a; curl -s https://inference.baseten.co/v1/models -H "Authorization: Bearer $BASETEN_API_KEY" | jq -r '.data[].id' | sort`
Expected: a list of model ids. Apply the spec's rule: the smallest Qwen 3.5 instruct model with tool calling if there is one, otherwise the smallest capable instruct model with tool calling. Write the chosen id into `.env` as `BASETEN_MODEL=<id>` and `AGENT_LLM_MODEL=<id>`, with `AGENT_LLM_URL=https://inference.baseten.co/v1`, `AGENT_LLM_API_KEY=<the same key>`, `AGENT_LLM_SERVICE_NAME=Baseten Model APIs`. Record the stated precision from the catalog page in the doc's row. Never paste the key anywhere but `.env`.

- [ ] **Step 2: One completion through sovrag's client, environment only**

Run:

```bash
set -a; . ./.env; set +a
LLM_BASE_URL=https://inference.baseten.co/v1 LLM_MODEL="$BASETEN_MODEL" LLM_API_KEY="$BASETEN_API_KEY" \
uv run python -c "from sovrag.config import get_settings; from sovrag.generate.llm import Generator; print(Generator.from_settings(get_settings()).complete([{'role': 'user', 'content': 'Reply with the single word ok.'}]))"
```

Expected: `ok` (or a one-word reply). A 401 means the bearer header is not accepted: then, and only then, add an `LLM_AUTH_SCHEME` setting (default `Bearer`) read by `Generator`, with a test in `tests/test_llm.py` that the header is `Api-Key <key>` when set, and record the change in ADR 0017. A 404 on the model means the id is wrong: re-check the catalog.

- [ ] **Step 3: Point the plugin at Baseten and measure**

Run: `set -a; . ./.env; set +a; uv run python scripts/seed_mattermost.py | grep -i "plugin config"`
Expected: `plugin config: updated` (the service now names Baseten; the key is not printed).

Run: `docker model unload --all && uv run python scripts/bench_mattermost.py --configuration B-plugin-baseten --cold --plugin-model "$BASETEN_MODEL" --plugin-precision "<stated>" --tier model-apis --billing per-token`
Expected: one cold line and the file written. Cold here is sovrag's own generator reloading in hop 2; hops 1 and 3 are hosted.

Run: `uv run python scripts/bench_mattermost.py --configuration B-plugin-baseten --warm --passes 2 --plugin-model "$BASETEN_MODEL" --plugin-precision "<stated>" --tier model-apis --billing per-token`
Expected: ten warm lines, `summary.warm.n` 10.

- [ ] **Step 4: Record the row and commit**

Paste the row under the A-local row. Note under the table which hops are hosted in B.

```bash
git add eval/results/bench-mattermost-B-plugin-baseten-*.json docs/serving-backends.md
git commit -m "Measure the plugin's model on Baseten Model APIs"
```

---

### Task 8: Measurement C, the generator on Baseten, with quality (live, if the evening allows)

**Files:**
- Create: `eval/results/bench-mattermost-C-both-baseten-<date>.json`, `eval/results/generation-<date>.json`, `eval/results/leak-answers-<date>.json` (generated)
- Modify: `docs/serving-backends.md`

- [ ] **Step 1: Recreate `mcp` with the override**

Run: `set -a; . ./.env; set +a; docker compose -f compose.yaml -f compose.baseten.yaml --profile mattermost --profile mcp up -d mcp && docker compose --profile mcp exec mcp env | grep -E "^(LLM_BASE_URL|LLM_MODEL|ENGINE)="`
Expected: `LLM_BASE_URL=https://inference.baseten.co/v1`, the chosen model, `ENGINE=baseten-model-apis`. The key is not grepped for.

- [ ] **Step 2: Measure**

Run: `uv run python scripts/bench_mattermost.py --configuration C-both-baseten --warm --passes 2 --plugin-model "$BASETEN_MODEL" --plugin-precision "<stated>" --generator-model "$BASETEN_MODEL" --generator-precision "<stated>" --tier model-apis --billing per-token --engine baseten-model-apis --engine-backend managed`
Expected: ten warm lines, `n` 10. No cold run: Model APIs has no cold state and the generator is hosted.

- [ ] **Step 3: Quality in this configuration, host side**

Run:

```bash
set -a; . ./.env; set +a
export LLM_BASE_URL=https://inference.baseten.co/v1 LLM_MODEL="$BASETEN_MODEL" LLM_API_KEY="$BASETEN_API_KEY" ENGINE=baseten-model-apis ENGINE_BACKEND=managed
uv run sovrag eval generation
uv run sovrag eval leak --check --answers
```

Expected: `eval/results/generation-<date>.json` with `engine: baseten-model-apis` and `llm_model` the chosen id, judge `ai/gemma4:12b-q4_K_M`; the leak run reports zero forbidden chunks (retrieval is local and unchanged) and refusal counts per principal for the answer run. If the judge refuses to start because `JUDGE_MODEL` equals `LLM_MODEL`, the rule holds: pick a different judge is not an option here, so the chosen Baseten model must not be Gemma 4.

- [ ] **Step 4: Record and commit**

Add the C row to the results table and a row to the quality table with faithful, correct, false refusals, unanswerable refused and the leak-answer refusals, from the two files. Mark every number measured.

```bash
git add eval/results/bench-mattermost-C-both-baseten-*.json eval/results/generation-*.json eval/results/leak-answers-*.json docs/serving-backends.md
git commit -m "Measure sovrag's generator on Baseten: latency and quality beside the local rows"
```

- [ ] **Step 5: Put the generator back**

Run: `docker compose --profile mattermost --profile mcp up -d mcp`
Expected: `mcp` recreated from `compose.yaml` alone, `LLM_BASE_URL` back to the Model Runner value.

---

### Task 9: Cost, caveats, and the optional dedicated row

**Files:**
- Modify: `docs/serving-backends.md`
- Create (optional): `eval/results/bench-mattermost-D-dedicated-<date>.json`

- [ ] **Step 1: Cost observed**

Ask the person for the Baseten account balance before Task 7 and after Task 8 (the billing page shows it). Sum `prompt_tokens` and `completion_tokens` are not in the results files; state the balance difference as the observed cost and the number of runs it covered. Write the "Cost observed" section with those two numbers, marked observed, and the per-million-token prices from the catalog, marked quoted.

- [ ] **Step 2: The dedicated tier, only if the evening allows**

Deploy one small instruct model from Baseten's model library through the Baseten UI with minimum replicas 0 (the docs page "Scaling" shows `min_replica: 0`). The endpoint is `https://model-<model_id>.api.baseten.co/environments/production/sync/v1`. Measure cold once with the Task 7 Step 2 one-liner wrapped in `time`, after the deployment shows "Scaled to zero", then once more warm. Record both as a D-dedicated row with `--tier dedicated --billing per-gpu-minute` using the runner only if the plugin was also pointed at it; otherwise record the two direct timings in the doc as measured, marked "direct path, not through Mattermost". Scale it down or delete the deployment afterwards and say so in the doc. If the deployment fights for more than the evening, write the tier from documentation and mark every statement about it as documented, not measured.

- [ ] **Step 3: Final prose check and commit**

Run: `grep -n "—\|delve\|leverage\|seamless\|journey" docs/serving-backends.md docs/adr/0017-inference-placement.md README.md; grep -c "measured\|observed\|estimated\|documented" docs/serving-backends.md`
Expected: no matches on the first grep; a count above zero on the second.

```bash
git add docs/serving-backends.md eval/results/
git commit -m "Serving backends: cost observed, caveats, and the dedicated tier"
```

Then the full suite, ruff, mypy, and superpowers:finishing-a-development-branch.
