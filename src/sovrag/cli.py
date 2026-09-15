"""sovrag CLI: ingest | query | serve | serve-mcp | eval."""

from __future__ import annotations

import typer

app = typer.Typer(
    help="Permissions-aware RAG over Canadian federal public documents.",
    no_args_is_help=True,
)


@app.command()
def ingest(manifest: str = typer.Option("data/manifest.yaml", help="Corpus manifest")) -> None:
    """Fetch, parse, chunk, embed and index the corpus."""
    raise typer.Exit(code=_todo("ingest"))


@app.command()
def query(
    question: str,
    principal: str = typer.Option(..., help="Principal id from principals.yaml"),
    mode: str = typer.Option("hybrid", help="vector | bm25 | hybrid"),
    k: int = typer.Option(5),
) -> None:
    """Retrieve (and later answer) as a principal."""
    raise typer.Exit(code=_todo("query"))


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the FastAPI server."""
    raise typer.Exit(code=_todo("serve"))


def _todo(name: str) -> int:
    typer.echo(f"{name}: not implemented yet", err=True)
    return 2


if __name__ == "__main__":
    app()
