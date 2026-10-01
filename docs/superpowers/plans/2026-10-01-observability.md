# Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Compose profile `observability` that ships `audit.jsonl` to Loki through Alloy and shows it in a provisioned Grafana dashboard as a formatted stream, with no application change.

**Architecture:** Three services on the existing Compose network. Alloy tails the shared `audit` volume read-only, parses each line as JSON, promotes `event`, `principal` and `refused` to labels, stamps the line with its own `ts`, and pushes to Loki. Loki runs single-binary on a named volume and is not published to the host. Grafana is provisioned from files in `observability/` with one Loki datasource (uid `loki`) and one dashboard with two Logs panels and a `principal` variable.

**Tech Stack:** Docker Compose profiles, `grafana/loki:3.7.8`, `grafana/alloy:v1.20.1`, `grafana/grafana:13.2.3`, LogQL, Grafana file provisioning, pytest for structural tests.

**Spec:** `docs/superpowers/specs/2026-10-01-observability-design.md`

## Global Constraints

- Images pinned exactly: `grafana/loki:3.7.8`, `grafana/alloy:v1.20.1`, `grafana/grafana:13.2.3` (newest releases checked 2026-10-01).
- No change to the audit line format or to any Python under `src/`; no new Python dependency.
- Alloy mounts the `audit` volume read-only at `/app/audit`; the path it tails equals the `app` service's `AUDIT_LOG_PATH` (`/app/audit/audit.jsonl`).
- Labels are exactly `job="sovrag-audit"`, `event`, `principal`, `refused`; nothing else becomes a label.
- Loki publishes no host port; Grafana publishes `3000:3000` only; no host port collides with another profile.
- Datasource uid is `loki` and every dashboard panel references it by uid; every query starts with `{job="sovrag-audit"`.
- Grafana anonymous access is Viewer only, and the README says it is for the local demo.
- `ruff format --check`, `pytest` and `docker compose config --quiet` pass at every commit.
- Never write copy implying real protected data was used (ADR 0003). No private planning, schedule or session references in files or commits.
- Commit messages end with the two trailer lines main uses: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_013MWHVGjco5f2iYBbKKtZ2f`.

## Review Focus

1. The audit file does not exist yet when the profile starts (fresh volume, no question asked). Alloy must start cleanly and pick the file up when it appears; the live check in Task 3 covers it by starting the profile on a fresh volume before the first `ask`.
2. The file is written by the `sovrag` user inside the app container and read by Alloy's user; it must be world-readable. Task 3's live check confirms a line arrives; Task 1 adds a comment on the mount explaining the dependency.
3. `resolve` events carry no `refused` field; the label must simply be absent and the refusals query must still parse. Task 2's test asserts the refusals query filters on `refused="true"` after `| json`, not on the label alone.
4. Alloy restarts must not re-ingest the whole file; Task 1 gives Alloy a named volume for its positions.
5. A reader opening Grafana sees the dashboard without hunting: Task 2 sets the dashboard as the org home via provisioning is not supported, so the README gives the direct dashboard URL and the dashboard uid is fixed (`sovrag-audit`).

---

## File map

| Path | Responsibility |
|---|---|
| `compose.yaml` | three services under profile `observability`, two volumes |
| `observability/loki.yaml` | Loki single-binary config |
| `observability/alloy.alloy` | tail, parse, label, timestamp, push |
| `observability/grafana/provisioning/datasources/loki.yaml` | datasource uid `loki` |
| `observability/grafana/provisioning/dashboards/provider.yaml` | file provider pointing at the dashboards dir |
| `observability/grafana/dashboards/sovrag-audit.json` | the dashboard |
| `tests/test_compose.py` | profile structure assertions |
| `tests/test_observability.py` | config and dashboard assertions |
| `docs/adr/0016-ship-logs-from-the-audit-file.md`, `README.md`, `docs/architecture.md`, `CLAUDE.md` | docs |

---

### Task 1: Loki, Alloy and the Compose profile

**Files:**
- Create: `observability/loki.yaml`, `observability/alloy.alloy`
- Modify: `compose.yaml` (three services, two volumes)
- Test: `tests/test_compose.py`

**Interfaces:**
- Produces: service names `loki`, `alloy`, `grafana` under profile `observability`; Loki reachable inside the network at `http://loki:3100`; labels `job="sovrag-audit"`, `event`, `principal`, `refused`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_compose.py`)

