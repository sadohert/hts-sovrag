"""MCP server: search and answer over Streamable HTTP, with the principal bound by the server.

Two identity modes, chosen by MCP_IDENTITY with no default and no fallback (ADR 0009):

- static: PRINCIPAL is resolved from the principals file when the app is built and every tool
  call runs as that principal.
- mattermost: an ASGI gate in front of the MCP app answers 401 to any request without the
  shared-secret bearer or a well-formed X-Mattermost-UserID. The tool handler then resolves
  that user's principal once per tool call (not per HTTP request, since one call is several)
  and writes a `resolve` audit event. A resolution failure is a tool error that retrieves
  nothing; it is never a downgrade.

Neither tool has a principal parameter. Entitlement filtering happens in SQL inside the search
functions and answer(); this module only binds and reports.
"""

from __future__ import annotations

import hmac
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

from sovrag.audit import write_event
from sovrag.auth import (
    MattermostResolver,
    Principal,
    Resolution,
    ResolutionError,
    resolve_principal,
)
from sovrag.config import Settings
from sovrag.generate import MAX_K, GenerationError
from sovrag.generate.answer import answer as generate_answer
from sovrag.generate.answer import sources_markdown
from sovrag.index.embed import Embedder
from sovrag.index.store import connect
from sovrag.retrieve import Mode, entitlement_for, search_hybrid

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

USER_ID_HEADER = "x-mattermost-userid"
ENTITLEMENT_ERROR = "entitlement could not be determined"
_USER_ID = re.compile(r"^[a-z0-9]{26}$")
_NO_PRINCIPAL_ARG = (
    " The principal is bound by the server from the authenticated connection; there is no way"
    " to name or change it here."
)


class Identity(StrEnum):
    static = "static"
    mattermost = "mattermost"


class Resolver(Protocol):
    def resolve(self, user_id: str) -> Resolution: ...


@dataclass(frozen=True)
class SovragMcp:
    server: MCPServer
    app: ASGIApp
    identity: Identity


@dataclass(frozen=True)
class Binding:
    principal: Principal
    request_id: str


class _Gate:
    """Pure ASGI: every HTTP request must carry the bearer and a well-formed user id.

    Lifespan and other non-HTTP scopes pass straight through. The comparison is constant time
    so the secret cannot be guessed byte by byte from response timing."""

    def __init__(self, app: ASGIApp, secret: str) -> None:
        self._app = app
        self._secret = secret.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self._authorised(scope):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _authorised(self, scope: Scope) -> bool:
        headers: dict[str, str] = {}
        for raw_name, raw_value in scope["headers"]:
            name = raw_name.decode("latin-1").lower()
            # A repeated credential header is rejected: last-wins would let a second copy,
            # added by a proxy or a misconfigured header map, replace the plugin's own.
            if name in headers and name in ("authorization", USER_ID_HEADER):
                return False
            headers[name] = raw_value.decode("latin-1")
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or not hmac.compare_digest(token.encode(), self._secret):
            return False
        return _USER_ID.match(headers.get(USER_ID_HEADER, "")) is not None


class _Binder:
    """Turns a tool call's context into the principal it runs as, once per call."""

    def __init__(
        self,
        identity: Identity,
        *,
        principal: Principal | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._identity = identity
        self._principal = principal
        self._resolver = resolver

    def bind(self, ctx: Context) -> Binding:
        request_id = uuid.uuid4().hex
        if self._identity is Identity.static:
            assert self._principal is not None
            return Binding(self._principal, request_id)
        assert self._resolver is not None
        user_id = (ctx.headers or {}).get(USER_ID_HEADER, "")
        if not _USER_ID.match(user_id):
            # The gate refuses these before the SDK runs; this only guards a misconfigured mount.
            raise ToolError(ENTITLEMENT_ERROR)
        try:
            resolution = self._resolver.resolve(user_id)
        except ResolutionError as exc:
            write_event("resolve", request_id=request_id, user_id=user_id, error=str(exc))
            raise ToolError(ENTITLEMENT_ERROR) from None
        write_event(
            "resolve",
            request_id=request_id,
            user_id=user_id,
            username=resolution.username,
            principal=resolution.principal.id,
            attributes=dict(resolution.attributes),
            note=resolution.note,
        )
        return Binding(resolution.principal, request_id)


def _identity(settings: Settings) -> Identity:
    try:
        return Identity(settings.mcp_identity)
    except ValueError:
        raise ValueError(
            f"MCP_IDENTITY must be 'static' or 'mattermost', got {settings.mcp_identity!r}"
        ) from None


def _binder(settings: Settings, identity: Identity, resolver: Resolver | None) -> _Binder:
    if identity is Identity.static:
        if not settings.principal:
            raise ValueError("PRINCIPAL is required in static mode")
        path = Path(settings.principals_path)
        try:
            principal = resolve_principal(settings.principal, path)
        except KeyError:
            raise ValueError(f"PRINCIPAL {settings.principal!r} is not in {path}") from None
        return _Binder(identity, principal=principal)
    if not settings.mcp_shared_secret:
        raise ValueError("MCP_SHARED_SECRET is required in mattermost mode")
    return _Binder(identity, resolver=resolver or MattermostResolver.from_settings(settings))


def _security(settings: Settings) -> TransportSecuritySettings | None:
    hosts = [h.strip() for h in settings.mcp_allowed_hosts.split(",") if h.strip()]
    if not hosts:
        return None
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[])


