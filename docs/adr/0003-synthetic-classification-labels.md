# ADR 0003: Classification labels are synthetic and assigned per document from doc_type

Status: accepted (2026-09-15)

## Context

Every document in the corpus is public, published under the Open Government Licence - Canada. The entitlement model needs a classification axis to enforce, and the Government of Canada vocabulary (`UNCLASSIFIED`, `PROTECTED_A`, `PROTECTED_B`) is the one a defence or public-sector buyer recognises.

## Decision

Classification is a deterministic function of `doc_type`, applied to the whole document at ingest time:

| doc_type | classification | rationale for the tier |
|---|---|---|
| DP (Departmental Plan) | UNCLASSIFIED | forward-looking, widely circulated |
| POLICY (TBS instruments) | UNCLASSIFIED | org-wide rules everyone must be able to read |
| DRR (Departmental Results Report) | PROTECTED_A | gives the model a middle tier per department |
| OAG (Auditor General report) | PROTECTED_B | audit findings are the most sensitive tier in the story |

An earlier design tagged only the findings sections of OAG reports as PROTECTED_B. Classification lives on the `documents` row, not on chunks, so the whole OAG document takes the tag. Section-level classification would need a per-chunk column and a rule for mixed documents, and nothing in the evaluation design requires it.

The rule is implemented in `src/sovrag/corpus/manifest.py` and covered by `tests/test_manifest.py`. The manifest file carries no classification field on purpose; the label is derived, never hand-assigned.

## Consequences

- The README must state that no protected information was used, handled, or stored. Any copy that implies otherwise is a defect.
- OAG content on canada.ca is the summary page only; the full reports are PDF. The PROTECTED_B tier is therefore thin (about 1,200 to 2,700 words per report). If leak tests need more PROTECTED_B material, PDF text extraction of the full reports is the next step and is not OCR.
- Changing the rule changes every eval result. Record the rule version with eval outputs.
