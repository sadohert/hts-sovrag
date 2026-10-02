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
  `CLASSIFICATION_RANK` and `DEPARTMENTS` move here from `corpus/manifest.py`, which
  imports `DEPARTMENTS` back, so there is one source. `DEPARTMENTS` holds the vocabulary
  used by `documents.department`; `MEMBERSHIPS` is that set without `GC_WIDE`.
- `Principal(frozen dataclass)`: `id: str`, `clearance: Clearance`,
  `departments: frozenset[str]`. It validates itself on construction (ADR 0012), so the
  Mattermost resolver gets the same guarantees as the YAML loader.
- `LEAST_PRIVILEGED = Principal("anonymous", Clearance.UNCLASSIFIED, frozenset())`.
- `load_principals(path) -> dict[str, Principal]` and `resolve_principal(id, path) -> Principal`
  over `data/principals.yaml`. Unknown id raises `KeyError`; the CLI turns that into a usage
  error. Never a silent downgrade.
- `data/principals.yaml` ships five principals covering the entitlement grid:
  `public` (UNCLASSIFIED, none), `analyst_dnd` (PROTECTED_A, DND), `analyst_ps`
  (PROTECTED_A, PS), `cio_ssc` (PROTECTED_A, SSC and TBS), `auditor` (PROTECTED_B, every
  membership). The file loads strictly; see ADR 0012.

### `sovrag.auth.mattermost` (new)

`MattermostResolver(base_url, bot_token)` with `resolve(user_id) -> Resolution`, where
`Resolution` carries the `Principal`, the attribute snapshot (field name to value) and a
`note` saying why a user was bound to least privilege. It raises `ResolutionError` when it
cannot tell what the user is entitled to. It calls three REST endpoints with the bot token, in order: `GET /api/v4/users/{id}` to
prove the user exists and read the username, `GET /api/v4/custom_profile_attributes/fields`
for the field definitions, and `GET /api/v4/users/{id}/custom_profile_attributes` for the
values. The values endpoint answers an unknown ID with an empty map, which is why the user
call comes first (ADR 0010, amendment). The caller has already checked that `user_id` is
shaped like a Mattermost ID; the resolver checks again because the ID goes into a URL path.

Rules, in order:

1. Only fields whose definition is admin-managed (`attrs.managed == "admin"`) are read.
   A value from any other field is ignored. The definitions are fetched with every
   resolution, so a field that stops being admin-managed stops counting at once. This
   guards the definition only; an administrator who sets a value grants that access.
2. `sovrag_clearance` (single select) maps to `Clearance`; `sovrag_departments`
   (multi-select) maps to `departments`. Select values arrive as option IDs and are
   mapped to option names through the definitions fetched in the same resolution. A name
   outside the vocabulary is ignored.
3. A user with no attributes, or none recognised, is bound to `LEAST_PRIVILEGED`, with the
   reason in `Resolution.note`.
4. If any call fails, times out, or the user is unknown or deactivated, `resolve` raises
   `ResolutionError`. A failed lookup says nothing about entitlement, so it is never
   treated as least privilege.

No cache. Three REST calls per resolution, one resolution per tool call, so an admin's change
shows on the next question.

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

In mattermost mode every HTTP request is authenticated by an ASGI middleware in front of
the MCP app, and the principal is resolved once per tool call, in the tool handler. One tool
call over Streamable HTTP is several HTTP requests (`initialize`, `tools/list`,
`tools/call` and more), so resolving in the middleware would multiply the Mattermost
lookups and the `resolve` audit events.

Every request, in the middleware:

1. Compare `Authorization: Bearer <token>` against `MCP_SHARED_SECRET` in constant time.
   Missing or wrong: HTTP 401, nothing else runs.
2. Read `X-Mattermost-UserID`. Missing, empty, or not 26 lowercase letters and digits:
   HTTP 401. The plugin always sends a real ID, so anything else is a different caller.

Each tool call, in the handler:

