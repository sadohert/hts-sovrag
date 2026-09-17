# Mattermost integration: permissions-aware answers in direct messages

Date: 2026-09-17. Status: approved.

## Goal

A Mattermost user sends the sovrag bot a direct message and gets a grounded answer with
citations, or a refusal, according to their own entitlement. Two users asking the same
question get different answers, and the chunk a user may not see never leaves Postgres.
The demo story is "same question, different answers", recorded for YouTube and the
website.

To get there, this work also delivers three roadmap items the README already promises:
the principal model, the SQL entitlement filter, and the MCP server. Generation is built on
its own branch; the part of its contract this work relies on is reproduced under
"Integration points".

## Why the Agents plugin cannot be pointed straight at sovrag

Verified against `mattermost-plugin-agents` v2.7.0 source. The plugin's OpenAI-compatible
provider sends messages, tools and token limits upstream and nothing else: no `user`
field, no custom headers, no per-request hook. The only identity that reaches the model is
prose in the system prompt (username, name, position). Parsing identity out of prompt text
is the escalation channel the project's invariant forbids, so that route is rejected.

The plugin's remote MCP path does carry identity. Every connection sends the asking
user's Mattermost ID in an `X-Mattermost-UserID` header, set server-side by the plugin,
and since v2.7.0 an admin can attach static service-account headers such as a bearer
token. sovrag verifies the bearer, then trusts the user ID. That is the standard
trusted-gateway pattern and is the design here.

A bot mentioned in a channel replies in a thread visible to every member, which would
broadcast one user's entitled answer. The bot is therefore restricted to direct messages,
a built-in plugin setting (`channelAccessLevel: None`).

## Components

### `sovrag.auth` (new package; the existing empty package is used)

- `Clearance(StrEnum)`: `UNCLASSIFIED`, `PROTECTED_A`, `PROTECTED_B`.
  `CLASSIFICATION_RANK` moves here from `corpus/manifest.py`, which imports it back, so
  there is one source. `DEPARTMENTS` holds the vocabulary used by `documents.department`.
- `Principal(frozen dataclass)`: `id: str`, `clearance: Clearance`,
  `departments: frozenset[str]`.
- `LEAST_PRIVILEGED = Principal("anonymous", Clearance.UNCLASSIFIED, frozenset())`.
- `load_principals(path) -> dict[str, Principal]` and `resolve_principal(id, path) -> Principal`
  over `data/principals.yaml`. Unknown id raises `KeyError`; the CLI turns that into a usage
  error. Never a silent downgrade.
- `data/principals.yaml` ships five principals covering the entitlement grid:
  `public` (UNCLASSIFIED, none), `analyst_dnd` (PROTECTED_A, DND), `analyst_ps`
  (PROTECTED_A, PS), `cio_ssc` (PROTECTED_A, SSC and TBS), `auditor` (PROTECTED_B, all
  five departments).

### `sovrag.auth.mattermost` (new)

`MattermostResolver(base_url, bot_token)` with `resolve(user_id) -> Resolution`, where
`Resolution` carries the `Principal`, the attribute snapshot (field name to value) and an
optional `error` string. It calls two REST endpoints with the bot token:
`GET /api/v4/custom_profile_attributes/fields` for the field definitions and
`GET /api/v4/users/{id}/custom_profile_attributes` for the values.

Rules, in order:

1. Only fields whose definition is admin-managed (`attrs.managed == "admin"`) are read.
   A value from any other field is ignored. This is checked on every request, so a later
   change in the System Console can only reduce access.
2. `sovrag_clearance` (single select) maps to `Clearance`; `sovrag_departments`
   (multi-select) maps to `departments`. A value outside the vocabulary is ignored.
3. No attributes, no recognised values, an HTTP error, or a timeout all yield
   `LEAST_PRIVILEGED`, with the reason in `Resolution.error`.

No cache. One attributes call per request, so an admin's change shows on the next question.

### `sovrag.retrieve.filters` (new)

`Entitlement(frozen dataclass)`: `clearance`, `departments`, `sql: str`, `params: dict`,
and `render() -> str`, which substitutes the values for the audit log and for
`Answer.applied_filter`. `entitlement_for(principal) -> Entitlement`.

The predicate is two bound arrays, no string building:

```sql
d.classification = any(%(cls)s)
and (d.classification = 'UNCLASSIFIED' or d.department = 'GC_WIDE' or d.department = any(%(depts)s))
```

