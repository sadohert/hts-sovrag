# syntax=docker/dockerfile:1
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.14-slim AS runtime
RUN groupadd -r sovrag && useradd -r -g sovrag -d /app sovrag
WORKDIR /app
COPY --from=builder --chown=sovrag:sovrag /app/.venv /app/.venv
COPY --from=builder --chown=sovrag:sovrag /app/src /app/src
COPY --chown=sovrag:sovrag data/manifest.yaml ./data/manifest.yaml
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER sovrag
EXPOSE 8000
CMD ["sovrag", "serve", "--host", "0.0.0.0", "--port", "8000"]
