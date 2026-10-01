# sovrag

A reference implementation of permissions-aware retrieval-augmented generation over public Government of Canada documents, running entirely on Docker: Postgres with pgvector for storage, Docker Model Runner for inference and embeddings, and Compose as the whole deployment story.

Status: corpus indexed; vector, keyword and hybrid retrieval, each filtered by the principal's entitlement in SQL; grounded answers with citations and refusal from the CLI, over MCP, and in Mattermost direct messages; retrieval and leak suites gate CI, generation judged locally; the HTTP API serves the same answers with the principal bound by the server.

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

In Mattermost mode each direct message runs as the Mattermost user asking, with entitlement read from admin-managed user attributes. `docker compose --profile mattermost up -d` and `uv run python scripts/seed_mattermost.py` set it up; `docs/mattermost-demo.md` is the walkthrough. Prerequisites: Docker Desktop with Model Runner, and amd64 emulation on Apple silicon for the Mattermost image. Mattermost Entry needs no licence key (ADR 0011).

## Serve it

The HTTP API is the entry point for a web application, a portal, or a probe. `docker compose up -d` serves it on port 8000 in static mode, bound to `PRINCIPAL` (default `analyst_dnd`):

```
curl -s localhost:8000/healthz
curl -s -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"question": "What did the Auditor General find about ArriveCAN?"}'
```

The response is the same object `sovrag ask --json` prints: the answer with `[n]` citations or the refusal sentence, the citations, the rendered entitlement filter and a `request_id` that also appears in the audit log. As `analyst_dnd` is not cleared for the Auditor General reports, that question comes back as the refusal sentence. For many users, put an authenticating gateway in front and set `API_IDENTITY=gateway` and `API_SHARED_SECRET`; the gateway sends the secret as a bearer and names the user's principal in `X-Sovrag-Principal`. A request without both is 401. The body cannot name a principal. See ADR 0015.

## Measure it

```
uv run sovrag eval load-fixture       # the committed index; no model needed
uv run sovrag eval retrieval --check  # hit@5, recall@10, MRR per mode, against eval/thresholds.json
uv run sovrag eval leak --check       # zero forbidden chunks for every principal
uv run sovrag eval generation         # needs JUDGE_MODEL; judged locally, results labelled by model
```

Measured baseline, `eval/results/retrieval-2026-09-22.json`:

| Mode | hit@5 | recall@10 | MRR |
|---|---|---|---|
| hybrid | 0.900 | 0.933 | 0.716 |
| vector | 0.867 | 0.933 | 0.705 |

Measured baseline, `eval/results/generation-2026-09-22.json`, judge `ai/gemma4:12b-q4_K_M`:

| faithful_rate | correct_yes | correct_partial | correct_no | false_refusal_rate | unanswerable_refusal_rate | mean_latency_s |
|---|---|---|---|---|---|---|
| 1.000 | 0.966 | 0.000 | 0.034 | 0.033 | 0.900 | 4.121 |

Generator `ai/qwen3.5:9b-q4_K_M`, embedder `hf.co/gpustack/bge-m3-gguf:Q8_0`, on
`docker-model-runner` with the `llama.cpp` backend. Results under `eval/results/` carry these
model, quantization and backend labels, so a run on a different backend is compared against
this baseline, never confused with it.

See `docs/architecture.md` for the component and data model diagrams, `docs/adr/` for decisions.