`cls` is the list of labels at or below the principal's clearance. Rendered example for
`analyst_dnd`:
`classification in (UNCLASSIFIED, PROTECTED_A) and (classification = UNCLASSIFIED or department in (GC_WIDE, DND))`.

Visibility rule, in words: a document is visible when its classification is at or below
the principal's clearance, and it is unclassified, or GC_WIDE, or in one of the
principal's departments. Unclassified material is open to everyone; protected material is
need-to-know by department. Against the indexed corpus, `analyst_dnd` sees 16 of 27
documents and is forbidden 11: the other departments' Departmental Results Reports and the
three Auditor General reports.

### `sovrag.retrieve.hybrid` (changed)

`search_vector`, `search_keyword` and `search_hybrid` each gain a required keyword argument
`principal: Principal` and splice `entitlement_for(principal).sql` into their WHERE clause.
`search_hybrid` passes it to both inner calls; fusion never filters. There is no retrieval
path without a principal.

Two settings are applied with `SET LOCAL` inside a transaction in `search_vector`:
`hnsw.ef_search` (converted from the existing session-level `SET`) and
`hnsw.iterative_scan = relaxed_order`, so a WHERE clause applied after the HNSW scan does
not under-return.

### `sovrag.audit` (new, shared)

`write_event(event: str, **fields) -> None` appends one JSON line with an ISO timestamp to
`Settings.audit_log_path`, creating the parent directory on first write. Events: `answer`
(written by `answer()`), `resolve` and `search` (written by the MCP server). Whichever PR
lands first creates it with this exact signature.

### `sovrag.mcp.server` (new) and `sovrag serve-mcp`

Official Python MCP SDK (the existing `mcp` extra), Streamable HTTP, host and port from
the command line. New settings:

| Setting | Meaning |
|---|---|
| `MCP_IDENTITY` | `static` or `mattermost`. Required. No default, no fallback. |
| `PRINCIPAL` | static mode only: id resolved from `principals.yaml` at startup |
| `MCP_SHARED_SECRET` | mattermost mode: bearer the plugin must present |
| `MATTERMOST_URL` | mattermost mode: base URL for the resolver |
| `MATTERMOST_BOT_TOKEN` | mattermost mode: bot token for the resolver |

Request handling in mattermost mode, as an ASGI middleware in front of the MCP app:

1. Compare `Authorization: Bearer <token>` against `MCP_SHARED_SECRET` in constant time.
   Missing or wrong: HTTP 401, nothing else runs.
2. Read `X-Mattermost-UserID`. Missing: HTTP 401. The plugin always sends it; its absence
   means a different caller.
3. Generate a `request_id`, resolve the principal, write a `resolve` audit event with the
   user id, attribute snapshot, resulting principal and any error.

In static mode steps 1 to 3 are replaced by the principal bound at startup. Whether the
handler reads headers from the SDK's request context or the middleware stores them on the
ASGI scope is settled at implementation against the SDK docs; the observable behaviour
above does not change.

Tools. Neither has a principal parameter, and each description says so.

- `search(question: str, k: int = 5)`: embeds, runs `search_hybrid` with the bound
  principal, returns hits (doc_id, chunk_id, heading_path, source_url, title, score) and
  the rendered filter. Writes a `search` audit event with the chunk ids returned.
- `answer(question: str)`: calls `sovrag.generate.answer.answer(question, principal=...,
  mode=Mode.hybrid, k=5, request_id=...)` and returns the `Answer` as JSON, unchanged.
  Until the generation module exists on main, the tool returns
  `{"refused": true, "text": "Generation is not available on this server.", "citations": []}`
  plus the rendered filter, so auth, resolution and the filter are testable end to end
  before generation merges.

The `mcp` Compose service keeps its name and gains the settings above. `Dockerfile.mcp`
is not created; the service builds the single `Dockerfile` and overrides the command.

### Mattermost in Compose (`mattermost` profile)

- Image `mattermost/mattermost-enterprise-edition`, pinned at implementation time to the
  newest release. With no licence it runs as Mattermost Entry, which unlocks the features
  this design needs (user attributes, access control, multiple bots, remote MCP servers)
  with limits that matter for recording: 250 agent queries per month, 10,000 messages of
  history.
