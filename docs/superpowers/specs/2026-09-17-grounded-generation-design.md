# Grounded generation with citations and refusal

Date: 2026-09-17. Status: approved.

## Goal

`sovrag ask` answers a question from retrieved chunks only, marks every claim with a `[n]`
citation tied to a chunk, and refuses when the retrieved context does not contain the answer.
The Python entry point is the contract both this project and its consumers build against.

## Contract

Module `sovrag.generate.answer`:

```python
@dataclass(frozen=True)
class Citation:
    n: int  # the [n] marker used in the text, 1-based
    doc_id: str
    chunk_id: int
    title: str
    heading_path: str
    source_url: str


@dataclass(frozen=True)
class Answer:
    question: str
    principal: str  # principal.id
    mode: str  # "vector" | "keyword" | "hybrid"
    request_id: str | None
    text: str  # prose with [n] markers, or REFUSAL_TEXT
    citations: tuple[Citation, ...]  # only chunks the text cites; empty when refused
    refused: bool
    model: str  # generator id from settings
    retrieved_chunk_ids: tuple[int, ...]  # everything retrieval returned
    applied_filter: str | None  # None until entitlement filtering lands


def answer(
    question: str,
    *,
    principal: Principal,
    mode: Mode = Mode.hybrid,
    k: int = 5,
    request_id: str | None = None,
) -> Answer: ...
```

`Principal` comes from `sovrag.auth` (id, clearance, departments). Only `principal.id` is used on
this branch. `Mode` moves from `sovrag.cli` to `sovrag.retrieve.hybrid` and is re-exported from
`sovrag.retrieve`; the CLI imports it from there.

Guarantees: every `[n]` in `text` has a `Citation` with that `n`; markers outside 1..k are
removed; `refused` implies `citations == ()` and `text == REFUSAL_TEXT`; one chat completion call
per answer, two on the uncited-retry path; one audit line per call.

## Components

- `src/sovrag/generate/llm.py`: `Generator(base_url, model, api_key, http_client=None)` with
  `from_settings(settings)` and `complete(messages, *, max_tokens=600) -> str`. Temperature 0.
  Sends `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` so Qwen 3.x answers
  directly (ADR 0006). Raises `GenerationError` on transport failure, non-2xx, or an empty
  reply. Nothing else escapes.
- `src/sovrag/generate/prompts.py`: pure. `SENTINEL = "INSUFFICIENT_CONTEXT"`,
  `REFUSAL_TEXT = "The retrieved documents do not contain enough information to answer this question."`,
  `build_messages(question, hits, *, firm=False) -> list[dict[str, str]]`. The system message
  states: answer only from the numbered sources; cite every claim with `[n]`; if the sources do
  not contain the answer, reply with the sentinel alone. The user message lists sources as
  `[n] <title> > <heading_path>` followed by the chunk text, then the question. `firm=True` adds
  one sentence: the previous reply had no citations; every sentence must end with at least one.
- `src/sovrag/generate/citations.py`: pure. `is_refusal(text) -> bool` (sentinel present,
  whitespace-insensitive). `parse(text, k) -> tuple[str, tuple[int, ...]]` returns the text with
  out-of-range markers removed and the sorted, de-duplicated in-range indices. Markers are
  `[n]` with n a positive integer; `[1, 2]` and `[1][2]` both count.
- `src/sovrag/generate/answer.py`: orchestration. `MAX_K = 10`. Steps: clamp k; retrieve with the
  mode (embedding only when needed, as the CLI does today); if no hits, refuse without calling the
  model; build messages; complete; if refusal, refuse; parse; if no in-range markers, rebuild with
  `firm=True`, complete again, parse again; if still none, refuse with audit reason `uncited`;
  otherwise build citations from cited hits; write the audit line; return.
- `src/sovrag/audit.py`: `write_event(event: str, **fields) -> None` appends one JSON line with
  `ts` (UTC ISO 8601) and `event` to `Settings.audit_log_path`, creating the parent directory.
- `src/sovrag/cli.py`: `ask` command with `question`, `--principal` (resolved through
  `sovrag.auth.resolve_principal`; unknown id is a usage error), `--mode`, `--k` (1..10),
  `--json`. Text output: the answer, a blank line, then `Sources:` with one line per citation
  `[n] title, heading_path, source_url`; on refusal the refusal text only. JSON output: the
  `Answer` as a dict. `GenerationError` prints its message to stderr and exits 1.

## Audit line

`write_event("answer", request_id=..., principal=..., mode=..., k=..., model=...,
retrieved_chunk_ids=[...], cited_chunk_ids=[...], refused=..., reason=..., applied_filter=...)`
where `reason` is `None`, `"no_hits"`, `"sentinel"` or `"uncited"`.

## Edge cases

- No hits: refuse, reason `no_hits`, no model call.
- Sentinel plus prose: treated as refusal.
- Markers only out of range: same as no markers (retry, then refuse).
- k above `MAX_K`: clamped, so the prompt fits the 8192-token context (10 chunks of 340 tokens
  plus the system text and question stay under 4,500 tokens).
- Model unreachable: `GenerationError` with the transport message; CLI exit 1.

## Tests

- `tests/test_prompts.py`: sources numbered 1..k in hit order with title and heading; sentinel
  instruction present; `firm=True` adds the extra sentence and nothing else changes.
- `tests/test_citations.py`: parse finds `[1]`, `[1][3]`, `[1, 3]`; drops `[7]` when k = 5 and
  removes it from the text; de-duplicates; `is_refusal` true for the bare sentinel and for
  sentinel with surrounding whitespace or prose, false otherwise.
- `tests/test_llm.py`: `Generator` with a mocked HTTP transport returns the content, sends
  temperature 0 and the thinking flag, raises `GenerationError` on 500 and on empty content.
- `tests/test_answer.py`: with a fake generator and fake search functions (monkeypatched):
  cited path builds citations only for cited hits and preserves order by n; sentinel path
  refuses; uncited path retries once with `firm=True` then succeeds; uncited twice refuses with
  reason `uncited`; no hits refuses without calling the generator; k is clamped; every path
  writes exactly one audit line with the documented fields.
- `tests/test_audit.py`: `write_event` appends valid JSON lines with `ts` and `event`, creates the
  directory.
- `tests/test_cli.py`: `ask` text output shows Sources; `--json` round-trips; unknown principal
  is a usage error; `GenerationError` exits 1 with the message.
- Manual, recorded in the PR: the ten eyeball questions plus three the corpus cannot answer, with
  the real model; expected ten grounded answers with correct top citation and three refusals.

## Documentation

- ADR 0008: grounding and refusal: sentinel over structured output, temperature 0, the
  uncited-answer rule (retry once, then refuse), thinking mode off.
- `docs/architecture.md`: the query path now ends in `generate/answer.py`; status updated.
- README status line: answers with citations and refusal from the CLI; no entitlement filtering
  yet. CLAUDE.md Commands: add `uv run sovrag ask "..." --principal analyst_dnd`.

## Dependency on the principal package

`sovrag.auth` (Principal, Clearance, load_principals, resolve_principal, data/principals.yaml)
is being delivered by a separate branch, `principal-model`. If it is on main when implementation
starts, this branch imports it. If not, the first task creates exactly that module and the
principals file from the agreed note, and the other branch rebases.
