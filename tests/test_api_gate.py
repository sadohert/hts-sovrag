"""The binding layer: one principal in static mode; bearer plus header in gateway mode."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import httpx2 as httpx
import pytest

from sovrag.api.gate import GatewayGate, StaticBinding
from sovrag.auth import Clearance, Principal

SECRET = "gateway-shared-secret"
DND = Principal("analyst_dnd", Clearance.PROTECTED_A, frozenset({"DND"}))
AUD = Principal("auditor", Clearance.PROTECTED_B, frozenset({"DND", "PS"}))
PRINCIPALS = {"analyst_dnd": DND, "auditor": AUD}

Scope = MutableMapping[str, Any]


async def echo(
    scope: Scope, receive: Callable[[], Awaitable[Any]], send: Callable[[Any], Awaitable[None]]
) -> None:
    """Innermost app: reports the bound principal id (or null) and the method/path."""
    who = scope.get("state", {}).get("principal")
    body = json.dumps(
        {"principal": who.id if who else None, "method": scope["method"], "path": scope["path"]}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _get(
    app: Any, path: str = "/query", method: str = "POST", **headers: str
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        return await client.request(method, path, headers=headers)


@pytest.mark.asyncio
async def test_static_binding_binds_one_principal_and_ignores_headers() -> None:
    app = StaticBinding(echo, DND)
    r = await _get(app, **{"X-Sovrag-Principal": "auditor", "Authorization": "Bearer whatever"})
    assert r.status_code == 200 and r.json()["principal"] == "analyst_dnd"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": f"Bearer {SECRET}"},
        {"X-Sovrag-Principal": "analyst_dnd"},
        {"Authorization": f"Bearer {SECRET}x", "X-Sovrag-Principal": "analyst_dnd"},
        {"Authorization": f"Basic {SECRET}", "X-Sovrag-Principal": "analyst_dnd"},
        {"Authorization": SECRET, "X-Sovrag-Principal": "analyst_dnd"},
        {"Authorization": f"Bearer {SECRET}", "X-Sovrag-Principal": "nobody"},
        {"Authorization": f"Bearer {SECRET}", "X-Sovrag-Principal": ""},
        {"Authorization": f"Bearer {SECRET}", "X-Sovrag-Principal": "anonymous"},
    ],
)
async def test_gateway_rejects_bad_or_missing_credentials(headers: dict[str, str]) -> None:
    app = GatewayGate(echo, secret=SECRET, principals=PRINCIPALS)
    r = await _get(app, **headers)
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorised"}


@pytest.mark.asyncio
async def test_gateway_binds_the_named_principal_per_request() -> None:
    app = GatewayGate(echo, secret=SECRET, principals=PRINCIPALS)
    first = await _get(
        app, **{"Authorization": f"Bearer {SECRET}", "X-Sovrag-Principal": "analyst_dnd"}
    )
    second = await _get(
        app, **{"Authorization": f"Bearer {SECRET}", "X-Sovrag-Principal": " auditor "}
    )
    assert first.json()["principal"] == "analyst_dnd"
    assert second.json()["principal"] == "auditor"


@pytest.mark.asyncio
async def test_healthz_get_passes_the_gate_unbound_but_post_does_not() -> None:
    app = GatewayGate(echo, secret=SECRET, principals=PRINCIPALS)
    ok = await _get(app, "/healthz", "GET")
    assert ok.status_code == 200 and ok.json()["principal"] is None
    blocked = await _get(app, "/healthz", "POST")
    assert blocked.status_code == 401


@pytest.mark.asyncio
async def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    await GatewayGate(inner, secret=SECRET, principals=PRINCIPALS)({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    await StaticBinding(inner, DND)({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan", "lifespan"]
