# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12


# ---- Build stage -------------------------------------------------------------
FROM docker.io/library/python:${PYTHON_VERSION}-slim AS builder

WORKDIR /code

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Dependencies first (cached until pyproject.toml / uv.lock change)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Then install the project itself, non-editable, into the same venv
COPY README.md ./
COPY app ./app
COPY mcp_server ./mcp_server
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


# ---- Runtime stage -----------------------------------------------------------
FROM docker.io/library/python:${PYTHON_VERSION}-slim AS runtime

WORKDIR /code

# Runtime libs only: libpq5 for Postgres, curl for the healthcheck. No uv here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Ready-made venv from the builder (same path, so shebangs stay valid)
COPY --from=builder /code/.venv /code/.venv

ENV PATH="/code/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser mcp_server ./mcp_server

USER appuser

# Cloud Run injects PORT at runtime (default 8000). EXPOSE is documentation only.
EXPOSE 8000

# Cloud Run health check probes /health on $PORT. The start-period must cover
# cold-start time (Python + venv + first import of the app can take 5-10s).
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${PORT:-8000}/health || exit 1

# Cloud Run passes PORT as an env var; we must honour it. Using the shell form
# lets the variable expand at container start. --proxy-headers is required because
# Cloud Run sits behind Google's frontend load balancer (X-Forwarded-For etc).
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips "*"