def build_app(settings: Settings, *, resolver: Resolver | None = None) -> SovragMcp:
    """Build the server and its ASGI app. Raises ValueError for any configuration that could
    bind the wrong principal, so the process refuses to start rather than run degraded."""
    identity = _identity(settings)
    binder = _binder(settings, identity, resolver)
    mcp = MCPServer(
        "sovrag",
        instructions=(
            "Permissions-aware retrieval over Canadian federal public documents. Every result"
            " is already filtered to what the calling user may see; relay answers verbatim,"
            " followed by the sources field, which is ready-made markdown." + _NO_PRINCIPAL_ARG
        ),
    )

    @mcp.tool(
        description=(
            "Retrieve the passages most relevant to a question, each with its text and its"
            " citation (document, heading, source URL)." + _NO_PRINCIPAL_ARG
        )
    )
    def search(question: str, ctx: Context, k: int = 5) -> dict[str, object]:
        binding = binder.bind(ctx)
        k = max(1, min(k, MAX_K))
        applied_filter = entitlement_for(binding.principal).render()
        query_vec = Embedder.from_settings(settings).embed_one(question)
        with connect(settings.database_url) as conn:
            hits = search_hybrid(conn, question, query_vec, k=k, principal=binding.principal)
        write_event(
            "search",
            request_id=binding.request_id,
            principal=binding.principal.id,
            k=k,
            applied_filter=applied_filter,
            chunk_ids=[h.chunk_id for h in hits],
        )
        return {
            "request_id": binding.request_id,
            "principal": binding.principal.id,
            "applied_filter": applied_filter,
            "hits": [
                {
                    "doc_id": h.doc_id,
                    "chunk_id": h.chunk_id,
                    "heading_path": h.heading_path,
                    "source_url": h.source_url,
                    "title": h.title,
                    "text": h.text,
                    "score": h.score,
                }
                for h in hits
            ],
        }

    @mcp.tool(
        description=(
            "Answer a question from the documents the calling user may see, with [n] citations,"
            " or refuse when they do not support an answer." + _NO_PRINCIPAL_ARG
        )
    )
    def answer(question: str, ctx: Context) -> dict[str, object]:
        binding = binder.bind(ctx)
        try:
            result = generate_answer(
                question,
                principal=binding.principal,
                mode=Mode.hybrid,
                k=5,
                request_id=binding.request_id,
            )
        except GenerationError as exc:
            raise ToolError(f"generation failed: {exc}") from None
        # What the agent relays to the person. The principal, the filter, the model and the
        # chunk ids are in the audit line under the same request id, not in the chat.
        return {
            "text": result.text,
            "citations": [
                {
                    "n": c.n,
                    "title": c.title,
                    "heading_path": c.heading_path,
                    "source_url": c.source_url,
                }
                for c in result.citations
            ],
            "sources": sources_markdown(result.citations),
            "refused": result.refused,
            "request_id": result.request_id,
        }

    mcp_app = mcp.streamable_http_app(json_response=True, transport_security=_security(settings))

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    inner = Starlette(routes=[Mount("/", app=mcp_app)], lifespan=lifespan)
    app: ASGIApp = inner
    if identity is Identity.mattermost:
        app = _Gate(inner, settings.mcp_shared_secret)
    return SovragMcp(server=mcp, app=app, identity=identity)
