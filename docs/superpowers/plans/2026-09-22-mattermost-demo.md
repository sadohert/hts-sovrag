# Mattermost Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `docker compose --profile mattermost --profile mcp up` plus one seed script gives a Mattermost where three demo users ask the sovrag bot the same question in a direct message and get answers that differ by their entitlement, with every step reproducible from the repository.

**Architecture:** A `mattermost` Compose profile adds a one-shot database-init service and the pinned Mattermost enterprise image, running as Mattermost Entry on the existing Postgres. `scripts/seed_mattermost.py` configures everything through REST: the admin, the two admin-managed attributes, the three demo users and their values, the Agents plugin at 2.7.0 (uploaded, since the image bundles 2.6.1), the plugin's provider (Docker Model Runner) and its MCP server entry (sovrag, with the shared secret in the service-account header map and nothing in the static map), the bot restricted to direct messages, and the bot token the resolver uses. `docs/mattermost-demo.md` is the walkthrough the video and website copy derive from.

**Tech Stack:** Compose, `mattermost/mattermost-enterprise-edition:11.11.0` (amd64), Agents plugin 2.7.0, httpx2, argparse, pytest with `MockTransport`.

**Spec:** `docs/superpowers/specs/2026-09-17-mattermost-integration-design.md` (sections "Mattermost in Compose", "`scripts/seed_mattermost.py`", "`docs/mattermost-demo.md`", "Tests", "Documentation", and item 6 under "Sequencing"). ADR 0011 is the decision; Task 6 updates its "Not yet verified" list with what the live run shows.

**Verified before this plan was written**, on a throwaway 11.11.0 Entry instance and in the plugin source at v2.7.0:
- Entry serves admin-managed attributes: the admin creates a select field with `attrs.managed: admin`, sets another user's value, a bot token reads definitions and values, and the user gets 403 on self-edit. The first user created is the system admin. Bot creation needs `MM_SERVICESETTINGS_ENABLEBOTACCOUNTCREATION=true`; a bot token comes from `POST /api/v4/users/{bot_user_id}/tokens`.
- Plugin 2.7.0 stores its configuration in its own database store, migrated once from the server config, so it is written through the plugin's admin API: `GET`/`PUT /plugins/mattermost-ai/admin/config` with the whole `config.Config` document. Bots are created through `POST /plugins/mattermost-ai/agents`, which also creates the Mattermost bot user. Plugin routes hang directly under `/plugins/mattermost-ai/` with no version prefix.
- Config keys: `services[]` with `id`, `name`, `type: "openaicompatible"`, `apiURL`, `apiKey`, `defaultModel`, `useResponsesAPI`, `streamingTimeoutSeconds`; `mcp` with `enabled`, `servers[]` of `name`, `enabled`, `baseURL`, `headers`, `serviceAccountHeaders`, `tool_configs[]` of `name`, `policy` (`auto_run_in_dm`), `enabled`; `defaultBotName`. Agent request keys: `displayName`, `username`, `serviceID`, `customInstructions`, `channelAccessLevel` (3 = none, direct messages only), `userAccessLevel` (0 = all), `enabledMCPTools[]` of `server_origin` (the server's `baseURL`) and `tool_name`, `useServiceAccountAuth`, `disableTools`, `maxToolTurns`.
- The plugin sends `X-Mattermost-UserID` on every remote MCP connection; static `headers` and then `serviceAccountHeaders` are layered on top, later wins.
- Release asset for the upload: `mattermost-plugin-agents-v2.7.0-linux-amd64.tar.gz` (published 2026-09-02). Its SHA-256 is computed in Task 3 and recorded in the script.

## Global Constraints

- Classification labels and clearances are synthetic (ADR 0003). Demo copy must say the users, attributes and labels are invented and the corpus is public.
- No Docker organisation-level governance is claimed; only local Docker Desktop settings are used.
- The bot token and the shared secret are printed once by the seed script for `.env`; they are never committed, never logged by the server, and redacted in the script's dry-run output.
- The seed script's static header map for the MCP server is empty and its service-account map holds `Authorization` and nothing else (spec edge case: an administrator-configured `X-Mattermost-UserID` in either map would override every user's identity). A test asserts both.
- Image, plugin version and asset hash are pinned; the plan records where each was checked.
- Type hints everywhere; `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src scripts` and `uv run pytest -q` pass before each commit. Run `uv run ruff format docs` before committing this plan or the walkthrough.
- Tests first for the script. Compose and docs tasks verify with `docker compose config` and the live run.
- Line length 100. No schedule, session or notes-folder references anywhere in the repo.
- End each commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Write import blocks by hand in sorted order.

## File Structure

| File | Responsibility |
|---|---|
| `compose.yaml` (modify) | `mattermost-db-init` and `mattermost` services under the `mattermost` profile; `mcp` gains the Mattermost-mode pass-throughs it already has, plus `depends_on` nothing new |
| `scripts/mattermost_db_init.sql` (create) | Idempotent role, database and the `REVOKE` |
| `scripts/seed_mattermost.py` (create) | The seed script, importable, with `--dry-run` |
| `tests/test_seed_mattermost.py` (create) | Dry-run sequence, header maps, pins, idempotency against a fake server |
| `tests/test_compose.py` (modify) | The two new services' shape |
| `docs/mattermost-demo.md` (create) | Setup, the users, the five beats, the audit tail, what each beat proves |
| `docs/adr/0011-mattermost-in-compose.md` (modify) | Move the tool-calling item from "Not yet verified" to verified, with the result |
| `README.md`, `CLAUDE.md`, `docs/architecture.md`, `.env.example` (modify) | Status, commands, prerequisites |

## Branch Setup

Already done: worktree `.claude/worktrees/mattermost-demo` on branch `mattermost-demo` from `origin/main` at 39fa237. Before Task 1:

```bash
uv sync --all-extras
docker start hts-sovrag-postgres-1 2>/dev/null || docker compose up -d postgres
uv run pytest -q
```

Expected: the suite passes with no skips.

---

### Task 1: Database init script and the `mattermost` Compose profile

**Files:**
- Create: `scripts/mattermost_db_init.sql`
- Modify: `compose.yaml`
- Test: `tests/test_compose.py`

**Interfaces:**
- Produces: services `mattermost-db-init` (profile `mattermost`, runs once) and `mattermost` (profile `mattermost`, port 8065); the `mcp` service unchanged except a comment.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_compose.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_compose.py -q`
Expected: `KeyError: 'mattermost'`.

- [ ] **Step 3: The init SQL**

Create `scripts/mattermost_db_init.sql`:

```sql
-- Run once by the mattermost-db-init service against the shared Postgres (ADR 0011).
-- Idempotent: safe on a fresh volume and on an existing development volume.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mattermost') THEN
    CREATE ROLE mattermost LOGIN PASSWORD 'mattermost';
  END IF;
END
$$;

SELECT 'CREATE DATABASE mattermost OWNER mattermost'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'mattermost') \gexec

