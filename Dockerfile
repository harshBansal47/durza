# syntax=docker/dockerfile:1

# Multi-stage because the runtime image should not contain a compiler, a test
# runner, or ~400MB of build cache. The previous single-stage Dockerfile was
# fine locally and un-shippable: every CVE scan reported the same transitive
# packages regardless of what was actually in use.

ARG PYTHON_VERSION=3.12


# ---- Build stage -------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

WORKDIR /code

# libpq-dev is needed to build psycopg/asyncpg from source on architectures
# without a manylinux wheel (arm64 is the one that bites people).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv and point it at a system-wide venv outside /code, so the final
# image can copy a self-contained environment without dragging the source tree
# or the .git directory along with it.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Dependencies first, and only the manifest, so the install layer is cached until
# a dependency actually changes. `--no-dev` is the point of the multi-stage
# build: pytest and ruff have no business in a production image.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev


# ---- Runtime stage -----------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

WORKDIR /code

# libpq5 is the runtime half of libpq-dev; curl is for the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /code/.venv /code/.venv

ENV PATH="/code/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

# Non-root. UID 1000 matches the typical first non-root user on Linux hosts so
# the bind-mounted ./app and ./alembic from docker-compose.yml do not hit
# permission errors in local dev.
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser mcp_server ./mcp_server

# Install the project itself, non-editable, so `import app` works from the
# venv's site-packages rather than depending on /code being the working
# directory.
COPY --chown=appuser:appuser pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --no-deps . \
    && chown -R appuser:appuser /code

USER appuser

EXPOSE 8000

# /health is the cheap liveness probe on purpose. /health/deep touches Postgres
# and would turn a slow database into a restart storm, which is the exact
# failure mode the two-endpoint split exists to prevent.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# No `--reload`: this is the production image. Scaling is the orchestrator's job.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
