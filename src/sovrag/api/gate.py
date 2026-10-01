"""Binding the principal for the HTTP API, before routing (ADR 0009, ADR 0015).

Two pure-ASGI middlewares put the principal on `scope["state"]["principal"]`, where the one
handler reads it. StaticBinding uses the principal configured at startup. GatewayGate trusts
an authenticating gateway: the request must carry the shared secret as a bearer and a known
principal id in X-Sovrag-Principal, each sent once; anything else is 401, never a downgrade.
GET and HEAD /healthz pass unbound so a load balancer can probe without the secret.
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


def _headers(scope: Scope) -> dict[str, str] | None:
    """Header map, or None when a credential header is repeated (last-wins would let a second
    header override the first)."""
    out: dict[str, str] = {}
    for k, v in scope.get("headers", []):
        name = k.decode("latin-1").lower()
        if name in out and name in (PRINCIPAL_HEADER, "authorization"):
            return None
        out[name] = v.decode("latin-1")
    return out


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
        if not secret:
            raise ValueError("API_SHARED_SECRET is required in gateway mode")
        self._app = app
        self._secret = secret.encode("utf-8")
        self._principals = principals

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if scope["method"] in ("GET", "HEAD") and scope["path"] == HEALTH_PATH:
            await self._app(scope, receive, send)
            return
        headers = _headers(scope)
        principal = self._authorise(headers) if headers is not None else None
        if principal is None:
            await JSONResponse(UNAUTHORISED, status_code=401)(scope, receive, send)
            return
        _bind(scope, principal)
        await self._app(scope, receive, send)

    def _authorise(self, headers: Mapping[str, str]) -> Principal | None:
        scheme, _, token = headers.get("authorization", "").strip().partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.strip().encode("utf-8"), self._secret
        ):
            return None
        return self._principals.get(headers.get(PRINCIPAL_HEADER, "").strip())
