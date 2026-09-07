"""Restore admission, progress and coordination of the live application."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anyio import CancelScope
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .auth import SESSION_COOKIE
from .db import Database, MigrationFailed, utcnow

if TYPE_CHECKING:
    from .state import AppState

log = logging.getLogger(__name__)
RESTORE_PATH = "/api/system/restore"
STATUS_PATH = RESTORE_PATH + "/status"


async def finish_before_cancel(coroutine):
    """An admitted switch must settle before its upload files can be removed."""
    task = asyncio.create_task(coroutine)
    cancelled = None
    with CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                break
        if cancelled is not None:
            if not task.cancelled():
                task.exception()
            raise cancelled
        return task.result()


class RestoreController:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.active = False
        self.phase = "idle"
        self.outcome: str | None = None
        self.detail = ""
        self.snapshot: str | None = None
        self.received_bytes = 0
        self.started_at: str | None = None
        self._owner_hash = ""
        self.requests: set[asyncio.Task] = set()
        self.sockets: dict[asyncio.Task, RestoreSocket] = {}
        self.idle = asyncio.Event()
        self.idle.set()

    def view(self) -> dict[str, Any]:
        return {
            "phase": self.phase, "outcome": self.outcome, "detail": self.detail,
            "snapshot": self.snapshot, "received_bytes": self.received_bytes,
            "started_at": self.started_at, "maintenance": self.active,
        }

    def owns_status(self, cookie: str | None) -> bool:
        return bool(cookie and self._owner_hash) and hmac.compare_digest(
            hashlib.sha256(cookie.encode()).hexdigest(), self._owner_hash,
        )

    def _space(self, state: AppState, size: int, written: int = 0) -> None:
        live_bytes = sum(
            path.stat().st_size for path in (
                state.db.path, Path(str(state.db.path) + "-wal"),
            ) if path.exists()
        )
        # Candidate migration, migration snapshot, rollback copy and live WAL.
        required = (
            max(0, 3 * size - written) + 2 * live_bytes + state.settings.restore_min_free_bytes
        )
        if shutil.disk_usage(state.settings.data_dir).free < required:
            raise HTTPException(status_code=507, detail="磁盘空间不足，在线数据库未修改")

    async def restore(self, state: AppState, request: Request) -> dict[str, Any]:
        if self.lock.locked():
            raise HTTPException(status_code=409, detail="已有恢复任务正在进行")
        async with self.lock:
            self.phase, self.outcome, self.detail = "uploading", None, ""
            self.snapshot, self.received_bytes = None, 0
            self.started_at = utcnow()
            self._owner_hash = hashlib.sha256(
                request.cookies[SESSION_COOKIE].encode(),
            ).hexdigest()
            try:
                return await self._upload(state, request)
            except BaseException as exc:
                self.phase = "complete" if self.outcome == "restored" else "failed"
                if self.outcome is None:
                    self.outcome = "unchanged"
                if isinstance(exc, HTTPException):
                    self.detail = str(exc.detail)
                    raise
                if isinstance(exc, asyncio.CancelledError):
                    if not self.detail:
                        self.detail = "请求中断，在线数据库未修改"
                    raise
                log.exception("restore failed before switch")
                self.detail = "恢复准备失败，在线数据库未修改"
                raise HTTPException(status_code=500, detail=self.detail) from exc

    async def _upload(self, state: AppState, request: Request) -> dict[str, Any]:
        raw_length = request.headers.get("content-length")
        declared = None
        if raw_length is not None:
            try:
                declared = int(raw_length)
                if declared < 0:
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=400, detail="Content-Length 无效") from None
            if declared > state.settings.restore_max_bytes:
                raise HTTPException(status_code=413, detail="备份文件超过恢复大小上限")
        await state.db.run(self._space, state, declared or 0)
        with tempfile.TemporaryDirectory(
            prefix="restore-upload-", dir=state.settings.data_dir,
        ) as temp:
            candidate = Path(temp) / "candidate.db"
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as out:
                    async with asyncio.timeout(state.settings.restore_upload_timeout):
                        async for chunk in request.stream():
                            size = self.received_bytes + len(chunk)
                            if size > state.settings.restore_max_bytes:
                                raise HTTPException(
                                    status_code=413, detail="备份文件超过恢复大小上限",
                                )
                            await state.db.run(self._space, state, size, self.received_bytes)
                            await state.db.run(out.write, chunk)
                            self.received_bytes = size
                        await state.db.run(out.flush)
            except TimeoutError:
                raise HTTPException(
                    status_code=408, detail="备份上传超时，在线数据库未修改",
                ) from None
            if not self.received_bytes:
                raise HTTPException(status_code=400, detail="未收到备份文件")
            if declared is not None and declared != self.received_bytes:
                raise HTTPException(status_code=400, detail="备份上传长度与 Content-Length 不一致")
            self.phase = "validating"
            try:
                await finish_before_cancel(asyncio.to_thread(Database.validate_backup, candidate))
                self.phase = "preparing"
                await finish_before_cancel(asyncio.to_thread(Database.prepare_restore, candidate))
            except MigrationFailed as exc:
                raise HTTPException(
                    status_code=400, detail="候选备份迁移失败，在线数据库未修改",
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return await finish_before_cancel(self._switch(state, candidate))

    async def _disconnect(self, state: AppState | None = None) -> None:
        sessions = list(self.sockets.items())
        # The close frame and synthetic disconnect wake each application task,
        # but TestClient (and a real peer that ignores the frame) may keep the
        # task alive until it explicitly closes its side.  Do not make the
        # database switch depend on that client-side lifecycle.
        self.sockets.clear()
        for _, socket in sessions:
            try:
                async with asyncio.timeout(1):
                    if state is not None:
                        state.db.metrics.note_gateway_close("maintenance", 1012)
                    await socket.close()
            except Exception:
                log.debug("socket already closed during restore")
                socket.disconnect()

    def _snapshot_path(self, state: AppState) -> Path:
        directory = state.settings.data_dir / "restore-snapshots"
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        descriptor, path = tempfile.mkstemp(prefix="before-", suffix=".db", dir=directory)
        os.close(descriptor)
        return Path(path)

    def _retain_snapshots(self, snapshot: Path) -> None:
        previous = sorted(
            (path for path in snapshot.parent.glob("before-*.db") if path != snapshot),
            key=lambda path: path.stat().st_mtime, reverse=True,
        )
        for path in previous[2:]:
            path.unlink()

    async def _switch(self, state: AppState, candidate: Path) -> dict[str, Any]:
        self.active, self.phase = True, "draining"
        paused = False
        try:
            state.gateway.retire_for_restore()
            await state.stop_background()
            await self._disconnect(state)
            try:
                async with asyncio.timeout(state.settings.restore_drain_timeout):
                    await self.idle.wait()
            except TimeoutError:
                raise HTTPException(
                    status_code=409, detail="仍有请求正在处理，在线数据库未修改",
                ) from None
            await state.alerter.aclose()
            await state.notifier.pause(timeout=state.settings.restore_drain_timeout)
            paused = True
            self.phase = "snapshot"
            await state.db.run(
                state.db.execute,
                "UPDATE command_operations SET status = 'cancelled', "
                "error = 'cancelled before database restore', updated_at = ? "
                "WHERE status = 'queued'", (utcnow(),),
            )
            await state.db.run(state.db.mark_all_agents_disconnected)
            await state.db.run(state.db.recover_operations)
            size = candidate.stat().st_size
            await state.db.run(self._space, state, size, size)
            snapshot = await state.db.run(self._snapshot_path, state)
            await state.db.run(state.db.backup_to, snapshot)
            await state.db.run(Database.sync_snapshot, snapshot)
            self.snapshot = str(snapshot.relative_to(state.settings.data_dir))
            self.phase = "switching"
            try:
                await state.db.run(state.db.replace_prepared, candidate)
                await state.db.run(state.db.reset_restored_runtime)
                self.phase = "verifying"
                await state.db.run(state.db.verify_restored)
            except Exception as exc:
                log.exception("restore switch failed; restoring online snapshot")
                try:
                    await state.db.run(state.db.replace_prepared, snapshot)
                    await state.db.run(state.db.verify_restored)
                except Exception:
                    self.outcome = "manual_recovery"
                    self.detail = f"切换及回退失败，需要人工恢复：{self.snapshot}"
                    log.exception("restore rollback failed")
                else:
                    self.outcome = "rolled_back"
                    self.detail = f"恢复失败，已回退到恢复前快照：{self.snapshot}"
                raise HTTPException(status_code=500, detail=self.detail) from exc
            self.outcome, self.phase = "restored", "complete"
            self.detail = "恢复完成，请重新登录"
            try:
                await state.db.run(self._retain_snapshots, snapshot)
            except OSError:
                log.exception("could not prune older restore snapshots")
            return {"ok": True, **self.view(), "maintenance": False}
        finally:
            if self.outcome != "manual_recovery":
                try:
                    if paused:
                        await state.notifier.client.aclose()
                        state.rebuild_services()
                    elif state.gateway.retiring_for_restore:
                        # A drain failure leaves the online database intact,
                        # but the old registry remains intentionally retired.
                        state.rebuild_gateway()
                    state.start_background()
                except Exception as exc:
                    self.outcome = "manual_recovery"
                    self.detail = f"运行状态重建失败，需要人工恢复：{self.snapshot or '原库未覆盖'}"
                    raise HTTPException(status_code=500, detail=self.detail) from exc
                self.active = False


class RestoreSocket:
    """Close one ASGI socket without losing its protocol close frame."""

    def __init__(self, receive, send) -> None:
        self._receive = receive
        self._send = send
        self._closed = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._closing_task: asyncio.Task | None = None

    async def receive(self) -> dict[str, Any]:
        if self._closed.is_set():
            return self._disconnect_message()
        incoming = asyncio.create_task(self._receive())
        if self._closing_task is None:
            self._closing_task = asyncio.create_task(self._closed.wait())
        closing = self._closing_task
        try:
            done, _ = await asyncio.wait(
                (incoming, closing), return_when=asyncio.FIRST_COMPLETED,
            )
            if closing in done:
                incoming.cancel()
                await asyncio.gather(incoming, return_exceptions=True)
                return self._disconnect_message()
            return incoming.result()
        finally:
            if not incoming.done():
                incoming.cancel()

    async def send(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            if not self._closed.is_set():
                await self._send(message)

    async def close(self) -> None:
        async with self._send_lock:
            if self._closed.is_set():
                return
            try:
                await self._send({
                    "type": "websocket.close", "code": 1012,
                    "reason": "database restore",
                })
            finally:
                self._closed.set()

    def disconnect(self) -> None:
        self._closed.set()

    @staticmethod
    def _disconnect_message() -> dict[str, Any]:
        return {
            "type": "websocket.disconnect", "code": 1012,
            "reason": "database restore",
        }


class RestoreMiddleware:
    """Block new API work and track whole requests, including streamed exports."""

    def __init__(self, app, *, state: AppState) -> None:
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send) -> None:
        control = self.state.restore
        kind, path = scope["type"], scope.get("path", "")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if kind == "http" and (
            not path.startswith("/api/") or (path == STATUS_PATH and scope["method"] == "GET")
        ):
            await self.app(scope, receive, send)
            return
        if control.active:
            if kind == "websocket":
                self.state.db.metrics.note_gateway_close("maintenance", 1013)
                await send({"type": "websocket.close", "code": 1013})
            else:
                await JSONResponse(
                    {"detail": "数据库恢复维护中", "phase": control.phase},
                    status_code=503, headers={"Retry-After": "2"},
                )(scope, receive, send)
            return
        if path == RESTORE_PATH:
            await self.app(scope, receive, send)
            return
        task = asyncio.current_task()
        if kind == "websocket":
            socket = RestoreSocket(receive, send)
            control.sockets[task] = socket
        else:
            control.requests.add(task)
            control.idle.clear()
        try:
            if kind == "websocket":
                await self.app(scope, socket.receive, socket.send)
            else:
                await self.app(scope, receive, send)
        finally:
            if kind == "websocket":
                socket.disconnect()
            control.sockets.pop(task, None)
            control.requests.discard(task)
            if not control.requests:
                control.idle.set()