- Database: the existing Postgres container with a second database named `mattermost`,
  if the pinned Mattermost release supports Postgres 18. Otherwise a separate Postgres
  service under the same profile. The check and its outcome go in ADR 0010.
- Agents plugin, pinned to the newest release (v2.7.0 at the time of writing, minimum
  server 11.9.0). One service of type "OpenAI Compatible" pointed at Docker Model Runner
  through the Docker internal hostname, "Use Responses API" off because Model Runner serves
  chat completions with streaming. One bot, `channelAccessLevel: None` (direct messages
  only), `disableTools: false`, custom instructions telling it to relay the `answer`
  tool's text and citations verbatim and to add nothing. One remote MCP server, sovrag,
  with service-account authentication carrying `Authorization: Bearer <MCP_SHARED_SECRET>`,
  both tools set to `auto_run_in_dm`.

### `scripts/seed_mattermost.py` (new)

Idempotent, REST API with an admin token. Creates the admin and the three demo users;
creates the two admin-managed attributes with the document vocabulary as options; sets
each demo user's values; uploads and enables the plugin; writes the plugin configuration
described above; creates the bot token for the resolver; prints the two values to put in
`.env` (`MATTERMOST_BOT_TOKEN`, `MCP_SHARED_SECRET`). A `--dry-run` flag prints the calls
it would make and is what the tests exercise.

Demo users:

| user | clearance | departments | sees | forbidden |
|---|---|---|---|---|
| analyst_dnd | PROTECTED_A | DND | DPs, policies, DND DRRs | other DRRs, OAG reports |
| auditor | PROTECTED_B | DND, PS, PSPC, SSC, TBS | everything | nothing |
| public | UNCLASSIFIED | none | DPs, policies | all DRRs, OAG reports |

### `docs/mattermost-demo.md` (new)

Setup commands, the three users, the questions, and what each beat proves. The video
script and website copy derive from this page so they cannot drift from the code. Beats,
in order: a DRR question to `analyst_dnd` (answer) and `public` (refusal); an OAG question
to `auditor` (answer) and `analyst_dnd` (refusal); the admin raises `analyst_dnd` to
PROTECTED_B in the System Console and the OAG question answers. The audit log tails in a
terminal throughout.

## Data flow

1. User sends the bot a direct message.
2. The plugin's LLM (Model Runner) decides to call `answer`. The plugin opens the MCP
   connection with the bearer and the user's Mattermost ID.
3. Middleware verifies the bearer, reads the user ID, resolves the principal from
   Mattermost attributes, writes the `resolve` event.
4. `answer` retrieves with the entitlement predicate in SQL, generates, writes the
   `answer` event, returns text, citations, `refused`, `applied_filter`, `request_id`.
5. The plugin's LLM relays the result to the user.

The CLI is unchanged in shape: `--principal <id>` resolves through `principals.yaml`.

## Edge cases

- Bearer wrong or missing, or user ID header missing: 401 before any retrieval. Never
  mapped to `anonymous`.
- Authenticated user with no attributes, unrecognised values, a non-admin-managed field,
  or a Mattermost API failure: `LEAST_PRIVILEGED`, reason in the `resolve` event.
- `MCP_IDENTITY` unset or invalid: the server refuses to start.
- Static mode with an unknown `PRINCIPAL`: the server refuses to start.
- Zero entitled hits: `answer` refuses with the fixed sentence and no citations; `search`
  returns an empty list. Neither reveals that forbidden documents exist.
- Generation module absent: `answer` returns the "not available" refusal above.
- Residual risk, recorded in ADR 0009: the plugin's LLM wraps the tool result in its own
  words and could soften a refusal. Mitigated by the bot instructions and by the audit
  log showing the tool's raw output.

## Tests

Each lands with its pull request. Database tests use the existing `db` fixture.

- `tests/test_principal.py`: loading, resolution, unknown id, rank ordering, vocabulary,
  the five shipped principals.
- `tests/test_filters.py`: rendered predicate and bound params for each shipped principal;
  leak tests seeding one document per classification and department cell with a chunk
  whose vector guarantees a hit, asserting the exact document set per principal and zero
  rows for forbidden documents; the three search functions reject a call without
  `principal`; `search_hybrid` never returns a chunk either arm would have filtered.
- `tests/test_mattermost_resolver.py`: recorded responses for each demo user, a user with
  no values, an unknown value, a non-admin-managed field, an HTTP error, a timeout; each
  asserts the principal and the error text.
