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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import PROTOCOL_VERSION, __version__
from .auth import verify_agent_token, verify_agent_token_hash
from .config import Settings
from .db import Database, _pdu_is_data, utcnow

log = logging.getLogger(__name__)


def _optional_int(value: Any) -> int | None:
    """Coerce a frame field to int, or None when it is absent or malformed.

    Frames come off the wire, so a field can be missing, null, or a string. A
    diagnostic column is not worth refusing a message over.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_zero(value: Any) -> float:
    """Coerce a frame field to float, treating anything malformed as zero.

    Same reasoning as ``_optional_int``, with a sharper consequence: a handler
    that raises rolls its event back and closes the stream, so the agent
    replays the identical frame and the pair loops on it forever.  A ring
    duration is not worth wedging ingest over.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


CLOSE_AUTH_FAILED = 4001
CLOSE_PROTOCOL_ERROR = 4002
CLOSE_AGENT_CONFLICT = 4003
CLOSE_INTERNAL_ERROR = 1011

COMMAND_TIMEOUT = 30.0
SETTING_PREVIOUS_AGENT_TOKEN_HASH = "previous_agent_token_hash"
SETTING_PREVIOUS_AGENT_TOKEN_EXPIRES_AT = "previous_agent_token_expires_at"

# Below the EC618's own 3.3 V floor the module is not merely poorly fed: a
# transmit burst can drop it under the brown-out point and reset it mid-send.
# The Agent decides what counts as *low* (its config knows the supply); this is
# only the line between "watch it" and "it can fail right now", which follows
# from the chip rather than from any one installation.
VOLTAGE_CRITICAL_MV = 3300

RECOVERY_ACTION_NAMES = {
    "serial_reconnect": "串口重连",
    "operator_reselect": "自动选择运营商",
    "radio_cycle": "射频重启",
    "module_reset": "模块重启",
    "registration_recovery": "网络注册恢复",
    "registration_watch": "网络注册监测",
}

MessageHook = Callable[[int, dict[str, Any]], Awaitable[None]]
TaskResultHook = Callable[[int, dict[str, Any]], Awaitable[None]]
CallHook = Callable[[int, dict[str, Any]], Awaitable[None]]
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
    protocol_version: int = 0
    devices: list[dict[str, Any]] = field(default_factory=list)
    connected_at: str = field(default_factory=utcnow)

    async def send(self, frame: dict[str, Any]) -> None:
        await self.websocket.send_text(json.dumps(frame, ensure_ascii=False))


@dataclass
class AppliedEvent:
    """External work to start only after the event transaction commits."""

    message_id: int | None = None
    task_id: int | None = None
    call_id: int | None = None
    device_change: tuple[str, bool] | None = None
    command_result: bool = False


