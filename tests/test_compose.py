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
