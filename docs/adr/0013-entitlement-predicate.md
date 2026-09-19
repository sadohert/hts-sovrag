# ADR 0013: The entitlement predicate is one bound WHERE clause inside every retrieval query

Status: accepted (2026-09-19)

## Context

The README promises a two-axis entitlement model, classification level and department,
enforced as a SQL pre-filter and never as a Python post-filter, with a leak-test suite
proving that a principal can never retrieve a chunk they are not entitled to. ADR 0012 gave
the principal its two axes. This decision fixes the rule that maps a principal to a set of
visible documents, and where that rule runs.

Two facts about the store shape the decision. Classification and department live on
`documents`, and every retrieval query already joins `chunks` to `documents` for the
citation fields, so the predicate has its columns at hand. And vector search is an HNSW index
scan: pgvector applies a `WHERE` clause after the scan, so a principal who may see few
documents can receive fewer than `k` rows unless the scan is told to continue.

## Decision

**The visibility rule.** A document is visible to a principal when its classification is at
or below the principal's clearance, and one of three things holds: the document is
`UNCLASSIFIED`, or its department is `GC_WIDE`, or its department is one of the principal's
memberships. In words: unclassified material is open to everyone, and protected material is
need-to-know by department, with government-wide material open to anyone cleared for it.

**One predicate, two bound arrays.** The rule is a single SQL fragment, fixed at import
time:

```sql
d.classification = any(%(cls)s)
and (d.classification = 'UNCLASSIFIED' or d.department = 'GC_WIDE' or d.department = any(%(depts)s))
```

`cls` is the list of labels at or below the clearance and `depts` the sorted memberships.
Both are bound parameters. No principal value is ever interpolated into SQL, so the fragment
is the same string for every principal and a reviewer can read it once.

**Applied inside every query.** `search_vector`, `search_keyword` and `search_hybrid` each
require a `principal` keyword and put the predicate in their `WHERE` clause. Hybrid
retrieval passes the principal to both arms and fuses only what they return; fusion never
filters. There is no retrieval path without a principal, and the type system enforces it: a
call without one is a `TypeError`.

**The HNSW scan continues past the filter.** `search_vector` runs inside a transaction and
sets `hnsw.iterative_scan = relaxed_order` and `hnsw.ef_search` with `SET LOCAL`, so the
settings end with the query and never leak to the connection. With iterative scanning,
pgvector keeps scanning the index until `k` rows pass the `WHERE` clause or
`hnsw.max_scan_tuples` (default 20,000, more than the corpus) is reached. Relaxed order can
return the `k` rows slightly out of distance order, so the query sorts them again by score
in an outer `SELECT`; that is `k` rows, not the index.

**The rendered predicate travels with the answer.** `Entitlement.render()` substitutes the
values for a human, for example `classification in (UNCLASSIFIED, PROTECTED_A) and
(classification = UNCLASSIFIED or department in (GC_WIDE, DND))`. `answer()` reports it as
`Answer.applied_filter` and writes it to the audit line, and `sovrag query` prints it, so
every result says which rule produced it.

## Consequences

- Against the indexed corpus, `analyst_dnd` sees 16 of 27 documents and is forbidden 11: the
  other departments' Departmental Results Reports and the three Auditor General reports.
- The leak tests seed one document per classification-and-department cell and assert the
  exact visible set for each shipped principal, and that a forbidden document is zero rows
  even when it is the nearest neighbour or the only keyword match.
- `sovrag query --principal` now resolves the id and rejects an unknown one as a usage error,
  the same as `sovrag ask`. The path to the principals file moves into settings
  (`PRINCIPALS_PATH`), as ADR 0012 anticipated.
- Callers that stub `search` in tests must accept the `principal` keyword.
- `hnsw.ef_search` is now set on every vector query, not only when `k` exceeds the default,
  because `SET LOCAL` costs nothing and one code path is easier to verify than two.
