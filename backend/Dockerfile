# Build context: the repository root (see docker-compose.yml), so the image can include the
# vendored MCP server and the review sample alongside the backend. Inside the image the repo
# layout is kept: /app/backend, /app/mcp_server, /app/data.

# --- 1. Build the MCP server (TypeScript -> dist/, production dependencies only) -----------
FROM node:22-bookworm-slim AS mcp
WORKDIR /mcp
COPY mcp_server/package.json mcp_server/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY mcp_server/tsconfig.json ./
COPY mcp_server/src ./src
RUN npm run build && npm prune --omit=dev

# --- 2. The API, which starts the MCP server over stdio for each approved action ----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Node runtime for the MCP server: the binary from the same Debian release, plus the C++
# runtime it links against.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/ ./
COPY data/ /app/data/
COPY --from=mcp /mcp/package.json /app/mcp_server/package.json
COPY --from=mcp /mcp/node_modules /app/mcp_server/node_modules
COPY --from=mcp /mcp/dist /app/mcp_server/dist

ENV PATH="/app/backend/.venv/bin:$PATH" \
    MCP_SERVER_DIR=/app/mcp_server
EXPOSE 8000
# Render sets $PORT. Migrations run on every start and are a no-op when up to date.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