3. Generate a `request_id`, resolve the principal, and write a `resolve` audit event with
   the user id, attribute snapshot and resulting principal. If resolution raises, write the
   event with the failure, return a tool error ("entitlement could not be determined") and
   retrieve nothing.

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
  this design needs (user attributes, access control, multiple bots, remote MCP servers).
  Entry caps viewable history at 10,000 messages, and documents a limit of 250 agent
  queries per month that "will take effect in a future release"; see ADR 0011. Mattermost
  publishes amd64 images only, so the service sets `platform: linux/amd64` and runs under
  emulation on Apple silicon.
- Database: the existing Postgres container, with a database and a role both named
  `mattermost`. Mattermost 11.11.0 was booted against the pinned Postgres 18 image and its
  migrations ran cleanly (ADR 0011). A one-shot service under the same profile creates the
  role and database if they are missing, because Postgres init scripts only run on an
  empty volume, and runs `REVOKE CONNECT, TEMPORARY ON DATABASE sovrag FROM PUBLIC`, since
  Postgres grants both to every role by default. The isolation runs one way: the `sovrag`
  role is the container's superuser and can read the `mattermost` database. Acceptable for
  a single-machine demo, not a production layout.
- Agents plugin, pinned to v2.7.0 (minimum server 11.9.0). The 11.11.0 image bundles
  v2.6.1, which predates the MCP service-account headers, so the seed script uploads the
  v2.7.0 release asset. One service of type "OpenAI Compatible" pointed at Docker Model Runner
  through the Docker internal hostname, "Use Responses API" off because Model Runner serves
  chat completions with streaming. One bot, `channelAccessLevel: None` (direct messages
  only), `disableTools: false`, custom instructions telling it to relay the `answer`
  tool's text and citations verbatim and to add nothing. One remote MCP server, sovrag,
  with `Authorization: Bearer <MCP_SHARED_SECRET>` in the static header map and
  service-account authentication off, because the service-account pool is bot-owned and
  names the bot rather than the user (ADR 0009, amendment),
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
3. Middleware verifies the bearer and the shape of the user ID on every HTTP request. When
   the `tools/call` request arrives, the tool handler resolves the principal from
   Mattermost attributes and writes the `resolve` event.
4. `answer` retrieves with the entitlement predicate in SQL, generates, writes the
   `answer` event, returns text, citations, `refused`, `applied_filter`, `request_id`.
5. The plugin's LLM relays the result to the user.

The CLI is unchanged in shape: `--principal <id>` resolves through `principals.yaml`.

## Edge cases

- Bearer wrong or missing, or user ID header missing, empty or malformed: 401 before any
  retrieval. Never mapped to `anonymous`.
- A user Mattermost knows, with no attributes, unrecognised values, or only
  non-admin-managed fields: `LEAST_PRIVILEGED`, reason in the `resolve` event.
- A Mattermost API error, a timeout on either call, or a user ID Mattermost does not know:
  the tool call fails with an error and nothing is retrieved. An outage must not look like
  a refusal.
- An administrator-configured plugin header named `X-Mattermost-UserID`, in the static or
  the service-account map, would override every user's ID, since those layers win over the
  plugin's own header. The seed script writes only `Authorization` in the static map and
  leaves the service-account map empty, and a test asserts both.
- `MCP_IDENTITY` unset or invalid: the server refuses to start.
- Static mode with an unknown `PRINCIPAL`: the server refuses to start.
- Zero entitled hits: `answer` refuses with the fixed sentence and no citations; `search`
  returns an empty list. Neither reveals that forbidden documents exist.
- Generation module absent: `answer` returns the "not available" refusal above.
- Residual risk, recorded in ADR 0010: the plugin's LLM wraps the tool result in its own
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
- `tests/test_mattermost_resolver.py`: recorded responses for each demo user, option IDs
  mapped to names, a user with no values, an unknown value, a non-admin-managed field; each
  asserts the principal and the note. An HTTP error and a timeout on the definitions call,
  the same on the values call, and an unknown user each raise `ResolutionError`.
