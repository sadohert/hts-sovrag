# ADR 0008: Grounded answers with a refusal sentinel and mandatory citations

Status: accepted (2026-09-17)

## Context

The generator must answer only from retrieved chunks, cite every claim to a chunk, and refuse when the chunks do not contain the answer. The generator is a 9B-parameter quantised model served locally (ADR 0006), so the mechanism has to be robust to imperfect instruction following.

## Decision

- The prompt lists the retrieved chunks as numbered sources with title and heading path, and instructs the model to cite with `[n]` and to reply with the single token `INSUFFICIENT_CONTEXT` when the sources lack the answer. The code maps that sentinel to one fixed refusal sentence with no citations. A sentinel was chosen over structured JSON output because it survives a small model's formatting lapses and keeps the prompt short.
- Citation markers are validated by code: markers outside the provided range are removed, and a `Citation` object is built for each marker kept. The returned text never carries a marker without a citation.
- An answer with no in-range markers is retried once with a firmer instruction. If the retry still cites nothing, the answer is refused and the audit line records `uncited`. An uncited answer presented as grounded is the one output this system must not produce; withholding an occasional correct but uncited answer is the accepted cost.
- Temperature 0, a 600-token budget, and Qwen's thinking mode off on every call, so results are reproducible for the eval.
- `k` is capped at 10 so ten chunks of at most 340 tokens plus the instructions stay well inside the 8192-token context.
- Every call writes one audit line: principal, mode, k, model, retrieved and cited chunk ids, refused, reason, applied filter. The same writer serves the HTTP and MCP paths.

## Consequences

- Refusal quality depends on the model honouring the sentinel. The generation eval will measure refusal precision on questions the corpus cannot answer, and faithfulness on those it can, with a different judge model.
- The uncited-retry doubles latency on the rare uncited path. Acceptable at demo scale; the eval will report how often it triggers.
- Structured output remains an option if a later model supports it reliably; the parsing layer is the only thing that would change.
