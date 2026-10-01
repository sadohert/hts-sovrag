"""Binding the principal for the HTTP API, before routing (ADR 0009, ADR 0015).

Two pure-ASGI middlewares put the principal on `scope["state"]["principal"]`, where the one
handler reads it. StaticBinding uses the principal configured at startup. GatewayGate trusts
an authenticating gateway: the request must carry the shared secret as a bearer and a known
principal id in X-Sovrag-Principal; anything else is 401, never a downgrade. GET /healthz
passes unbound so a load balancer can probe without the secret.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from enum import StrEnum
from typing import Any

from starlette.responses import JSONResponse

from sovrag.auth import Principal

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

PRINCIPAL_HEADER = "x-sovrag-principal"
HEALTH_PATH = "/healthz"
UNAUTHORISED = {"detail": "unauthorised"}


class Identity(StrEnum):
    static = "static"
    gateway = "gateway"


def _bind(scope: Scope, principal: Principal) -> None:
    scope.setdefault("state", {})["principal"] = principal


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


class StaticBinding:
    """Every HTTP request runs as the one principal configured at startup."""

    def __init__(self, app: ASGIApp, principal: Principal) -> None:
        self._app = app
        self._principal = principal

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            _bind(scope, self._principal)
        await self._app(scope, receive, send)


class GatewayGate:
    """Bearer plus X-Sovrag-Principal, resolved against the principals loaded at startup."""

    def __init__(self, app: ASGIApp, *, secret: str, principals: Mapping[str, Principal]) -> None:
        self._app = app
        self._secret = secret
        self._principals = principals

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if scope["method"] == "GET" and scope["path"] == HEALTH_PATH:
            await self._app(scope, receive, send)
            return
        principal = self._authorise(_headers(scope))
        if principal is None:
            await JSONResponse(UNAUTHORISED, status_code=401)(scope, receive, send)
            return
        _bind(scope, principal)
        await self._app(scope, receive, send)

    def _authorise(self, headers: Mapping[str, str]) -> Principal | None:
        scheme, _, token = headers.get("authorization", "").strip().partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), self._secret):
            return None
        return self._principals.get(headers.get(PRINCIPAL_HEADER, "").strip())
