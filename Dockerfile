# syntax=docker/dockerfile:1.7

# Stage 1 — build wheel + install into a venv with uv.
FROM python:3.14-slim-bookworm AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv directly from the official image — avoids pip overhead.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

# Create venv and install. We don't install dev dependencies in production.
RUN uv venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
RUN uv pip install --no-cache .

# Stage 2 — slim runtime image.
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --home-dir /home/app --shell /usr/sbin/nologin app

WORKDIR /home/app
COPY --from=build /opt/venv /opt/venv

USER app

# stdio is the standard MCP transport. For remote/http deployment, set
# MCP_TRANSPORT=http and expose PORT.
ENV MCP_TRANSPORT=stdio \
    PORT=8000

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "kalshi-mcp"]
# CMD provides default args appended to ENTRYPOINT. For containerized
# HTTP serving we MUST bind to 0.0.0.0 — the CLI's default 127.0.0.1
# is safe for local dev but means Render/Cloud Run/etc. can't route
# external traffic to the container. Override at runtime by passing
# `docker run ... --host 127.0.0.1` if you want localhost-only.
CMD ["--host", "0.0.0.0"]
