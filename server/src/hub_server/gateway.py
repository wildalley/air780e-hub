"""WebSocket gateway: where agent events become server state.

The agent dials in, authenticates with a bearer token, and then the link is
symmetric — events flow up, commands flow down.  Two invariants matter:

* **Ingest is idempotent.**  The agent replays after a lost ack, so every
  event is claimed by ``(agent_id, seq)`` before it is applied.
* **Acks are cumulative.**  Acking seq N frees everything up to N on the
  agent, so a missed ack costs one replay, not a stuck queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .auth import verify_agent_token
from .config import Settings
from .db import Database, utcnow

log = logging.getLogger(__name__)

CLOSE_AUTH_FAILED = 4001
CLOSE_PROTOCOL_ERROR = 4002
CLOSE_AGENT_CONFLICT = 4003

COMMAND_TIMEOUT = 30.0

MessageHook = Callable[[int, dict[str, Any]], Awaitable[None]]
TaskResultHook = Callable[[int, dict[str, Any]], Awaitable[None]]
# (agent_id, device, online) — fired on every module up/down state the gateway
# learns, so the offline alerter can debounce and page.  Synchronous: it only
# schedules work and returns, never awaits a push on the ingest path.
DeviceChangeHook = Callable[[str, str, bool], None]


class AgentUnavailable(RuntimeError):
    """No live connection for the requested agent."""


class CommandFailed(RuntimeError):
    def __init__(self, error: str) -> None:
        self.error = error
        super().__init__(error)


@dataclass
class AgentConnection:
    agent_id: str
    websocket: Any
    version: str = ""
    devices: list[dict[str, Any]] = field(default_factory=list)
    connected_at: str = field(default_factory=utcnow)

    async def send(self, frame: dict[str, Any]) -> None:
        await self.websocket.send_text(json.dumps(frame, ensure_ascii=False))


class Gateway:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        on_message: MessageHook | None = None,
        on_task_result: TaskResultHook | None = None,
        on_device_change: DeviceChangeHook | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        # Called for each newly stored inbound message; the push engine hangs
        # off this.  Both hooks must return promptly — the ack waits on them.
        self.on_message = on_message
        self.on_task_result = on_task_result
        # Called on each module up/down edge; the offline alerter hangs off it.
        self.on_device_change = on_device_change
        self.connections: dict[str, AgentConnection] = {}
        self._pending: dict[str, asyncio.Future] = {}

    # -- connection lifecycle ---------------------------------------------

    def authenticate(self, header: str | None) -> bool:
        token = ""
        if header and header.lower().startswith("bearer "):
            token = header[7:].strip()
        return verify_agent_token(token, self.settings.agent_token)

    async def serve(self, websocket: Any) -> None:
        """Drive one agent connection start to finish."""
        await websocket.accept()

        if not self.authenticate(websocket.headers.get("authorization")):
            log.warning("agent connection rejected: bad token")
            await websocket.close(code=CLOSE_AUTH_FAILED, reason="bad token")
            return

        agent_id: str | None = None
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    frame = json.loads(raw)
                except ValueError:
                    await websocket.close(code=CLOSE_PROTOCOL_ERROR, reason="bad json")
                    return
                if not isinstance(frame, dict) or "type" not in frame:
                    await websocket.close(code=CLOSE_PROTOCOL_ERROR, reason="no type")
                    return

                if frame["type"] == "hello":
                    agent_id = str(frame.get("agent_id") or "").strip()
                    if not agent_id:
                        await websocket.close(
                            code=CLOSE_PROTOCOL_ERROR, reason="no agent_id"
                        )
                        return
                    if agent_id in self.connections:
                        log.warning("agent %s already connected; rejecting", agent_id)
                        await websocket.close(
                            code=CLOSE_AGENT_CONFLICT, reason="already connected"
                        )
                        return
                    self._register(agent_id, websocket, frame)
                    # Keep-alive tasks are the one piece of state the agent
                    # persists on its own (D3), so a task edited or deleted
                    # while it was offline would otherwise keep running
                    # forever.  Push the full list every connect; it is a
                    # small frame and full-replace makes it self-correcting.
                    await self.push_tasks(agent_id)
                    continue

                if agent_id is None:
                    await websocket.close(
                        code=CLOSE_PROTOCOL_ERROR, reason="hello must come first"
                    )
                    return

                await self._ingest(agent_id, frame, websocket)
        except Exception as exc:
            # A disconnect surfaces as WebSocketDisconnect; anything else is
            # worth a look but must not take the server down.
            if type(exc).__name__ != "WebSocketDisconnect":
                log.exception("agent %s connection failed", agent_id or "?")
        finally:
            if agent_id is not None:
                self._unregister(agent_id)

    def _register(self, agent_id: str, websocket: Any, frame: dict[str, Any]) -> None:
        version = str(frame.get("version", ""))
        devices = frame.get("devices") or []
        self.connections[agent_id] = AgentConnection(
            agent_id=agent_id, websocket=websocket, version=version, devices=devices
        )
        self.db.upsert_agent(agent_id, version, connected=True)
        for device in devices:
            if isinstance(device, dict):
                self.db.upsert_device(agent_id, device)
                if "online" in device:
                    self._note_device(agent_id, device.get("name", ""),
                                      bool(device.get("online")))
        log.info(
            "agent %s connected (v%s, %d device(s), last_seq=%s)",
            agent_id, version or "?", len(devices), frame.get("last_seq"),
        )

    def _unregister(self, agent_id: str) -> None:
        self.connections.pop(agent_id, None)
        self.db.set_agent_connected(agent_id, False)
        # Capture which modules were up *before* flipping them: the agent's link
        # dropping takes all of them offline at once, and each such edge is what
        # arms an offline alert (a whole host going dark is the case that most
        # wants paging).  Modules already down are skipped — no re-page.
        going_down = self.db.query(
            "SELECT name FROM devices WHERE agent_id = ? AND online = 1", (agent_id,)
        )
        self.db.set_devices_offline(agent_id)
        for row in going_down:
            self._note_device(agent_id, row["name"], False)
        log.info("agent %s disconnected", agent_id)

    def _note_device(self, agent_id: str, name: str, online: bool) -> None:
        if self.on_device_change is not None and name:
            self.on_device_change(agent_id, name, online)

    # -- ingest ------------------------------------------------------------

    async def _ingest(
        self, agent_id: str, frame: dict[str, Any], websocket: Any
    ) -> None:
        kind = frame["type"]
        seq = frame.get("seq")

        if kind == "cmd_result":
            self._resolve_command(frame)

        if isinstance(seq, int):
            fresh = self.db.claim_event(agent_id, seq, kind)
            if fresh:
                try:
                    await self._apply(agent_id, kind, frame)
                except Exception:
                    log.exception("failed to apply %s seq=%s", kind, seq)
            else:
                log.debug("duplicate %s seq=%d from %s, skipped", kind, seq, agent_id)
            # Ack either way: a duplicate is still safely delivered.
            await websocket.send_text(json.dumps({"type": "ack", "seq": seq}))

    async def _apply(self, agent_id: str, kind: str, frame: dict[str, Any]) -> None:
        if kind == "sms_in":
            await self._apply_sms_in(agent_id, frame)
        elif kind == "sms_out":
            self._apply_sms_out(agent_id, frame)
        elif kind == "status":
            self._apply_status(agent_id, frame)
        elif kind == "log":
            self._apply_log(agent_id, frame)
        elif kind == "task_result":
            await self._apply_task_result(agent_id, frame)
        elif kind == "cmd_result":
            pass  # already resolved before the claim, nothing to persist
        else:
            log.debug("ignoring unknown event kind %r", kind)

    async def _apply_sms_in(self, agent_id: str, frame: dict[str, Any]) -> None:
        message_id = self.db.insert_message(
            agent_id=agent_id,
            device=frame.get("device", ""),
            direction="in",
            peer=frame.get("peer", ""),
            body=frame.get("body", ""),
            ts=frame.get("ts") or utcnow(),
            iccid=frame.get("iccid", "") or "",
            status="received",
            segments=int(frame.get("segments") or 1),
            seq=frame.get("seq"),
        )
        if self.on_message is not None:
            await self.on_message(message_id, frame)

    def _apply_sms_out(self, agent_id: str, frame: dict[str, Any]) -> None:
        self.db.insert_message(
            agent_id=agent_id,
            device=frame.get("device", ""),
            direction="out",
            peer=frame.get("peer", ""),
            body=frame.get("body", ""),
            ts=frame.get("ts") or utcnow(),
            iccid=frame.get("iccid", "") or "",
            status=frame.get("status", "sent"),
            segments=len(frame.get("refs") or []) or 1,
            seq=frame.get("seq"),
            error=frame.get("error"),
        )

    def _apply_status(self, agent_id: str, frame: dict[str, Any]) -> None:
        payload = dict(frame)
        payload["name"] = frame.get("device", "")
        device_id = self.db.upsert_device(agent_id, payload)
        self.db.record_status(device_id, frame)
        # A status frame need not carry online (some only sample signal); act
        # only on the ones that state it, so the alerter sees real edges.
        if "online" in frame:
            self._note_device(agent_id, frame.get("device", ""), bool(frame.get("online")))

    def _apply_log(self, agent_id: str, frame: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO agent_logs (agent_id, device, level, message, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                agent_id,
                frame.get("device", "") or "",
                frame.get("level", "info"),
                frame.get("message", ""),
                frame.get("ts") or utcnow(),
            ),
        )

    async def _apply_task_result(self, agent_id: str, frame: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO task_logs (task_id, ts, status, attempts, detail, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                frame.get("task_id"),
                frame.get("ts") or utcnow(),
                frame.get("status", "unknown"),
                int(frame.get("attempts") or 1),
                frame.get("detail", "") or "",
                frame.get("error"),
            ),
        )
        task_id = frame.get("task_id")
        if task_id is None:
            return

        # "skipped" means nothing was attempted, so it must not count as a run;
        # a failed attempt did happen and is what the next interval counts from.
        if frame.get("status") != "skipped":
            self.db.execute(
                "UPDATE tasks SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                (frame.get("ts") or utcnow(), frame.get("next_run_at"), task_id),
            )
        elif frame.get("next_run_at"):
            self.db.execute(
                "UPDATE tasks SET next_run_at = ? WHERE id = ?",
                (frame.get("next_run_at"), task_id),
            )

        if self.on_task_result is not None:
            await self.on_task_result(int(task_id), frame)

    # -- commands ----------------------------------------------------------

    def _resolve_command(self, frame: dict[str, Any]) -> None:
        cmd_id = frame.get("cmd_id")
        future = self._pending.pop(str(cmd_id), None)
        if future is None or future.done():
            return
        if frame.get("ok"):
            future.set_result(frame.get("data") or {})
        else:
            future.set_exception(CommandFailed(frame.get("error") or "command failed"))

    async def call(
        self, agent_id: str, frame: dict[str, Any], *, timeout: float = COMMAND_TIMEOUT
    ) -> dict[str, Any]:
        """Send a command and wait for the agent's ``cmd_result``."""
        connection = self.connections.get(agent_id)
        if connection is None:
            raise AgentUnavailable(f"agent {agent_id!r} is not connected")

        cmd_id = "c-" + secrets.token_hex(4)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = future
        try:
            await connection.send({**frame, "cmd_id": cmd_id})
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise CommandFailed(
                f"agent {agent_id} did not answer within {timeout:.0f}s"
            ) from None
        finally:
            self._pending.pop(cmd_id, None)

    async def broadcast(self, frame: dict[str, Any]) -> None:
        for connection in list(self.connections.values()):
            try:
                await connection.send(frame)
            except Exception:
                log.warning("failed to push to agent %s", connection.agent_id)

    def any_agent(self) -> str | None:
        """The agent to talk to when the caller did not name one."""
        return next(iter(self.connections), None)

    def agent_for_device(self, device_name: str) -> str | None:
        for agent_id, connection in self.connections.items():
            if any(d.get("name") == device_name for d in connection.devices):
                return agent_id
        row = self.db.one(
            "SELECT agent_id FROM devices WHERE name = ?", (device_name,)
        )
        return row["agent_id"] if row else None

    # -- keep-alive tasks --------------------------------------------------

    def tasks_for(self, agent_id: str) -> list[dict[str, Any]]:
        """This agent's keep-alive tasks, in ``sync_tasks`` wire form.

        Ownership is by ``agent_id`` when the row carries one, and by which
        agent currently owns the named device otherwise — a task created from
        the UI only names a device.
        """
        tasks = []
        for row in self.db.query("SELECT * FROM tasks ORDER BY id"):
            owner = row.get("agent_id") or self.agent_for_device(
                row.get("device", "")
            )
            if owner != agent_id:
                continue
            tasks.append({
                "id": row["id"],
                "device": row["device"],
                "name": row["name"],
                "enabled": bool(row["enabled"]),
                "action": row["action"],
                "target_number": row["target_number"],
                "content": row["content"],
                "schedule_type": row["schedule_type"],
                "schedule_expr": row["schedule_expr"],
                "jitter_seconds": row["jitter_seconds"],
                "random_suffix": bool(row["random_suffix"]),
                "retry_max": row["retry_max"],
                "notify_on_result": bool(row["notify_on_result"]),
            })
        return tasks

    async def push_tasks(self, agent_id: str) -> None:
        """Send the full task list without waiting for a receipt.

        Deliberately not ``call()``: this runs inside the connection's own
        receive loop at hello time, and that loop is what would have to read
        the ``cmd_result`` — waiting for it here would deadlock until the
        command timed out.
        """
        connection = self.connections.get(agent_id)
        if connection is None:
            return
        tasks = self.tasks_for(agent_id)
        try:
            await connection.send({"type": "sync_tasks", "tasks": tasks})
        except Exception:
            log.warning("failed to push tasks to agent %s", agent_id)
            return
        log.info("pushed %d keep-alive task(s) to %s", len(tasks), agent_id)
