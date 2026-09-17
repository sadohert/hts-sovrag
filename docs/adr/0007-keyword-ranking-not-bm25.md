# ADR 0007: Keyword ranking uses Postgres ts_rank_cd, not BM25; hybrid fuses by rank

Status: accepted (2026-09-16)

## Context

Vector search handles paraphrase but is loose about exact tokens, and this corpus is full of exact tokens: acronyms, program names, fiscal years. A keyword signal is needed, and the schema already carries a generated `tsvector` column with a GIN index. The question is how to rank keyword matches and how to combine the two signals.

## Decision

Keyword mode parses the question with `websearch_to_tsquery('english', ...)`, matches with `@@`, and ranks with `ts_rank_cd`. The CLI mode is named `keyword`, not `bm25`, because `ts_rank_cd` is not BM25: it weights term frequency and proximity but has no corpus-wide inverse document frequency term and no BM25 length normalisation. Naming it BM25 would invite a fair correction.

Hybrid mode runs vector and keyword search with a candidate window of `max(4k, 20)` each and fuses by reciprocal rank fusion with k = 60. Fusion uses positions only, because cosine similarity and `ts_rank_cd` are not on comparable scales. Ties, which are common because a chunk in only one list scores the same as the chunk at the same rank in the other, resolve by chunk id ascending for determinism. That is ingestion order, not a quality signal; the retrieval eval will compare it against preferring the vector rank and against weighted fusion. It happens in Python over rows SQL already returned; it never filters.

## Consequences

- True BM25 in Postgres needs an extension (for example `pg_search` from ParadeDB), which means a different database image for the whole stack. That trade is deferred until the retrieval eval shows the keyword signal is the weak link.
- The retrieval eval will report hit@5, recall@10 and MRR for all three modes, so the value of fusion is measured rather than assumed.
- Keyword mode never loads the embedding model, which makes it the cheapest mode to run and a useful control in the eval.
- pgvector's HNSW index scan returns at most `hnsw.ef_search` rows (default 40, max 1000); `search_vector` raises `hnsw.ef_search` on the connection when k exceeds it. The entitlement branch must revisit this because a WHERE clause applied after the index scan shrinks results further (pgvector 0.8 iterative scans are the likely answer).
- `websearch_to_tsquery` joins terms with AND, so keyword mode is precise on short queries with exact tokens and often empty on sentence-length questions. On the first ten-question set it found the right document first for 4 of 10, vector for 9, hybrid for 8. The retrieval eval will decide whether to relax the query (OR semantics with ranking, or dropping low-value terms).
- The `tsv` column is generated from chunk text alone, while the embedding covers title, heading path and text. Keyword mode therefore cannot match on document title, department or fiscal year. Changing the generated column is a migration and is deferred; it is the first hypothesis for the retrieval eval to test.
