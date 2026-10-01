# ADR 0016: Ship logs from the audit file rather than instrument the application

Status: accepted (2026-10-01)

## Context

Every answer writes one JSON line to `audit.jsonl`: the principal, the rendered entitlement
filter, the chunk ids retrieved and cited, whether the answer was a refusal and why, and a
request id. In Mattermost mode every tool call writes a `resolve` line first with the same
request id. The CLI, the HTTP API and the MCP server all write to the same file through the
shared `audit` volume (ADR 0009).

That line is the evidence the system offers for what it did, and it is unreadable at a glance.
A reader watching an entitlement change land, for example an administrator raising a user's
clearance in Mattermost, wants to see the next answer's filter widen and its refusal flip,
without parsing JSON by eye.

## Decision

A Compose profile `observability` ships the file to Loki and shows it in Grafana. Logs are
shipped from the file by a collector, not emitted from the application:

- **Grafana Alloy** tails `/app/audit/audit.jsonl` from the `audit` volume, mounted read-only,
  parses each line as JSON, promotes `event`, `principal` and `refused` to labels, drops the
  collector's automatic `filename` label, stamps the entry with the line's own `ts`, and pushes
  to Loki. The label set is exactly `job`, `event`, `principal`, `refused`: all low cardinality.
  Request ids, chunk ids and the filter string stay inside the line and are parsed at query
  time, because a high-cardinality label degrades Loki quickly.
- **Loki** runs single-binary on a named volume, accepts samples of any age (the file predates
  the profile, and the whole history should appear on first start), asks the ingester for every
  query range and flushes chunks within minutes (with the defaults, backlog streams stayed
  invisible to queries), does not add its automatic `service_name` label, and is not published to
  the host. A query range wider than Loki's default 30 days is refused; raise `max_query_length`
  for a longer history.
- **Grafana** is provisioned from files in the repository: one Loki datasource with uid `loki`
  and one dashboard, uid `sovrag-audit`, with an "Audit stream" panel formatted as
  `event principal refused filter cited`, a "Refusals" panel selected by the `refused` label
  (resolution events carry none), and a `principal` variable. Array fields such as
  `cited_chunk_ids` are extracted by name, because Loki's bare `| json` skips arrays. It opens anonymously in the
  Viewer role on port 3000, bound to this host only because the default administrator login is
  left in place; both are demo settings, wrong anywhere else, and the README says so.

No application code changes, no new Python dependency, no change to the audit line format.

## Consequences

- Any new writer of the audit file appears in the stream for free. The CLI, the API and the MCP
  server already do.
- The audit line format is now also a display contract: renaming a field is a dashboard change.
- Metrics (request rates, latencies, refusal rates as time series) are a separate extension,
  tracked as issue #29, and would share the profile.
- Retention, authentication for Grafana, and shipping logs off the machine are deployment
  concerns outside this profile.
- The collector runs as root inside its container with a read-only mount, so it reads the file
  whatever its mode; the file is created world-readable by the application's umask in any case.
  A deployment that runs the collector unprivileged must grant it read access.
- The stream carries three event types: `answer` from every entry point, and `resolve` and
  `search` from the MCP server. The formatted line shows a resolution error when one is present.
