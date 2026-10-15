# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- builder
# Builder stage: resolves and installs dependencies; never shipped.
FROM python:3.12-slim AS builder

# Pinned uv binary copied from the official image for reproducible builds.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

# Precompile bytecode for faster startup; copy files instead of hardlinking across layers.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install third-party dependencies first so this layer is cached until the lockfile changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install the project itself as a regular (non-editable) package.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- runtime
# Runtime stage: slim image with only the virtualenv and migrations.
FROM python:3.12-slim AS runtime

# Unbuffered output so logs reach `docker logs` immediately; venv first on PATH.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Dedicated unprivileged system user (least privilege).
RUN groupadd --system app && useradd --system --gid app --no-create-home app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv

# Migrations are shipped so `alembic upgrade head` can run from this image.
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations

USER app

EXPOSE 8000

# Readiness probe via the Python stdlib, because slim images ship without curl.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4).status == 200 else 1)"]

# Start the ASGI server through the application factory.
CMD ["uvicorn", "booking_agent.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
