# air780e-hub server image.
#
# Build from the repository root so both server/ and frontend/ are in context:
#   docker build -t air780e-hub -f server/Dockerfile .

# --- frontend ---------------------------------------------------------------
# Must match `node-version` in .github/workflows/{ci,security}.yml: the image
# performs the same locked npm install and production build as CI. This keeps
# deployment independent of any generated frontend files being committed.
FROM node:24-alpine AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
ARG NPM_REGISTRY=https://registry.npmjs.org/
RUN npm ci --registry="${NPM_REGISTRY}"
COPY frontend/ ./
RUN npm run build


# --- Python environment ------------------------------------------------------
FROM python:3.12-slim AS python-build

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY server/pyproject.toml server/uv.lock ./
COPY server/src ./src

# Include the SPA produced by the frontend stage, then install exactly the
# versions in uv.lock. The runtime receives this environment without a
# resolver or uv.
COPY --from=frontend /build/dist ./src/hub_server/www
RUN uv sync --frozen --no-dev --no-editable


# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HUB_DATA_DIR=/data \
    HUB_PORT=8080 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=python-build /app/.venv /app/.venv

# Apply Debian security updates that may land between base-image publishes,
# then run unprivileged; /data is the only writable path the service needs.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /app hub \
    && mkdir -p /data \
    && chown hub:hub /data
USER hub

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

CMD ["hub-server", "serve"]
