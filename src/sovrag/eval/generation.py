"""Generation suite: faithfulness and correctness judged by a different model family.

The judge sees the question, the chunks the answer cited, the reference answer and the
answer, and returns one small JSON object. A judge from the generator's own family would
share its blind spots, so the run refuses to start when the two model ids are equal.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

import psycopg

from sovrag.eval.golden import RetrievalItem, UnanswerableItem
from sovrag.eval.metrics import mean
from sovrag.eval.report import SuiteResult
from sovrag.generate.answer import Answer

CORRECT_VALUES = ("yes", "partial", "no")
HUMAN_CHECK_SAMPLE = 10

JUDGE_SYSTEM = (
    "You grade an answer produced from numbered sources. Reply with one JSON object and nothing "
    'else: {"faithful": true or false, "unsupported_claims": [strings], "correct": "yes" or '
    '"partial" or "no"}. faithful is false when any claim in the answer is not supported by the '
    "sources; list each such claim in unsupported_claims. correct compares the answer to the "
    "reference: yes when it states the same facts, partial when it states some, no otherwise."
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass(frozen=True)
class Verdict:
    faithful: bool
    unsupported_claims: tuple[str, ...]
    correct: str


def judge_messages(
    question: str, answer_text: str, sources: Sequence[tuple[int, str]], reference: str
) -> list[dict[str, str]]:
    numbered = "\n\n".join(f"[{n}] {text}" for n, text in sources)
    user = (
        f"Sources:\n\n{numbered}\n\nQuestion: {question}\n\nReference answer: {reference}\n\n"
        f"Answer to grade: {answer_text}"
    )
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}]


def parse_verdict(text: str) -> Verdict:
    cleaned = _FENCE.sub("", text.strip()).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"judge reply is not JSON: {text[:80]!r}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("faithful"), bool):
        raise ValueError(f"judge reply lacks a boolean faithful: {cleaned[:80]!r}")
    correct = data.get("correct")
    if correct not in CORRECT_VALUES:
        raise ValueError(f"judge reply has correct={correct!r}, expected one of {CORRECT_VALUES}")
    claims = data.get("unsupported_claims") or []
    return Verdict(
        faithful=bool(data["faithful"]),
        unsupported_claims=tuple(str(c) for c in claims),
        correct=str(correct),
    )


def chunk_texts(conn: psycopg.Connection, chunk_ids: Sequence[int]) -> dict[int, str]:
    if not chunk_ids:
        return {}
    rows = conn.execute("select id, text from chunks where id = any(%s)", (list(chunk_ids),))
    return {int(cid): str(text) for cid, text in rows.fetchall()}


def _timed(fn: Callable[[], Answer]) -> tuple[Answer, float]:
    start = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - start


def run_generation(
    items: Sequence[RetrievalItem],
    unanswerable: Sequence[UnanswerableItem],
    *,
    answer_fn: Callable[[str], Answer],
    judge_fn: Callable[[Sequence[dict[str, str]]], str],
    chunk_text_fn: Callable[[Sequence[int]], Mapping[int, str]],
    generator_model: str,
    judge_model: str,
    seed: int = 0,
) -> SuiteResult:
    if generator_model == judge_model:
        raise ValueError(f"judge model must differ from the generator, both are {judge_model!r}")
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    verdicts: list[Verdict] = []
    parse_failures = 0
    false_refusals = 0
    for item in items:
        result, latency = _timed(partial(answer_fn, item.question))
        latencies.append(latency)
        row: dict[str, Any] = {
            "id": item.id,
            "kind": "answerable",
            "refused": result.refused,
            "answer": result.text,
            "citations": [c.chunk_id for c in result.citations],
            "verdict": None,
            "latency_s": round(latency, 3),
        }
        if result.refused:
            false_refusals += 1
        else:
            texts = chunk_text_fn([c.chunk_id for c in result.citations])
            sources = [(c.n, texts.get(c.chunk_id, "")) for c in result.citations]
            try:
                verdict = parse_verdict(
                    judge_fn(
                        judge_messages(item.question, result.text, sources, item.reference_answer)
                    )
                )
            except ValueError:
                parse_failures += 1
            else:
                verdicts.append(verdict)
                row["verdict"] = {
                    "faithful": verdict.faithful,
                    "unsupported_claims": list(verdict.unsupported_claims),
                    "correct": verdict.correct,
                }
        rows.append(row)
    unanswerable_refusals = 0
    for u in unanswerable:
        result, latency = _timed(partial(answer_fn, u.question))
        latencies.append(latency)
        unanswerable_refusals += int(result.refused)
        rows.append(
            {
                "id": u.id,
                "kind": "unanswerable",
                "refused": result.refused,
                "answer": result.text,
                "latency_s": round(latency, 3),
            }
        )
    judged = [r for r in rows if r["kind"] == "answerable" and r["verdict"] is not None]
    for r in random.Random(seed).sample(judged, min(HUMAN_CHECK_SAMPLE, len(judged))):
        r["human_check"] = None
    n_judged = len(verdicts)
    metrics = {
        "faithful_rate": mean([1.0 if v.faithful else 0.0 for v in verdicts]),
        "correct_yes": mean([1.0 if v.correct == "yes" else 0.0 for v in verdicts]),
        "correct_partial": mean([1.0 if v.correct == "partial" else 0.0 for v in verdicts]),
        "correct_no": mean([1.0 if v.correct == "no" else 0.0 for v in verdicts]),
        "false_refusal_rate": false_refusals / len(items) if items else 0.0,
        "unanswerable_refusal_rate": (
            unanswerable_refusals / len(unanswerable) if unanswerable else 0.0
        ),
        "judge_parse_failures": float(parse_failures),
        "judged": float(n_judged),
        "mean_latency_s": mean(latencies),
    }
    return SuiteResult(metrics=metrics, rows=rows)


def agreement_rate(rows: Sequence[Mapping[str, Any]]) -> float | None:
    filled = [r for r in rows if isinstance(r.get("human_check"), bool) and r.get("verdict")]
    if not filled:
        return None
    agreed = sum(1 for r in filled if bool(r["human_check"]) == bool(r["verdict"]["faithful"]))
    return agreed / len(filled)
