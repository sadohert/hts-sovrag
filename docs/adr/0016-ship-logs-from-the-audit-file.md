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
  the profile, and the whole history should appear on first start), does not add its automatic
  `service_name` label, and is not published to the host.
- **Grafana** is provisioned from files in the repository: one Loki datasource with uid `loki`
  and one dashboard, uid `sovrag-audit`, with an "Audit stream" panel formatted as
  `event principal refused filter cited`, a "Refusals" panel filtered on the parsed `refused`
  field (resolution events carry none), and a `principal` variable. It opens anonymously in the
  Viewer role on port 3000 because this stack is a one-machine demo; that setting is wrong
  anywhere else and the README says so.

No application code changes, no new Python dependency, no change to the audit line format.

## Consequences

- Any new writer of the audit file appears in the stream for free. The CLI, the API and the MCP
  server already do.
- The audit line format is now also a display contract: renaming a field is a dashboard change.
- Metrics (request rates, latencies, refusal rates as time series) are a separate extension,
  tracked as issue #29, and would share the profile.
- Retention, authentication for Grafana, and shipping logs off the machine are deployment
  concerns outside this profile.
- The collector reads a file owned by the application's unprivileged user; the file is created
  world-readable, which the live check confirmed. A deployment that tightens that mode must
  grant the collector read access another way.
