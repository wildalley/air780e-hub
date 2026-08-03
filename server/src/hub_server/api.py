"""REST API.

Every route except the auth handshake requires a session cookie.  Commands
that reach the hardware (send an SMS, run a raw AT command) go through the
gateway and surface the agent's own error text rather than a generic 500 —
when a message fails to send, the +CMS code is the whole answer.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from .auth import SESSION_COOKIE, AuthError
from .db import utcnow
from .gateway import AgentUnavailable, CommandFailed
from .state import AppState

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------


class PasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordBody(BaseModel):
    current: str
    new: str = Field(min_length=1, max_length=128)


class SendSmsBody(BaseModel):
    device: str
    number: str = Field(min_length=1, max_length=32)
    body: str = Field(min_length=1, max_length=2000)


class RawAtBody(BaseModel):
    device: str
    command: str = Field(min_length=2, max_length=200)


class SimPatch(BaseModel):
    label: str | None = None
    phone_number: str | None = None
    note: str | None = None


class ChannelBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: str = Field(min_length=1, max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class RuleBody(BaseModel):
    name: str = ""
    sim_id: int | None = None
    channel_id: int
    match: Literal["all", "keyword", "regex"] = "all"
    pattern: str = ""
    template: str = ""
    priority: int = 0
    enabled: bool = True


class TaskBody(BaseModel):
    name: str = ""
    sim_id: int | None = None
    device: str
    agent_id: str = ""
    enabled: bool = True
    action: Literal["send_sms", "ping", "raw_at"] = "send_sms"
    target_number: str = "10086"
    content: str = "1"
    schedule_type: Literal["interval", "cron"] = "interval"
    schedule_expr: str = "25"
    jitter_seconds: int = Field(default=1800, ge=0, le=86400)
    random_suffix: bool = True
    retry_max: int = Field(default=3, ge=0, le=10)
    notify_on_result: bool = True


# --------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------


def build_router(state: AppState) -> APIRouter:
    router = APIRouter(prefix="/api")

    def require_session(request: Request) -> None:
        if not state.auth.validate_session(request.cookies.get(SESSION_COOKIE)):
            raise HTTPException(status_code=401, detail="not authenticated")

    guard = [Depends(require_session)]

    # -- auth --------------------------------------------------------------

    @router.get("/auth/status")
    def auth_status(request: Request) -> dict[str, Any]:
        return {
            "configured": state.auth.is_configured,
            "authenticated": state.auth.validate_session(
                request.cookies.get(SESSION_COOKIE)
            ),
        }

    def _issue_session(request: Request, response: Response) -> None:
        token = state.auth.create_session()
        # Mark the cookie Secure only when the request actually arrived over
        # HTTPS.  Hard-coding it would lock out a plain-HTTP LAN deployment,
        # since the browser would then refuse to send the cookie back at all.
        # Behind the 1Panel proxy, X-Forwarded-Proto makes this https.
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            max_age=state.settings.session_ttl_hours * 3600,
            secure=request.url.scheme == "https",
        )

    @router.post("/auth/setup")
    def auth_setup(
        body: PasswordBody, request: Request, response: Response
    ) -> dict[str, Any]:
        if state.auth.is_configured:
            raise HTTPException(status_code=409, detail="already configured")
        try:
            state.auth.set_password(body.password)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _issue_session(request, response)
        return {"ok": True}

    @router.post("/auth/login")
    def auth_login(
        body: PasswordBody, request: Request, response: Response
    ) -> dict[str, Any]:
        if not state.auth.is_configured:
            raise HTTPException(status_code=409, detail="not configured")
        if not state.auth.verify_password(body.password):
            raise HTTPException(status_code=401, detail="incorrect password")
        _issue_session(request, response)
        return {"ok": True}

    @router.post("/auth/logout")
    def auth_logout(request: Request, response: Response) -> dict[str, Any]:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            state.auth.revoke_session(token)
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @router.post("/auth/password", dependencies=guard)
    def auth_change_password(body: ChangePasswordBody) -> dict[str, Any]:
        try:
            state.auth.change_password(body.current, body.new)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "note": "all sessions were signed out"}

    # -- overview ----------------------------------------------------------

    @router.get("/overview", dependencies=guard)
    def overview() -> dict[str, Any]:
        db = state.db
        # Join the SIM so the dashboard can title each card with the card's
        # own label rather than the module's slot name.
        devices = db.query(
            "SELECT d.*, s.iccid, s.label AS sim_label, s.phone_number "
            "FROM devices d LEFT JOIN sims s ON s.id = d.sim_id ORDER BY d.name"
        )
        today = utcnow()[:10]
        return {
            "agents": db.query("SELECT * FROM agents"),
            "devices": devices,
            "sims": db.query("SELECT * FROM sims ORDER BY id"),
            "counters": {
                "messages_total": db.one(
                    "SELECT COUNT(*) AS n FROM messages"
                )["n"],
                "messages_today": db.one(
                    "SELECT COUNT(*) AS n FROM messages WHERE ts >= ?",
                    (today,),
                )["n"],
                "devices_online": sum(1 for d in devices if d["online"]),
                "devices_total": len(devices),
                "tasks_enabled": db.one(
                    "SELECT COUNT(*) AS n FROM tasks WHERE enabled = 1"
                )["n"],
            },
            "recent_messages": db.messages(limit=10),
        }

    # -- devices and SIMs --------------------------------------------------

    @router.get("/devices", dependencies=guard)
    def list_devices() -> list[dict[str, Any]]:
        return state.db.query(
            "SELECT d.*, s.iccid, s.label AS sim_label, s.phone_number "
            "FROM devices d LEFT JOIN sims s ON s.id = d.sim_id ORDER BY d.name"
        )

    @router.get("/devices/{name}/history", dependencies=guard)
    def device_history(
        name: str, hours: int = Query(24, ge=1, le=24 * 30)
    ) -> list[dict[str, Any]]:
        row = state.db.one("SELECT id FROM devices WHERE name = ?", (name,))
        if row is None:
            raise HTTPException(status_code=404, detail="no such device")
        # Compute the cutoff here rather than with SQLite's datetime('now'):
        # that function formats with a space separator, which does not compare
        # correctly against the ISO-8601 'T' timestamps stored in the column.
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat(timespec="seconds")
        return state.db.query(
            "SELECT ts, online, registered, rssi, dbm, bars, rsrp, rsrq, "
            "storage_used, storage_cap FROM device_status "
            "WHERE device_id = ? AND ts >= ? ORDER BY ts",
            (row["id"], cutoff),
        )

    @router.post("/devices/{name}/refresh", dependencies=guard)
    async def refresh_device(name: str) -> dict[str, Any]:
        agent_id = state.gateway.agent_for_device(name)
        if agent_id is None:
            raise HTTPException(status_code=404, detail="no such device")
        return await _call(agent_id, {"type": "query", "device": name, "what": "status"})

    @router.get("/sims", dependencies=guard)
    def list_sims() -> list[dict[str, Any]]:
        return state.db.query(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM messages m WHERE m.sim_id = s.id) AS message_count "
            "FROM sims s ORDER BY s.id"
        )

    @router.patch("/sims/{sim_id}", dependencies=guard)
    def patch_sim(sim_id: int, body: SimPatch) -> dict[str, Any]:
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if not fields:
            raise HTTPException(status_code=400, detail="nothing to update")
        assignments = ",".join(f"{key} = :{key}" for key in fields)
        state.db.execute(
            f"UPDATE sims SET {assignments} WHERE id = :id", {**fields, "id": sim_id}
        )
        row = state.db.one("SELECT * FROM sims WHERE id = ?", (sim_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="no such SIM")
        return row

    # -- messages ----------------------------------------------------------

    @router.get("/messages", dependencies=guard)
    def list_messages(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        sim_id: int | None = None,
        direction: Literal["in", "out"] | None = None,
        peer: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        return {
            "items": state.db.messages(
                limit=limit, offset=offset, sim_id=sim_id,
                direction=direction, peer=peer, search=search,
            ),
            "total": state.db.count_messages(),
        }

    @router.get("/conversations", dependencies=guard)
    def list_conversations(
        limit: int = Query(200, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        """One row per (card, correspondent), newest first.

        The UI is a messaging app, so the list it opens with is threads, not
        rows.  Grouping here rather than in the browser keeps it correct once
        the history is longer than one page.
        """
        return state.db.conversations(limit=limit)

    @router.post("/messages/send", dependencies=guard)
    async def send_message(body: SendSmsBody) -> dict[str, Any]:
        agent_id = state.gateway.agent_for_device(body.device)
        if agent_id is None:
            raise HTTPException(status_code=404, detail="no such device")
        return await _call(agent_id, {
            "type": "send_sms", "device": body.device,
            "number": body.number, "body": body.body,
        })

    @router.post("/at", dependencies=guard)
    async def raw_at(body: RawAtBody) -> dict[str, Any]:
        agent_id = state.gateway.agent_for_device(body.device)
        if agent_id is None:
            raise HTTPException(status_code=404, detail="no such device")
        return await _call(agent_id, {
            "type": "raw_at", "device": body.device, "command": body.command,
        })

    # -- channels and rules ------------------------------------------------

    @router.get("/channels", dependencies=guard)
    def list_channels() -> list[dict[str, Any]]:
        return state.db.query("SELECT * FROM channels ORDER BY id")

    @router.post("/channels", dependencies=guard)
    def create_channel(body: ChannelBody) -> dict[str, Any]:
        cursor = state.db.execute(
            "INSERT INTO channels (name, type, config, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (body.name, body.type, json.dumps(body.config), int(body.enabled),
             utcnow()),
        )
        return state.db.one("SELECT * FROM channels WHERE id = ?", (cursor.lastrowid,))

    @router.put("/channels/{channel_id}", dependencies=guard)
    def update_channel(channel_id: int, body: ChannelBody) -> dict[str, Any]:
        state.db.execute(
            "UPDATE channels SET name = ?, type = ?, config = ?, enabled = ? "
            "WHERE id = ?",
            (body.name, body.type, json.dumps(body.config), int(body.enabled),
             channel_id),
        )
        row = state.db.one("SELECT * FROM channels WHERE id = ?", (channel_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="no such channel")
        return row

    @router.post("/channels/{channel_id}/test", dependencies=guard)
    async def test_channel(channel_id: int) -> dict[str, Any]:
        """Really send a sample message, and report what the provider said."""
        row = state.db.one("SELECT * FROM channels WHERE id = ?", (channel_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="no such channel")
        result = await state.notifier.test_channel(row)
        if result["status"] != "ok":
            # Same reasoning as a failed SMS: the provider's own complaint is
            # the whole answer, so pass it through instead of a generic 500.
            raise HTTPException(status_code=502, detail=result["detail"])
        return {"ok": True, "detail": result["detail"]}

    @router.delete("/channels/{channel_id}", dependencies=guard)
    def delete_channel(channel_id: int) -> dict[str, Any]:
        state.db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        return {"ok": True}

    @router.get("/rules", dependencies=guard)
    def list_rules() -> list[dict[str, Any]]:
        return state.db.query(
            "SELECT r.*, c.name AS channel_name, c.type AS channel_type "
            "FROM rules r LEFT JOIN channels c ON c.id = r.channel_id "
            "ORDER BY r.priority DESC, r.id"
        )

    @router.post("/rules", dependencies=guard)
    def create_rule(body: RuleBody) -> dict[str, Any]:
        cursor = state.db.execute(
            "INSERT INTO rules "
            "(name, sim_id, channel_id, match, pattern, template, priority, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (body.name, body.sim_id, body.channel_id, body.match, body.pattern,
             body.template, body.priority, int(body.enabled)),
        )
        return state.db.one("SELECT * FROM rules WHERE id = ?", (cursor.lastrowid,))

    @router.put("/rules/{rule_id}", dependencies=guard)
    def update_rule(rule_id: int, body: RuleBody) -> dict[str, Any]:
        state.db.execute(
            "UPDATE rules SET name = ?, sim_id = ?, channel_id = ?, match = ?, "
            "pattern = ?, template = ?, priority = ?, enabled = ? WHERE id = ?",
            (body.name, body.sim_id, body.channel_id, body.match, body.pattern,
             body.template, body.priority, int(body.enabled), rule_id),
        )
        row = state.db.one("SELECT * FROM rules WHERE id = ?", (rule_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="no such rule")
        return row

    @router.delete("/rules/{rule_id}", dependencies=guard)
    def delete_rule(rule_id: int) -> dict[str, Any]:
        state.db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        return {"ok": True}

    @router.get("/notify-logs", dependencies=guard)
    def notify_logs(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return state.db.query(
            "SELECT n.*, c.name AS channel_name FROM notify_logs n "
            "LEFT JOIN channels c ON c.id = n.channel_id "
            "ORDER BY n.ts DESC LIMIT ?",
            (limit,),
        )

    # -- tasks (scheduler lands in M5) -------------------------------------

    @router.get("/tasks", dependencies=guard)
    def list_tasks() -> list[dict[str, Any]]:
        return state.db.query(
            "SELECT t.*, s.label AS sim_label, s.iccid FROM tasks t "
            "LEFT JOIN sims s ON s.id = t.sim_id ORDER BY t.id"
        )

    @router.post("/tasks", dependencies=guard)
    async def create_task(body: TaskBody) -> dict[str, Any]:
        data = body.model_dump()
        data["enabled"] = int(data["enabled"])
        data["random_suffix"] = int(data["random_suffix"])
        data["notify_on_result"] = int(data["notify_on_result"])
        data["created_at"] = utcnow()
        columns = ",".join(data)
        placeholders = ",".join(f":{key}" for key in data)
        cursor = state.db.execute(
            f"INSERT INTO tasks ({columns}) VALUES ({placeholders})", data
        )
        await _sync_tasks()
        return state.db.one("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,))

    @router.put("/tasks/{task_id}", dependencies=guard)
    async def update_task(task_id: int, body: TaskBody) -> dict[str, Any]:
        data = body.model_dump()
        data["enabled"] = int(data["enabled"])
        data["random_suffix"] = int(data["random_suffix"])
        data["notify_on_result"] = int(data["notify_on_result"])
        assignments = ",".join(f"{key} = :{key}" for key in data)
        state.db.execute(
            f"UPDATE tasks SET {assignments} WHERE id = :id", {**data, "id": task_id}
        )
        row = state.db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="no such task")
        await _sync_tasks()
        return row

    @router.delete("/tasks/{task_id}", dependencies=guard)
    async def delete_task(task_id: int) -> dict[str, Any]:
        state.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await _sync_tasks()
        return {"ok": True}

    @router.get("/task-logs", dependencies=guard)
    def task_logs(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return state.db.query(
            "SELECT l.*, t.name AS task_name FROM task_logs l "
            "LEFT JOIN tasks t ON t.id = l.task_id ORDER BY l.ts DESC LIMIT ?",
            (limit,),
        )

    # -- logs and system ---------------------------------------------------

    @router.get("/logs", dependencies=guard)
    def agent_logs(limit: int = Query(200, ge=1, le=2000)) -> list[dict[str, Any]]:
        return state.db.query(
            "SELECT * FROM agent_logs ORDER BY ts DESC LIMIT ?", (limit,)
        )

    @router.get("/system/agent-token", dependencies=guard)
    def agent_token() -> dict[str, Any]:
        """The token the agent must present.  Shown so it can be copied once."""
        return {"token": state.settings.agent_token}

    @router.post("/system/purge", dependencies=guard)
    def purge() -> dict[str, Any]:
        return state.db.purge(
            message_days=state.settings.message_retention_days,
            status_days=state.settings.status_retention_days,
        )

    # -- helpers -----------------------------------------------------------

    async def _call(agent_id: str, frame: dict[str, Any]) -> dict[str, Any]:
        try:
            return await state.gateway.call(agent_id, frame)
        except AgentUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except CommandFailed as exc:
            # The agent's own error text (a +CMS code, "device offline", …) is
            # far more useful than a generic failure.
            raise HTTPException(status_code=502, detail=exc.error) from exc

    async def _sync_tasks() -> None:
        """Push the full task list to every connected agent.

        Fire-and-forget on purpose: the database is the source of truth for
        what a task *is*, and the push is best-effort — an agent that misses
        it gets the same list again on its next connect, and full-replace
        makes a repeat harmless.  Waiting for each agent's receipt here would
        stall the Web request behind a wedged agent for the command timeout.
        """
        for agent_id in list(state.gateway.connections):
            await state.gateway.push_tasks(agent_id)

    router.sync_tasks = _sync_tasks  # type: ignore[attr-defined]
    return router