```python
def test_observability_profile_pins_images_and_keeps_loki_private() -> None:
    svc = COMPOSE["services"]
    for name in ("loki", "alloy", "grafana"):
        assert svc[name]["profiles"] == ["observability"], name
    assert svc["loki"]["image"] == "grafana/loki:3.7.8"
    assert svc["alloy"]["image"] == "grafana/alloy:v1.20.1"
    assert svc["grafana"]["image"] == "grafana/grafana:13.2.3"
    assert "ports" not in svc["loki"]
    assert svc["grafana"]["ports"] == ["3000:3000"]
    assert "ports" not in svc["alloy"]


def test_alloy_reads_the_audit_volume_read_only_and_keeps_its_positions() -> None:
    alloy = COMPOSE["services"]["alloy"]
    assert "audit:/app/audit:ro" in alloy["volumes"]
    assert any(v.startswith("alloy-data:") for v in alloy["volumes"])
    assert "./observability/alloy.alloy:/etc/alloy/config.alloy:ro" in alloy["volumes"]
    assert COMPOSE["services"]["app"]["environment"]["AUDIT_LOG_PATH"] == "/app/audit/audit.jsonl"


def test_loki_and_grafana_mount_their_config_read_only() -> None:
    loki = COMPOSE["services"]["loki"]
    assert "./observability/loki.yaml:/etc/loki/config.yaml:ro" in loki["volumes"]
    assert any(v.startswith("loki-data:") for v in loki["volumes"])
    grafana = COMPOSE["services"]["grafana"]
    assert "./observability/grafana/provisioning:/etc/grafana/provisioning:ro" in grafana["volumes"]
    assert "./observability/grafana/dashboards:/var/lib/grafana/dashboards:ro" in grafana["volumes"]
    env = grafana["environment"]
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "true"
    assert env["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Viewer"
    assert set(COMPOSE["volumes"]) >= {"loki-data", "alloy-data"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_compose.py -q`
Expected: the three new tests FAIL with `KeyError: 'loki'`; existing tests pass.

- [ ] **Step 3: Write `observability/loki.yaml`**

```yaml
# Loki single-binary for the local observability profile (ADR 0016). Filesystem storage on the
# loki-data volume; no authentication because the port is not published to the host.
auth_enabled: false

server:
  http_listen_port: 3100
  grpc_listen_port: 9096
  log_level: warn

common:
  instance_addr: 127.0.0.1
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

query_range:
  results_cache:
    cache:
      embedded_cache:
        enabled: true
        max_size_mb: 100

limits_config:
  allow_structured_metadata: true
```

- [ ] **Step 4: Write `observability/alloy.alloy`**

```
// Ship the audit log to Loki (ADR 0016). The file is written by the app and mcp services on
// the shared audit volume, mounted here read-only. Only event, principal and refused become
// labels; everything else stays in the line and is parsed at query time.
local.file_match "audit" {
  path_targets = [{__path__ = "/app/audit/audit.jsonl", job = "sovrag-audit"}]
}

loki.source.file "audit" {
  targets    = local.file_match.audit.targets
  forward_to = [loki.process.audit.receiver]
}

loki.process "audit" {
  stage.json {
    expressions = {ts = "ts", event = "event", principal = "principal", refused = "refused"}
  }
  stage.labels {
    values = {event = "", principal = "", refused = ""}
  }
  stage.timestamp {
    source = "ts"
    format = "RFC3339Nano"
  }
  forward_to = [loki.write.local.receiver]
}

loki.write "local" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
```

- [ ] **Step 5: Add the services and volumes to `compose.yaml`**

Insert after the `mattermost` service (before the top-level `models:`):

