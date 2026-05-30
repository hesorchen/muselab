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

# Install Node.js (for claude CLI + npm-based MCP), claude CLI, and the most
# common MCP servers up front so users can opt in via mcp.json. Also keep git
# (needed by mcp-server-git) and curl (used by HEALTHCHECK in some setups).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    # claude-code pin — keep in lockstep with CLAUDE_CLI_VERSION in
    # scripts/versions.env (the native installers read it from there).
    npm install -g \
        @anthropic-ai/claude-code@2.1.156 \
        @modelcontextprotocol/server-filesystem@2026.1.14 \
        @modelcontextprotocol/server-memory@2026.1.26 && \
    apt-get purge -y --auto-remove gnupg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /root/.npm /tmp/*

# uv binary (for `uvx mcp-server-fetch` / `uvx mcp-server-git`). Pinned — keep
# in lockstep with the builder-stage uv above.
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy pre-built venv from builder
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MUSELAB_PORT=8765 \
    MUSELAB_ROOT=/data

# App code
COPY backend ./backend
COPY frontend ./frontend
COPY pyproject.toml ./

# Non-root user (uid 1000 — matches default host user on Linux/Mac)
RUN groupadd -g 1000 muse && \
    useradd -u 1000 -g 1000 -m -s /bin/bash muse && \
    mkdir -p /app/sessions /data && \
    chown -R muse:muse /app /data

USER muse

EXPOSE 8765

# Probe the dedicated /api/health endpoint. Using `curl` (already installed
# for the Node.js setup above) over `python -c …` shaves the ~150ms Python
# interpreter startup off every probe (every 30s × 24h = 2880 probes/day).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS --max-time 3 http://127.0.0.1:8765/api/health >/dev/null || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8765"]
