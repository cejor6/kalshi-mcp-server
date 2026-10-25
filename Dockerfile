# syntax=docker/dockerfile:1.7

# Base images are pinned by tag AND digest, so a rebuild of the same commit
# gets the same bytes rather than whatever the tag points at today. The tag is
# kept alongside the digest for readability and so Dependabot (docker
# ecosystem, see .github/dependabot.yml) can propose bumps. Both stages use
# the SAME python image on purpose: the venv built in stage 1 symlinks to
# stage 1's interpreter, so the runtime stage must provide an identical one.
#
# To bump by hand: `docker buildx imagetools inspect <image>:<tag>` and copy
# the top-level index `Digest:` (not a per-platform manifest digest — the
# release builds linux/amd64 + linux/arm64).

# uv, as its own named stage so Dependabot's docker ecosystem (which parses
# FROM lines, not `COPY --from=<image>` refs) can propose bumps. Pinned to an
# exact release + digest: a floating `0.x` tag would let a new uv change
# lockfile handling under an unchanged commit.
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

# Stage 1 — install the LOCKED dependency set into a venv with uv.
FROM python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS build

# UV_PYTHON_DOWNLOADS=never: use the base image's interpreter, never a
# uv-managed download (the runtime stage wouldn't have it).
# UV_PROJECT_ENVIRONMENT: `uv sync` installs into /opt/venv instead of
# /app/.venv, so the runtime stage can copy it wholesale.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv from the pinned `uv` stage above — avoids pip overhead.
COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

# Install from uv.lock, not a fresh resolve. `uv pip install ".[oauth]"`
# ignored the lock entirely, so every image build pulled whatever versions
# were newest on PyPI that day — a different (unreviewed, untested) dependency
# set from the one CI ran against. `--locked` both installs exactly the
# pinned + hash-checked versions AND fails the build if uv.lock is stale
# relative to pyproject.toml, rather than silently re-resolving.
#
# `--extra oauth` installs the [oauth] extras so the OAuth proxy (FastMCP
# GitHubProvider + Redis-backed DCR store) works out of the box on hosted
# deploys — without them the proxy crashes on import when MCP_REDIS_URL is
# set. `--no-dev` drops dependency groups; the `dev` *extra* is simply not
# requested.
#
# Two steps so the (slow, rarely-changing) dependency layer is cached
# independently of source edits.
#
# Known residual: the build backend (`hatchling`, used only to build this
# project's own wheel in the second step) is fetched into an isolated build
# env and is NOT covered by uv.lock. Runtime dependencies all are.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --extra oauth --no-install-project --no-cache

# `--no-editable` installs the project itself as a regular package in
# /opt/venv. The default editable install would leave a .pth pointing at
# /app/src, which doesn't exist in the runtime stage.
COPY src ./src
RUN uv sync --locked --no-dev --extra oauth --no-editable --no-cache

# Stage 2 — slim runtime image.
FROM python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS runtime

# MCP Registry ownership verification: the registry confirms this image
# belongs to the io.github.cejor6/* namespace by reading this annotation.
# It MUST match the `name` field in server.json.
LABEL io.modelcontextprotocol.server.name="io.github.cejor6/kalshi-mcp-server"

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
