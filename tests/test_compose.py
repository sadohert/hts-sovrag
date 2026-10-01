"""compose.yaml: the mcp service is the single Dockerfile with an identity it cannot guess."""

from pathlib import Path

import yaml

COMPOSE = yaml.safe_load((Path(__file__).parents[1] / "compose.yaml").read_text())


def test_mcp_service_builds_the_single_dockerfile_and_runs_serve_mcp() -> None:
    mcp = COMPOSE["services"]["mcp"]
    assert mcp["profiles"] == ["mcp"]
    assert mcp["build"] == {"context": "."}
    assert mcp["command"] == ["sovrag", "serve-mcp", "--host", "0.0.0.0", "--port", "8002"]
    assert not (Path(__file__).parents[1] / "Dockerfile.mcp").exists()


def test_mcp_host_port_does_not_collide_with_another_profile() -> None:
    published = [
        p.split(":")[0] for svc in COMPOSE["services"].values() for p in svc.get("ports", [])
    ]
    assert len(published) == len(set(published))


def test_mcp_identity_is_required_and_principal_has_no_default() -> None:
    env = COMPOSE["services"]["mcp"]["environment"]
    assert env["MCP_IDENTITY"] == "${MCP_IDENTITY:-}"  # no default; the server refuses without one
    assert env["PRINCIPAL"] == "${PRINCIPAL:-}"
    assert env["MCP_SHARED_SECRET"] == "${MCP_SHARED_SECRET:-}"
    assert env["MCP_ALLOWED_HOSTS"] == "${MCP_ALLOWED_HOSTS:-mcp:*,localhost:*}"
    assert env["AUDIT_LOG_PATH"] == "/app/audit/audit.jsonl"


def test_app_service_defaults_to_static_identity() -> None:
    env = COMPOSE["services"]["app"]["environment"]
    assert env["API_IDENTITY"] == "${API_IDENTITY:-static}"
    assert env["PRINCIPAL"] == "${PRINCIPAL:-analyst_dnd}"
    assert env["API_SHARED_SECRET"] == "${API_SHARED_SECRET:-}"


def test_mattermost_profile_runs_the_pinned_enterprise_image_on_amd64() -> None:
    mm = COMPOSE["services"]["mattermost"]
    assert mm["profiles"] == ["mattermost"]
    assert mm["image"] == "mattermost/mattermost-enterprise-edition:11.11.0"
    assert mm["platform"] == "linux/amd64"
    assert mm["ports"] == ["8065:8065"]
    env = mm["environment"]
    assert env["MM_SQLSETTINGS_DRIVERNAME"] == "postgres"
    assert "mattermost:mattermost@postgres:5432/mattermost" in env["MM_SQLSETTINGS_DATASOURCE"]
    assert env["MM_PLUGINSETTINGS_ENABLEUPLOADS"] == "true"
    assert env["MM_SERVICESETTINGS_ENABLEBOTACCOUNTCREATION"] == "true"
    assert env["MM_SERVICESETTINGS_ENABLEUSERACCESSTOKENS"] == "true"
    assert "mcp" in env["MM_SERVICESETTINGS_ALLOWEDUNTRUSTEDINTERNALCONNECTIONS"]
    assert mm["depends_on"]["mattermost-db-init"]["condition"] == "service_completed_successfully"


def test_mattermost_database_is_created_by_a_one_shot_service_with_the_revoke() -> None:
    init = COMPOSE["services"]["mattermost-db-init"]
    assert init["profiles"] == ["mattermost"]
    assert init["image"] == COMPOSE["services"]["postgres"]["image"]
    assert init["depends_on"]["postgres"]["condition"] == "service_healthy"
    sql = (Path(__file__).parents[1] / "scripts" / "mattermost_db_init.sql").read_text()
    assert "REVOKE CONNECT, TEMPORARY ON DATABASE sovrag FROM PUBLIC" in sql
    assert "create role mattermost" in sql.lower() and "create database mattermost" in sql.lower()
    assert any("mattermost_db_init.sql" in v for v in init["volumes"])


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
