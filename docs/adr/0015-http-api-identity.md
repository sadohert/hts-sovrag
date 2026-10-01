# ADR 0015: The HTTP API binds its principal like the MCP server, static or from a verified gateway

Status: accepted (2026-10-01)

## Context

The README promised an HTTP entry point for callers that are neither a person at a terminal
nor an agent: a web application, a portal, a reporting job, a probe. The CLI and the MCP
server already exist. ADR 0009 fixed the rule that the server binds the principal, from
configuration or from a transport it verifies, and that identity never comes from a value a
caller can set. Over MCP the Mattermost plugin vouches for the user. Over plain HTTP there is
no plugin, so the API needs its own answer to the same question.

## Decision

**Two identity modes, chosen by `API_IDENTITY`, with no default.** The server refuses to start
when it is unset or unknown, and never falls back from one mode to the other.

- `static` binds one principal, `PRINCIPAL`, resolved from the principals file when the app is
  built; an unknown id refuses to start. It suits one tenant, development, and the default
  Compose stack.
- `gateway` trusts an authenticating reverse proxy or portal that has already identified the
  user. A request must carry `Authorization: Bearer <API_SHARED_SECRET>` and an
  `X-Sovrag-Principal` header naming an entry in the principals file. The bearer is compared in
  constant time with `hmac.compare_digest`, after both sides are encoded as UTF-8 bytes, so a
  non-ASCII token is a 401 and not a server error. An empty secret refuses to start, checked
  both when the app is built and by the gate itself.

**Resolved before routing, never downgraded.** The binding is a pure-ASGI layer in
`api/gate.py`, not a FastAPI dependency, so it runs before routing and cannot be forgotten on
a future route. In gateway mode a missing or wrong bearer, a missing or empty header and an
unknown principal id are all HTTP 401 with `{"detail": "unauthorised"}`, and the unknown id
never reaches a handler. A rejected request is not mapped to `LEAST_PRIVILEGED`: it is
rejected (ADR 0009's three outcomes). `GET /healthz` is exempt so a load balancer can probe
without the secret, and it is unbound.

**One query endpoint.** `POST /query` returns the `Answer` the CLI prints, unchanged. There is
no `/search`: agents have the MCP tools, and retrieval-only callers have `sovrag query`. The
body takes `question`, `mode` and `k` and nothing else; the model sets `extra="forbid"`, so a
caller that tries to name a principal gets a 422 rather than silence. The handler reads the
principal from request state and from nowhere else.

**One audit line per request.** `answer()` writes it, keyed by a request id the server
generates. The id is returned in the body and in the `X-Request-ID` response header, so the
audit line joins to an access log. A client-supplied `X-Request-ID` is ignored. A 401 writes
no line, as on the MCP server: a rejected caller is not a principal.

**The gateway's obligations, and the pattern's limits, are ADR 0009's.** The gateway must
strip any inbound `X-Sovrag-Principal` before setting its own, and the API port must be
reachable only from the gateway. Whoever holds `API_SHARED_SECRET` can name any principal, so
it is an administrator credential: kept in the environment, never logged. Per-caller tokens
or OAuth could replace the mode later without changing the endpoint.

## Consequences

- The Compose `app` service runs `API_IDENTITY: static` with `PRINCIPAL` defaulting to
  `analyst_dnd`, so `docker compose up -d` still serves.
- The Dockerfile's default command, `sovrag serve`, is now real.
- `sovrag serve` leaves the "not implemented" list in `CLAUDE.md`.
- The tests run in process with `answer` faked, so they need no database or model.
- A generator failure is a 502 after `answer()` has written its audit line with reason
  `error`; a refusal is a 200 whose `refused` is true.
- The eval harness's leak answer check could be pointed at the API later; this ADR does not do
  it. Rate limiting and TLS termination stay with the gateway.
