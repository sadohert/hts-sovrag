# sovrag

A reference implementation of permissions-aware retrieval-augmented generation over public Government of Canada documents, running entirely on Docker: Postgres with pgvector for storage, Docker Model Runner for inference and embeddings, and Compose as the whole deployment story.

Status: everything below exists on `main` and is exercised by the test suite and the CI evaluation gates.

## What it does

- Ingests Departmental Plans, Departmental Results Reports, Treasury Board security policy instruments, and Auditor General reports: 27 public documents from five departments (DND, PS, PSPC, SSC, TBS) plus the government-wide instruments and reports. No Departmental Plan or Departmental Results Report could be located for CSE, so it has none in the corpus (the manifest records the search).
- Indexes them in Postgres with pgvector (HNSW) and full-text search, and retrieves by vector, keyword or hybrid ranking.
- Enforces a two-axis entitlement model (classification level and department) as a SQL pre-filter inside every retrieval query, never as a Python post-filter.
- Answers questions from the entitled chunks only, with `[n]` citations traceable to a public source, and refuses when they do not contain the answer.
- Serves those answers four ways: the `sovrag ask` command, an HTTP API, an MCP server for agents, and Mattermost direct messages. The HTTP API, the MCP server and Mattermost bind the principal on the server side, never from anything an agent or caller can set; the CLI takes `--principal` from the operator running it.
- Measures itself: a retrieval suite and a leak suite run in CI against a committed index and fail the build on a quality regression or a single forbidden chunk; a generation suite judged by a second model family runs locally, with results labelled by model, quantization and backend.

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
uv run sovrag ask "What did the Auditor General find about ArriveCAN?" --principal analyst_dnd
uv run sovrag ask "What did the Auditor General find about ArriveCAN?" --principal auditor
```

The two queries differ: `analyst_dnd` is cleared to `PROTECTED_A` and never sees the Auditor General reports, which are labelled `PROTECTED_B`; `auditor` does. Each result prints the filter that was applied. `ask` shows the consequence: `analyst_dnd` gets the refusal sentence, because the model is only ever shown entitled chunks, and `auditor` gets an answer with `[n]` citations. `ingest` pulls the two models declared in `compose.yaml` on first use. Pages are cached under `data/raw/`; re-running is idempotent.

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

To watch the audit log while it runs, `docker compose --profile observability up -d` adds Loki, Alloy and Grafana; the dashboard is at `http://localhost:3000/d/sovrag-audit`: counts of answers, refusals and resolutions, a table of every audit line with the principal, the outcome and what that principal may see, and a table of refusals. Grafana is bound to this host only and opens anonymously as a viewer, with its default administrator login left in place; both are local-demo settings, wrong anywhere else. See ADR 0016.

Where the models run is configuration: `docs/serving-backends.md` measures the three hops of a Mattermost reply on this laptop and with the plugin's model, then sovrag's generator, moved to Baseten, and says what moving inference off the machine means for sovereignty. See ADR 0017.

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
