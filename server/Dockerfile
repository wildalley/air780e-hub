# air780e-hub server image.
#
# Build from the repository root so both server/ and frontend/ are in context:
#   docker build -t air780e-hub -f server/Dockerfile .

# --- frontend ---------------------------------------------------------------
# Must match `node-version` in .github/workflows/{ci,security}.yml: CI is what
# proves the bundle builds, and a different major here means the artefact that
# ships was never the one CI validated.  24 is the active LTS (Krypton); 25 is
# a current release with no LTS window.
FROM node:24-alpine AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci
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

# Include the SPA before packaging, then install exactly the versions in
# uv.lock. The runtime receives this environment without a resolver or uv.
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

# Runs unprivileged; /data is the only writable path it needs.
RUN useradd --system --uid 10001 --home /app hub \
    && mkdir -p /data \
    && chown hub:hub /data
USER hub

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

CMD ["hub-server", "serve"]
