# Serving-backend benchmark: Baseten beside Docker Model Runner

Date: 2026-10-11. Status: approved in discussion; this file records it.

## Goal

Measure, rather than observe, where the time goes when a person asks the assistant a question
in Mattermost, then move the slow part to a hosted serving backend and measure again. The
output is a benchmark table and a short write-up on the serving-backend tradeoff, including
what moving inference off the machine means for sovereignty. This is a benchmark extension,
not a feature build: no new product features, no refactors beyond what measurement needs.

## What is known and what is not

The direct path (embed, hybrid retrieval, generate) was measured at 4.1 s mean over the 30
questions of the generation eval (`eval/results/generation-2026-09-22.json`). The reply in
Mattermost was observed at around 40 s. The difference was attributed, by reasoning and not by
measurement, to the Agents plugin's loop: the plugin's model reads the conversation and decides
to call the tool (hop 1), the tool runs the direct path (hop 2), and the plugin's model writes the
reply token by token from the tool result (hop 3), with Docker Model Runner reloading a model it
had unloaded when the machine was idle. Hops 1 and 3 were never timed. This work times them.

## Measurement

Hop boundaries come from timestamps that already exist, so no component is added to the path
being measured:

| Hop | Starts | Ends | Source |
|---|---|---|---|
| 1, the agent decides to call the tool | the person's post `create_at` | `resolve` audit event `ts` | Mattermost API, audit log |
| 2, the sovrag pipeline | `resolve` ts | `answer` audit event `ts` | audit log |
| 3, the agent writes the reply | `answer` ts | the reply post's last `update_at` | Mattermost API |
| 3, first token | `answer` ts | the reply post's `create_at` | Mattermost API, when the plugin streams |
| end to end | the person's post `create_at` | the reply post's last `update_at` | Mattermost API |

Hop 1 includes Mattermost delivering the post to the plugin, and hop 3 includes the final post
edit. Both are milliseconds against seconds; the write-up says so. The `resolve` and `answer`
events already share a `request_id` and record the Mattermost user id, which is how a run is
joined to its audit lines. Mattermost timestamps are milliseconds since the epoch; audit
timestamps are RFC 3339 with microseconds. Both clocks are the same machine's.

**Cold and warm.** Cold is the first run after Docker Model Runner has unloaded the model,
forced with `docker model unload --all` and confirmed empty with `docker model ps` before the
run. Warm is every run after that. A hosted shared endpoint has no cold state and its rows say
`n/a`. A dedicated scale-to-zero deployment has a cold state measured in minutes; if that tier
is run, its cold run is recorded once.

**Runs.** Five answerable questions from the generation golden set, asked as `analyst_dnd`, two
warm passes each: ten warm runs per configuration, reported as mean and median with the ten
values kept. One cold run per configuration where cold exists. Fewer than five warm runs is
not a row.

## The runner

`scripts/bench_mattermost.py`, outside `src/` so the benchmark is not part of the product. It:

1. logs in to Mattermost as the demo user, opens the direct-message channel with the
   assistant, and posts one question;
2. polls the channel until the assistant's reply exists and has not changed for a settle
   interval, or a timeout passes (a timeout is recorded as a failed run, not dropped);
3. reads the audit file from the `mcp` container (`docker compose exec mcp cat
   /app/audit/audit.jsonl`, or a local path when given) and joins the run's `resolve` and
   `answer` events by user id within the run's time window;
4. writes one JSON file per configuration under `eval/results/`, named
   `bench-mattermost-<configuration>-<date>.json`, holding each run's question, hop timings,
   cold or warm flag, request id, and the configuration labels: plugin model and precision,
   sovrag generator model and precision, tier (`local`, `model-apis`, `dedicated`), billing
   mode (`none`, `per-token`, `per-gpu-minute`), engine and engine backend, hardware;
5. prints the table row for the docs.

The pure parts are functions with unit tests in `tests/test_bench_mattermost.py`: hop
arithmetic from the two timestamp formats, the reply-settled rule, the audit join, the labels,
the JSON shape, and the table rendering. The network parts run live and the PR records the
output.

Hardware label for local rows: MacBook Pro, Apple M3 Max (16 cores), 48 GB, macOS 26.6.2,
Docker 29.8.2, Docker Model Runner 1.2.6. Versions are re-read at run time and written into
the JSON rather than typed.

## Configurations

Retrieval, the entitlement predicate, identity binding and the audit log are local and
unchanged in every configuration. Embeddings stay on Docker Model Runner because the committed
eval fixture holds BGE-M3 vectors and the retrieval thresholds were set against them. The judge
stays Gemma 4 on Docker Model Runner so quality scores are comparable across rows.

