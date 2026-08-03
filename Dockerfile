# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# uv for reproducible installs from uv.lock
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so a source-only change doesn't reinstall the world.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# README.md is referenced by pyproject's `readme` field, so the build backend needs it here.
COPY README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Non-root. /data is the only writable path; see the read_only + tmpfs settings in
# docker-compose.yml. No docker.sock is mounted here and none ever should be — the
# existing webhook-server on erebus has one and signet must not copy that.
RUN useradd --system --uid 10001 --home /app signet \
    && mkdir -p /data \
    && chown -R signet:signet /app /data
USER signet

ENV SIGNET_DATA_DIR=/data \
    SIGNET_HOST=0.0.0.0 \
    SIGNET_PORT=8300

EXPOSE 8300

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8300/healthz', timeout=4).status==200 else 1)"

CMD ["python", "-m", "signet"]