-- Postgres grants CONNECT and TEMPORARY on every database to PUBLIC by default. Without this
-- the mattermost role could open the sovrag database. The isolation runs one way: sovrag is
-- the container's superuser and can still read the mattermost database (ADR 0011).
REVOKE CONNECT, TEMPORARY ON DATABASE sovrag FROM PUBLIC;
```

`\gexec` is a psql meta-command, so the service must run the file through `psql`, not through the server's init mechanism.

- [ ] **Step 4: The services**

In `compose.yaml`, update the line-2 comment to list the profile:

```yaml
# Profiles: (default) postgres + app on Docker Model Runner; vllm = GPU serving comparison; mcp = MCP server; mattermost = Mattermost Entry for the direct-message demo.
```

After the `mcp` service and before the top-level `models:` block, add:

```yaml
  # Creates the mattermost role and database on the shared Postgres, and revokes the default
  # PUBLIC access to the sovrag database, then exits (ADR 0011). Postgres init scripts only run
  # on an empty volume, so this runs every time and is idempotent.
  mattermost-db-init:
    profiles: [mattermost]
    image: pgvector/pgvector:0.8.6-pg18-trixie
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      PGPASSWORD: sovrag
    volumes:
      - ./scripts/mattermost_db_init.sql:/init.sql:ro
    command: ["psql", "-v", "ON_ERROR_STOP=1", "-h", "postgres", "-U", "sovrag", "-d", "sovrag", "-f", "/init.sql"]

  # Mattermost Entry: the enterprise image with no licence key (ADR 0011). amd64 only, so it
  # runs under emulation on Apple silicon. The seed script (scripts/seed_mattermost.py) does
  # every bit of configuration through the REST API; nothing is set in the System Console.
  mattermost:
    profiles: [mattermost]
    image: mattermost/mattermost-enterprise-edition:11.11.0
    platform: linux/amd64
    depends_on:
      mattermost-db-init:
        condition: service_completed_successfully
    environment:
      MM_SQLSETTINGS_DRIVERNAME: postgres
      MM_SQLSETTINGS_DATASOURCE: postgres://mattermost:mattermost@postgres:5432/mattermost?sslmode=disable
      MM_SERVICESETTINGS_SITEURL: http://localhost:8065
      MM_SERVICESETTINGS_ENABLEBOTACCOUNTCREATION: "true"
      MM_SERVICESETTINGS_ENABLEUSERACCESSTOKENS: "true"
      MM_SERVICESETTINGS_ALLOWEDUNTRUSTEDINTERNALCONNECTIONS: "mcp model-runner.docker.internal"
      MM_PLUGINSETTINGS_ENABLEUPLOADS: "true"
      MM_LOGSETTINGS_CONSOLELEVEL: INFO
    ports:
      - "8065:8065"
    volumes:
      - mattermost-data:/mattermost/data
      - mattermost-plugins:/mattermost/plugins
```

and add to the top-level `volumes:` block:

```yaml
  mattermost-data: {}
  mattermost-plugins: {}
```

In the `mcp` service, add after the `MATTERMOST_BOT_TOKEN` line nothing; the pass-throughs already exist. Change its leading comment's last sentence to end with: `In mattermost mode set MCP_IDENTITY=mattermost, MCP_SHARED_SECRET, MATTERMOST_URL=http://mattermost:8065 and MATTERMOST_BOT_TOKEN from the seed script's output.`

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/test_compose.py -q && MCP_IDENTITY=static docker compose --profile mattermost --profile mcp config --quiet && echo ok`
Expected: tests pass, `ok`.

- [ ] **Step 6: Commit**

```bash
git add compose.yaml scripts/mattermost_db_init.sql tests/test_compose.py
git commit -m "Add the mattermost Compose profile: Entry on the shared Postgres with its own role"
```

---

### Task 2: The seed script, dry run first

**Files:**
- Create: `scripts/seed_mattermost.py`
- Test: `tests/test_seed_mattermost.py`

**Interfaces:**
- Produces: `scripts/seed_mattermost.py` importable as a module, with:
  - constants `PLUGIN_ID = "mattermost-ai"`, `PLUGIN_VERSION = "2.7.0"`, `PLUGIN_ASSET_URL`, `PLUGIN_SHA256`, `CLEARANCES`, `DEPARTMENTS`, `DEMO_USERS`, `BOT_USERNAME = "sovrag"`, `BOT_INSTRUCTIONS`, `TOOL_NAMES = ("search", "answer")`.
  - `class Mattermost` REST client with `call(method, path, *, json=None, files=None, params=None) -> Any`, and `token` set by `login`.
  - `seed(mm: Mattermost, *, admin_password: str, demo_password: str, shared_secret: str, llm_url: str, llm_model: str, mcp_url: str, rotate_token: bool, download: Callable[[str], bytes]) -> Result` where `Result` has `bot_token: str | None`, `shared_secret: str`, `bot_user_id: str`.
  - `main(argv: list[str] | None = None) -> int` with `--url`, `--admin-password`, `--demo-password`, `--shared-secret`, `--llm-url`, `--llm-model`, `--mcp-url`, `--rotate-token`, `--dry-run`.
  - `class DryRunTransport(httpx2.BaseTransport)`: answers every GET as "nothing exists yet" and every write with a plausible id, and records `(method, path)` in `calls`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_seed_mattermost.py`:

```python
"""The Mattermost seed script: what it would do on a fresh server, and that it changes nothing
on a server that is already seeded."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx2 as httpx
import pytest


def _load() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "seed_mattermost.py"
    spec = importlib.util.spec_from_file_location("seed_mattermost", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed = _load()
ID = re.compile(r"^[a-z0-9]{26}$")


def _run_dry(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[tuple[str, str]], list[str]]:
    transport = seed.DryRunTransport()
    mm = seed.Mattermost("http://mm.test", transport=transport)
    printed: list[str] = []
    monkeypatch.setattr(seed, "_say", printed.append)
    result = seed.seed(
        mm,
        admin_password="Passw0rd!admin",
        demo_password="Passw0rd!demo",
        shared_secret="s3cret",
        llm_url="http://model-runner.docker.internal/engines/v1",
        llm_model="ai/qwen3.5:9b-q4_K_M",
        mcp_url="http://mcp:8002/mcp",
        rotate_token=False,
        download=lambda url: b"not-a-real-plugin",
    )
    return result, transport.calls, printed


def test_dry_run_makes_the_expected_calls_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    result, calls, _ = _run_dry(monkeypatch)
    writes = [(m, p) for m, p in calls if m != "GET"]
    assert writes == [
        ("POST", "/api/v4/users"),  # admin
        ("POST", "/api/v4/users/login"),
        ("POST", "/api/v4/custom_profile_attributes/fields"),  # sovrag_clearance
        ("POST", "/api/v4/custom_profile_attributes/fields"),  # sovrag_departments
        ("POST", "/api/v4/users"),  # analyst_dnd
        ("PATCH", f"/api/v4/users/{result.user_ids['analyst_dnd']}/custom_profile_attributes"),
        ("POST", "/api/v4/users"),  # auditor
        ("PATCH", f"/api/v4/users/{result.user_ids['auditor']}/custom_profile_attributes"),
        ("POST", "/api/v4/users"),  # public: no values to set
        ("POST", "/api/v4/plugins"),
        ("POST", f"/api/v4/plugins/{seed.PLUGIN_ID}/enable"),
        ("PUT", f"/plugins/{seed.PLUGIN_ID}/admin/config"),
        ("POST", f"/plugins/{seed.PLUGIN_ID}/agents"),
        ("POST", f"/api/v4/users/{result.bot_user_id}/tokens"),
    ]
    assert result.bot_token and result.shared_secret == "s3cret"


def test_the_plugin_upload_verifies_the_pinned_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    assert seed.PLUGIN_VERSION == "2.7.0"
    assert seed.PLUGIN_ASSET_URL.endswith(
        "/v2.7.0/mattermost-plugin-agents-v2.7.0-linux-amd64.tar.gz"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", seed.PLUGIN_SHA256)
    transport = seed.DryRunTransport()
    mm = seed.Mattermost("http://mm.test", transport=transport)
    monkeypatch.setattr(seed, "_say", lambda s: None)
    monkeypatch.setattr(seed, "PLUGIN_SHA256", "0" * 64)
    with pytest.raises(seed.SeedError, match="SHA-256"):
        seed.seed(
            mm,
            admin_password="a",
            demo_password="d",
            shared_secret="s",
            llm_url="u",
            llm_model="m",
            mcp_url="http://mcp:8002/mcp",
            rotate_token=False,
            download=lambda url: b"tampered",
        )


def test_static_header_map_is_empty_and_service_account_map_is_only_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _ = _run_dry(monkeypatch)
    transport_calls = seed.DryRunTransport.last_bodies
    config = transport_calls[("PUT", f"/plugins/{seed.PLUGIN_ID}/admin/config")]
    (server,) = config["mcp"]["servers"]
    assert server["name"] == "sovrag" and server["baseURL"] == "http://mcp:8002/mcp"
    assert server["headers"] == {}
    assert server["serviceAccountHeaders"] == {"Authorization": "Bearer s3cret"}
    assert [t["name"] for t in server["tool_configs"]] == ["search", "answer"]
    assert all(t["policy"] == "auto_run_in_dm" and t["enabled"] for t in server["tool_configs"])
    assert config["mcp"]["enabled"] is True
    (service,) = config["services"]
    assert service["type"] == "openaicompatible" and service["useResponsesAPI"] is False
    assert service["apiURL"] == "http://model-runner.docker.internal/engines/v1"
    assert service["defaultModel"] == "ai/qwen3.5:9b-q4_K_M"


def test_the_bot_is_direct_message_only_with_service_account_auth_and_both_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_dry(monkeypatch)
    agent = seed.DryRunTransport.last_bodies[("POST", f"/plugins/{seed.PLUGIN_ID}/agents")]
    assert agent["username"] == "sovrag"
    assert agent["channelAccessLevel"] == 3 and agent["userAccessLevel"] == 0
    assert agent["useServiceAccountAuth"] is True and agent["disableTools"] is False
    assert agent["enabledMCPTools"] == [
        {"server_origin": "http://mcp:8002/mcp", "tool_name": "search"},
        {"server_origin": "http://mcp:8002/mcp", "tool_name": "answer"},
    ]
    assert "verbatim" in agent["customInstructions"]


def test_attribute_fields_are_admin_managed_with_the_document_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_dry(monkeypatch)
    bodies = seed.DryRunTransport.all_bodies
    fields = [
        b for (m, p), b in bodies if (m, p) == ("POST", "/api/v4/custom_profile_attributes/fields")
    ]
    by_name = {f["name"]: f for f in fields}
    assert by_name["sovrag_clearance"]["type"] == "select"
    assert by_name["sovrag_departments"]["type"] == "multiselect"
    for f in fields:
        assert f["attrs"]["managed"] == "admin"
    assert [o["name"] for o in by_name["sovrag_clearance"]["attrs"]["options"]] == [
        "UNCLASSIFIED",
        "PROTECTED_A",
        "PROTECTED_B",
    ]
    assert [o["name"] for o in by_name["sovrag_departments"]["attrs"]["options"]] == [
        "CSE",
        "DND",
        "PS",
        "PSPC",
        "SSC",
        "TBS",
    ]


def test_secrets_are_redacted_in_dry_run_output(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _, printed = _run_dry(monkeypatch)
    text = "\n".join(printed)
    assert "s3cret" not in text and "Passw0rd" not in text
    assert result.bot_token is not None and result.bot_token not in text


class FakeMattermost:
    """Enough of a Mattermost to seed twice: users, fields, values, plugins, plugin config,
    agents and tokens live in dicts. Records every write."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.fields: list[dict[str, Any]] = []
        self.values: dict[str, dict[str, Any]] = {}
        self.plugins: dict[str, dict[str, Any]] = {
            "mattermost-ai": {"id": "mattermost-ai", "version": "2.6.1"}
        }
        self.active: set[str] = set()
        self.config: dict[str, Any] = {
            "services": [],
            "bots": [],
            "mcp": {"enabled": False, "servers": []},
        }
        self.agents: list[dict[str, Any]] = []
        self.tokens: dict[str, list[dict[str, Any]]] = {}
        self.writes: list[tuple[str, str]] = []
        self.n = 0

    def _id(self) -> str:
        self.n += 1
        return f"{self.n:026d}".replace("0", "a", 1)[-26:]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        body: Any = None
        if request.content and request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            body = json.loads(request.content)
        if method != "GET":
            self.writes.append((method, path))
        if path == "/api/v4/users" and method == "POST":
            uid = self._id()
            self.users[uid] = {"id": uid, "username": body["username"], "delete_at": 0}
            return httpx.Response(201, json=self.users[uid])
        if path.startswith("/api/v4/users/username/"):
            name = path.rsplit("/", 1)[1]
            for u in self.users.values():
                if u["username"] == name:
                    return httpx.Response(200, json=u)
            return httpx.Response(404, json={"message": "no such user"})
        if path == "/api/v4/users/login":
            for u in self.users.values():
                if u["username"] == body["login_id"]:
                    return httpx.Response(200, json=u, headers={"Token": "session-" + u["id"]})
            return httpx.Response(401, json={"message": "bad login"})
        if path == "/api/v4/custom_profile_attributes/fields":
            if method == "GET":
                return httpx.Response(200, json=self.fields)
            field = {**body, "id": self._id()}
            field["attrs"]["options"] = [{**o, "id": self._id()} for o in field["attrs"]["options"]]
            self.fields.append(field)
            return httpx.Response(201, json=field)
        if path.endswith("/custom_profile_attributes") and "/users/" in path:
            uid = path.split("/")[4]
            if method == "GET":
                return httpx.Response(200, json=self.values.get(uid, {}))
            self.values.setdefault(uid, {}).update(body)
            return httpx.Response(200, json=self.values[uid])
        if path == "/api/v4/plugins" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "active": [p for p in self.plugins.values() if p["id"] in self.active],
                    "inactive": [p for p in self.plugins.values() if p["id"] not in self.active],
                },
            )
        if path == "/api/v4/plugins" and method == "POST":
            self.plugins["mattermost-ai"] = {"id": "mattermost-ai", "version": seed.PLUGIN_VERSION}
            self.active.discard("mattermost-ai")
            return httpx.Response(201, json=self.plugins["mattermost-ai"])
        if path == "/api/v4/plugins/mattermost-ai/enable":
            self.active.add("mattermost-ai")
            return httpx.Response(200, json={"status": "OK"})
        if path == "/plugins/mattermost-ai/admin/config":
            if method == "GET":
                return httpx.Response(200, json=self.config)
            self.config = body
            return httpx.Response(200, json=self.config)
        if path == "/plugins/mattermost-ai/agents":
            if method == "GET":
                return httpx.Response(200, json=self.agents)
            uid = self._id()
            self.users[uid] = {
                "id": uid,
                "username": body["username"],
                "delete_at": 0,
                "is_bot": True,
            }
            agent = {**body, "id": self._id(), "botUserID": uid, "name": body["username"]}
            self.agents.append(agent)
            return httpx.Response(200, json=agent)
        if path.startswith("/plugins/mattermost-ai/agents/") and method == "PUT":
            aid = path.rsplit("/", 1)[1]
            for i, a in enumerate(self.agents):
                if a["id"] == aid:
                    self.agents[i] = {**a, **body}
                    return httpx.Response(200, json=self.agents[i])
            return httpx.Response(404, json={})
        if path.endswith("/tokens") and "/users/" in path:
            uid = path.split("/")[4]
            if method == "GET":
                return httpx.Response(200, json=self.tokens.get(uid, []))
            tok = {
                "id": self._id(),
                "token": "tok-" + self._id(),
                "user_id": uid,
                "description": body["description"],
            }
            self.tokens.setdefault(uid, []).append(tok)
            return httpx.Response(200, json=tok)
        return httpx.Response(500, json={"message": f"unexpected {method} {path}"})


def _seed_against(server: FakeMattermost, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Any:
    monkeypatch.setattr(seed, "_say", lambda s: None)
    mm = seed.Mattermost("http://mm.test", transport=httpx.MockTransport(server))
    args: dict[str, Any] = {
        "admin_password": "Passw0rd!admin",
        "demo_password": "Passw0rd!demo",
        "shared_secret": "s3cret",
        "llm_url": "http://model-runner.docker.internal/engines/v1",
        "llm_model": "ai/qwen3.5:9b-q4_K_M",
        "mcp_url": "http://mcp:8002/mcp",
        "rotate_token": False,
        "download": lambda url: b"plugin-bytes",
    }
    args.update(overrides)
    monkeypatch.setattr(seed, "PLUGIN_SHA256", seed.hashlib.sha256(b"plugin-bytes").hexdigest())
    return seed.seed(mm, **args)


def test_seeding_twice_changes_nothing_the_second_time(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeMattermost()
    first = _seed_against(server, monkeypatch)
    assert first.bot_token is not None
    first_writes = list(server.writes)
    server.writes.clear()
    second = _seed_against(server, monkeypatch)
    assert second.bot_token is None  # an existing token is kept, never re-read or replaced
    assert [w for w in server.writes if w[1] != "/api/v4/users/login"] == []
    assert len(first_writes) > 10


def test_the_demo_users_end_up_with_the_right_values(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeMattermost()
    result = _seed_against(server, monkeypatch)
    by_name = {f["name"]: f for f in server.fields}
    clr = by_name["sovrag_clearance"]
    dep = by_name["sovrag_departments"]
    clr_opt = {o["id"]: o["name"] for o in clr["attrs"]["options"]}
    dep_opt = {o["id"]: o["name"] for o in dep["attrs"]["options"]}

    def values(username: str) -> tuple[str | None, list[str]]:
        v = server.values.get(result.user_ids[username], {})
        return clr_opt.get(v.get(clr["id"])), sorted(dep_opt[i] for i in v.get(dep["id"], []))

    assert values("analyst_dnd") == ("PROTECTED_A", ["DND"])
    assert values("auditor") == ("PROTECTED_B", ["CSE", "DND", "PS", "PSPC", "SSC", "TBS"])
    assert values("public") == (None, [])


def test_rotate_token_makes_a_new_token_on_a_seeded_server(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeMattermost()
    first = _seed_against(server, monkeypatch)
    second = _seed_against(server, monkeypatch, rotate_token=True)
    assert second.bot_token is not None and second.bot_token != first.bot_token


def test_main_dry_run_prints_the_env_lines_with_placeholders(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = seed.main(
        ["--dry-run", "--shared-secret", "s3cret", "--admin-password", "x", "--demo-password", "y"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "MATTERMOST_BOT_TOKEN=" in out and "MCP_SHARED_SECRET=" in out
    assert "s3cret" not in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_seed_mattermost.py -q`