| Configuration | Plugin model (hops 1, 3) | sovrag generator (hop 2) |
|---|---|---|
| A, baseline | Qwen 3.5 9B Q4_K_M, Model Runner | Qwen 3.5 9B Q4_K_M, Model Runner |
| B, plugin on Baseten | Model APIs model | Qwen 3.5 9B Q4_K_M, Model Runner |
| C, both on Baseten (if the evening allows) | Model APIs model | Model APIs model |
| D, dedicated (stretch) | dedicated deployment | as B or C, stated |

**Choosing the Model APIs model.** The catalog is listed with the account's key. The rule is
nearest to local first: if the catalog has a Qwen 3.5 instruct model that supports tool
calling, the smallest such model, so the rows isolate the backend as far as possible;
otherwise the smallest capable instruct model with tool calling, and the write-up says the
rows then compare backend and model together. Every row names the exact model id and the
precision the provider states.

**Switching without code changes.**

- Plugin model: `scripts/seed_mattermost.py` gains `--llm-api-key` and `--llm-service-name`,
  read from `AGENT_LLM_URL`, `AGENT_LLM_MODEL`, `AGENT_LLM_API_KEY`, defaulting to today's
  Model Runner values. Re-running the seed updates the plugin's service in place.
- sovrag generator: `compose.baseten.yaml`, an override used as
  `docker compose -f compose.yaml -f compose.baseten.yaml ...`, that removes the Compose
  `models:` binding for the generator on `app` and `mcp` and sets `LLM_BASE_URL`, `LLM_MODEL`,
  `LLM_API_KEY`, `ENGINE` and `ENGINE_BACKEND` from `.env`. Embedding binding unchanged.
- Labels: the eval report's existing `engine` and `engine_backend` fields carry provider and
  tier (`baseten-model-apis` / `managed`); no new report fields. Billing mode and hardware
  are columns in the docs table and fields in the benchmark JSON.
- Secrets: `LLM_API_KEY` and `AGENT_LLM_API_KEY` are blank in `.env.example`, never printed,
  never committed. The seed script and the runner never echo them.

**Compatibility check.** Before any measurement against Baseten: list the catalog, pick the
model by the rule, run one chat completion through `sovrag.generate.llm.Generator` with only
environment changed. Baseten's documentation shows bearer authentication with the OpenAI SDK,
which is what the client sends, so no code change is expected. If one is needed for the
authorization header or the model name format, it is minimal and tested.

## Order

1. Instrument and record the local baseline (configuration A): cold run, ten warm runs.
2. Compatibility check against Baseten Model APIs.
3. Configuration B: re-seed the plugin to Baseten, measure.
4. Configuration C if the evening allows: generator on Baseten, measure, and run the full
   generation eval and the leak suite in that configuration, so quality (faithfulness,
   correctness, refusals) sits beside latency with the Baseten labels.
5. Docs.
6. Configuration D, optional: one cold run, one warm run, a separate labelled row. If the
   deployment fights us, skip it, and the write-up characterises the tier from documentation
   and says so.

Time-box: if the Baseten endpoint fights us for more than an evening, stop, record what was
learned, and publish the local-only story first.

## Documentation

- `docs/serving-backends.md`: the benchmark table (configuration by hop timings, end-to-end
  wall time, eval quality where run, every row labelled with model, precision, tier, billing
  mode, backend, hardware); method and caveats, with every number marked measured, observed
  or estimated; the tradeoff section, including this paragraph in substance: moving inference
  to a hosted provider means the provider sees the question and the entitled chunks, which is
  exactly what the all-local configuration avoids; placement of inference becomes a
  data-classification decision while retrieval, entitlements and audit stay local; the two
  Baseten product shapes (always-warm shared, per-token, curated list; dedicated, any model,
  scale-to-zero, cold start in minutes, per GPU-minute); and the Baseten cost observed, with
  the account balance before and after.
- ADR 0017: inference placement is a configuration and data-classification decision; what
  stays local in every configuration; how rows are labelled.
- README: one paragraph in the serving section with a link. `CLAUDE.md`: the benchmark command.
- Prose rules: no em dashes; no "delve", "leverage", "seamless" or "journey"; claims map to
  measurements.

## Tests and guardrails

- Unit tests for the runner's pure functions, for the seed script's new arguments, and for the
  override file (parses, removes the generator binding, sets the expected variables).
- Existing CI is unchanged and must stay green. The leak suite is also run live in every
  configuration that changes the generator, and the result goes in the docs table.
- Nothing in `src/sovrag/retrieve/`, `src/sovrag/auth/`, the gate modules or `audit.py`
  changes.

## Out of scope

Embeddings or the judge on Baseten (fixture and comparability), streaming from sovrag (a tool
and API contract change), a logging proxy between the plugin and its model (the next instrument
if hops 1 and 3 need splitting into prefill and decode), any retrieval or entitlement change,
per-page citation URLs, and issues #29 and #30.
