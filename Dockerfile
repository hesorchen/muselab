# syntax=docker/dockerfile:1.6
# ===========================================================================
# muselab — multi-stage image
#   stage 1 (builder):  install Python deps with uv (cached)
#   stage 2 (runtime):  slim image with venv + claude CLI + app code
# ===========================================================================

# ---------- builder ----------
FROM python:3.12-slim AS builder

# uv: single static binary, fast resolver. Pinned (not :latest) so the image
# is reproducible and matches the "frozen" promise of `uv sync --frozen`.
# Bump deliberately (or via renovate/dependabot), not implicitly on rebuild.
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /uvx /bin/

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy

# Cache deps by copying only lockfile + pyproject first
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---------- runtime ----------
FROM python:3.12-slim

# Install Node.js (for the claude CLI + any npm-based MCP a user opts into)
# and the claude CLI itself. We deliberately DO NOT pre-bake MCP servers:
# the built-in tools (Read/Edit/Write/Grep/Glob/Bash/WebFetch + native
# extended thinking) already cover what the old presets did, and pre-baking
# unused servers bloats the image. Users add external connectors via the UI;
# those resolve at add-time, not in the image. Keep curl (HEALTHCHECK) and
# git (common in archives / user-added git MCP). procps supplies `ps` for the
# real terminal and lets terminal teardown find every job in a PTY session.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git procps && \
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    # claude-code pin — keep in lockstep with CLAUDE_CLI_VERSION in
    # scripts/versions.env and the CLI bundled by claude-agent-sdk (the native
    # installers read the pin from versions.env).
    npm install -g \
        @anthropic-ai/claude-code@2.1.277 && \
    apt-get purge -y --auto-remove gnupg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /root/.npm /tmp/*

# uv binary (for any `uvx`-based MCP a user opts into, e.g. mcp-server-fetch).
# Pinned — keep in lockstep with the builder-stage uv above.
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy pre-built venv from builder
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MUSELAB_PORT=8765 \
    MUSELAB_ROOT=/data \
    MUSELAB_SESSIONS_DIR=/app/sessions \
    MUSELAB_CONFIG_DIR=/app/sessions/config \
    MUSELAB_ENV_PATH=/app/sessions/config/.env \
    MUSELAB_ENV_OVERRIDE=1 \
    XDG_STATE_HOME=/app/sessions/state

# App code
COPY backend ./backend
COPY frontend ./frontend
COPY skills ./skills
COPY scripts/templates ./scripts/templates
COPY pyproject.toml LICENSE THIRD_PARTY_LICENSES.md ./

# Source builds can match a Linux bind-mount owner's UID/GID without
# broadening permissions on credentials. Published images default to 1000.
ARG MUSE_UID=1000
ARG MUSE_GID=1000
RUN test "$MUSE_UID" -gt 0 && test "$MUSE_GID" -gt 0 && \
    groupadd -g "$MUSE_GID" muse && \
    useradd -u "$MUSE_UID" -g "$MUSE_GID" -m -s /bin/bash muse && \
    mkdir -p /app/sessions/config /app/sessions/state /data && \
    chown -R muse:muse /app /data

USER muse

EXPOSE 8765

# Probe the dedicated /api/health endpoint. Using `curl` (already installed
# for the Node.js setup above) over `python -c …` shaves the ~150ms Python
# interpreter startup off every probe (every 30s × 24h = 2880 probes/day).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS --max-time 3 http://127.0.0.1:8765/api/health >/dev/null || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8765"]
