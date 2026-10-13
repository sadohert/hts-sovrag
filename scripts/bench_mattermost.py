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


# --- Mattermost ----------------------------------------------------------------------------


class Mattermost:
    """A thin REST client. Every call raises BenchError on a 4xx or 5xx."""

    def __init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=60, transport=transport)
        self.token: str | None = None

    def call(
        self, method: str, path: str, *, json: Any = None, params: Any = None
    ) -> httpx.Response:
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self._client.request(method, path, json=json, params=params, headers=headers)
        if response.status_code not in (200, 201):
            raise BenchError(f"{method} {path}: HTTP {response.status_code} {response.text[:200]}")
        return response

    def login(self, username: str, password: str) -> str:
        response = self.call(
            "POST", "/api/v4/users/login", json={"login_id": username, "password": password}
        )
        self.token = response.headers.get("Token")
        return str(response.json()["id"])

    def user_id(self, username: str) -> str:
        return str(self.call("GET", f"/api/v4/users/username/{username}").json()["id"])

    def direct_channel(self, a: str, b: str) -> str:
        return str(self.call("POST", "/api/v4/channels/direct", json=[a, b]).json()["id"])

    def post(self, channel_id: str, message: str) -> dict[str, Any]:
        body = self.call(
            "POST", "/api/v4/posts", json={"channel_id": channel_id, "message": message}
        ).json()
        return dict(body)

    def posts_since(self, channel_id: str, since_ms: int) -> list[dict[str, Any]]:
        body = self.call(
            "GET", f"/api/v4/channels/{channel_id}/posts", params={"since": since_ms}
        ).json()
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
        # The plugin also creates empty status posts in the same second as the question and
        # keeps editing them; the reply is the earliest bot post that carries text.
        replies = [
            p
            for p in mm.posts_since(channel_id, post_ms - 1)
            if p.get("user_id") == bot_user_id
            and int(p.get("create_at", 0)) >= post_ms
            and str(p.get("message") or "").strip()
        ]
        if replies:
            reply = min(replies, key=lambda p: int(p["create_at"]))
            samples.append((clock(), int(reply["update_at"])))
            if settled(samples, settle_s):
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
    except OSError, subprocess.CalledProcessError:
        return "unknown"


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
    out = run_command(
        [
            "docker",
            "compose",
            "--profile",
            "mcp",
            "exec",
            "-T",
            "mcp",
            "cat",
            "/app/audit/audit.jsonl",
        ]
    )
    return out.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--configuration",
        required=True,
        help="A-local, B-plugin-baseten, C-both-baseten, D-dedicated",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--cold",
        action="store_true",
        help="one run of the first question; refuses if a model is loaded",
    )
    mode.add_argument("--warm", action="store_true", help="every question, --passes times")
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument(
        "--url", default=os.environ.get("MATTERMOST_SITE_URL", "http://localhost:8065")
    )
    parser.add_argument("--username", default="analyst_dnd")
    parser.add_argument(
        "--password", default=os.environ.get("MATTERMOST_DEMO_PASSWORD", "Passw0rd!demo")
    )
    parser.add_argument(
        "--audit", type=Path, default=None, help="read this file instead of the mcp container"
    )
    parser.add_argument("--settle-s", type=float, default=3.0)
    parser.add_argument("--timeout-s", type=float, default=240.0)
    parser.add_argument("--poll-s", type=float, default=1.0)
    parser.add_argument(
        "--plugin-model",
        default=os.environ.get("AGENT_LLM_MODEL")
        or os.environ.get("LLM_MODEL", "ai/qwen3.5:9b-q4_K_M"),
    )
    parser.add_argument("--plugin-precision", default="Q4_K_M")
    parser.add_argument(
        "--generator-model", default=os.environ.get("LLM_MODEL", "ai/qwen3.5:9b-q4_K_M")
    )
    parser.add_argument("--generator-precision", default="Q4_K_M")
    parser.add_argument("--tier", choices=TIERS, default="local")
    parser.add_argument("--billing", choices=BILLING, default="none")
    parser.add_argument("--engine", default=os.environ.get("ENGINE", "docker-model-runner"))
    parser.add_argument("--engine-backend", default=os.environ.get("ENGINE_BACKEND", "llama.cpp"))
    args = parser.parse_args(argv)

    if args.cold:
        loaded = models_loaded(run_command(["docker", "model", "ps"]))
        if loaded:
            print(
                f"bench: {', '.join(loaded)} still loaded; run `docker model unload --all` first",
                file=sys.stderr,
            )
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
        (lambda: args.audit.read_text("utf-8").splitlines())
        if args.audit
        else _audit_from_container
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
