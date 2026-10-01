# HTTP API: `sovrag serve` with one query endpoint and a server-bound principal

Date: 2026-10-01. Status: draft for review.

## Goal

`sovrag serve` exposes the answer function over HTTP for callers that are neither a person at a
terminal nor an agent speaking MCP: a web application, a portal, a reporting job, a probe. One
endpoint returns the same `Answer` the CLI prints, including citations and the rendered
entitlement filter, and writes the same audit line. The principal is bound by the server, from
configuration or from a transport it verifies, exactly as ADR 0009 requires; nothing in the
request body can name it.

## Identity modes

A required setting, `API_IDENTITY`, with no default and no fallback between modes. The server
refuses to start when it is unset or unknown.

| Mode | Principal comes from | When |
|---|---|---|
| `static` | `PRINCIPAL`, resolved from `PRINCIPALS_PATH` at startup; an unknown id refuses to start | one tenant, development, the default Compose stack |
| `gateway` | the `X-Sovrag-Principal` header, after the bearer is verified; resolved per request from `PRINCIPALS_PATH` | behind an authenticating reverse proxy or portal that has already identified the user |

In `gateway` mode a pure-ASGI gate runs before routing, on every request except `GET /healthz`:

1. `Authorization: Bearer <token>` is compared with `API_SHARED_SECRET` in constant time.
   Missing or wrong: HTTP 401 with body `{"detail": "unauthorised"}`. The server refuses to
   start in `gateway` mode with an empty secret.
