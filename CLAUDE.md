# sovrag

Reference implementation of permissions-aware RAG over Canadian federal public documents.
Portfolio artifact. Optimise for clarity and defensibility over cleverness.

## Conventions
- Python 3.12, uv, ruff, pytest. Type hints everywhere. No LangChain/LlamaIndex.
- All LLM and embedding access goes through the OpenAI-compatible client in
  src/sovrag/generate/llm.py and src/sovrag/index/embed.py. Base URL and model
  come from env; Docker Model Runner is the default, vLLM and Cohere are swaps.
- Entitlement filtering happens in SQL (retrieve/filters.py). Never filter in Python after retrieval.
- The MCP server binds its principal from config at startup. Tool arguments never carry identity.
- Every retrieval returns citations with doc_id, chunk_id, heading_path, source_url.
- Classification labels are synthetic. Never write copy implying real protected data was used.
- Never claim to have run Docker org-level governance; local sbx policy only.
- Public text (README, docs/): no em dashes; avoid "delve", "leverage", "seamless",
  "landscape", "journey"; no triads for rhythm; no summary sign-off paragraphs.
- Any architecture decision gets a short ADR in docs/adr/.

## Commands
- docker compose up -d
- uv run sovrag ingest --manifest data/manifest.yaml
- uv run sovrag query "..." --principal analyst_dnd --mode hybrid
- uv run sovrag serve
- uv run sovrag eval --suite retrieval|generation|leak [--via mcp]
- docker compose --profile mcp up mcp && docker mcp ...   # MCP profile
- uv run pytest

## Scope
Keep the scope to what the README states. Anything beyond it needs an ADR first.
