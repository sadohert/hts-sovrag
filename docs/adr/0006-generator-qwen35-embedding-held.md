# ADR 0006: Generator upgraded to Qwen 3.5 9B; embedding model held at BGE-M3

Status: accepted (2026-09-16)

## Context

The generator pulled at scaffold time, `ai/qwen2.5:7B-Q4_K_M`, was 18 months old. Docker Hub's `ai/` namespace now carries the Qwen 3.5, 3.6 and 3.8 lines, plus `ai/qwen3-embedding` and `ai/qwen3-reranker`. The project rule is to run the newest supported release that works on a 48 GB laptop and on a reviewer's machine.

## Decision

Generator: `ai/qwen3.5:9b-q4_K_M`. Options considered:

| Model | Size on disk | Notes |
|---|---|---|
| `ai/qwen3.5:9b-q4_K_M` | about 6 GB | Newest line with a small dense model. Fast on Apple silicon, fits a 16 GB reviewer machine. Chosen. |
| `ai/qwen3.6:35b-a3b-q4_K_M` | about 20 GB | Mixture of experts, 3B active per token, so fast and stronger. Too large for a 32 GB reviewer machine once Postgres and the embedder are loaded. |
| `ai/qwen3.8:27b-q4_K_M` | about 16 GB | Dense; strongest reasoning; noticeably slower generation. |

Embeddings: keep `hf.co/gpustack/bge-m3-gguf:Q8_0` for now. The current index was checked by hand against a ten-question set and hit the right document first every time. Swapping the embedding model before the retrieval eval suite exists would discard the only quality signal available. `ai/qwen3-embedding:0.6b-q8_0` is the candidate; it also emits 1024-dimensional vectors, so the schema would not change.

Reranker: when reranking is added, prefer `ai/qwen3-reranker` served by Model Runner over an in-process `sentence-transformers` cross-encoder, so the reranker is managed like the other models.

## Consequences

- Prompts and refusal behaviour will be tuned against Qwen 3.5, not 2.5.
- Qwen 3.x models think before answering by default. Verified on Model Runner: with `max_tokens` 120 the model spent every token on reasoning and returned empty content. Passing `chat_template_kwargs: {"enable_thinking": false}` in the chat completion request turns it off (45 tokens, direct answer). The generation layer must send that flag, and the eval can later compare thinking on and off.
- Developers must pull the new model once: `docker model pull ai/qwen3.5:9b-q4_K_M`. Compose pulls it automatically.
- The embedding swap is a follow-up gated on the eval suite, and will be its own ADR with before and after numbers.
