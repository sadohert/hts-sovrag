# sovrag

Reference implementation of permissions-aware RAG over Canadian federal public documents.
Optimise for clarity and defensibility over cleverness: a reader should be able to verify
every claim in the README against the code.

## Stack
- Python 3.14, uv, ruff, mypy strict, pytest. Type hints everywhere. No LangChain or
  LlamaIndex, so every step of retrieval and generation is visible in this repo.
- Inference: Docker Model Runner by default, vLLM as the production swap. All LLM and
  embedding calls go through the OpenAI-compatible clients in src/sovrag/generate/llm.py and
  src/sovrag/index/embed.py, with base URL and model from env, so backends swap without
  code changes.
- Storage: PostgreSQL 18 with pgvector. Schema in src/sovrag/index/schema.sql.

## Invariants
- Entitlement filtering happens in SQL (src/sovrag/retrieve/filters.py), never in Python
  after retrieval. A chunk the principal may not see must never leave the database.
- Every retrieval returns citations with doc_id, chunk_id, heading_path and source_url, so
  any answer can be traced to a public source.
- The MCP server binds its principal from config at startup. Tool arguments never carry
  identity, because an agent that can name its principal can escalate it.
- Classification labels are synthetic (ADR 0003). Never write copy implying real protected
  data was used.
- Never claim Docker org-level governance was applied; only local sandbox policy was.
- Pin dependencies, images, actions and models to the newest supported release, checked at
  the time of the change. Do not write versions from memory.

## Working
- Tests first. Watch the test fail, then implement. Keep test-only helpers out of src/.
- Any architecture decision gets a short ADR in docs/adr/ before the code lands.
- Design specs and implementation plans live in docs/superpowers/.
- Keep the scope to what the README states. Anything beyond it needs an ADR first.
- Done means: ruff, mypy and pytest pass locally, CI is green, and the README status line
  matches what the code does.

## Commands
- docker compose up -d postgres
- uv run sovrag ingest --manifest data/manifest.yaml
- uv run sovrag query "..." --principal analyst_dnd --mode vector
- uv run pytest
- Not implemented yet: sovrag serve, sovrag eval, the bm25 and hybrid modes, the mcp profile.
