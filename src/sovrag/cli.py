"""sovrag CLI: ingest | query | ask | eval | serve."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from sovrag.auth import Principal, resolve_principal
from sovrag.config import Settings, get_settings
from sovrag.corpus.fetch import browser_getter, fetch_document
from sovrag.corpus.ingest import ingest_document
from sovrag.corpus.manifest import load_manifest
from sovrag.eval.cli import eval_app
from sovrag.generate import MAX_K, GenerationError
from sovrag.generate.answer import answer
from sovrag.index.embed import Embedder
from sovrag.index.store import connect
from sovrag.retrieve import Mode, entitlement_for, search

app = typer.Typer(
    help="Permissions-aware RAG over Canadian federal public documents.",
    no_args_is_help=True,
)
app.add_typer(eval_app, name="eval")


def _bind(principal_id: str, settings: Settings) -> Principal:
    """Resolve --principal against the configured file; an unknown id is a usage error."""
    try:
        return resolve_principal(principal_id, Path(settings.principals_path))
    except KeyError as exc:
        raise typer.BadParameter(
            f"unknown principal: {principal_id}", param_hint="--principal"
        ) from exc


@app.command()
def ingest(
    manifest: Annotated[Path, typer.Option(help="Corpus manifest")] = Path("data/manifest.yaml"),
    raw_dir: Annotated[Path, typer.Option(help="Fetched HTML cache")] = Path("data/raw"),
    fetch: Annotated[bool, typer.Option(help="Fetch pages missing from raw-dir first")] = True,
    only: Annotated[list[str] | None, typer.Option(help="Restrict to these document ids")] = None,
) -> None:
    """Fetch (if needed), parse, chunk, embed and index the corpus."""
    settings = get_settings()
    docs = load_manifest(manifest)
    if only:
        docs = [d for d in docs if d.id in set(only)]
    embedder = Embedder.from_settings(settings)
    getter = browser_getter() if fetch else None
    total = 0
    with connect(settings.database_url) as conn:
        for meta in docs:
            if getter is not None:
                fetch_document(
                    {"id": meta.id, "source_url": meta.source_url, "pages": list(meta.pages)},
                    raw_dir,
                    get=getter,
                )
            n = ingest_document(conn, meta, raw_dir=raw_dir, embedder=embedder)
            total += n
            typer.echo(f"{meta.id}: {n} chunks")
    typer.echo(f"{len(docs)} documents, {total} chunks indexed")


@app.command()
def query(
    question: str,
    principal: Annotated[str, typer.Option(help="Principal id in the file PRINCIPALS_PATH names")],
    mode: Annotated[Mode, typer.Option(help="vector | keyword | hybrid")] = Mode.vector,
    k: Annotated[int, typer.Option(min=1)] = 5,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text")] = False,
) -> None:
    """Retrieve the top-k chunks for a question, as a principal.

    Use `ask` for an answer with citations.
    """
    settings = get_settings()
    who = _bind(principal, settings)
    applied_filter = entitlement_for(who).render()
    with connect(settings.database_url) as conn:
        hits = search(
            conn,
            question,
            mode=mode,
            k=k,
            embed_query=lambda q: Embedder.from_settings(settings).embed_one(q),
            principal=who,
        )

    if json_out:
        payload = {
            "question": question,
            "principal": principal,
            "mode": mode.value,
            "applied_filter": applied_filter,
            "hits": [asdict(h) for h in hits],
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    typer.echo(
        f"question: {question}\nprincipal: {who.id}  mode: {mode.value}  k: {k}\n"
        f"filter: {applied_filter}\n"
    )
    for i, h in enumerate(hits, start=1):
        typer.echo(f"[{i}] {h.doc_id} chunk {h.chunk_id}  score={h.score:.3f}")
        typer.echo(f"    {h.heading_path}")
        typer.echo(f"    {h.source_url}")
        preview = h.text.replace("\n", " ")
        typer.echo(f"    {preview[:240]}{'...' if len(preview) > 240 else ''}\n")


@app.command()
def ask(
    question: str,
    principal: Annotated[str, typer.Option(help="Principal id in the file PRINCIPALS_PATH names")],
    mode: Annotated[Mode, typer.Option(help="vector | keyword | hybrid")] = Mode.hybrid,
    k: Annotated[int, typer.Option(min=1, max=MAX_K)] = 5,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text")] = False,
    request_id: Annotated[
        str | None, typer.Option(help="Caller's id, echoed into the audit log")
    ] = None,
) -> None:
    """Answer a question from retrieved chunks only, with citations; refuse when unsupported."""
    settings = get_settings()
    who = _bind(principal, settings)
    try:
        result = answer(question, principal=who, mode=mode, k=k, request_id=request_id)
    except GenerationError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_out:
        typer.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return
    typer.echo(result.text)
    if result.citations:
        typer.echo("\nSources:")
        for c in result.citations:
            typer.echo(f"[{c.n}] {c.title}, {c.heading_path}, {c.source_url}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the FastAPI server."""
    typer.echo("serve: not implemented yet", err=True)
    raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
