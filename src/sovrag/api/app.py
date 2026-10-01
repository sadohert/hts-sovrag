"""The HTTP API: POST /query and GET/HEAD /healthz, with the principal bound before routing.

The handler reads the principal from request state, which only the binding layer in gate.py
writes. It generates the request id, so the audit line answer() writes can be joined to the
access log, and returns the Answer unchanged: the same object `sovrag ask --json` prints.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from sovrag.api.gate import HEALTH_PATH, GatewayGate, Identity, StaticBinding
from sovrag.auth import Principal, load_principals, resolve_principal
from sovrag.config import Settings
from sovrag.generate import MAX_K, GenerationError
from sovrag.generate.answer import answer
from sovrag.retrieve import Mode

# Module-level name so tests can substitute the generator without touching the route.
generate_answer = answer

QUESTION_MAX_CHARS = 2000


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=QUESTION_MAX_CHARS)
    mode: Mode = Mode.hybrid
    k: StrictInt = 5

    @field_validator("question", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


def _identity(settings: Settings) -> Identity:
    try:
        return Identity(settings.api_identity)
    except ValueError:
        raise ValueError(
            f"API_IDENTITY must be 'static' or 'gateway', got {settings.api_identity!r}"
        ) from None


def build_app(settings: Settings) -> FastAPI:
    """Build the app. Raises ValueError for any configuration that could bind the wrong
    principal, so `sovrag serve` refuses to start rather than run degraded."""
    identity = _identity(settings)
    app = FastAPI(
        title="sovrag", docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False
    )
    app.state.identity = identity
    app.state.principal_id = None
    path = Path(settings.principals_path)

    if identity is Identity.static:
        if not settings.principal:
            raise ValueError("PRINCIPAL is required in static mode")
        try:
            principal = resolve_principal(settings.principal, path)
        except KeyError:
            raise ValueError(f"PRINCIPAL {settings.principal!r} is not in {path}") from None
        app.state.principal_id = principal.id
        app.add_middleware(StaticBinding, principal=principal)
    else:
        if not settings.api_shared_secret:
            raise ValueError("API_SHARED_SECRET is required in gateway mode")
        app.add_middleware(
            GatewayGate, secret=settings.api_shared_secret, principals=load_principals(path)
        )

    @app.api_route(HEALTH_PATH, methods=["GET", "HEAD"])
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "identity": identity.value, "principal": app.state.principal_id}

    @app.post("/query")
    def query(request: Request, body: QueryRequest) -> JSONResponse:
        principal: Principal = request.state.principal
        request_id = uuid.uuid4().hex
        k = max(1, min(body.k, MAX_K))
        try:
            result = generate_answer(
                body.question, principal=principal, mode=body.mode, k=k, request_id=request_id
            )
        except GenerationError as exc:
            message = str(exc)
            detail = (
                message
                if message.startswith("generation failed")
                else f"generation failed: {message}"
            )
            return JSONResponse(
                {"detail": detail, "request_id": request_id},
                status_code=502,
                headers={"X-Request-ID": request_id},
            )
        return JSONResponse(asdict(result), headers={"X-Request-ID": request_id})

    return app
