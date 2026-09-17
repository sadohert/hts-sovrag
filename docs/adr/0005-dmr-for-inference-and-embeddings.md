# ADR 0005: Docker Model Runner serves both the generator and the embedding model

Status: accepted (2026-09-15)

## Context

The app talks to inference through the OpenAI-compatible client only. The design called for Docker Model Runner (DMR) as the default backend and left open whether DMR could also serve BGE-M3 embeddings, with an in-process `sentence-transformers` fallback if not.

## Decision

Both models are served by DMR and declared in the Compose `models:` block:

| Role | Model | Quantisation | Source |
|---|---|---|---|
| generator | `ai/qwen2.5:7B-Q4_K_M` (superseded by ADR 0006) | Q4_K_M GGUF | Docker Hub `ai/` namespace |
| embeddings | `hf.co/gpustack/bge-m3-gguf:Q8_0` | Q8_0 GGUF | Hugging Face, pulled directly by DMR |

`ai/bge-m3` does not exist on Docker Hub, so the embedding model is pulled from Hugging Face by repository reference. DMR's `/engines/v1/embeddings` endpoint returns 1024-dimensional vectors for this model, which matches the `vector(1024)` column in the schema.

Verified on Docker Desktop 29.7.2, DMR CLI v1.2.6, llama.cpp backend b9879-metal, Apple M3 Max. Two-string embedding call: about 9 s cold (model load), sub-second warm. Chat completion, 34 output tokens: about 3.8 s cold.

## Consequences

- DMR's llama.cpp backend embeds at most 512 tokens per input. `--batch-size` is accepted as a runtime flag but `--ubatch-size` is not, and llama.cpp clamps the batch to the physical batch for embedding models, so the ceiling is fixed at 512 on this version (verified against the runner log: "setting n_batch = n_ubatch = 512"). BGE-M3's tokenizer runs about 11% heavier than cl100k on this corpus. The ingest pipeline therefore budgets 400 cl100k tokens for title + heading path + chunk text, with chunks capped at 340 and long heading paths trimmed from the left.

- Every model in the stack is an OCI artifact managed by Docker, so the air-gap procedure is uniform: `docker model package` and push to a private registry, alongside `docker save` for the app images.
- Embedding quality of the Q8_0 GGUF has not been compared against the fp16 `sentence-transformers` model. If the retrieval eval is weak, that comparison is the first thing to run; the `local-embed` extra in `pyproject.toml` keeps the fallback one config change away.
- DMR is a llama.cpp-backed local runner. It is the development and demo backend, not the production serving story; see the vLLM profile and `docs/production-gap.md`.
