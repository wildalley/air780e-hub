# air780e-hub server image.
#
# Build from the repository root so both server/ and frontend/ are in context:
#   docker build -t air780e-hub -f server/Dockerfile .

# --- frontend ---------------------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build


# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HUB_DATA_DIR=/data \
    HUB_PORT=8080

WORKDIR /app

COPY server/pyproject.toml ./
COPY server/src ./src

# The built SPA ships inside the package, so one process on one port serves
# both the API and the UI — that makes the whole deployment a single
# reverse-proxy entry.  Copy it in before the install so it gets packaged.
COPY --from=frontend /build/dist ./src/hub_server/www

RUN pip install --no-cache-dir .

# Runs unprivileged; /data is the only writable path it needs.
RUN useradd --system --uid 10001 --home /app hub \
    && mkdir -p /data \
    && chown -R hub:hub /data /app
USER hub

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

CMD ["hub-server", "serve"]
