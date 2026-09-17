# ADR 0010: Mattermost user attributes are the source of entitlement for Mattermost users

Status: accepted (2026-09-17)

## Context

ADR 0009 lets the MCP server learn which Mattermost user is asking. It still has to learn what that user may see. sovrag's entitlement has two axes, a clearance and a set of departments, and the answer must come from somewhere a user cannot edit, or the attribute becomes the same escalation channel as identity in a tool argument.

Mattermost has user attributes (custom profile attributes). They are the store that Mattermost's own attribute-based access control evaluates. Verified against Mattermost server 11.11 source and documentation:

- Attributes are admin-managed by default from v11. A field's definition carries `attrs.managed == "admin"`, and only an administrator, or an LDAP or SAML sync, can set its values.
- An administrator can make a field user-editable. Mattermost's own access control ignores such fields unless `AccessControlSettings.EnableUserManagedAttributes` is true; it defaults to false and has no System Console switch.
- A field can be synced from LDAP or SAML, after which only the identity provider or an administrator changes it.
- Values are readable over REST by a bot token that can see the user: `GET /api/v4/custom_profile_attributes/fields` for definitions and `GET /api/v4/users/{id}/custom_profile_attributes` for a user's values.
- The feature needs an Enterprise-tier licence or Mattermost Entry (ADR 0011).

## Decision

Two attributes carry the entitlement, using the same vocabulary as `documents`:

| Attribute | Type | Maps to |
|---|---|---|
| `sovrag_clearance` | single select: `UNCLASSIFIED`, `PROTECTED_A`, `PROTECTED_B` | `Principal.clearance` |
| `sovrag_departments` | multi-select: `DND`, `CSE`, `PS`, `PSPC`, `SSC`, `TBS` | `Principal.departments` |

`sovrag.auth.mattermost` resolves a user ID to a `Principal` with these rules, in order:

1. Only a field whose definition is admin-managed is read. A value from any other field is ignored. The definition is checked on every request, so a later change in the System Console can only reduce access.
2. A value outside the vocabulary is ignored. `GC_WIDE` is a document label and is not offered as a department.
3. No attributes, no recognised values, an HTTP error, or a timeout yields `LEAST_PRIVILEGED`, with the reason recorded.

There is no cache. One attribute lookup per question is cheap beside an embedding call and a chat completion, and it means an administrator's change takes effect on the next question. The attribute snapshot (field name and value) is written to the audit log with every request, so an answer can be traced to the entitlement that produced it.

The seed script creates both fields as admin-managed. In a real deployment they would be synced from the identity provider; the resolver does not care which.

## Alternatives considered

- **A mapping file in sovrag keyed by Mattermost user ID.** Trustworthy, but it duplicates the directory and hides entitlement from the administrators who manage users.
- **A Mattermost plugin that registers a policy and asks the server to evaluate it.** Mattermost 11.10 added plugin APIs for this. It needs a Go plugin and ties the decision to Mattermost's policy engine, while sovrag's predicate must run in SQL regardless. Not needed for two attributes.
- **Mattermost channel membership as the entitlement.** Membership under an access policy is re-synced on a schedule (hourly by default), so it can lag an attribute change, and it says nothing in a direct message.

## Consequences

- Entitlement for Mattermost users is managed where users are managed, and sovrag's labels stay synthetic (ADR 0003): these attributes describe demo users against a public corpus, not real clearances.
- The bot token is a credential that can read user attributes. It is configured in the MCP service's environment and never logged.
- Residual risk: the plugin's model relays the tool result to the user and could soften or embellish a refusal. The bot's instructions tell it to relay the answer and citations verbatim, and the audit log holds the tool's exact output. The retrieval guarantee does not depend on the model: a chunk the principal may not see never leaves the database.
- If Mattermost changes the attribute API or the meaning of `attrs.managed`, the resolver's recorded-response tests fail closed: an unrecognised definition is treated as not admin-managed.
