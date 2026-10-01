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

1. Only a field whose definition is admin-managed is read. A value from any other field is ignored. The definition is fetched with every resolution, so a field that stops being admin-managed stops counting at once. This guards the field's definition only. An administrator who sets a user's value grants that access, which is the purpose of the feature, and an administrator's mistake in a value can raise access as easily as lower it.
2. Select values arrive as option IDs. They are mapped to option names through the definition fetched in the same resolution, and a name outside the vocabulary is ignored. `GC_WIDE` is a document label and is not offered as a department.
3. A user with no attributes, or none that are recognised, is bound to `LEAST_PRIVILEGED`, with the reason recorded.
4. If either REST call fails, times out, or reports that Mattermost does not know the user, resolution fails. The tool call returns an error and nothing is retrieved (ADR 0009). A failed lookup says nothing about the user's entitlement, so it is never treated as least privilege.

There is no cache. Each resolution makes two REST calls, the field definitions and the user's values, once per tool call and so once per question. That is cheap beside an embedding call and a chat completion, and it means an administrator's change takes effect on the next question. The attribute snapshot (field name and value) is written to the audit log with every tool call, so an answer can be traced to the entitlement that produced it.

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

## Amendment (2026-09-22): three calls, and the user call comes first

The values endpoint does not check that the user exists. In the server source at 11.11.0, `listCPAValues` returns whatever values are stored for the ID, and for a bot with no view restrictions the access check passes for any ID, so an ID Mattermost does not know answers `200` with an empty map. With the two calls above, an unknown user would be indistinguishable from a known user with no attributes and would be bound to least privilege, which rule 4 forbids.

The resolver therefore makes three calls, in this order, all with the bot token:

1. `GET /api/v4/users/{id}`. A 404 means Mattermost does not know the user; a user with `delete_at` set is deactivated. Both are resolution failures. The reply's `username` becomes the principal's id, so the audit log names the person the way Mattermost does. A user whose username is the reserved id `anonymous`, in any letter case, is a resolution failure whatever their attributes: that id belongs to `LEAST_PRIVILEGED` (ADR 0012), and a real user carrying it would be indistinguishable from an unentitled caller in the audit log.
2. `GET /api/v4/custom_profile_attributes/fields`.
3. `GET /api/v4/users/{id}/custom_profile_attributes`.

The rules are otherwise unchanged. Any transport error, timeout, non-200 status, or reply that is not the expected JSON shape on any of the three calls is a resolution failure. Three calls per question is still cheap beside an embedding call and a chat completion.
