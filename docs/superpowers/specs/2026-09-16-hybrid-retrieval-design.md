# Hybrid retrieval: keyword and fused modes

Date: 2026-09-16. Status: approved.

## Goal

`sovrag query` gains `--mode vector|keyword|hybrid`. All three modes return the same `Hit`
shape with citations, so nothing downstream depends on which mode produced a result. This
is the retrieval the generation layer and the retrieval eval will build on.

## Components

All in `src/sovrag/retrieve/`.

- `hybrid.py` keeps `Hit` and `search_vector` and gains:
  - `search_keyword(conn, query, *, k) -> list[Hit]`. One SQL statement: the question is
    parsed with `websearch_to_tsquery('english', ...)`, matched against `chunks.tsv` with
    `@@`, and scored with `ts_rank_cd(c.tsv, query)`. Ordered by score descending, limited
    to k.
  - `search_hybrid(conn, query, query_embedding, *, k) -> list[Hit]`. Runs
    `search_vector` and `search_keyword` with a candidate window of `max(4 * k, 20)`, fuses
    the two rankings with `rrf`, and returns the top k. The reported `score` is the fusion
    score.
- `fusion.py`: `rrf(rankings: Sequence[Sequence[int]], *, k: int = 60) -> list[tuple[int, float]]`.
  Pure function over chunk ids. Each list contributes `1 / (k + rank)` per chunk, ranks
  starting at 1. Output is sorted by score descending, then by chunk id ascending so ties
  are deterministic.

`cli.py` maps the mode to the search function through a `Mode` enum. The embedder is
constructed and called only for `vector` and `hybrid`, so `keyword` never loads the
embedding model.

## Data flow

Keyword: question -> `websearch_to_tsquery` -> `tsv @@ query` -> `ts_rank_cd` -> top k.

Hybrid: question -> vector list (top 4k) and keyword list (top 4k) -> `rrf` over the two
lists of chunk ids -> top k ids -> `Hit` objects assembled from the candidate lists, score
replaced by the fusion score.

## Edge cases

- A question of filler words only yields an empty tsquery, which matches nothing. Keyword
  mode returns an empty list. Hybrid degrades to the vector ranking alone; `rrf` over one
  non-empty list and one empty list is well defined.
- An invalid mode string is rejected by the CLI enum before any work runs.
- No entitlement filtering yet. The principal is accepted and echoed for the next branch.

## Tests

- `tests/test_fusion.py` (no database): a chunk in both lists outranks a chunk in one
  list; ties break by chunk id; an empty list contributes nothing; `k` shifts scores but
  not order for a single list.
- `tests/test_keyword_search.py` (database fixture): an exact acronym present in one chunk
  is returned first by keyword search; a filler-only query returns no rows; results carry
  citations (doc_id, chunk_id, heading_path, source_url).
- `tests/test_hybrid_search.py` (database fixture): a chunk that is close in vector space
  and matches the keyword outranks chunks that satisfy only one; keyword-empty hybrid equals
  the vector ranking.
- `tests/test_cli.py`: keyword mode does not construct the embedder; hybrid and vector do;
  an unknown mode exits non-zero.
- Manual: the ten-question eyeball set run in all three modes, table in the PR.

## Documentation

- ADR 0007: keyword ranking is Postgres `ts_rank_cd`, not BM25; what true BM25 would
  require (an extension such as pg_search, hence a different database image) and why that
  is deferred.
- `docs/architecture.md`: retrieval modes and the fusion step.
- README status line: three retrieval modes, still no generation or entitlement filtering.
