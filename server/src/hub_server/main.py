"""FastAPI application.

Serves the REST API, Agent WebSocket, and built frontend from one process and
one port so deployment needs only one reverse-proxy upstream.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import build_router
from .config import Settings
from .state import AppState

log = logging.getLogger(__name__)

PURGE_INTERVAL = 6 * 3600
FRONTEND_DIR = Path(__file__).parent / "www"


class AuditMiddleware:
    """Record admin mutations without buffering request or response bodies."""

    def __init__(self, app, *, state: AppState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        path = scope["path"]
        should_audit = path.startswith("/api/") and method in {
            "POST", "PUT", "PATCH", "DELETE",
        }
        if not should_audit:
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        status_code = 500

        async def capture_status(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        except Exception:
            self._record(method, path, "error", "unhandled server error", scope)
            raise

        # This middleware sits ahead of authentication, so anyone who can reach
        # the port can append rows.  The router records the matched route in
        # this same scope dict, and an unknown /api path is absorbed by the SPA
        # catch-all ("/{path:path}") rather than a route of its own — so
        # spraying invented paths leaves no trace, while a rejected call on a
        # real route (a failed login above all) is still recorded.  Retention
        # and the row cap bound what remains.
        matched = getattr(scope.get("route"), "path", "")
        if not matched.startswith("/api/"):
            return

        self._record(
            method,
            path,
            "ok" if status_code < 400 else "rejected",
            f"HTTP {status_code}; {time.monotonic() - started:.3f}s",
            scope,
        )

    def _record(self, method, path, status, detail, scope) -> None:
        client = scope.get("client")
        self.state.db.record_audit(
            f"{method} {path}",
            status=status,
            detail=detail,
            client_ip=client[0] if client else "",
        )


async def _housekeeping(state: AppState) -> None:
    """Enforce retention.  Verification codes should not pile up forever."""
    while True:
        await asyncio.sleep(PURGE_INTERVAL)
        try:
            removed = state.db.purge(
                message_days=state.message_retention_days,
                status_days=state.settings.status_retention_days,
                log_days=state.settings.log_retention_days,
                audit_days=state.settings.audit_retention_days,
                incident_days=state.settings.incident_retention_days,
                audit_max_rows=state.settings.audit_max_rows,
            )
            state.auth.purge_expired_sessions()
            if any(removed.values()):
                log.info("housekeeping removed %s", removed)
        except Exception:
            log.exception("housekeeping failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    state = AppState.build(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_housekeeping(state), name="housekeeping")
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Cancel any armed offline timers, then let pushes already on the
            # wire finish before the HTTP client closes — AppState.close() is
            # synchronous and cannot await them.
            await state.alerter.aclose()
            await state.notifier.drain()
            await state.notifier.aclose()
            state.close()

    app = FastAPI(
        title="air780e-hub",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,       # no public schema browser on an internet-facing box
        redoc_url=None,
        openapi_url=None,
    )
    app.state.hub = state

    app.add_middleware(AuditMiddleware, state=state)

    router = build_router(state)
    app.include_router(router)

    @app.websocket("/ws")
    async def agent_socket(websocket: WebSocket) -> None:
        await state.gateway.serve(websocket)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({
            "ok": True,
            "agents_connected": len(state.gateway.connections),
        })

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built SPA, with a catch-all so client-side routes work."""
    if not FRONTEND_DIR.exists():
        log.warning("frontend bundle not found at %s; API only", FRONTEND_DIR)

        @app.get("/")
        async def no_frontend() -> JSONResponse:
            return JSONResponse(
                {"detail": "frontend not built; run `npm run build` in frontend/"},
                status_code=503,
            )

        return

    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIR / "assets", check_dir=False),
        name="assets",
    )

    index = FRONTEND_DIR / "index.html"

    # response_model=None: the union return annotation is a plain Starlette
    # response, not something FastAPI should build a response model from.
    @app.get("/{path:path}", response_model=None)
    async def spa(path: str) -> FileResponse | JSONResponse:
        # Never let the catch-all shadow the API or the socket.
        if path.startswith(("api/", "ws", "healthz")):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = (FRONTEND_DIR / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(FRONTEND_DIR):
            return FileResponse(candidate)
        return FileResponse(index)


app = None  # populated by the CLI; keeps import-time side effects out of tests