- `tests/test_audit.py`: one line per event, timestamp present, directory created, and
  concurrent writers never interleave or tear a line.
- `tests/test_mcp_server.py`: in-process SDK client against the test database with a
  stubbed resolver. The 401 cases (bad bearer, missing, empty and malformed user ID);
  static mode; each demo principal's document set through `search`; a resolver failure
  returns a tool error and retrieves nothing; one tool call resolves exactly once and
  writes exactly one `resolve` event, however many HTTP requests it took; `answer` returns
  the "not available" refusal when generation is absent and the `Answer` JSON when present;
  audit lines for `resolve`, `search` and `answer` share a `request_id`; the server refuses
  to start with `MCP_IDENTITY` unset.
- `tests/test_seed_mattermost.py`: dry run prints the expected sequence of calls; running
  twice is a no-op; the MCP server's static header map contains `Authorization` and
  nothing else and its service-account header map is empty.
- Manual, in the demo walkthrough: the full stack, the five beats, the `mattermost` role
  being refused a connection to the `sovrag` database, and the eval harness's leak
  questions through the MCP path (a later branch).

## Documentation

- ADR 0009: principal binding. Rewrites the CLAUDE.md invariant to: "A principal is bound
  by the server, from configuration or from an authenticated transport the server
  verifies. Identity never comes from message text, tool arguments, or any value an agent
  or model can set, because whoever names the principal chooses the entitlement." Records
  the two MCP modes, the bearer plus user-ID pattern, the 401 cases and the
  least-privileged cases. Updates `.claude/rules/retrieval.md` to match.
- ADR 0010: Mattermost user attributes as the entitlement source. What was verified about
  admin-managed attributes and the `EnableUserManagedAttributes` flag, the request-time
  field check, no cache, the attribute snapshot in the audit log, the residual relay risk.
- ADR 0011: Mattermost in Compose. Entry edition and its limits, image and plugin pins,
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
2. `mattermost-integration`: ADRs 0009 to 0011, CLAUDE.md and retrieval rule text.
3. `entitlement-filter`: `filters.py`, `principal` on the three search functions, the two
   `SET LOCAL`s, leak tests, CLI resolution of `--principal`; if generation has merged,
   the two marked call sites in `generate/answer.py` pass the principal through.
4. `mattermost-resolver`: `sovrag.auth.mattermost`, and a locking or single-writer change
   to `sovrag.audit`, which appends without a lock and will be shared by concurrent writers.
5. `mcp-server`: `serve-mcp`, both tools, Compose `mcp` service settings.
6. `mattermost-demo`: Compose profile, seed script, demo walkthrough, README.

## Out of scope

Channel and group-message replies; per-user OAuth 2.1 to the MCP server; plugin-owned
Mattermost access-control policies (would need a Go plugin); the HTTP API; caching of
attributes; the eval harness; any Mattermost core change.

## Amendment (2026-10-01): the `answer` tool returns what is relayed

The Agents plugin renders every tool result as a card in the chat, so returning the full
`Answer` showed the person their principal, the filter, the model and the retrieved chunk ids,
and then the bot repeated the text below. The `answer` tool now returns the text, the citations
(number, title, heading path, source URL), the refusal flag and the request id. The principal,
the rendered filter, the model and the chunk ids are in the `answer` audit line under that
request id, which is where they are traced. `search` is unchanged. The sentences above that
describe the tool as returning the `Answer` unchanged are superseded by this amendment.

## Amendment (2026-10-02): citations arrive as markdown

Left to compose the citation lines itself, the agent wrote title, heading and URL as plain
text, which Mattermost showed as wrapped, unclickable URLs. The `answer` tool now also returns
`sources`: the citations rendered once, in `sovrag.generate.answer.sources_markdown`, as a
markdown list whose items are the number in bold, the title linking to the source URL, and the
heading path. The bot instruction tells the agent to print `Sources:` and that field exactly as
given. The `citations` list stays for clients that want the fields.
