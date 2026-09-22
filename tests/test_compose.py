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
    assert env["MCP_IDENTITY"].startswith("${MCP_IDENTITY:?")
    assert env["PRINCIPAL"] == "${PRINCIPAL:-}"
    assert env["MCP_SHARED_SECRET"] == "${MCP_SHARED_SECRET:-}"
    assert env["MCP_ALLOWED_HOSTS"] == "${MCP_ALLOWED_HOSTS:-mcp:*,localhost:*}"
    assert env["AUDIT_LOG_PATH"] == "/app/audit/audit.jsonl"