Expected: `FileNotFoundError` for `scripts/seed_mattermost.py` at collection.

- [ ] **Step 3: Write the script**

Create `scripts/seed_mattermost.py` (the `PLUGIN_SHA256` placeholder is replaced in Task 3):

```python
#!/usr/bin/env python3
"""Seed a Mattermost Entry for the sovrag demo, entirely through the REST API (ADR 0011).

Idempotent: every step checks before it writes, so running it again on a seeded server changes
nothing. What it creates: the admin (the first user, which Mattermost makes a system admin);
the two admin-managed attributes with the document vocabulary as options; the three demo users
and their attribute values; the Agents plugin at the pinned version, uploaded because the image
bundles an older one; the plugin's provider (Docker Model Runner) and its sovrag MCP server
entry, with the shared secret in the service-account header map and nothing in the static map;
the bot, restricted to direct messages, with both sovrag tools enabled; and a token for the bot
user, which the sovrag resolver uses to read attributes. It prints the two values to put in
.env. --dry-run prints the calls it would make against an empty server and touches nothing.

    uv run python scripts/seed_mattermost.py --url http://localhost:8065
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx2

PLUGIN_ID = "mattermost-ai"
PLUGIN_VERSION = "2.7.0"
PLUGIN_ASSET_URL = (
    "https://github.com/mattermost/mattermost-plugin-agents/releases/download/"
    f"v{PLUGIN_VERSION}/mattermost-plugin-agents-v{PLUGIN_VERSION}-linux-amd64.tar.gz"
)
PLUGIN_SHA256 = "REPLACED_IN_TASK_3"

CLEARANCES = ["UNCLASSIFIED", "PROTECTED_A", "PROTECTED_B"]
DEPARTMENTS = ["CSE", "DND", "PS", "PSPC", "SSC", "TBS"]
CLEARANCE_FIELD = "sovrag_clearance"
DEPARTMENTS_FIELD = "sovrag_departments"

# username -> (clearance, departments). Synthetic demo users against a public corpus (ADR 0003).
DEMO_USERS: dict[str, tuple[str | None, list[str]]] = {
    "analyst_dnd": ("PROTECTED_A", ["DND"]),
    "auditor": ("PROTECTED_B", list(DEPARTMENTS)),
    "public": (None, []),
}
ADMIN_USERNAME = "admin"
BOT_USERNAME = "sovrag"
BOT_DISPLAY_NAME = "sovrag"
TOOL_NAMES = ("search", "answer")
SERVICE_ID = "model-runner"
TOKEN_DESCRIPTION = "sovrag resolver"
CHANNEL_ACCESS_NONE = 3  # direct messages only
USER_ACCESS_ALL = 0
BOT_INSTRUCTIONS = (
    "You answer questions about Canadian federal public documents for the user you are talking"
    " to. Always call the answer tool with the user's question. Relay the tool's text and its"
    " citations verbatim, in full, and add nothing: no summary, no softening, no extra sources."
    " If the tool refuses, relay the refusal verbatim. Never guess, and never answer from memory."
)


class SeedError(Exception):
    pass


def _say(line: str) -> None:
    print(line)


@dataclass
class Result:
    shared_secret: str
    bot_user_id: str
    bot_token: str | None
    user_ids: dict[str, str] = field(default_factory=dict)


class Mattermost:
    """A thin REST client. Every call raises SeedError on a 4xx or 5xx."""

    def __init__(self, base_url: str, *, transport: httpx2.BaseTransport | None = None) -> None:
        self._client = httpx2.Client(
            base_url=base_url.rstrip("/"), timeout=120, transport=transport
        )
        self.token: str | None = None

    def call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        files: Any = None,
        data: Any = None,
        params: Any = None,
        ok: tuple[int, ...] = (200, 201),
    ) -> httpx2.Response:
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self._client.request(
            method, path, json=json, files=files, data=data, params=params, headers=headers
        )
        if response.status_code not in ok:
            raise SeedError(f"{method} {path}: HTTP {response.status_code} {response.text[:300]}")
        return response

    def get(self, path: str, **kw: Any) -> Any:
        return self.call("GET", path, **kw).json()


def _find_user(mm: Mattermost, username: str) -> dict[str, Any] | None:
    response = mm.call("GET", f"/api/v4/users/username/{username}", ok=(200, 404))
    return response.json() if response.status_code == 200 else None


def _ensure_user(mm: Mattermost, username: str, password: str) -> str:
    user = _find_user(mm, username)
    if user is not None:
        _say(f"user {username}: exists")
        return str(user["id"])
    created = mm.call(
        "POST",
        "/api/v4/users",
        json={"username": username, "email": f"{username}@example.test", "password": password},
    ).json()
    _say(f"user {username}: created")
    return str(created["id"])


def _login(mm: Mattermost, username: str, password: str) -> str:
    response = mm.call(
        "POST", "/api/v4/users/login", json={"login_id": username, "password": password}
    )
    mm.token = response.headers["Token"]
    return str(response.json()["id"])


def _ensure_field(mm: Mattermost, name: str, ftype: str, options: list[str]) -> dict[str, Any]:
    for existing in mm.get("/api/v4/custom_profile_attributes/fields"):
        if existing.get("name") == name:
            if existing.get("attrs", {}).get("managed") != "admin":
                raise SeedError(
                    f"field {name} exists but is not admin-managed; fix it in the System Console"
                )
            _say(f"field {name}: exists")
            return dict(existing)
    created = mm.call(
        "POST",
        "/api/v4/custom_profile_attributes/fields",
        json={
            "name": name,
            "type": ftype,
            "attrs": {"managed": "admin", "options": [{"name": o} for o in options]},
        },
    ).json()
    _say(f"field {name}: created ({ftype}, admin-managed)")
    return dict(created)


def _option_ids(fld: dict[str, Any]) -> dict[str, str]:
    return {o["name"]: o["id"] for o in fld.get("attrs", {}).get("options", [])}


def _ensure_values(
    mm: Mattermost,
    user_id: str,
    username: str,
    clearance_field: dict[str, Any],
    departments_field: dict[str, Any],
    clearance: str | None,
    departments: list[str],
) -> None:
    wanted: dict[str, Any] = {}
    if clearance is not None:
        wanted[clearance_field["id"]] = _option_ids(clearance_field)[clearance]
    if departments:
        ids = _option_ids(departments_field)
        wanted[departments_field["id"]] = [ids[d] for d in departments]
    if not wanted:
        _say(f"values {username}: none to set (least privilege)")
        return
    current = mm.get(f"/api/v4/users/{user_id}/custom_profile_attributes")
    if all(current.get(k) == v for k, v in wanted.items()):
        _say(f"values {username}: already set")
        return
    mm.call("PATCH", f"/api/v4/users/{user_id}/custom_profile_attributes", json=wanted)
    _say(f"values {username}: set clearance={clearance} departments={departments}")


def _ensure_plugin(mm: Mattermost, download: Callable[[str], bytes]) -> None:
    state = mm.get("/api/v4/plugins")
    active = {p["id"]: p for p in state.get("active", [])}
    inactive = {p["id"]: p for p in state.get("inactive", [])}
    installed = active.get(PLUGIN_ID) or inactive.get(PLUGIN_ID)
    if installed is None or installed.get("version") != PLUGIN_VERSION:
        _say(f"plugin {PLUGIN_ID}: downloading {PLUGIN_VERSION}")
        blob = download(PLUGIN_ASSET_URL)
        digest = hashlib.sha256(blob).hexdigest()
        if digest != PLUGIN_SHA256:
            raise SeedError(f"plugin asset SHA-256 mismatch: got {digest}, pinned {PLUGIN_SHA256}")
        mm.call(
            "POST",
            "/api/v4/plugins",
            files={"plugin": ("agents.tar.gz", blob, "application/gzip")},
            data={"force": "true"},
        )
        _say(f"plugin {PLUGIN_ID}: uploaded {PLUGIN_VERSION}")
        installed = {"version": PLUGIN_VERSION}
        active.pop(PLUGIN_ID, None)
    else:
        _say(f"plugin {PLUGIN_ID}: {PLUGIN_VERSION} present")
    if PLUGIN_ID not in active:
        mm.call("POST", f"/api/v4/plugins/{PLUGIN_ID}/enable")
        _say(f"plugin {PLUGIN_ID}: enabled")


def _plugin_config(
    shared_secret: str, llm_url: str, llm_model: str, mcp_url: str
) -> dict[str, Any]:
    return {
        "services": [
            {
                "id": SERVICE_ID,
                "name": "Docker Model Runner",
                "type": "openaicompatible",
                "apiURL": llm_url,
                "apiKey": "not-needed",
                "defaultModel": llm_model,
                "useResponsesAPI": False,
                "streamingTimeoutSeconds": 300,
            }
        ],
        "defaultBotName": BOT_USERNAME,
        "mcp": {
            "enabled": True,
            "servers": [
                {
                    "name": "sovrag",
                    "enabled": True,
                    "baseURL": mcp_url,
                    "headers": {},
                    "serviceAccountHeaders": {"Authorization": f"Bearer {shared_secret}"},
                    "tool_configs": [
                        {"name": name, "policy": "auto_run_in_dm", "enabled": True}
                        for name in TOOL_NAMES
                    ],
                }
            ],
        },
    }


def _ensure_plugin_config(mm: Mattermost, wanted: dict[str, Any]) -> None:
    current = mm.get(f"/plugins/{PLUGIN_ID}/admin/config")
    merged = {**current, **wanted, "mcp": {**current.get("mcp", {}), **wanted["mcp"]}}
    if all(current.get(k) == v for k, v in merged.items()):
        _say("plugin config: already set")
        return
    mm.call("PUT", f"/plugins/{PLUGIN_ID}/admin/config", json=merged)
    _say("plugin config: written (provider, MCP server, tool policies)")


def _agent_body(mcp_url: str) -> dict[str, Any]:
    return {
        "displayName": BOT_DISPLAY_NAME,
        "username": BOT_USERNAME,
        "serviceID": SERVICE_ID,
        "customInstructions": BOT_INSTRUCTIONS,
        "channelAccessLevel": CHANNEL_ACCESS_NONE,
        "channelIDs": [],
        "userAccessLevel": USER_ACCESS_ALL,
        "userIDs": [],
        "teamIDs": [],
        "adminUserIDs": [],
        "enabledMCPTools": [{"server_origin": mcp_url, "tool_name": name} for name in TOOL_NAMES],
        "autoEnableNewMCPTools": False,
        "mcpDynamicToolLoading": False,
        "useServiceAccountAuth": True,
        "model": "",
        "enableVision": False,
        "disableTools": False,
        "enabledNativeTools": [],
        "reasoningEnabled": False,
        "reasoningEffort": "",
        "thinkingBudget": 0,
        "structuredOutputEnabled": False,
        "maxToolTurns": 3,
    }


def _ensure_agent(mm: Mattermost, mcp_url: str) -> str:
    wanted = _agent_body(mcp_url)
    for agent in mm.get(f"/plugins/{PLUGIN_ID}/agents"):
        if agent.get("name") == BOT_USERNAME or agent.get("username") == BOT_USERNAME:
            drift = {k: v for k, v in wanted.items() if k != "username" and agent.get(k) != v}
            if drift:
                mm.call("PUT", f"/plugins/{PLUGIN_ID}/agents/{agent['id']}", json=wanted)
                _say(f"bot {BOT_USERNAME}: updated {sorted(drift)}")
            else:
                _say(f"bot {BOT_USERNAME}: exists")
            return str(agent["botUserID"])
    created = mm.call("POST", f"/plugins/{PLUGIN_ID}/agents", json=wanted).json()
    _say(f"bot {BOT_USERNAME}: created (direct messages only, service-account auth)")
    return str(created["botUserID"])


def _ensure_bot_token(mm: Mattermost, bot_user_id: str, rotate: bool) -> str | None:
    existing = [
        t
        for t in mm.get(f"/api/v4/users/{bot_user_id}/tokens")
        if t.get("description") == TOKEN_DESCRIPTION
    ]
    if existing and not rotate:
        _say(
            f"bot token: exists ({TOKEN_DESCRIPTION}); its value cannot be re-read. Pass --rotate-token for a new one"
        )
        return None
    created = mm.call(
        "POST", f"/api/v4/users/{bot_user_id}/tokens", json={"description": TOKEN_DESCRIPTION}
    ).json()
    _say("bot token: created")
    return str(created["token"])


def seed(
    mm: Mattermost,
    *,
    admin_password: str,
    demo_password: str,
    shared_secret: str,
    llm_url: str,
    llm_model: str,
    mcp_url: str,
    rotate_token: bool,
    download: Callable[[str], bytes],
) -> Result:
    _ensure_user(mm, ADMIN_USERNAME, admin_password)  # the first user is the system admin
    _login(mm, ADMIN_USERNAME, admin_password)
    clearance_field = _ensure_field(mm, CLEARANCE_FIELD, "select", CLEARANCES)
    departments_field = _ensure_field(mm, DEPARTMENTS_FIELD, "multiselect", DEPARTMENTS)
    user_ids: dict[str, str] = {}
    for username, (clearance, departments) in DEMO_USERS.items():
        user_ids[username] = _ensure_user(mm, username, demo_password)
        _ensure_values(
            mm,
            user_ids[username],
            username,
            clearance_field,
            departments_field,
            clearance,
            departments,
        )
    _ensure_plugin(mm, download)
    _ensure_plugin_config(mm, _plugin_config(shared_secret, llm_url, llm_model, mcp_url))
    bot_user_id = _ensure_agent(mm, mcp_url)
    token = _ensure_bot_token(mm, bot_user_id, rotate_token)
    return Result(
        shared_secret=shared_secret, bot_user_id=bot_user_id, bot_token=token, user_ids=user_ids
    )


class DryRunTransport(httpx2.BaseTransport):
    """Answers as an empty server would, records every call, and keeps the last body per route
    so tests can inspect what would have been written. Nothing is sent anywhere."""

    last_bodies: ClassVar[dict[tuple[str, str], Any]] = {}
    all_bodies: ClassVar[list[tuple[tuple[str, str], Any]]] = []

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._n = 0
        DryRunTransport.last_bodies = {}
        DryRunTransport.all_bodies = []

    def _id(self) -> str:
        self._n += 1
        return f"dryrun{self._n:020d}"[-26:]

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        body: Any = None
        if (
            request.headers.get("content-type", "").startswith("application/json")
            and request.content
        ):
            body = json.loads(request.content)
            DryRunTransport.last_bodies[(method, path)] = body
            DryRunTransport.all_bodies.append(((method, path), body))
        if method == "GET":
            if path.startswith("/api/v4/users/username/"):
                return httpx2.Response(404, json={})
            if path == "/api/v4/plugins":
                return httpx2.Response(200, json={"active": [], "inactive": []})
            if path.endswith("/admin/config"):
                return httpx2.Response(
                    200, json={"services": [], "bots": [], "mcp": {"enabled": False, "servers": []}}
                )
            if path.endswith("/custom_profile_attributes/fields"):
                return httpx2.Response(200, json=[])
            return httpx2.Response(200, json=[] if path.endswith(("/agents", "/tokens")) else {})
        if path == "/api/v4/users/login":
            return httpx2.Response(
                200, json={"id": self._id()}, headers={"Token": "dry-run-session"}
            )
        if path.endswith("/custom_profile_attributes/fields"):
            options = [{**o, "id": self._id()} for o in body["attrs"]["options"]]
            return httpx2.Response(
                201, json={**body, "id": self._id(), "attrs": {**body["attrs"], "options": options}}
            )
        if path.endswith("/agents"):
            return httpx2.Response(200, json={**body, "id": self._id(), "botUserID": self._id()})
        if path.endswith("/tokens"):
            return httpx2.Response(
                200, json={"id": self._id(), "token": "dry-run-token-" + self._id()}
            )
        return httpx2.Response(
            201 if method == "POST" else 200, json={"id": self._id(), "status": "OK"}
        )


def _download(url: str) -> bytes:
    with httpx2.Client(follow_redirects=True, timeout=600) as client:
        return client.get(url).content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url", default=os.environ.get("MATTERMOST_SITE_URL", "http://localhost:8065")
    )
    parser.add_argument(
        "--admin-password", default=os.environ.get("MATTERMOST_ADMIN_PASSWORD", "Passw0rd!admin")
    )
    parser.add_argument(
        "--demo-password", default=os.environ.get("MATTERMOST_DEMO_PASSWORD", "Passw0rd!demo")
    )
    parser.add_argument(
        "--shared-secret", default=os.environ.get("MCP_SHARED_SECRET") or secrets.token_urlsafe(32)
    )
    parser.add_argument("--llm-url", default="http://model-runner.docker.internal/engines/v1")
    parser.add_argument("--llm-model", default=os.environ.get("LLM_MODEL", "ai/qwen3.5:9b-q4_K_M"))
    parser.add_argument("--mcp-url", default="http://mcp:8002/mcp")
    parser.add_argument(
        "--rotate-token", action="store_true", help="Make a new bot token even if one exists"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the calls for an empty server; send nothing"
    )
    args = parser.parse_args(argv)

    transport = DryRunTransport() if args.dry_run else None
    mm = Mattermost(args.url, transport=transport)
    try:
        result = seed(
            mm,
            admin_password=args.admin_password,
            demo_password=args.demo_password,
            shared_secret=args.shared_secret,
            llm_url=args.llm_url,
            llm_model=args.llm_model,
            mcp_url=args.mcp_url,
            rotate_token=args.rotate_token,
            download=(lambda url: b"") if args.dry_run else _download,
        )
    except SeedError as exc:
        print(f"seed: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        for method, path in transport.calls if transport else []:
            print(f"  {method} {path}")
        print("\nPut in .env (values redacted in dry run):")
        print("MATTERMOST_URL=http://mattermost:8065")
        print("MATTERMOST_BOT_TOKEN=<created on a real run>")
        print("MCP_SHARED_SECRET=<your --shared-secret>")
        print("MCP_IDENTITY=mattermost")
        return 0
    print("\nPut in .env, then start the MCP server in mattermost mode:")
    print("MATTERMOST_URL=http://mattermost:8065")
    if result.bot_token:
        print(f"MATTERMOST_BOT_TOKEN={result.bot_token}")
    else:
        print("MATTERMOST_BOT_TOKEN=<unchanged; pass --rotate-token to make a new one>")
    print(f"MCP_SHARED_SECRET={result.shared_secret}")
    print("MCP_IDENTITY=mattermost")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Notes for the implementer:
- `_say` is the one output channel for progress, so tests can capture it and redaction is checked in one place. Nothing in a `_say` line may include a password, the secret or a token.
- The plugin upload uses multipart with `force=true`, which replaces the bundled 2.6.1. The upload endpoint needs `MM_PLUGINSETTINGS_ENABLEUPLOADS=true`, set in Task 1.
- The `X-Requested-With` header is what Mattermost expects on requests to plugin routes made with a session token; it is harmless on the core API.
- If the real 11.11.0 server rejects any body key the plugin source showed (for example an unknown agent field), the live run in Task 6 is where that surfaces; fix the body and the fake together.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_seed_mattermost.py -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src scripts`
Expected: all pass except `test_the_plugin_upload_verifies_the_pinned_asset`, which fails on the `[0-9a-f]{64}` check until Task 3 fills the hash. If mypy complains about `scripts/` not being a package, add `scripts` to the mypy `files` list only if `mypy src scripts` does not work as a direct invocation.