class Gateway:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        on_message: MessageHook | None = None,
        on_task_result: TaskResultHook | None = None,
        on_call: CallHook | None = None,
        on_device_change: DeviceChangeHook | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        # Called for each newly stored inbound message; the push engine hangs
        # off this.  Both hooks must return promptly — the ack waits on them.
        self.on_message = on_message
        self.on_task_result = on_task_result
        self.on_call = on_call
        # Called on each module up/down edge; the offline alerter hangs off it.
        self.on_device_change = on_device_change
        self.connections: dict[str, AgentConnection] = {}
        self._pending: dict[str, asyncio.Future] = {}

    @property
    def pending_command_count(self) -> int:
        return len(self._pending)

    # -- connection lifecycle ---------------------------------------------

    def authenticate(self, header: str | None) -> bool:
        token = ""
        if header and header.lower().startswith("bearer "):
            token = header[7:].strip()
        if verify_agent_token(token, self.settings.agent_token):
            return True
        expected_hash = str(
            self.db.get_setting(SETTING_PREVIOUS_AGENT_TOKEN_HASH, "") or ""
        )
        expires_at = str(
            self.db.get_setting(SETTING_PREVIOUS_AGENT_TOKEN_EXPIRES_AT, "") or ""
        )
        if not expected_hash or not expires_at:
            return False
        try:
            expires = datetime.fromisoformat(expires_at)
        except ValueError:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            return False
        return verify_agent_token_hash(token, expected_hash)

    async def serve(self, websocket: Any) -> None:
        """Drive one agent connection start to finish."""
        await websocket.accept()

        if not self.authenticate(websocket.headers.get("authorization")):
            log.warning("agent connection rejected: bad token")
            await websocket.close(code=CLOSE_AUTH_FAILED, reason="bad token")
            return

        # Deployment checks need to verify the complete HTTP upgrade and the
        # bearer token without registering a fake agent in the database.
        if websocket.query_params.get("self_check") == "1":
            await websocket.send_text(json.dumps({"type": "self_check", "ok": True}))
            await websocket.close(code=1000, reason="self-check complete")
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
                try:
                    # Stop this ordered stream before any higher cumulative
                    # ACK can overtake the event that just rolled back.
                    await websocket.close(
                        code=CLOSE_INTERNAL_ERROR,
                        reason="event application failed",
                    )
                except Exception:
                    pass
        finally:
            if agent_id is not None:
                self._unregister(agent_id)

    def _register(self, agent_id: str, websocket: Any, frame: dict[str, Any]) -> None:
        version = str(frame.get("version", ""))
        protocol_version = _optional_int(frame.get("protocol_version")) or 0
        devices = frame.get("devices") or []
        self.connections[agent_id] = AgentConnection(
            agent_id=agent_id,
            websocket=websocket,
            version=version,
            protocol_version=protocol_version,
            devices=devices,
        )
        self.db.upsert_agent(agent_id, version, protocol_version, connected=True)
        fingerprint = f"agent-version:{agent_id}"
        problems = []
        if version != __version__:
            problems.append(f"Agent {version or '未上报'}，Server {__version__}")
        if protocol_version != PROTOCOL_VERSION:
            problems.append(
                f"Agent 协议 {protocol_version or '未上报'}，Server 协议 {PROTOCOL_VERSION}"
            )
        if problems:
            self.db.open_incident(
                fingerprint,
                kind="agent_version_mismatch",
                severity=("critical" if protocol_version != PROTOCOL_VERSION else "warning"),
                source=agent_id,
                title="Agent 与 Server 版本不一致",
                detail="；".join(problems),
            )
        else:
            self.db.resolve_incident(fingerprint, detail="Agent 与 Server 版本已一致")
        for device in devices:
            if isinstance(device, dict):
                self.db.upsert_device(agent_id, device)
                if "online" in device:
                    self._note_device(agent_id, device.get("name", ""),
                                      bool(device.get("online")))
        log.info(
            "agent %s connected (v%s, protocol=%s, %d device(s), last_seq=%s)",
            agent_id, version or "?", protocol_version or "?", len(devices),
            frame.get("last_seq"),
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

        if isinstance(seq, int):
            # The callback is synchronous on purpose: Database holds one
            # short transaction and its lock around all persistence writes.
            # Notifications and alert hooks run only after COMMIT below.
            fresh, applied = self.db.apply_event(
                agent_id,
                seq,
                kind,
                lambda: self._apply(agent_id, kind, frame),
            )
            if not fresh:
                log.debug("duplicate %s seq=%d from %s, skipped", kind, seq, agent_id)
            else:
                await self._after_apply(agent_id, kind, frame, applied)
            # Ack either way: a duplicate is still safely delivered.
            await websocket.send_text(json.dumps({"type": "ack", "seq": seq}))

    def _apply(
        self, agent_id: str, kind: str, frame: dict[str, Any]
    ) -> AppliedEvent:
        applied = AppliedEvent()
        if kind == "sms_in":
            applied.message_id = self._apply_sms_in(agent_id, frame)
        elif kind == "sms_out":
            self._apply_sms_out(agent_id, frame)
        elif kind == "sms_delivery":
            self._apply_sms_delivery(agent_id, frame)
        elif kind == "status":
            applied.device_change = self._apply_status(agent_id, frame)
        elif kind == "call_event":
            applied.call_id = self._apply_call_event(agent_id, frame)
        elif kind == "log":
            self._apply_log(agent_id, frame)
        elif kind == "task_result":
            applied.task_id = self._apply_task_result(agent_id, frame)
        elif kind == "cmd_result":
            applied.command_result = True
        else:
            log.debug("ignoring unknown event kind %r", kind)
        return applied

    async def _after_apply(
        self,
        agent_id: str,
        kind: str,
        frame: dict[str, Any],
        applied: AppliedEvent,
    ) -> None:
        """Start non-durable side effects after the event is safely stored."""
        try:
            if applied.command_result:
                self._resolve_command(frame)
            if applied.device_change is not None:
                device, online = applied.device_change
                self._note_device(agent_id, device, online)
            if applied.message_id is not None and self.on_message is not None:
                await self.on_message(applied.message_id, frame)
            if applied.task_id is not None and self.on_task_result is not None:
                await self.on_task_result(applied.task_id, frame)
            if applied.call_id is not None and self.on_call is not None:
                await self.on_call(applied.call_id, frame)
        except Exception:
            # Persistence has committed and replaying it cannot repair an
            # in-process callback.  Keep the event durable and log the hook
            # failure instead of replaying and duplicating business data.
            log.exception("post-apply hook failed for %s", kind)

    def _apply_sms_in(self, agent_id: str, frame: dict[str, Any]) -> int:
        raw_pdu = frame.get("pdu") or None
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
            # The agent has always sent these; dropping them left a garbled
            # message undiagnosable once the modem's copy was deleted.
            raw_pdu=raw_pdu,
            dcs=_optional_int(frame.get("dcs")),
            # Recheck the PDU so a stale Agent cannot reintroduce mojibake
            # during a Server-first rolling upgrade.
            is_binary=bool(frame.get("binary"))
            or bool(raw_pdu and _pdu_is_data(raw_pdu)),
            # Optional v1 extension: an Agent old enough not to send these
            # simply has no salvage to report, and the row reads as an
            # undamaged message — which is what it looked like before.
            truncated=bool(frame.get("truncated")),
            recovered_body=frame.get("recovered_text") or None,
            recovered_code=frame.get("code") or None,
        )
        return message_id

    def _apply_sms_out(self, agent_id: str, frame: dict[str, Any]) -> None:
        references = [
            value for raw in (frame.get("refs") or [])
            if (value := _optional_int(raw)) is not None
        ]
        frame_status = str(frame.get("status") or "sent")
        stored_status = "pending" if frame_status == "sent" and references else frame_status
        message_id = self.db.insert_message(
            agent_id=agent_id,
            device=frame.get("device", ""),
            direction="out",
            peer=frame.get("peer", ""),
            body=frame.get("body", ""),
            ts=frame.get("ts") or utcnow(),
            iccid=frame.get("iccid", "") or "",
            status=stored_status,
            segments=len(references) or 1,
            seq=frame.get("seq"),
            error=frame.get("error"),
        )
        if references:
            self.db.attach_sms_segments(
                message_id=message_id,
                agent_id=agent_id,
                device=str(frame.get("device") or ""),
                recipient=str(frame.get("peer") or ""),
                references=references,
                submitted_at=str(frame.get("ts") or utcnow()),
            )
        # Aggregate per module, not per message: a fingerprint carrying
        # message_id could never recover, so every failed send would leave a
        # permanently active incident behind.
        fingerprint = f"sms-send:{agent_id}:{frame.get('device', '')}"
        status = frame_status
        if status == "failed":
            self.db.open_incident(
                fingerprint,
                kind="sms_send_failed",
                severity="warning",
                source=f"{agent_id}/{frame.get('device', '')}",
                title="短信发送失败",
                detail=str(frame.get("error") or "运营商或模块未返回成功状态"),
            )
        elif status in {"sent", "delivered"}:
            self.db.resolve_incident(fingerprint, detail="短信发送已恢复")

    def _apply_sms_delivery(self, agent_id: str, frame: dict[str, Any]) -> None:
        reference = _optional_int(frame.get("reference"))
        if reference is None or not 0 <= reference <= 255:
            log.warning(
                "invalid SMS delivery reference from %s: %r",
                agent_id,
                frame.get("reference"),
            )
            return
        status_code = _optional_int(frame.get("status_code"))
        if status_code is not None and not 0 <= status_code <= 255:
            status_code = None
        self.db.record_sms_delivery(
            agent_id=agent_id,
            device=str(frame.get("device") or ""),
            reference=reference,
            recipient=str(frame.get("peer") or ""),
            status_code=status_code,
            status=str(frame.get("status") or "pending"),
            service_center_ts=frame.get("service_center_ts"),
            discharge_ts=frame.get("discharge_ts"),
            reported_at=str(frame.get("ts") or utcnow()),
            raw_pdu=frame.get("pdu") or None,
            event_seq=_optional_int(frame.get("seq")),
        )

    def _apply_status(
        self, agent_id: str, frame: dict[str, Any]
    ) -> tuple[str, bool] | None:
        payload = dict(frame)
        payload["name"] = frame.get("device", "")
        device_id = self.db.upsert_device(agent_id, payload)
        self.db.record_status(device_id, frame)
        # A status frame need not carry online (some only sample signal); act
        # only on the ones that state it, so the alerter sees real edges.
        device_change = None
        if "online" in frame:
            device_change = (
                str(frame.get("device") or ""),
                bool(frame.get("online")),
            )
        if "registered" in frame or frame.get("online") is False:
            fingerprint = f"network-registration:{agent_id}:{frame.get('device', '')}"
            if frame.get("online") is False:
                # An offline module can neither register nor send.  Leaving
                # these open would strand extra incidents next to the offline
                # one and keep the nav badge lit after the real problem is
                # acknowledged; device_offline already covers this state.
                self.db.resolve_incident(
                    fingerprint, detail="模块已离线，注册状态由掉线事件跟踪"
                )
                self.db.resolve_incident(
                    f"sms-send:{agent_id}:{frame.get('device', '')}",
                    detail="模块已离线，发送状态由掉线事件跟踪",
                )
            elif frame.get("radio_enabled") is False:
                # Flight mode is an explicit operator choice, not a network
                # failure.  It must not open an incident that can only resolve
                # after the same operator turns RF back on.
                self.db.resolve_incident(
                    fingerprint, detail="射频已由管理员关闭"
                )
            elif not frame.get("registered"):
                self.db.open_incident(
                    fingerprint,
                    kind="network_unregistered",
                    severity="warning",
                    source=f"{agent_id}/{frame.get('device', '')}",
                    title="模块未注册到移动网络",
                    detail="模块在线，但当前未注册到运营商网络",
                )
            else:
                self.db.resolve_incident(fingerprint, detail="移动网络注册已恢复")
        self._apply_supply_voltage(agent_id, frame)
        return device_change

    def _apply_supply_voltage(self, agent_id: str, frame: dict[str, Any]) -> None:
        """Open or resolve the low-supply incident for one status frame.

        The threshold arrives in the frame rather than being configured here:
        it is a property of the module's own supply, which only the Agent knows.
        A frame that carries no reading is left alone — an older Agent, or a
        firmware that refuses ``AT+CBC``, must not resolve a real incident by
        being silent about it.
        """
        device = str(frame.get("device") or "")
        voltage = _optional_int(frame.get("voltage_mv"))
        threshold = _optional_int(frame.get("low_voltage_mv"))
        if voltage is None or not threshold:
            return

        fingerprint = f"supply-voltage:{agent_id}:{device}"
        if frame.get("online") is False:
            # The last reading before the module went away is not evidence about
            # now, and an offline module cannot produce a new one to clear this.
            self.db.resolve_incident(
                fingerprint, detail="模块已离线，供电状态由掉线事件跟踪"
            )
            return
        if voltage >= threshold:
            self.db.resolve_incident(
                fingerprint, detail=f"供电已恢复至 {voltage} mV"
            )
            return

        # Two levels, because they call for different responses: a little low is
        # worth watching, while below the EC618's own floor the module can brown
        # out mid-transmit and the symptom shows up as random unregistrations.
        critical = voltage < VOLTAGE_CRITICAL_MV
        self.db.open_incident(
            fingerprint,
            kind="device_supply_voltage",
            severity="critical" if critical else "warning",
            source=f"{agent_id}/{device}",
            title="模块供电电压过低" if critical else "模块供电电压偏低",
            detail=(
                f"当前 {voltage} mV，低于阈值 {threshold} mV"
                + (
                    "；已低于模块标称下限，发送时可能掉电重启，"
                    "表现为随机掉网。请检查供电与线材。"
                    if critical
                    else "。USB 线材或供电可能供流不足，建议更换。"
                )
            ),
        )

    def _apply_call_event(self, agent_id: str, frame: dict[str, Any]) -> int:
        """Store one call attempt.

        ``seq`` is carried through so the row can be traced back to the queued
        event that produced it, the same way an SMS row can.
        """
        return self.db.insert_call(
            agent_id=agent_id,
            device=str(frame.get("device") or ""),
            direction=str(frame.get("direction") or "out"),
            ts=str(frame.get("ts") or utcnow()),
            peer=str(frame.get("peer") or ""),
            iccid=str(frame.get("iccid") or ""),
            outcome=str(frame.get("outcome") or ""),
            reached_network=bool(frame.get("reached_network")),
            ring_seconds=_float_or_zero(frame.get("ring_seconds")),
            detail=str(frame.get("detail") or ""),
            seq=_optional_int(frame.get("seq")),
        )

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
        if frame.get("event") == "device_recovery":
            self._apply_device_recovery(agent_id, frame)

    def _apply_device_recovery(self, agent_id: str, frame: dict[str, Any]) -> None:
        device = str(frame.get("device") or "")
        action = str(frame.get("action") or "")
        outcome = str(frame.get("outcome") or "")
        if not device or not action:
            return

        fingerprint = f"device-recovery:{agent_id}:{device}"
        if outcome in {"succeeded", "cancelled"}:
            detail = "模块自动恢复成功" if outcome == "succeeded" else "自动恢复已取消"
            self.db.resolve_incident(fingerprint, detail=detail)
            return
        if outcome not in {"started", "exhausted"}:
            # A failed action leaves the incident opened by its matching
            # `started` event active while the Agent waits for the next stage.
            return

        action_name = RECOVERY_ACTION_NAMES.get(action, action)
        attempt = _optional_int(frame.get("attempt"))
        reason = str(frame.get("reason") or "")
        detail_parts = [action_name]
        if attempt is not None:
            detail_parts.append(f"第 {attempt} 次恢复动作")
        if reason:
            detail_parts.append(reason)
        exhausted = outcome == "exhausted"
        self.db.open_incident(
            fingerprint,
            kind="device_recovery",
            severity="critical" if exhausted else "warning",
            source=f"{agent_id}/{device}",
            title="设备自动恢复已达到限频上限" if exhausted else "设备正在自动恢复",
            detail="；".join(detail_parts),
        )

    def _apply_task_result(self, agent_id: str, frame: dict[str, Any]) -> int | None:
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
            return None

        task = self.db.one("SELECT name FROM tasks WHERE id = ?", (task_id,)) or {}
        fingerprint = f"keepalive-task:{task_id}"
        if frame.get("status") == "failed":
            self.db.open_incident(
                fingerprint,
                kind="task_failed",
                severity="warning",
                source=task.get("name") or f"任务 {task_id}",
                title=f"{task.get('name') or f'保号任务 {task_id}'} 执行失败",
                detail=str(frame.get("error") or frame.get("detail") or "执行失败"),
            )
        elif frame.get("status") == "ok":
            self.db.resolve_incident(fingerprint, detail="保号任务执行已恢复")

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

        return int(task_id)

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
        except TimeoutError:
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