```yaml
  # Observability profile (ADR 0016): the audit log, shipped from the shared volume to Loki by
  # Alloy and shown in a provisioned Grafana dashboard. Loki is not published to the host.
  # Grafana opens anonymously as Viewer on 3000 for the local demo; disable that anywhere else.
  loki:
    profiles: [observability]
    image: grafana/loki:3.7.8
    command: ["-config.file=/etc/loki/config.yaml"]
    volumes:
      - ./observability/loki.yaml:/etc/loki/config.yaml:ro
      - loki-data:/loki

  # Reads the audit file the app and mcp services write (owned by their unprivileged user,
  # world-readable), so no application change is needed to ship it.
  alloy:
    profiles: [observability]
    image: grafana/alloy:v1.20.1
    command:
      - run
      - --server.http.listen-addr=0.0.0.0:12345
      - --storage.path=/var/lib/alloy/data
      - /etc/alloy/config.alloy
    depends_on:
      - loki
    volumes:
      - ./observability/alloy.alloy:/etc/alloy/config.alloy:ro
      - audit:/app/audit:ro
      - alloy-data:/var/lib/alloy/data

  grafana:
    profiles: [observability]
    image: grafana/grafana:13.2.3
    depends_on:
      - loki
    ports:
      - "3000:3000"
    environment:
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer
      GF_USERS_DEFAULT_THEME: light
    volumes:
      - ./observability/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./observability/grafana/dashboards:/var/lib/grafana/dashboards:ro
```

Add `loki-data: {}` and `alloy-data: {}` to the top-level `volumes:` block.

- [ ] **Step 6: Run to verify it passes**

Run: `uv run pytest tests/test_compose.py -q && docker compose config --quiet && docker compose --profile observability config --services`
Expected: all tests pass; config valid; the services list includes `loki`, `alloy`, `grafana`.

- [ ] **Step 7: Commit**

```bash
git add observability/loki.yaml observability/alloy.alloy compose.yaml tests/test_compose.py
git commit -m "Ship the audit log to Loki with Alloy under an observability profile"
```

---

### Task 2: Grafana provisioning and the dashboard

**Files:**
- Create: `observability/grafana/provisioning/datasources/loki.yaml`, `observability/grafana/provisioning/dashboards/provider.yaml`, `observability/grafana/dashboards/sovrag-audit.json`
- Test: `tests/test_observability.py`

**Interfaces:**
- Consumes: Task 1's service name `loki` and labels.
- Produces: dashboard uid `sovrag-audit` at `http://localhost:3000/d/sovrag-audit`.

- [ ] **Step 1: Write the failing tests**

```python
"""The observability files agree with each other and with the audit path the app writes."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
OBS = ROOT / "observability"
AUDIT_PATH = "/app/audit/audit.jsonl"


def _dashboard() -> dict:
    return json.loads((OBS / "grafana/dashboards/sovrag-audit.json").read_text())


def test_datasource_is_loki_by_uid_at_the_compose_service() -> None:
    ds = yaml.safe_load((OBS / "grafana/provisioning/datasources/loki.yaml").read_text())
    (source,) = ds["datasources"]
    assert source["uid"] == "loki" and source["type"] == "loki"
    assert source["url"] == "http://loki:3100"
    assert source["isDefault"] is True


def test_dashboard_provider_points_at_the_mounted_dashboards_dir() -> None:
    provider = yaml.safe_load((OBS / "grafana/provisioning/dashboards/provider.yaml").read_text())
    (p,) = provider["providers"]
    assert p["type"] == "file"
    assert p["options"]["path"] == "/var/lib/grafana/dashboards"


def test_dashboard_panels_query_the_audit_job_through_the_loki_uid() -> None:
    dash = _dashboard()
    assert dash["uid"] == "sovrag-audit"
    assert len(dash["panels"]) == 2
    for panel in dash["panels"]:
        assert panel["type"] == "logs"
        assert panel["datasource"] == {"type": "loki", "uid": "loki"}
        for target in panel["targets"]:
            assert target["expr"].startswith('{job="sovrag-audit"')
            assert "| json" in target["expr"]
    titles = {p["title"] for p in dash["panels"]}
    assert titles == {"Audit stream", "Refusals"}


def test_refusals_panel_filters_on_the_parsed_field_not_only_the_label() -> None:
    (refusals,) = [p for p in _dashboard()["panels"] if p["title"] == "Refusals"]
    expr = refusals["targets"][0]["expr"]
    assert 'event="answer"' in expr
    assert '| refused="true"' in expr  # resolve events carry no refused field


def test_principal_variable_filters_both_panels() -> None:
    dash = _dashboard()
    (var,) = dash["templating"]["list"]
    assert var["name"] == "principal" and var["type"] == "query"
    assert var["query"] == "label_values(principal)"
    assert var["includeAll"] is True
    for panel in dash["panels"]:
        assert 'principal=~"$principal"' in panel["targets"][0]["expr"]


def test_alloy_tails_the_path_the_app_writes_with_only_the_agreed_labels() -> None:
    alloy = (OBS / "alloy.alloy").read_text()
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert compose["services"]["app"]["environment"]["AUDIT_LOG_PATH"] == AUDIT_PATH
    assert f'__path__ = "{AUDIT_PATH}"' in alloy
    assert 'job = "sovrag-audit"' in alloy
    assert 'values = {event = "", principal = "", refused = ""}' in alloy
    assert 'url = "http://loki:3100/loki/api/v1/push"' in alloy
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_observability.py -q`
Expected: FAIL with `FileNotFoundError` on the datasource file.

