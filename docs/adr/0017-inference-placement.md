# ADR 0017: Inference placement, Docker Model Runner on the box or Baseten hosted, is a configuration and data-classification decision

Status: accepted (2026-10-13)

## Context

Every model call goes through two OpenAI-compatible clients whose base URL, model name and API
key come from the environment (ADR 0005). The Mattermost Agents plugin calls its own model the
same way, from one service entry the seed script writes. A reply in Mattermost was observed at
around 40 s on a laptop while the direct path measured 4.1 s, and the difference was attributed
to the plugin's loop on a laptop-sized model without ever being timed.

## Decision

- Where inference runs is configuration, and the benchmark proves it: the plugin's model moves
  with four `AGENT_LLM_*` variables and a re-seed; sovrag's generator moves with
  `compose.baseten.yaml` and three `BASETEN_*` variables. No code path changes.
- Retrieval, the entitlement predicate, identity binding and the audit log are local in every
  configuration. Embeddings stay on Docker Model Runner because the committed eval fixture holds
  BGE-M3 vectors and the thresholds were set against them. The judge stays local so quality
  scores are comparable across rows.
- Moving inference to a hosted provider means the provider sees the question and the entitled
  chunks, which is exactly what the all-local configuration avoids. Placement of inference is
  therefore a data-classification decision: the question and the chunks a principal may see go
  wherever the generator runs, while what a principal may see is decided at home.
- Every measurement is labelled with the plugin model and precision, the generator model and
  precision, the tier (`local`, `model-apis`, `dedicated`), the billing mode (`none`,
  `per-token`, `per-gpu-minute`), the engine, the engine backend and the hardware, read at run
  time. Hops are measured from timestamps that already exist: the person's post, the `resolve`
  and `answer` audit events, and the reply's last edit. Nothing is added to the measured path.
- Baseten has two product shapes and the write-up covers both: Model APIs (shared, always
  warm, per-token, a curated list) and dedicated deployments (any model, scale-to-zero, a cold
  start measured in minutes, per GPU-minute). Model APIs is the measured backend for the rows
  that move sovrag's generator; the dedicated tier was measured with the plugin's model only,
  once warm and once as a scale-to-zero wake, and then deleted.

## Consequences

- `docs/serving-backends.md` holds the table and the method; every number there is marked
  measured, observed or estimated.
- The benchmark runner lives in `scripts/`, outside the product, and reads the audit file; a
  change to the audit line's `ts`, `event`, `user_id` or `request_id` fields is a benchmark
  change.
- Keys are read from `.env`. The plugin's key is written into the plugin's settings inside
  Mattermost, where the plugin needs it to call the endpoint; sovrag's own key reaches only the
  container environment. Neither is printed, committed, or written into a results file.
- A hosted generator sees public documents with synthetic labels (ADR 0003). The sovereignty
  paragraph describes what would happen with real markings, not what happened here.
