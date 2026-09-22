# sovrag

A reference implementation of permissions-aware retrieval-augmented generation over public Government of Canada documents, running entirely on Docker: Postgres with pgvector for storage, Docker Model Runner for inference and embeddings, and Compose as the whole deployment story.

Status: corpus indexed; vector, keyword and hybrid retrieval, each filtered by the principal's entitlement in SQL; grounded answers with citations and refusal from the CLI and over MCP.

## What it will do

- Ingest Departmental Plans, Departmental Results Reports, Treasury Board security policy instruments, and Auditor General reports for six departments.
- Index them in Postgres with pgvector (HNSW) and full-text search.
- Enforce a two-axis entitlement model (classification level and department) as a SQL pre-filter, never as a Python post-filter.
- Answer questions with citations and refuse when the entitled context does not contain the answer.
- Ship a leak-test suite proving that a principal can never retrieve a chunk they are not entitled to.

## Classification labels are synthetic

Every document in the corpus is public, published under the Open Government Licence - Canada. Classification tags (`UNCLASSIFIED`, `PROTECTED_A`, `PROTECTED_B`) are assigned by a deterministic rule from document type so that the entitlement model has something to enforce. No protected information was used, handled, or stored.

## Run it

```
docker desktop enable model-runner --tcp=12434   # once; Docker Desktop
docker compose up -d postgres
uv sync
uv run sovrag ingest                              # fetch, parse, chunk, embed, index (~2 min)
uv run sovrag query "What did the Auditor General find about ArriveCAN?" --principal analyst_dnd
uv run sovrag query "What did the Auditor General find about ArriveCAN?" --principal auditor
```

The two queries differ: `analyst_dnd` is cleared to `PROTECTED_A` and never sees the Auditor General reports, which are labelled `PROTECTED_B`; `auditor` does. Each result prints the filter that was applied. `ingest` pulls the two models declared in `compose.yaml` on first use. Pages are cached under `data/raw/`; re-running is idempotent.

To expose the same answers to an agent, run the MCP server. The principal is bound by the server, never by the agent (ADR 0009):

```
uv run sovrag serve-mcp                                          # MCP_IDENTITY=static PRINCIPAL=analyst_dnd from .env
MCP_IDENTITY=static PRINCIPAL=auditor docker compose --profile mcp up -d mcp
```

The Mattermost mode, where each direct message runs as the Mattermost user asking, is configured in the Mattermost walkthrough that ships with the demo profile.

See `docs/architecture.md` for the component and data model diagrams, `docs/adr/` for decisions.
