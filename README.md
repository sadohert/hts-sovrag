# sovrag

A reference implementation of permissions-aware retrieval-augmented generation over public Government of Canada documents, running entirely on Docker: Postgres with pgvector for storage, Docker Model Runner for inference and embeddings, and Compose as the whole deployment story.

Status: scaffold. Nothing here is usable yet.

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
docker compose up -d
uv run sovrag ingest --manifest data/manifest.yaml
uv run sovrag query "What are National Defence's core responsibilities?" --principal analyst_dnd
```

More once it exists. See `docs/` for design notes.
