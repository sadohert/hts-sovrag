"""Generation suite with a fake generator and a fake judge."""

from collections.abc import Sequence

import pytest

from sovrag.eval.generation import (
    Verdict,
    agreement_rate,
    judge_messages,
    parse_verdict,
    run_generation,
)
from sovrag.eval.golden import GoldChunk, RetrievalItem, UnanswerableItem
from sovrag.generate.answer import Answer, Citation
from sovrag.generate.prompts import REFUSAL_TEXT


def _answer(question: str, *, refused: bool = False) -> Answer:
    cites = () if refused else (Citation(1, "d", 7, "t", "h", "https://example.test/d"),)
    return Answer(
        question=question,
        principal="auditor",
        mode="hybrid",
        request_id=None,
        text=REFUSAL_TEXT if refused else "The plan is X [1].",
        citations=cites,
        refused=refused,
        model="gen",
        retrieved_chunk_ids=(7,),
        applied_filter="f",
    )


def _judge(messages: Sequence[dict[str, str]]) -> str:
    return '```json\n{"faithful": true, "unsupported_claims": [], "correct": "yes"}\n```'


ITEMS = [
    RetrievalItem(f"r{i}", f"q{i}", (GoldChunk("d", i),), "The plan is X.", "n") for i in range(3)
]
UNANSWERABLE = [UnanswerableItem("u1", "u1", "n"), UnanswerableItem("u2", "u2", "n")]


def test_parse_verdict_accepts_fenced_json_and_rejects_bad_values() -> None:
    assert parse_verdict(_judge([])) == Verdict(True, (), "yes")
    assert parse_verdict(
        '{"faithful": false, "unsupported_claims": ["x"], "correct": "no"}'
    ) == Verdict(False, ("x",), "no")
    with pytest.raises(ValueError):
        parse_verdict('{"faithful": true, "unsupported_claims": [], "correct": "maybe"}')
    with pytest.raises(ValueError):
        parse_verdict("not json")


def test_judge_messages_carry_sources_reference_and_answer() -> None:
    messages = judge_messages("q?", "a [1]", ["source one"], "ref")
    assert messages[0]["role"] == "system"
    user = messages[1]["content"]
    assert "source one" in user and "ref" in user and "a [1]" in user and "q?" in user


def test_run_generation_reports_rates_and_samples_for_human_check() -> None:
    refusals = {"u1", "u2", "q2"}

    def answer_fn(q: str) -> Answer:
        return _answer(q, refused=q in refusals)

    result = run_generation(
        ITEMS,
        UNANSWERABLE,
        answer_fn=answer_fn,
        judge_fn=_judge,
        chunk_text_fn=lambda ids: {i: "text" for i in ids},
        generator_model="gen",
        judge_model="judge",
    )
    m = result.metrics
    assert m["faithful_rate"] == 1.0
    assert m["correct_yes"] == 1.0 and m["correct_no"] == 0.0
    assert m["false_refusal_rate"] == pytest.approx(1 / 3)
    assert m["unanswerable_refusal_rate"] == 1.0
    assert m["judge_parse_failures"] == 0
    assert m["mean_latency_s"] >= 0.0
    judged = [r for r in result.rows if r["kind"] == "answerable" and r["verdict"]]
    assert len(judged) == 2
    assert all("human_check" in r and r["human_check"] is None for r in judged)


def test_judge_parse_failure_is_counted_not_raised() -> None:
    result = run_generation(
        ITEMS[:1],
        [],
        answer_fn=_answer,
        judge_fn=lambda m: "garbage",
        chunk_text_fn=lambda ids: {i: "text" for i in ids},
        generator_model="gen",
        judge_model="judge",
    )
    assert result.metrics["judge_parse_failures"] == 1
    assert result.rows[0]["verdict"] is None


def test_same_model_for_judge_and_generator_is_refused() -> None:
    with pytest.raises(ValueError, match="judge"):
        run_generation(
            ITEMS,
            [],
            answer_fn=_answer,
            judge_fn=_judge,
            chunk_text_fn=lambda ids: {},
            generator_model="same",
            judge_model="same",
        )


def test_agreement_rate_over_filled_rows() -> None:
    rows = [
        {"verdict": {"faithful": True}, "human_check": True},
        {"verdict": {"faithful": True}, "human_check": False},
        {"verdict": {"faithful": False}, "human_check": None},
    ]
    assert agreement_rate(rows) == 0.5
    assert agreement_rate([{"verdict": {"faithful": True}, "human_check": None}]) is None
