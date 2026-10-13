# Serving backends: where the time goes, and what moving inference costs

Every model call in sovrag goes through an OpenAI-compatible client configured from the
environment, so the serving backend is a configuration choice. This page records what that
choice costs in latency and in sovereignty, from measurements, with every number marked
measured, observed or estimated. Method and labels: ADR 0017.

## What is measured

A question asked in Mattermost passes through three hops:

1. **The agent decides.** The Agents plugin's model reads the conversation and decides to call
   the `answer` tool. From the person's post to the `resolve` audit event.
2. **sovrag answers.** Embed, hybrid retrieval under the entitlement filter, generate. From the
   `resolve` event to the `answer` event.
3. **The agent writes.** The plugin's model writes the reply from the tool result, token by
   token. From the `answer` event to the reply's last edit.

The runner (`scripts/bench_mattermost.py`) posts a question as `analyst_dnd`, waits for the
reply to settle, and joins the Mattermost timestamps with the audit events by user id and
request id. Hop 1 includes Mattermost delivering the post to the plugin and hop 3 the final
edit; both are milliseconds. Cold is the first run after `docker model unload --all`. Warm rows
are ten runs: five questions from the retrieval golden set (`r01` to `r04`, `r09`), two passes.
Fewer than five warm runs is not a row.

```
uv run python scripts/bench_mattermost.py --configuration A-local --cold
uv run python scripts/bench_mattermost.py --configuration A-local --warm --passes 2
```

## Results

Hardware for local rows is read at run time and recorded in each results file under
`eval/results/bench-mattermost-*.json`. Values are warm medians in seconds; `cold` is the
cold run's end-to-end time; `n` is warm runs.

| Configuration | Plugin model | sovrag generator | Tier | Billing | Hop 1 | Hop 2 | Hop 3 | End to end | Cold | n |
|---|---|---|---|---|---|---|---|---|---|---|
| A-local | ai/qwen3.5:9b-q4_K_M (Q4_K_M) | ai/qwen3.5:9b-q4_K_M (Q4_K_M) | local | none | 5.5 | 4.6 | 6.3 | 16.8 | 40.2 | 13 |
| B-plugin-baseten | zai-org/GLM-5.3-Flash (fp8) | ai/qwen3.5:9b-q4_K_M (Q4_K_M) | model-apis | per-token | 1.0 | 4.2 | 2.0 | 8.3 | 17.7 | 10 |
| C-both-baseten | zai-org/GLM-5.3-Flash (fp8) | zai-org/GLM-5.3-Flash (fp8) | model-apis | per-token | 0.8 | 0.8 | 2.1 | 3.8 | n/a | 10 |

Notes on A-local (measured, 2026-10-13, file `eval/results/bench-mattermost-A-local-2026-10-13.json`):

- Hardware: Apple M3 Max, 48 GB, macOS 26.6.2, Docker 29.8.2, Model Runner v1.2.8, read at run time.
- Cold: hop 1 9.5 s, hop 2 6.7 s, hop 3 23.9 s. The plugin and sovrag use the same Qwen model, so the plugin's first call loads it for both; the cold penalty lands in hop 1 and in the embedder's load inside hop 2.
- Warm mean end to end is 20.3 s against the 16.8 s median: hop 3 is proportional to reply length, and two of the five questions (`r04`, `r09`) draw short replies or refusals.
- 2 of 15 warm runs are counted as failed: on both, for the same question (`r03`), the agent called `search` instead of `answer`, so there is no `answer` event to time. The agent's model straying from its instruction is part of what hop 1 measures and is reported, not dropped.
- No first-token mark: the plugin creates its reply post in the same second as the question, before the tool call, and fills it at the end.

Notes on B-plugin-baseten (measured, 2026-10-13, file `eval/results/bench-mattermost-B-plugin-baseten-2026-10-13.json`):

