# syntax=docker/dockerfile:1.6
# ===========================================================================
# muselab — multi-stage image
#   stage 1 (builder):  install Python deps with uv (cached)
#   stage 2 (runtime):  slim image with venv + claude CLI + app code
# ===========================================================================

# ---------- builder ----------
FROM python:3.12-slim AS builder

# uv: single static binary, fast resolver
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

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
    npm install -g \
        @anthropic-ai/claude-code@latest \
        @modelcontextprotocol/server-filesystem \
        @modelcontextprotocol/server-memory && \
    apt-get purge -y --auto-remove gnupg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /root/.npm /tmp/*

# uv binary (for `uvx mcp-server-fetch` / `uvx mcp-server-git`)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

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

# Probe the dedicated /api/health endpoint — way cheaper than rendering the
# full HTML page on every 30s probe (~2880 page renders / day otherwise).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3).status==200 else 1)" \
        || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8765"]