- [ ] **Step 3: Write the provisioning files**

`observability/grafana/provisioning/datasources/loki.yaml`:

```yaml
apiVersion: 1
datasources:
  - name: Loki
    uid: loki
    type: loki
    access: proxy
    url: http://loki:3100
    isDefault: true
    editable: false
```

`observability/grafana/provisioning/dashboards/provider.yaml`:

```yaml
apiVersion: 1
providers:
  - name: sovrag
    type: file
    disableDeletion: true
    updateIntervalSeconds: 30
    options:
      path: /var/lib/grafana/dashboards
      foldersFromFilesStructure: false
```

- [ ] **Step 4: Write the dashboard** `observability/grafana/dashboards/sovrag-audit.json`

```json
{
  "uid": "sovrag-audit",
  "title": "sovrag audit",
  "tags": ["sovrag"],
  "timezone": "browser",
  "schemaVersion": 39,
  "version": 1,
  "editable": true,
  "refresh": "5s",
  "time": {"from": "now-15m", "to": "now"},
  "templating": {
    "list": [
      {
        "name": "principal",
        "label": "Principal",
        "type": "query",
        "datasource": {"type": "loki", "uid": "loki"},
        "query": "label_values(principal)",
        "refresh": 2,
        "includeAll": true,
        "allValue": ".*",
        "multi": false,
        "current": {"text": "All", "value": "$__all"},
        "options": []
      }
    ]
  },
  "panels": [
    {
      "id": 1,
      "type": "logs",
      "title": "Audit stream",
      "datasource": {"type": "loki", "uid": "loki"},
      "gridPos": {"h": 14, "w": 24, "x": 0, "y": 0},
      "options": {
        "showTime": true,
        "wrapLogMessage": true,
        "prettifyLogMessage": false,
        "enableLogDetails": true,
        "dedupStrategy": "none",
        "sortOrder": "Descending"
      },
      "targets": [
        {
          "refId": "A",
          "datasource": {"type": "loki", "uid": "loki"},
          "expr": "{job=\"sovrag-audit\", principal=~\"$principal\"} | json | line_format \"{{.event}} {{.principal}} refused={{.refused}} filter=[{{.applied_filter}}] cited={{.cited_chunk_ids}}\""
        }
      ]
    },
    {
      "id": 2,
      "type": "logs",
      "title": "Refusals",
      "datasource": {"type": "loki", "uid": "loki"},
      "gridPos": {"h": 10, "w": 24, "x": 0, "y": 14},
      "options": {
        "showTime": true,
        "wrapLogMessage": true,
        "prettifyLogMessage": false,
        "enableLogDetails": true,
        "dedupStrategy": "none",
        "sortOrder": "Descending"
      },
      "targets": [
        {
          "refId": "A",
          "datasource": {"type": "loki", "uid": "loki"},
          "expr": "{job=\"sovrag-audit\", event=\"answer\", principal=~\"$principal\"} | json | refused=\"true\" | line_format \"{{.principal}} reason={{.reason}} filter=[{{.applied_filter}}]\""
        }
      ]
    }
  ]
}
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_observability.py tests/test_compose.py -q && docker compose config --quiet`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add observability/grafana tests/test_observability.py
git commit -m "Provision Grafana with the Loki datasource and the audit dashboard"
```

---

### Task 3: Live check, ADR 0016 and docs

**Files:**
- Create: `docs/adr/0016-ship-logs-from-the-audit-file.md`
- Modify: `README.md` ("Serve it" paragraph), `docs/architecture.md` (Compose section sentence; decisions list), `CLAUDE.md` (Commands)

- [ ] **Step 1: Live check on a fresh volume** (Postgres, Model Runner up; the app service's audit volume is the one Alloy reads, so write through the app container or through the API)

```bash
docker compose --profile observability up -d --build app loki alloy grafana
sleep 15
docker compose logs alloy --tail 20                      # expect no error lines
curl -s localhost:3000/api/health                        # expect database: ok
curl -s -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"question": "What did National Defence report as its results for 2024-25?"}' | head -c 200
sleep 10
curl -s -G 'http://localhost:3000/api/datasources/proxy/uid/loki/loki/api/v1/query_range' \
  --data-urlencode 'query={job="sovrag-audit"} | json' --data-urlencode 'limit=5' | head -c 600
