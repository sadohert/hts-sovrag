"""sovrag CLI: ingest | query | serve | serve-mcp | eval."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from sovrag.config import get_settings
from sovrag.corpus.fetch import browser_getter, fetch_document
from sovrag.corpus.ingest import ingest_document
from sovrag.corpus.manifest import load_manifest
from sovrag.index.embed import Embedder
from sovrag.index.store import connect
from sovrag.retrieve import Mode, search

app = typer.Typer(
    help="Permissions-aware RAG over Canadian federal public documents.",
    no_args_is_help=True,
)


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
    principal: Annotated[str, typer.Option(help="Principal id from principals.yaml")],
    mode: Annotated[Mode, typer.Option(help="vector | keyword | hybrid")] = Mode.vector,
    k: Annotated[int, typer.Option(min=1)] = 5,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text")] = False,
) -> None:
    """Retrieve the top-k chunks for a question, as a principal. Generation is not wired in yet."""
    settings = get_settings()
    with connect(settings.database_url) as conn:
        hits = search(
            conn,
            question,
            mode=mode,
            k=k,
            embed_query=lambda q: Embedder.from_settings(settings).embed([q])[0],
        )

    if json_out:
        payload = {
            "question": question,
            "principal": principal,
            "mode": mode.value,
            "hits": [asdict(h) for h in hits],
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    typer.echo(f"question: {question}\nprincipal: {principal}  mode: {mode.value}  k: {k}\n")
    for i, h in enumerate(hits, start=1):
        typer.echo(f"[{i}] {h.doc_id} chunk {h.chunk_id}  score={h.score:.3f}")
        typer.echo(f"    {h.heading_path}")
        typer.echo(f"    {h.source_url}")
        preview = h.text.replace("\n", " ")
        typer.echo(f"    {preview[:240]}{'...' if len(preview) > 240 else ''}\n")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the FastAPI server."""
    typer.echo("serve: not implemented yet", err=True)
    raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
