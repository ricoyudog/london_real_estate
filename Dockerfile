# syntax=docker/dockerfile:1
FROM node:22.19.0-bookworm-slim

# uv provisions the locked Python runtime used by the agent-tool facade.
COPY --from=ghcr.io/astral-sh/uv:0.8.14 /uv /uvx /bin/

WORKDIR /app

# FacadeLauncher invokes `uv run`; do not re-sync dev dependencies per request.
ENV NODE_ENV=production \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_SYNC=1 \
    UV_PYTHON=3.12 \
    PATH="/app/.venv/bin:${PATH}" \
    CRE_DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8787

COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable --no-cache

COPY agent-runtime/package.json agent-runtime/package-lock.json ./agent-runtime/
COPY agent-runtime/scripts ./agent-runtime/scripts
RUN npm ci --prefix agent-runtime --omit=dev --audit=false \
    && node agent-runtime/scripts/resolve-pi-brace-expansion.mjs
RUN npm audit --prefix agent-runtime --omit=dev

COPY agent-runtime/src ./agent-runtime/src
COPY agent-runtime/public ./agent-runtime/public
COPY agent-runtime/skills.manifest.json ./agent-runtime/skills.manifest.json
COPY skills ./skills
COPY demo ./demo

# This Linux image is read/runtime only. Canonical ingestion requires macOS
# sandbox-exec parser isolation, so never run `cre daemon` from this container.
VOLUME ["/data"]
EXPOSE 8787

# Compose gates this service on the one-shot demo initializer. exec keeps the
# Node server as PID 1; no ingestion daemon runs in this Linux image.
CMD ["npm", "--prefix", "/app/agent-runtime", "run", "serve"]