- [ ] **Step 5: Commit**

```bash
git add scripts/seed_mattermost.py tests/test_seed_mattermost.py
git commit -m "Seed Mattermost for the demo through the REST API, with a dry run"
```

---

### Task 3: Pin the plugin asset hash

**Files:**
- Modify: `scripts/seed_mattermost.py` (`PLUGIN_SHA256`)

- [ ] **Step 1: Download the asset and compute the hash**

```bash
curl -sL -o /tmp/agents-2.7.0.tar.gz "https://github.com/mattermost/mattermost-plugin-agents/releases/download/v2.7.0/mattermost-plugin-agents-v2.7.0-linux-amd64.tar.gz"
shasum -a 256 /tmp/agents-2.7.0.tar.gz
tar -tzf /tmp/agents-2.7.0.tar.gz | head -3
```

Expected: a 26 MB file whose listing starts with `mattermost-ai/plugin.json`. Record the hash.

- [ ] **Step 2: Put it in the script and run the tests**

Replace `PLUGIN_SHA256 = "REPLACED_IN_TASK_3"` with the 64-hex value. Add a comment above it: `# SHA-256 of the release asset above, computed from the download.`

Run: `uv run pytest tests/test_seed_mattermost.py -q`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add scripts/seed_mattermost.py
git commit -m "Pin the Agents plugin 2.7.0 asset by SHA-256"
```

---

### Task 4: The walkthrough

**Files:**
- Create: `docs/mattermost-demo.md`

- [ ] **Step 1: Write it**

````markdown
# Mattermost demo: the same question, three answers

Three demo users ask the sovrag bot the same question in a direct message and get three
different answers, because Mattermost tells sovrag who is asking and sovrag's database only
returns what that person may see. Nothing leaves the machine: Mattermost, the Agents plugin,
the model, the embedder, the MCP server and Postgres all run locally.

Everything here is synthetic. The corpus is public Government of Canada documents; the
classification labels, clearances, departments and users are invented so the entitlement model
has something to enforce (ADR 0003). No protected information was used.

## Prerequisites

- Docker Desktop with Model Runner enabled, and the two models pulled (`docker compose up`
  does that on first use). Mattermost publishes amd64 images only, so on Apple silicon it runs
  under emulation; allow a minute for the first boot.
- The corpus indexed: `uv run sovrag ingest` once.
- Mattermost Entry needs no licence key. It caps message history at 10,000 and is meant for
  small teams; see ADR 0011.

## Setup

```
docker compose --profile mattermost up -d            # postgres, the db-init step, Mattermost
uv run python scripts/seed_mattermost.py             # users, attributes, plugin, bot, token
```

The seed script prints four lines for `.env`: `MATTERMOST_URL`, `MATTERMOST_BOT_TOKEN`,
`MCP_SHARED_SECRET` and `MCP_IDENTITY=mattermost`. Put them in `.env`, then:

```
docker compose --profile mattermost --profile mcp up -d mcp
docker compose --profile mcp exec mcp tail -f /app/audit/audit.jsonl   # keep this open
```

Open http://localhost:8065. Sign in as `admin` (password `Passw0rd!admin` unless you changed it)
once to create a team and add the three users to it; that is the one manual step, because a
team is a workspace choice, not configuration. Then sign in as each demo user in a private
window and open a direct message with `@sovrag`.

## The users

| user | clearance | departments | sees | never sees |
|---|---|---|---|---|
| `analyst_dnd` | PROTECTED_A | DND | departmental plans, policies, the DND results report | other departments' results reports, Auditor General reports |
| `auditor` | PROTECTED_B | all six | everything | nothing |
| `public` | UNCLASSIFIED | none | departmental plans, policies | every results report, every Auditor General report |

Password for all three: `Passw0rd!demo`. The attributes are admin-managed: a user cannot change
their own (ADR 0010).

## The beats

1. **`analyst_dnd` asks:** "What did National Defence report as its results for 2024-25?"
   The bot answers with citations into the DND Departmental Results Report.
2. **`public` asks the same.** The bot refuses: the results reports are PROTECTED_A and `public`
   is unclassified. The refusal says nothing about what exists; the audit tail shows
   `retrieved_chunk_ids: []` and the filter that was applied.
3. **`auditor` asks:** "What did the Auditor General find about ArriveCAN?" An answer with
   citations into the Auditor General's report.
4. **`analyst_dnd` asks the same.** Refused: the Auditor General reports are PROTECTED_B.
5. **The admin raises `analyst_dnd` to PROTECTED_B** in System Console, User Management, Users,
   `analyst_dnd`, User Attributes. `analyst_dnd` asks again and gets the answer. No restart,
   no cache: the resolver reads the attributes on every question.

What each beat proves: 1 and 3, retrieval and grounded answers work through Mattermost; 2 and
4, the same question gives a different answer to a different person, decided in SQL from the
principal the server bound, not by the model; 5, entitlement is managed where users are managed
and takes effect on the next question.

## What to look at in the audit tail

Each question writes a `resolve` line (the Mattermost user id, the attribute snapshot, the
principal it became, or the reason it was least privilege) and an `answer` line (the principal,
the rendered filter, the chunk ids retrieved and cited, `refused`) that share a `request_id`.
A refusal and an outage look different: an outage is a tool error and writes no `answer` line.

## Checks

- The `mattermost` role cannot open the sovrag database:
  `docker compose exec postgres psql -U mattermost -d sovrag -c 'select 1'` fails with
  "permission denied for database sovrag".
- A request without the shared secret gets 401 before anything runs:
  `curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8002/mcp -H 'Content-Type: application/json' -d '{}'`
  prints `401`.
- The plugin's MCP server entry has an empty static header map and only `Authorization` in the
  service-account map; `tests/test_seed_mattermost.py` asserts it, because a header named
  `X-Mattermost-UserID` in either map would override every user's identity (ADR 0009).

## Reset

`docker compose --profile mattermost --profile mcp down -v` removes Mattermost's data and the
audit volume. The sovrag index is in the `postgres` volume; `down -v` removes that too, so
re-run `uv run sovrag ingest` afterwards.
````

Fix the two question strings if the live run in Task 6 finds better ones; the beats must use questions the corpus answers.

- [ ] **Step 2: Format and commit**

```bash
uv run ruff format docs
git add docs/mattermost-demo.md
git commit -m "Write the Mattermost demo walkthrough"
```

---

### Task 5: README, CLAUDE.md, architecture, .env.example

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `docs/architecture.md`, `.env.example`

- [ ] **Step 1: README**

Status line becomes:

```markdown
Status: corpus indexed; vector, keyword and hybrid retrieval, each filtered by the principal's entitlement in SQL; grounded answers with citations and refusal from the CLI, over MCP, and in Mattermost direct messages.
```

Replace the paragraph beginning `In Mattermost mode each direct message runs as the Mattermost user asking` with:

```markdown
In Mattermost mode each direct message runs as the Mattermost user asking, with entitlement read from admin-managed user attributes. `docker compose --profile mattermost up -d` and `uv run python scripts/seed_mattermost.py` set it up; `docs/mattermost-demo.md` is the walkthrough. Prerequisites: Docker Desktop with Model Runner, and amd64 emulation on Apple silicon for the Mattermost image. Mattermost Entry needs no licence key (ADR 0011).
```

- [ ] **Step 2: CLAUDE.md**

Commands: add after the `serve-mcp` line:

```markdown
- docker compose --profile mattermost up -d && uv run python scripts/seed_mattermost.py   # demo
```

and change the last line to `- Not implemented yet: sovrag serve, sovrag eval.`

- [ ] **Step 3: architecture.md**

Status line: `Status: the corpus pipeline, all three retrieval modes with entitlement filtering in SQL, grounded generation with citations and refusal, the MCP server, and the Mattermost demo profile exist. The HTTP API follows.`

Under "## Deployment", add a paragraph:

```markdown
The `mattermost` profile adds Mattermost Entry (the enterprise image with no licence key) on the same Postgres with its own role and database, and a one-shot service that creates them and revokes the default `PUBLIC` access to the sovrag database. `scripts/seed_mattermost.py` configures everything through REST: attributes, users, the Agents plugin at its pinned version, the plugin's provider and MCP server entry, the bot, and the bot token the resolver uses. See ADR 0011 and `docs/mattermost-demo.md`.
```

- [ ] **Step 4: .env.example**

After the `MATTERMOST_BOT_TOKEN=` line add:

```
# The seed script prints MATTERMOST_URL, MATTERMOST_BOT_TOKEN and MCP_SHARED_SECRET for the
# mattermost profile; inside Compose the URL is http://mattermost:8065.
```

- [ ] **Step 5: Check and commit**

```bash
uv run ruff format docs
uv run ruff check . && uv run ruff format --check . && uv run mypy src scripts && uv run pytest -q
git add README.md CLAUDE.md docs/architecture.md .env.example
git commit -m "Describe the Mattermost demo profile"
```

---

### Task 6: The live run

**Files:**
- Modify: `docs/adr/0011-mattermost-in-compose.md` ("Not yet verified" list), `docs/mattermost-demo.md` if the questions change, `scripts/seed_mattermost.py` and its fake if the real server rejects a body.

- [ ] **Step 1: Bring it up and seed**

```bash
docker compose --profile mattermost up -d
until curl -sf http://localhost:8065/api/v4/system/ping >/dev/null; do sleep 3; done
uv run python scripts/seed_mattermost.py
```

Expected: every line reports created; the four `.env` lines print. Put them in `.env`.

- [ ] **Step 2: Start the MCP server in mattermost mode and confirm the gate**

```bash
docker compose --profile mattermost --profile mcp up -d --build mcp
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8002/mcp -H 'Content-Type: application/json' -d '{}'
docker compose exec postgres psql -U mattermost -d sovrag -c 'select 1' 2>&1 | tail -1
```

Expected: `401`, and `permission denied for database "sovrag"`.

- [ ] **Step 3: Create the team and run the beats**

Sign in as admin at http://localhost:8065, create a team, add the three users. In private windows sign in as each and DM `@sovrag`. Run beats 1 to 5 while tailing the audit log. Record for each beat: the answer or refusal text, the citations, and the `resolve` and `answer` lines.

If the bot does not call the tool (answers from the model instead, or errors), check in order: the plugin's server logs for the MCP connection (`docker compose logs mattermost | grep -i mcp`), a 401 in the mcp logs (secret mismatch), a 421 (allowed hosts), then the model. If the model will not produce tool calls, try `--llm-model` with a larger local model as the plugin's provider only; sovrag's generator is unaffected.

- [ ] **Step 4: Record the result in ADR 0011**

Move the first "Not yet verified" bullet into the verified table with what was observed: the model, whether tool calling worked, and the answer latency for one question. Keep the two Entry bullets as not verified unless observed. Commit:

```bash
git add docs/adr/0011-mattermost-in-compose.md docs/mattermost-demo.md scripts/seed_mattermost.py tests/test_seed_mattermost.py
git commit -m "Record the live Mattermost run in ADR 0011"
```

- [ ] **Step 5: Tear down**

```bash
docker compose --profile mattermost --profile mcp stop mattermost mattermost-db-init mcp
```

Leave the volumes so the demo can be replayed.

---

## Pull request

Push `mattermost-demo` and open a pull request against `main` titled "Run the Mattermost demo from Compose and one seed script". The body covers the profile, the db-init step and the revoke, the seed script and its dry run, the plugin 2.7.0 upload and hash, the header-map assertion, the walkthrough, and the live-run result including whether tool calling worked with the local model. Wait for `ci` and `claude-review`; squash-merge with `gh pr merge --squash --delete-branch`.

## Self-review

- Spec coverage: image and platform (Task 1); one-shot db service with the revoke (Task 1); plugin 2.7.0 upload with a recorded hash (Tasks 2 and 3); one openaicompatible service on Model Runner with the Responses API off (Task 2); bot with `channelAccessLevel` none, tools on, verbatim instructions (Task 2); MCP server entry with service-account bearer, both tools `auto_run_in_dm` (Task 2); seed idempotent, `--dry-run`, prints the two values (Task 2); the demo user table (Tasks 2 and 4); walkthrough beats and audit tail (Task 4); documentation list (Task 5); tests listed in the spec for the seed script (Task 2); the demo's manual checks (Task 4 and Task 6).
- Names used across tasks: `seed`, `Mattermost`, `DryRunTransport`, `SeedError`, `PLUGIN_SHA256`, `PLUGIN_ASSET_URL`, `_say`, `Result.user_ids`, `mattermost-db-init` are spelled the same everywhere.