```

Expected: the last call returns a stream containing the `answer` line with `principal` `analyst_dnd`. Open `http://localhost:3000/d/sovrag-audit` and confirm the Audit stream panel shows the formatted line and the Refusals panel is empty or shows earlier refusals. Record the alloy log tail and the query output in the PR description. Then `docker compose --profile observability down` (keep volumes).

If the line does not arrive: check `docker compose exec alloy ls -l /app/audit` (file must be readable), the Alloy UI at `http://localhost:12345` is not published, so use `docker compose logs alloy`; fix the config in Task 1's files and re-run; do not change the audit writer.

- [ ] **Step 2: Write ADR 0016**

Shape as `docs/adr/0015-http-api-identity.md`: title `ADR 0016: Ship logs from the audit file rather than instrument the application`, `Status: accepted (2026-10-01)`, Context (one audit line per answer and resolution already exists; three writers share one file; reading raw JSON on screen does not work), Decision (Alloy tails the shared volume read-only; three low-cardinality labels; timestamps from the line's `ts`; Loki unpublished; Grafana anonymous Viewer for the local demo; dashboard provisioned from the repo), Consequences (no app change and no new dependency; a fourth writer appears in the stream for free; metrics are issue #29; retention and auth are deployment concerns outside this profile; the audit line format is now also a display contract, so a field rename is a dashboard change).

- [ ] **Step 3: README, architecture, CLAUDE.md**

README, at the end of "Serve it", add:

```
To watch the audit log while it runs, `docker compose --profile observability up -d` adds Loki, Alloy and Grafana; the dashboard is at `http://localhost:3000/d/sovrag-audit` and shows each answer as `event principal refused filter cited`, with a second panel of refusals. Grafana opens anonymously as a viewer because this is a local demo; disable that anywhere else. See ADR 0016.
```

`docs/architecture.md`: in the Compose section after the Mattermost paragraph, add one paragraph: the `observability` profile ships the audit file to Loki with Alloy and provisions a Grafana dashboard; three labels; no application change; ADR 0016. Add `- ADR 0016: logs are shipped from the audit file by a collector, not emitted by the application; three low-cardinality labels; Grafana anonymous only for the local demo.` to the decisions list.

`CLAUDE.md` Commands: add `- docker compose --profile observability up -d   # audit log in Grafana at localhost:3000/d/sovrag-audit`.

- [ ] **Step 4: Check and commit**

```bash
uv run ruff format --check docs README.md CLAUDE.md
uv run pytest -q
git add docs/adr/0016-ship-logs-from-the-audit-file.md README.md docs/architecture.md CLAUDE.md
git commit -m "Record the observability decision (ADR 0016) and document the audit dashboard"
```

---

## Self-review

**Spec coverage.** Services, images, volumes, ports, read-only mounts: Task 1. Alloy pipeline with the three labels and `ts` timestamp: Task 1. Datasource uid, provider, dashboard with two panels and the `principal` variable: Task 2. Tests named in the spec: Tasks 1 and 2 (`test_compose.py`, `test_observability.py`). Live check: Task 3. ADR, README, architecture, CLAUDE.md: Task 3. Out of scope untouched.

**Review Focus coverage.** (1) and (2) are covered by Task 3's live check on a fresh volume (not automatable without Docker in CI). (3) Task 2 `test_refusals_panel_filters_on_the_parsed_field_not_only_the_label`. (4) Task 1 `test_alloy_reads_the_audit_volume_read_only_and_keeps_its_positions`. (5) Task 2 asserts the fixed uid and the README gives the URL.

**Placeholders.** None.

**Consistency.** Service names, the datasource uid `loki`, the Loki URL `http://loki:3100`, the push path, the audit path and the label set are identical across Tasks 1, 2 and 3 and the tests.
