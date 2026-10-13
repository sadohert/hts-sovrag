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
    says nothing about hop 3's first token, and hop 3 still runs from the answer to the last
    edit."""
    h = bench.hops(
        post_ms=T0_MS,
        resolve_ts=_iso(12.0),
        answer_ts=_iso(16.5),
        reply_first_ms=T0_MS + 11_000,
        reply_last_ms=T0_MS + 40_000,
    )
    assert h.hop3_first_token_s is None
    assert h.hop3_s == pytest.approx(23.5)


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
        bench.join_audit(
            _audit_lines(), user_id="u1", after_ms=T0_MS + 20_000, before_ms=T0_MS + 40_000
        )
        is None
    )


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
    row = bench.table_row(
        _labels(tier="model-apis", billing="per-token"),
        bench.summarise([_run("warm", 1.0, 1.0, 1.0)]),
    )
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
            json.dumps(
                {"id": i, "question": f"Q {i}", "gold": [], "reference_answer": "", "notes": ""}
            )
            for i in ("r01", "r02", "r09", "r03")
        )
        + "\n"
    )
    assert bench.load_questions(golden, ("r09", "r01")) == [("r09", "Q r09"), ("r01", "Q r01")]
    with pytest.raises(bench.BenchError):
        bench.load_questions(golden, ("r99",))


class FakeMattermost:
    """Enough of the REST API for one question: login, user lookup, the direct channel, the
    post, and the channel's posts as the assistant's reply streams in over successive polls."""

    def __init__(
        self,
        *,
        reply_updates: list[int],
        reply_create_ms: int,
        stale_reply: bool = False,
        placeholders: bool = False,
    ) -> None:
        self.reply_updates = reply_updates  # update_at seen on poll 1, 2, ...
        self.reply_create_ms = reply_create_ms
        self.stale_reply = stale_reply
        self.placeholders = placeholders  # the plugin's empty status posts, edited every poll
        self.polls = 0
        self.posted: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/api/v4/users/login":
            return httpx.Response(
                200, json={"id": "u1", "username": "analyst_dnd"}, headers={"Token": "tok"}
            )
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
                posts["old"] = {
                    "id": "old",
                    "user_id": "bot1",
                    "create_at": T0_MS - 60_000,
                    "update_at": T0_MS - 50_000,
                    "message": "stale",
                }
            if self.placeholders:
                for i in ("ph1", "ph2"):
                    posts[i] = {
                        "id": i,
                        "user_id": "bot1",
                        "create_at": T0_MS + 1,
                        "update_at": T0_MS + 1000 * self.polls,
                        "message": "",
                    }
            if self.polls <= len(self.reply_updates):
                posts["r1"] = {
                    "id": "r1",
                    "user_id": "bot1",
                    "create_at": self.reply_create_ms,
                    "update_at": self.reply_updates[self.polls - 1],
                    "message": "x" * self.polls,
                }
            elif self.reply_updates:
                posts["r1"] = {
                    "id": "r1",
                    "user_id": "bot1",
                    "create_at": self.reply_create_ms,
                    "update_at": self.reply_updates[-1],
                    "message": "done",
                }
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


def _ask(server: FakeMattermost, clock: FakeClock, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "channel_id": "dm1",
        "bot_user_id": "bot1",
        "question": "q",
        "settle_s": 3.0,
        "timeout_s": 120.0,
        "poll_s": 1.0,
        "clock": clock,
        "sleep": clock.sleep,
    }
    kwargs.update(overrides)
    return bench.ask(_client(server), **kwargs)


def _run_one(server: FakeMattermost, clock: FakeClock, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "channel_id": "dm1",
        "bot_user_id": "bot1",
        "user_id": "u1",
        "question_id": "r01",
        "question": "q",
        "state": "warm",
        "audit": _audit_lines,
        "settle_s": 3.0,
        "timeout_s": 120.0,
        "poll_s": 1.0,
        "clock": clock,
        "sleep": clock.sleep,
    }
    kwargs.update(overrides)
    return bench.run_one(_client(server), **kwargs)


def test_ask_returns_the_post_time_and_the_reply_marks_once_settled() -> None:
    server = FakeMattermost(
        reply_updates=[T0_MS + 17_000, T0_MS + 30_000, T0_MS + 40_000],
        reply_create_ms=T0_MS + 17_000,
    )
    marks = _ask(server, FakeClock(), question="How much for NORAD?")
    assert marks == (T0_MS, T0_MS + 17_000, T0_MS + 40_000)
    assert server.posted == ["How much for NORAD?"]


def test_ask_ignores_the_plugins_empty_placeholder_posts() -> None:
    """Seen live: the plugin creates two empty posts in the same second as the question and
    keeps editing them; the earliest bot post is never the reply. The reply is the bot post
    with text."""
    server = FakeMattermost(
        reply_updates=[T0_MS + 17_000, T0_MS + 40_000, T0_MS + 40_000],
        reply_create_ms=T0_MS + 17_000,
        placeholders=True,
    )
    marks = _ask(server, FakeClock())
    assert marks == (T0_MS, T0_MS + 17_000, T0_MS + 40_000)


def test_a_reply_older_than_the_question_is_ignored() -> None:
    server = FakeMattermost(reply_updates=[], reply_create_ms=0, stale_reply=True)
    with pytest.raises(bench.BenchError, match="timeout"):
        _ask(server, FakeClock(), timeout_s=10.0)


def test_run_one_joins_the_audit_and_computes_hops() -> None:
    server = FakeMattermost(
        reply_updates=[T0_MS + 17_000, T0_MS + 40_000, T0_MS + 40_000],
        reply_create_ms=T0_MS + 17_000,
    )
    run = _run_one(server, FakeClock())
    assert run.error is None and run.request_id == "a1" and run.refused is True
    assert run.hops is not None and run.hops.hop1_s == pytest.approx(13.0)
    assert run.hops.hop2_s == pytest.approx(3.5) and run.hops.hop3_s == pytest.approx(23.5)


def test_a_timeout_is_a_failed_run_not_a_missing_one() -> None:
    server = FakeMattermost(reply_updates=[], reply_create_ms=0)
    run = _run_one(server, FakeClock(), timeout_s=5.0)
    assert run.hops is None and run.error is not None and "timeout" in run.error
    # A state whose every run failed is still reported, not dropped.
    assert bench.summarise([run]) == {"warm": {"n": 0, "failed": 1}}


def test_table_row_shows_a_failed_cold_run_instead_of_na() -> None:
    """n/a means the tier has no cold state; a cold run that failed must say so instead."""
    s = bench.summarise([_run("warm", 1.0, 1.0, 1.0)] * 5 + [_run("cold", 0, 0, 0, failed=True)])
    cells = [c.strip() for c in bench.table_row(_labels(), s).strip().strip("|").split("|")]
    assert cells[-2] == "failed (1)" and cells[-1] == "5"


class RaisingTransport(httpx.BaseTransport):
    """Answers like the fake server until the nth request, then drops the connection."""

    def __init__(self, server: FakeMattermost, fail_at: int) -> None:
        self.server, self.fail_at, self.count = server, fail_at, 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        if self.count >= self.fail_at:
            raise httpx.ConnectError("connection reset")
        return self.server(request)


def test_a_transport_error_mid_run_is_a_failed_run_not_an_abort() -> None:
    server = FakeMattermost(reply_updates=[T0_MS + 17_000], reply_create_ms=T0_MS + 17_000)
    mm = bench.Mattermost("http://mm.test", transport=RaisingTransport(server, fail_at=3))
    assert mm.login("analyst_dnd", "pw") == "u1"
    clock = FakeClock()
    run = bench.run_one(
        mm,
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
    assert run.hops is None and run.error is not None and "ConnectError" in run.error


def test_an_audit_read_failure_is_a_failed_run_not_an_abort() -> None:
    import subprocess

    def broken_audit() -> list[str]:
        raise subprocess.CalledProcessError(1, ["docker", "compose", "exec"])

    server = FakeMattermost(
        reply_updates=[T0_MS + 17_000, T0_MS + 40_000], reply_create_ms=T0_MS + 17_000
    )
    run = _run_one(server, FakeClock(), audit=broken_audit)
    assert run.hops is None and run.error is not None and "CalledProcessError" in run.error


def test_a_run_without_audit_lines_is_failed_with_a_reason() -> None:
    server = FakeMattermost(
        reply_updates=[T0_MS + 17_000, T0_MS + 40_000], reply_create_ms=T0_MS + 17_000
    )
    run = _run_one(server, FakeClock(), user_id="u9")
    assert run.hops is None
    assert run.error == "no resolve/answer pair in the audit log for this run"


def test_report_merges_earlier_runs_and_never_carries_a_key() -> None:
    earlier = bench.report(
        _labels(),
        [_run("cold", 30.0, 10.0, 20.0)],
        ran_at="2026-10-13T00:00:00+00:00",
        commit="abc1234",
    )
    runs = bench.merge_runs(earlier, [_run("warm", 10.0, 4.0, 20.0)])
    assert [r.state for r in runs] == ["cold", "warm"]
    rep = bench.report(_labels(), runs, ran_at="2026-10-13T01:00:00+00:00", commit="abc1234")
    assert rep["summary"]["cold"]["n"] == 1 and rep["summary"]["warm"]["n"] == 1
    assert rep["labels"]["tier"] == "local" and rep["commit"] == "abc1234"
    text = json.dumps(rep)
    assert "api_key" not in text.lower() and "authorization" not in text.lower()
    path = bench.result_path("A-local", bench.date(2026, 10, 13))
    assert path.name == "bench-mattermost-A-local-2026-10-13.json"


def test_main_cold_refuses_when_a_model_is_still_loaded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def run_command(cmd: list[str]) -> str:
        if cmd[:3] == ["docker", "model", "ps"]:
            return "MODEL NAME  BACKEND\nai/qwen3.5:9b-q4_K_M  llama.cpp\n"
        return ""

    monkeypatch.setattr(bench, "run_command", run_command)
    code = bench.main(["--configuration", "A-local", "--cold", "--url", "http://mm.test"])
    assert code == 1
    assert "docker model unload --all" in capsys.readouterr().err


def test_main_writes_the_results_file_and_prints_the_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server = FakeMattermost(
        reply_updates=[T0_MS + 17_000, T0_MS + 40_000, T0_MS + 40_000],
        reply_create_ms=T0_MS + 17_000,
    )
    clock = FakeClock()
    golden = tmp_path / "retrieval.jsonl"
    golden.write_text(
        json.dumps({"id": "r01", "question": "q", "gold": [], "reference_answer": "", "notes": ""})
        + "\n"
    )

    def run_command(cmd: list[str]) -> str:
        return "\n".join(_audit_lines()) if cmd[:2] == ["docker", "compose"] else "x"

    monkeypatch.setattr(bench, "GOLDEN", golden)
    monkeypatch.setattr(bench, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(bench, "QUESTION_IDS", ("r01",))
    monkeypatch.setattr(bench, "run_command", run_command)
    monkeypatch.setattr(bench, "_transport", lambda: httpx.MockTransport(server))
    monkeypatch.setattr(bench, "_clock", lambda: clock)
    code = bench.main(
        [
            "--configuration",
            "A-local",
            "--warm",
            "--passes",
            "2",
            "--url",
            "http://mm.test",
            "--password",
            "pw",
        ]
    )
    assert code == 0
    files = list(tmp_path.glob("bench-mattermost-A-local-*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["summary"]["warm"]["n"] == 2 and data["labels"]["configuration"] == "A-local"
    assert "| A-local |" in capsys.readouterr().out
