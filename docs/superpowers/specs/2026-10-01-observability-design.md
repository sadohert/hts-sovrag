# Observability: the audit stream in Grafana

Date: 2026-10-01. Status: approved in discussion; this file records it.

## Goal

Make the audit log readable while the system runs. Every answer and every principal
resolution is already one JSON line in `audit.jsonl`, shared by the CLI, the HTTP API and the
MCP server through the `audit` volume. A Compose profile ships those lines to Loki and shows
them in Grafana as a formatted stream, so a viewer can watch an entitlement change land in the
log without reading raw JSON. Metrics are a later extension (issue #29).

## Decision

Logs are shipped from the file, not emitted from the application. The three entry points
already write the same lines to the same file, so a collector tailing that file sees all of
them with no client library, no new dependency and no change to the audit line format, which
the leak checks and the eval read.

## Components

A Compose profile `observability` with three services, all pinned to the newest release checked
on 2026-10-01:

| Service | Image | Role |
|---|---|---|
| `loki` | `grafana/loki:3.7.8` | single-binary log store, filesystem storage on a named volume `loki-data` |
| `alloy` | `grafana/alloy:v1.20.1` | tails `/app/audit/audit.jsonl` from the `audit` volume (mounted read-only), parses each line as JSON, promotes `event`, `principal` and `refused` to labels, forwards to Loki |
| `grafana` | `grafana/grafana:13.2.3` | provisioned Loki datasource and one dashboard, anonymous viewer access, port 3000 |

Configuration lives under `observability/` in the repo and is bind-mounted read-only:

```
observability/
  loki.yaml                              single-binary config, auth disabled, tsdb v13 schema
  alloy.alloy                            file match, source, process (json, labels, timestamp), write
  grafana/provisioning/datasources/loki.yaml
  grafana/provisioning/dashboards/provider.yaml
  grafana/dashboards/sovrag-audit.json   the dashboard
```

Loki's HTTP port is not published to the host; Alloy pushes to it over the Compose network.
Grafana is published on 127.0.0.1:3000 with anonymous access in the Viewer role and its default
administrator login, because the stack is a local demo on one machine; the README says so and
says both are wrong anywhere else. No other
service changes. The `app` and `mcp` services keep their `audit` volume mount unchanged.

## The dashboard

Datasource name `Loki`, uid `loki`, referenced by uid from the dashboard. Two panels:

1. **Audit stream** (Logs panel): `{job="sovrag-audit"} | json applied_filter, reason, error, cited="cited_chunk_ids" | line_format "{{.event}} {{.principal}} refused={{.refused}} filter=[{{.applied_filter}}] cited={{.cited}}"` (arrays must be extracted by name; bare `| json` skips them), newest first, wrapped, time range last 15 minutes, refresh 5 s.
2. **Refusals** (Logs panel): `{job="sovrag-audit", event="answer", refused="true"} | json applied_filter, reason | line_format "{{.principal}} reason={{.reason}} filter=[{{.applied_filter}}]"`; `refused` is a stream label, absent on resolve events.

A dashboard variable `principal` (Loki label values of `principal`) filters both panels; default All.

Labels are kept few on purpose. `event`, `principal` and `refused` are low cardinality; chunk
ids, request ids and the filter string stay in the line and are parsed at query time.

## Alloy pipeline

Amended after the live check: Alloy also drops its automatic `filename` label; Loki accepts
samples of any age, adds no `service_name` label, asks the ingester for every range and flushes
chunks within minutes, or the existing history is rejected or invisible on first start.

```
local.file_match "audit" {
  path_targets = [{__path__ = "/app/audit/audit.jsonl", job = "sovrag-audit"}]
}
loki.source.file "audit" {
  targets    = local.file_match.audit.targets
  forward_to = [loki.process.audit.receiver]
}
loki.process "audit" {
  stage.json      { expressions = {ts = "ts", event = "event", principal = "principal", refused = "refused"} }
  stage.labels    { values = {event = "", principal = "", refused = ""} }
  stage.timestamp { source = "ts", format = "RFC3339Nano" }
  forward_to = [loki.write.local.receiver]
}
loki.write "local" {
  endpoint { url = "http://loki:3100/loki/api/v1/push" }
}
```

`resolve` events have no `refused` field; the label is then absent, which is what the queries
expect. The timestamp stage uses the line's own `ts`, so a replayed or late line lands at the
time the audit writer recorded.

## Tests

`tests/test_compose.py` gains: the three services exist with the pinned images and the
`observability` profile; `alloy` mounts the `audit` volume read-only; `loki` publishes no
host port; `grafana` publishes 3000 only; no host port collides with another profile (the
existing test covers this once the ports are added). `tests/test_observability.py`: the
dashboard JSON parses, every panel's datasource uid is `loki`, every target's `expr` starts
with `{job="sovrag-audit"`, the datasource provisioning names uid `loki` and the Compose
service URL, and `alloy.alloy` names the audit path the `app` service writes to. A live check,
recorded in the PR and not automated: `docker compose --profile observability up -d`, one
`sovrag ask`, and the line appears in the Audit stream panel within the refresh interval.

## Documentation

- ADR 0016: ship logs from the audit file rather than instrument the application; label
  choice; anonymous Grafana is demo-only.
- README "Serve it": a short paragraph with the profile command and the dashboard URL.
- `docs/architecture.md`: a sentence in the Compose section; ADR 0016 in the decisions list.
- `CLAUDE.md` Commands: the profile command.

## Out of scope

Metrics, alerting, Grafana authentication, log retention policy, shipping anything off the
machine (issue #29 covers metrics).
