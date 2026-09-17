# ADR 0012: The principal model has two axes, validates itself, and loads strictly

Status: accepted (2026-09-17)

## Context

`sovrag.auth` introduced `Principal` (an id, a clearance, a set of departments) and a YAML loader for named principals. A review of that code after it merged found that its guarantees held only on the happy path:

- A principal id repeated in the YAML file was accepted, and the last entry won. In a file that grants entitlement, one entry could quietly replace another with a higher clearance.
- The checks lived only in the loader. Code that builds a `Principal` directly, as the Mattermost resolver will (ADR 0010), skipped all of them: a plain string was accepted as a clearance, and `GC_WIDE` as a department.
- Malformed files raised `TypeError` or a `KeyError` indistinguishable from "unknown id", so one bad entry made every principal in the file look unknown.
- YAML turns an unquoted `123` or `no` into an integer or a boolean, giving a principal that its own string id could not find.
- The shipped `auditor` was described as seeing everything but lacked the `CSE` membership.

The model also rested on decisions that had not been written down.

## Decision

**Two axes, both synthetic (ADR 0003).** A principal has a clearance, one of `UNCLASSIFIED`, `PROTECTED_A`, `PROTECTED_B` in that order, and a set of department memberships. Rank comes from the order of the `Clearance` enum; `CLASSIFICATION_RANK` is derived from it and is read-only, so there is one source.

**`GC_WIDE` is a label on documents, never a membership.** Government-wide documents are visible by rule, not because someone belongs to "government-wide". `MEMBERSHIPS` is `DEPARTMENTS` without `GC_WIDE`, and it is the only vocabulary a principal's departments may use.

**The type validates itself.** `Principal` checks on construction that its id is a non-empty string, its clearance is a `Clearance`, and its departments are a frozenset of known memberships. Every source of principals, the YAML file and the Mattermost attribute resolver alike, gets the same guarantees, and an invalid principal cannot exist.

**`anonymous` is reserved.** The id `anonymous`, in any letter case, is accepted only in the least privileged shape: unclassified, no departments. `LEAST_PRIVILEGED` is that principal. It is what an authenticated caller with no recognised entitlement is bound to (ADR 0009), so nothing else may carry the name.

**The file loads strictly.** `load_principals` rejects duplicate keys at any level, a missing or non-mapping `principals` key, an entry that is not a mapping, a missing clearance, departments that are not a list of strings, and an id that is not a string. Every rejection is a `ValueError` that names the principal. A `KeyError` from `resolve_principal` therefore means exactly one thing: the id is not in the file.

**Errors, never downgrades.** A bad file is a configuration error and stops the program. Falling back to least privilege is reserved for the one case where it is the safe answer to a runtime condition, an authenticated caller without attributes, and that case is logged.

## Consequences

- A principal that exists is valid, so the entitlement filter can use `principal.clearance.rank` and `principal.departments` without defending against bad shapes.
- Tests and callers must build principals with `Clearance` members, not strings.
- The shipped `auditor` holds every membership, and a test asserts that equality so the vocabulary and the file cannot drift apart.
- `DEFAULT_PRINCIPALS_PATH` is still relative to the working directory, like the manifest path. It moves into settings when the CLI starts resolving `--principal`, with the entitlement filter. Until then the CLI accepts any string for `--principal` and does not resolve it.