- Hops 1 and 3 are hosted (Baseten Model APIs, GLM-5.3-Flash, fp8, per token); hop 2 is the same local sovrag as in A. Same hardware for the local part.
- Cold here is sovrag's own reload: hop 1 3.4 s, hop 2 10.8 s (the embedder and the generator both load inside sovrag's call, since the plugin no longer loads Qwen first), hop 3 3.6 s.
- Warm mean end to end 8.9 s against the 8.3 s median. 0 failed runs: the hosted model called `answer` on every run.
- Hop 2 is 4.2 s here against 4.6 s in A: the same pipeline on the same machine, within noise.
- No Qwen 3.5 is offered on Model APIs, so this row compares backend and model together; the catalog's two cheapest tool-calling models were probed and the one that does not emit reasoning tokens by default was chosen (the other produced four times the completion tokens for the same tool call).

Notes on C-both-baseten (measured, 2026-10-13, file `eval/results/bench-mattermost-C-both-baseten-2026-10-13.json`):

- Hops 1 and 3 and the generation inside hop 2 are hosted; embedding, retrieval, the entitlement filter and the audit line inside hop 2 are still local, via `compose.baseten.yaml`.
- No cold row: Model APIs has no cold state, and the only local model left in the path is the embedder, which Model Runner keeps loaded for the run.
- Warm mean end to end 5.0 s against the 3.8 s median. 0 failed runs.
- Hop 2 fell from 4.2 s (B) to 0.8 s: the difference is the generation step moving off the laptop. Embedding and retrieval are what remains.
- In every configuration the same question (`r09`) came back as a refusal from sovrag on every run, from both generators. That is a retrieval or golden-set question for the eval harness, not a serving-backend effect, and the runs are counted because the hops are real.

Quality, where the generator changed (the generation eval, judged locally by Gemma 4 12B
Q4_K_M, and the leak suite's answer run):

| Configuration | Faithful | Correct | False refusals | Unanswerable refused | Leak answers refused |
|---|---|---|---|---|---|
| A-local (2026-09-22 baseline) | 1.000 (29 judged) | 0.966 | 1/30 | 9/10 | public 15/20, analyst_dnd 12/20, analyst_ps 13/20 |

## Configurations

| Configuration | Plugin model (hops 1 and 3) | sovrag generator (hop 2) | How |
|---|---|---|---|
| A-local | Qwen 3.5 9B Q4_K_M on Docker Model Runner | the same | `docker compose --profile mattermost --profile mcp up -d`, seed with defaults |
| B-plugin-baseten | a Baseten Model APIs model | Qwen 3.5 9B Q4_K_M on Docker Model Runner | set `AGENT_LLM_*`, re-seed |
| C-both-baseten | a Baseten Model APIs model | the same Baseten model | B, then `docker compose -f compose.yaml -f compose.baseten.yaml ...` |
| D-dedicated | a dedicated Baseten deployment, scale-to-zero | as stated in the row | optional |

Retrieval, the entitlement predicate, identity binding, the audit log, embeddings and the
judge are local in every configuration.

## The serving-backend tradeoff

Moving inference to a hosted provider means the provider sees the question and the entitled
chunks, which is exactly what the all-local configuration avoids. Placement of inference
becomes a data-classification decision: retrieval, entitlements and audit stay local, and the
question plus whatever the principal may see travels to wherever the generator runs. For public
documents that is a latency decision. For documents with real markings it is the decision.

Two product shapes on the hosted side:

- **Shared, always warm** (Baseten Model APIs): a curated list of open-weights models behind one
  OpenAI-compatible URL, billed per million tokens, no deployment to manage and no cold start.
- **Dedicated** (a Baseten deployment from the model library or your own weights): any model,
  your hardware choice, scale-to-zero when idle, a cold start measured in minutes while the
  container pulls, loads weights and initialises the engine, billed per GPU-minute while a
  replica is up.

The all-local shape pays in a different currency: a laptop-sized model at a laptop's decode
speed, and a reload whenever the model has been idle long enough to be unloaded.

## Cost observed

(filled in by the measurement tasks: account balance before and after, tokens per run)

## What is not measured

Prefill and decode inside hops 1 and 3 are not separated; a logging proxy between the plugin
and its model would be the next instrument. Hop 2 is not split into embed, retrieve and
generate here; the generation eval's latency figure covers the direct path. The dedicated tier,
where it is not run, is characterised from Baseten's documentation and marked as such.
