"""sovrag eval: build and load the fixture, run a suite, check thresholds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import psycopg
import typer

from sovrag.auth import Principal, load_principals, resolve_principal
from sovrag.config import Settings, get_settings
from sovrag.eval.fixture import (
    FIXTURE_DIR,
    build_fixture,
    load_fixture,
    load_question_embeddings,
)
from sovrag.eval.generation import agreement_rate, chunk_texts, run_generation
from sovrag.eval.golden import (
    GOLDEN_DIR,
    LeakItem,
    RetrievalItem,
    UnanswerableItem,
    load_leak,
    load_retrieval,
    load_unanswerable,
)
from sovrag.eval.leak import run_leak, run_leak_answers
from sovrag.eval.report import (
    THRESHOLDS_PATH,
    SuiteResult,
    check_thresholds,
    default_path,
    load_thresholds,
    make_report,
    write_report,
)
from sovrag.eval.retrieval import run_retrieval
from sovrag.generate.answer import answer
from sovrag.generate.llm import Generator
from sovrag.index.embed import Embedder
from sovrag.index.store import connect
from sovrag.retrieve import Mode

eval_app = typer.Typer(
    help="Measure retrieval, generation and leak behaviour against the golden sets.",
    no_args_is_help=True,
)

GoldenDir = Annotated[Path, typer.Option(help="Directory with the three golden files")]
FixtureDir = Annotated[Path, typer.Option(help="Committed index directory")]
Out = Annotated[
    Path | None, typer.Option(help="Result file; default eval/results/<suite>-<date>.json")
]
Check = Annotated[
    bool, typer.Option("--check", help="Compare to eval/thresholds.json; exit 1 on a miss")
]

LEAK_ANSWER_PRINCIPALS = ("public", "analyst_dnd", "analyst_ps")


def _bind(principal_id: str, settings: Settings) -> Principal:
    try:
        return resolve_principal(principal_id, Path(settings.principals_path))
    except KeyError as exc:
        raise typer.BadParameter(f"unknown principal: {principal_id}") from exc


def _known_doc_ids(conn: psycopg.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("select id from documents").fetchall()}


def _finish(suite: str, settings: Settings, result: SuiteResult, out: Path | None) -> None:
    path = write_report(make_report(suite, settings, result), out or default_path(suite))
    for name, value in result.metrics.items():
        typer.echo(f"{name:32} {value:.3f}")
    typer.echo(f"wrote {path}")


def _gate(misses: list[str]) -> None:
    if misses:
        for miss in misses:
            typer.echo(f"threshold miss: {miss}", err=True)
        raise typer.Exit(code=1)
    typer.echo("thresholds met")


@eval_app.command("build-fixture")
def build_fixture_cmd(golden_dir: GoldenDir = GOLDEN_DIR, out: FixtureDir = FIXTURE_DIR) -> None:
    """Serialise the live index and embed every golden question. Needs the embedding model."""
    settings = get_settings()
    with connect(settings.database_url) as conn:
        known = _known_doc_ids(conn)
        items = load_retrieval(golden_dir / "retrieval.jsonl", known_doc_ids=known)
        unanswerable = load_unanswerable(golden_dir / "unanswerable.jsonl")
        leaks = load_leak(golden_dir / "leak.jsonl", known_doc_ids=known)
        merged: list[RetrievalItem | UnanswerableItem | LeakItem] = [*items, *unanswerable, *leaks]
        questions = [(i.id, i.question) for i in merged]
        embedder = Embedder.from_settings(settings)
        counts = build_fixture(
            conn,
            out,
            questions,
            embedder.embed,
            embed_model=settings.embed_model,
            dim=settings.embed_dim,
        )
    typer.echo(", ".join(f"{k}: {v}" for k, v in counts.items()))


@eval_app.command("load-fixture")
def load_fixture_cmd(fixture_dir: FixtureDir = FIXTURE_DIR) -> None:
    """Load the committed index into the configured database. Needs no model."""
    settings = get_settings()
    with connect(settings.database_url) as conn:
        counts = load_fixture(conn, fixture_dir)
    typer.echo(", ".join(f"{k}: {v}" for k, v in counts.items()))


@eval_app.command()
def retrieval(
    golden_dir: GoldenDir = GOLDEN_DIR,
    fixture_dir: FixtureDir = FIXTURE_DIR,
    out: Out = None,
    check: Check = False,
    principal: Annotated[
        str, typer.Option(help="Who searches; must see every document")
    ] = "auditor",
) -> None:
    """hit@5, recall@10 and MRR for every mode, from precomputed question vectors."""
    settings = get_settings()
    who = _bind(principal, settings)
    items = load_retrieval(golden_dir / "retrieval.jsonl")
    embeddings = load_question_embeddings(fixture_dir)
    with connect(settings.database_url) as conn:
        result = run_retrieval(conn, items, embeddings, principal=who)
    _finish("retrieval", settings, result, out)
    if check:
        _gate(check_thresholds(result.metrics, load_thresholds(THRESHOLDS_PATH)["retrieval"]))


@eval_app.command()
def leak(
    golden_dir: GoldenDir = GOLDEN_DIR,
    fixture_dir: FixtureDir = FIXTURE_DIR,
    out: Out = None,
    check: Check = False,
    answers: Annotated[
        bool, typer.Option("--answers", help="Also ask the generator; needs a model")
    ] = False,
) -> None:
    """Zero forbidden chunks for every named principal, plus a positive control as auditor."""
    settings = get_settings()
    principals = load_principals(Path(settings.principals_path))
    items = load_leak(golden_dir / "leak.jsonl")
    embeddings = load_question_embeddings(fixture_dir)
    with connect(settings.database_url) as conn:
        result = run_leak(conn, items, embeddings, principals, control=principals["auditor"])
    if answers:
        subset = {name: principals[name] for name in LEAK_ANSWER_PRINCIPALS}
        asked = run_leak_answers(
            items, subset, lambda q, p: answer(q, principal=p, mode=Mode.hybrid, k=5)
        )
        result = SuiteResult({**result.metrics, **asked.metrics}, [*result.rows, *asked.rows])
    _finish("leak", settings, result, out)
    if check:
        floor = load_thresholds(THRESHOLDS_PATH)["leak"]
        misses = check_thresholds(result.metrics, {"positive_control": floor["positive_control"]})
        if result.metrics["leaks"] > 0:
            misses.insert(0, f"leaks: {int(result.metrics['leaks'])} forbidden chunks returned")
        _gate(misses)


@eval_app.command()
def generation(
    golden_dir: GoldenDir = GOLDEN_DIR,
    out: Out = None,
    seed: Annotated[int, typer.Option(help="Seed for the human-check sample")] = 0,
) -> None:
    """Faithfulness and correctness judged by JUDGE_MODEL; refusals on unanswerable questions."""
    settings = get_settings()
    if not settings.judge_model:
        raise typer.BadParameter(
            "set JUDGE_MODEL to a model from a different family than LLM_MODEL"
        )
    auditor = _bind("auditor", settings)
    items = load_retrieval(golden_dir / "retrieval.jsonl")
    unanswerable = load_unanswerable(golden_dir / "unanswerable.jsonl")
    judge = Generator(
        base_url=settings.judge_base_url or settings.llm_base_url,
        model=settings.judge_model,
        api_key=settings.judge_api_key or "not-needed",
        max_retries=settings.llm_max_retries,
        timeout_s=settings.llm_timeout_s,
    )
    try:
        with connect(settings.database_url) as conn:
            result = run_generation(
                items,
                unanswerable,
                answer_fn=lambda q: answer(q, principal=auditor, mode=Mode.hybrid, k=5),
                judge_fn=judge.complete,
                chunk_text_fn=lambda ids: chunk_texts(conn, ids),
                generator_model=settings.llm_model,
                judge_model=settings.judge_model,
                seed=seed,
            )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _finish("generation", settings, result, out)


@eval_app.command()
def agreement(result_file: Path) -> None:
    """Agreement between the judge and the human_check values filled into a generation result."""
    rows = json.loads(result_file.read_text("utf-8"))["rows"]
    rate = agreement_rate(rows)
    if rate is None:
        typer.echo("no human_check filled in", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"agreement {rate:.3f}")
