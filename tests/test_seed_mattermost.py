"""The Mattermost seed script: what it would do on a fresh server, and that it changes nothing
on a server that is already seeded."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
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
    sys.modules[spec.name] = module  # dataclasses resolve annotations through the registry
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
        download=None,
    )
    return result, transport.calls, printed


def test_dry_run_makes_the_expected_calls_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    result, calls, _ = _run_dry(monkeypatch)
    writes = [(m, p) for m, p in calls if m != "GET"]
    assert writes == [
        ("POST", "/api/v4/users/login"),  # 401: no admin yet
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


def test_static_header_map_is_only_authorization_and_service_account_map_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _ = _run_dry(monkeypatch)
    transport_calls = seed.DryRunTransport.last_bodies
    config = transport_calls[("PUT", f"/plugins/{seed.PLUGIN_ID}/admin/config")]
    (server,) = config["mcp"]["servers"]
    assert server["name"] == "sovrag" and server["baseURL"] == "http://mcp:8002/mcp"
    assert server["headers"] == {"Authorization": "Bearer s3cret"}
    assert server["serviceAccountHeaders"] == {}
    assert [t["name"] for t in server["tool_configs"]] == ["search", "answer"]
    assert all(t["policy"] == "auto_run_in_dm" and t["enabled"] for t in server["tool_configs"])
    assert config["mcp"]["enabled"] is True
    (service,) = config["services"]
    assert service["type"] == "openaicompatible" and service["useResponsesAPI"] is False
    assert service["apiURL"] == "http://model-runner.docker.internal/engines/v1"
    assert service["defaultModel"] == "ai/qwen3.5:9b-q4_K_M"


def test_the_bot_is_direct_message_only_with_per_user_connections_and_both_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_dry(monkeypatch)
    agent = seed.DryRunTransport.last_bodies[("POST", f"/plugins/{seed.PLUGIN_ID}/agents")]
    assert agent["username"] == "sovrag"
    assert agent["channelAccessLevel"] == 3 and agent["userAccessLevel"] == 0
    assert agent["useServiceAccountAuth"] is False and agent["disableTools"] is False
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
            stored = json.loads(json.dumps(body))
            stored["mcp"]["servers"][0]["serviceAccountHeaders"] = None  # {} is stored as null
            self.config = {**stored, "allowUnsafeLinks": False}  # and adds defaults
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
            stored = {k: (None if v == [] else v) for k, v in body.items()}  # as the server does
            stored.pop("username")
            agent = {**stored, "id": self._id(), "botUserID": uid, "name": body["username"]}
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