- `tests/test_audit.py`: one line per event, timestamp present, directory created.
- `tests/test_mcp_server.py`: in-process SDK client against the test database with a
  stubbed resolver. Both 401 cases; static mode; each demo principal's document set through
  `search`; `answer` returns the "not available" refusal when generation is absent and the
  `Answer` JSON when present; audit lines for `resolve`, `search` and `answer` share a
  `request_id`; the server refuses to start with `MCP_IDENTITY` unset.
- `tests/test_seed_mattermost.py`: dry run prints the expected sequence of calls; running
  twice is a no-op.
- Manual, in the demo walkthrough: the full stack, the five beats, and the original plan's
  20 leak questions through the MCP path (eval harness; a later branch).

## Documentation

- ADR 0008: principal binding. Rewrites the CLAUDE.md invariant to: "A principal is bound
  by the server, from configuration or from an authenticated transport the server
  verifies. Identity never comes from message text, tool arguments, or any value an agent
  or model can set, because whoever names the principal chooses the entitlement." Records
  the two MCP modes, the bearer plus user-ID pattern, the 401 cases and the
  least-privileged cases. Updates `.claude/rules/retrieval.md` to match.
- ADR 0009: Mattermost user attributes as the entitlement source. What was verified about
  admin-managed attributes and the `EnableUserManagedAttributes` flag, the request-time
  field check, no cache, the attribute snapshot in the audit log, the residual relay risk.
- ADR 0010: Mattermost in Compose. Entry edition and its limits, image and plugin pins,
  the Postgres 18 decision, Model Runner as the plugin's provider, and that no Docker
  organisation-level governance is claimed.
- `docs/architecture.md`: the MCP server and the principal resolver in the query path.
- README status line and "Run it" section: the `mattermost` profile and the seed script.
  The synthetic-labels statement stays.
- `.env.example`: the new settings.

## Integration points

| Dependency | Built by | This work's stance |
|---|---|---|
| Hybrid retrieval (`search_vector`, `search_keyword`, `search_hybrid`) | on main | the filter is added to all three |
| `sovrag.auth` | this work, first pull request | the generation branch imports `Principal` from it |
| `sovrag.audit` | whichever branch merges first | exact signature above; one implementation |
| `sovrag.generate.answer` | the generation branch | called as the contract below states; nothing else assumed |
| Eval harness and its leak questions | a later branch | out of scope here |

The generation contract this work calls, and nothing more:

```python
def answer(
    question: str,
    *,
    principal: Principal,
    mode: Mode = Mode.hybrid,  # sovrag.retrieve.Mode
    k: int = 5,
    request_id: str | None = None,
) -> Answer: ...
```

`Answer` is a frozen dataclass with `question`, `principal` (the principal's id), `mode`,
`request_id`, `text`, `citations` (each with `n`, `doc_id`, `chunk_id`, `title`,
`heading_path`, `source_url`), `refused`, `model`, `retrieved_chunk_ids` and
`applied_filter`. `refused=True` implies no citations and a fixed refusal sentence. Each
call writes one `answer` audit event. `GenerationError` is the only exception raised for
ordinary input; the MCP `answer` tool turns it into a tool error, not a refusal.

## Sequencing (stacked pull requests, each from current main)

1. `principal-model`: this spec and its plan, `sovrag.auth`, `principals.yaml`, manifest
   import, the Dockerfile copy of `principals.yaml`, tests.
2. `mattermost-integration`: ADRs 0008 to 0010, CLAUDE.md and retrieval rule text.
3. `entitlement-filter`: `filters.py`, `principal` on the three search functions, the two
   `SET LOCAL`s, leak tests, CLI resolution of `--principal`; if generation has merged,
   the two marked call sites in `generate/answer.py` pass the principal through.
4. `mattermost-resolver`: `sovrag.audit` (if not yet on main) and `sovrag.auth.mattermost`.
5. `mcp-server`: `serve-mcp`, both tools, Compose `mcp` service settings.
6. `mattermost-demo`: Compose profile, seed script, demo walkthrough, README.

## Out of scope

Channel and group-message replies; per-user OAuth 2.1 to the MCP server; plugin-owned
Mattermost access-control policies (would need a Go plugin); the HTTP API; caching of
attributes; the eval harness; any Mattermost core change.