2. `X-Sovrag-Principal` is read. Missing, empty, or not a principal id in `PRINCIPALS_PATH`:
   HTTP 401 with the same body. Resolution happens in the gate so an unknown id never reaches a
   handler. It is never mapped to `LEAST_PRIVILEGED`: a rejected request is rejected, not
   downgraded (ADR 0009's three outcomes).

The gateway must strip any inbound `X-Sovrag-Principal` from clients before setting its own,
and the port must be reachable only from the gateway. That is the trusted-gateway pattern and
its limits are ADR 0009's: whoever holds the shared secret can name any principal, so the
secret is an administrator credential kept in the environment and never logged.

The resolved principal is placed on the ASGI scope (`scope["state"]["principal"]`) by the gate
in gateway mode, or by the app at startup in static mode; the handler reads it from there and
from nowhere else.

## Endpoints

### `POST /query`

Request body (JSON, validated by FastAPI):

```json
{"question": "What did the Auditor General find about ArriveCAN?", "mode": "hybrid", "k": 5}
```

- `question`: string, 1 to 2000 characters after stripping; required.
- `mode`: `"vector" | "keyword" | "hybrid"`; default `"hybrid"`.
- `k`: integer; default 5; values outside 1..`MAX_K` are clamped, not rejected, matching the
  CLI and the MCP server.
- Any other field, including `principal`, is rejected with 422 (`extra="forbid"` on the model),
  so a caller that tries to name a principal gets an error rather than silence.

The server generates `request_id` (uuid4 hex), passes it to `answer()`, returns it in the body
and in the `X-Request-ID` response header, so the audit line `answer()` writes is joinable to
the HTTP access log.

Response 200: `dataclasses.asdict(Answer)` unchanged, which is what `sovrag ask --json` prints:
`question`, `principal`, `mode`, `request_id`, `text`, `citations`, `refused`, `model`,
`retrieved_chunk_ids`, `applied_filter`.

Errors:

| Condition | Status | Body |
|---|---|---|
| gate failure (gateway mode) | 401 | `{"detail": "unauthorised"}` |
| invalid body | 422 | FastAPI's validation detail |
| `GenerationError` from `answer()` | 502 | `{"detail": "generation failed: <message>", "request_id": ...}`; `answer()` has already written its audit line with reason `error` |

A refusal is not an error: it is a 200 whose `refused` is true and whose `text` is the refusal
sentence, as on the CLI.

### `GET /healthz`

200 with `{"status": "ok", "identity": "static" | "gateway", "principal": "<id>" | null}`;
the principal is reported only in static mode. No database or model call: the check says the
process is up and how it binds identity, nothing more. Exempt from the gate so a load balancer
can probe without the secret.

## Components

- `src/sovrag/api/app.py`: `Identity(StrEnum)` with `static` and `gateway`; `build_app(settings)
  -> FastAPI` that validates configuration (raises `ValueError` naming the problem, so `serve`
  can refuse to start), loads the principals file once, and mounts the two routes. The query
  handler calls `sovrag.generate.answer.answer` through a module-level name so tests can
  substitute it, the same way `mcp/server.py` exposes `generate_answer`.
- `src/sovrag/api/gate.py`: `Gate(app, *, secret, principals)` pure ASGI middleware: bearer
  compare with `hmac.compare_digest`, header lookup, 401 responses, `scope["state"]` binding.
  Pure ASGI rather than a FastAPI dependency so the check happens before routing and cannot be
  forgotten on a future route.
- `src/sovrag/cli.py`: `serve` builds the app, prints `serve: identity=<mode> principal=<id or
  per request> at http://host:port`, and runs uvicorn; a `ValueError` prints to stderr and exits
  2, mirroring `serve-mcp`.
- `src/sovrag/config.py`: `api_identity: str = ""`, `api_shared_secret: str = ""`, with the
  same comment style as the MCP settings.
- `compose.yaml`: the `app` service sets `API_IDENTITY: static` and `PRINCIPAL:
  ${PRINCIPAL:-analyst_dnd}` so `docker compose up -d` serves as before, with a comment on
  switching to gateway mode. `.env.example` documents both settings.

## Audit

`answer()` writes the `answer` event with the request id, principal, mode, k, model, chunk ids,
refusal and reason, and `applied_filter`. The API adds no second event: one request, one line.
A 401 writes nothing, as on the MCP server: a rejected caller is not a principal.

## Tests

`tests/test_api.py`, in process through `httpx2.ASGITransport`, with `answer` faked to return
a fixed `Answer` and record what it was called with. Postgres is not needed.

- Configuration: unset or unknown `API_IDENTITY` raises `ValueError` from `build_app`; static
  mode with a missing or unknown `PRINCIPAL` raises; gateway mode with an empty secret raises.
- Static mode binds the configured principal and ignores `X-Sovrag-Principal` and any bearer.
- Gateway mode: missing bearer, wrong bearer, missing header, unknown id each give 401 before
  the fake `answer` is called; a good request binds the named principal, and a different
  header binds a different principal on the next request (no caching of the wrong one).
- `/healthz` is 200 without a bearer in gateway mode and reports the mode; in static mode it
  reports the principal.
- Body: a `principal` field is 422; empty question is 422; `k` of 0 and 50 are clamped to 1 and
  `MAX_K`; default mode is hybrid.
- Response: `request_id` is present, matches the `X-Request-ID` header, and was the
  `request_id` passed to `answer()`; the body equals `asdict` of the fake answer.
- `GenerationError` from `answer()` is 502 with the message and the request id.
- `tests/test_cli.py`: `serve` with a bad configuration exits 2 with the message; a good
  configuration prints the identity line (uvicorn faked).

## Documentation

- ADR 0015: the HTTP API binds its principal by the same rule as the MCP server, with `static`
  and `gateway` modes; one `POST /query` endpoint and why no `/search`; 401 never downgrades;
  the gateway's obligations.
- README: a "Serve it" section after the MCP paragraph with the two modes, a curl example, and
  the status line updated.
- `docs/architecture.md`: an "HTTP API" section beside the MCP server section; status line;
  ADR 0015 in the decisions list.
- `CLAUDE.md`: `sovrag serve` moves from "not implemented" to Commands; the "Not implemented
  yet" line goes away if nothing remains on it.

## Out of scope

A `/search` endpoint, per-caller tokens or OAuth, rate limiting, TLS termination (the gateway's
job), and pointing the eval harness's leak answer check at the API.
