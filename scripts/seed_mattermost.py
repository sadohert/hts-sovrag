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
# SHA-256 of the release asset above, computed from the download (26,206,412 bytes).
PLUGIN_SHA256 = "72fdbd268342928a9c6ac2ced26dcd58b088f029c642359ab0c6c85a0edc8bfd"

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


def _ensure_admin(mm: Mattermost, password: str) -> str:
    """Log in as the admin, creating the account first if it does not exist yet. Nothing can be
    looked up before a session exists, and the first account Mattermost creates is a system
    admin, so this is the one step that starts from an unauthenticated call."""
    body = {"login_id": ADMIN_USERNAME, "password": password}
    response = mm.call("POST", "/api/v4/users/login", json=body, ok=(200, 401))
    if response.status_code == 401:
        mm.call(
            "POST",
            "/api/v4/users",
            json={
                "username": ADMIN_USERNAME,
                "email": f"{ADMIN_USERNAME}@example.test",
                "password": password,
            },
        )
        _say(f"user {ADMIN_USERNAME}: created (system admin)")
        response = mm.call("POST", "/api/v4/users/login", json=body)
    else:
        _say(f"user {ADMIN_USERNAME}: exists")
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


def _ensure_plugin(mm: Mattermost, download: Callable[[str], bytes] | None) -> None:
    """download is None in a dry run: nothing is fetched or hashed, and an empty upload is
    recorded so the call sequence is complete."""
    state = mm.get("/api/v4/plugins")
    active = {p["id"]: p for p in state.get("active", [])}
    inactive = {p["id"]: p for p in state.get("inactive", [])}
    installed = active.get(PLUGIN_ID) or inactive.get(PLUGIN_ID)
    if installed is None or installed.get("version") != PLUGIN_VERSION:
        blob = b""
        if download is not None:
            _say(f"plugin {PLUGIN_ID}: downloading {PLUGIN_VERSION}")
            blob = download(PLUGIN_ASSET_URL)
            digest = hashlib.sha256(blob).hexdigest()
            if digest != PLUGIN_SHA256:
                raise SeedError(
                    f"plugin asset SHA-256 mismatch: got {digest}, pinned {PLUGIN_SHA256}"
                )
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


def _matches(current: Any, wanted: Any) -> bool:
    """True when everything in `wanted` is already in `current`. The server adds default keys
    and stores an empty list or map as null, so this is a subset check where None, [] and {}
    are equal."""
    if isinstance(wanted, dict):
        current = current or {}
        return isinstance(current, dict) and all(
            _matches(current.get(k), v) for k, v in wanted.items()
        )
    if isinstance(wanted, list):
        current = current or []
        return (
            isinstance(current, list)
            and len(current) == len(wanted)
            and all(_matches(c, w) for c, w in zip(current, wanted, strict=True))
        )
    return bool(current == wanted or (not current and not wanted))


def _ensure_plugin_config(mm: Mattermost, wanted: dict[str, Any]) -> None:
    current = mm.get(f"/plugins/{PLUGIN_ID}/admin/config")
    merged = {**current, **wanted, "mcp": {**current.get("mcp", {}), **wanted["mcp"]}}
    if _matches(current, wanted):
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
            drift = {
                k: v for k, v in wanted.items() if k != "username" and not _matches(agent.get(k), v)
            }
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
            f"bot token: exists ({TOKEN_DESCRIPTION}); its value cannot be re-read."
            " Pass --rotate-token for a new one"
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
    download: Callable[[str], bytes] | None,
) -> Result:
    _ensure_admin(mm, admin_password)
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
        self._admin_created = False
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
            if not self._admin_created:
                return httpx2.Response(401, json={"message": "no such user"})
            return httpx2.Response(
                200, json={"id": self._id()}, headers={"Token": "dry-run-session"}
            )
        if path == "/api/v4/users":
            self._admin_created = True
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
            download=None if args.dry_run else _download,
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
