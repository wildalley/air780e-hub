"""REST API.

Every route except the auth handshake requires a session cookie.  Commands
that reach the hardware (send an SMS, run a raw AT command) go through the
gateway and surface the agent's own error text rather than a generic 500 —
when a message fails to send, the +CMS code is the whole answer.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import secrets
import shutil
import tempfile
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import __version__
from .alerts import SETTING_ENABLED
from .auth import SESSION_COOKIE, AuthError, hash_agent_token
from .config import ConfigError
from .db import SETTING_MESSAGE_RETENTION_DAYS, MigrationFailed, utcnow
from .gateway import (
    SETTING_PREVIOUS_AGENT_TOKEN_EXPIRES_AT,
    SETTING_PREVIOUS_AGENT_TOKEN_HASH,
    AgentUnavailable,
    CommandFailed,
)
from .notify import match_rules
from .state import AppState

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# device history downsampling
# --------------------------------------------------------------------------

# Roughly how many points a history series should carry, whatever the window.
# A chart is around 800px wide, so ~360 points is already more than it can
# resolve; past that the extra rows only cost transfer and client-side merging.
HISTORY_TARGET_POINTS = 360

# Bucket widths in seconds, coarsest last.  The first one that brings the
# window under the target wins.  Modules report every ~30s, so short windows
# land on the 30s bucket and stay effectively raw.
HISTORY_BUCKETS = (30, 60, 300, 900, 1800, 3600, 7200, 21600)


def history_bucket_seconds(hours: int) -> int:
    """Bucket width for a window of *hours*, bounding the row count.

    Without this the response carried every stored sample: a 7-day window
    across ten modules reporting every 30s is over 200k rows, rebuilt every
    15s by the dashboard's refresh.  Bucketing makes the size a function of
    the window instead of the sampling rate.
    """
    span = hours * 3600
    for width in HISTORY_BUCKETS:
        if span / width <= HISTORY_TARGET_POINTS:
            return width
    return HISTORY_BUCKETS[-1]


# How each column survives being collapsed into a bucket.  The rules differ
# because the columns mean different things:
#
#   signal metrics  AVG — the series is read as a trend, so the mean is the
#                   honest summary of a bucket.
#   online/reg.     MIN — a bucket the module dropped out of must read as down.
#                   Averaging would bury a two-minute outage inside a 30-minute
#                   point, which is the one thing an operations view must not do.
#   storage         MAX — a high-water mark.  Storage matters when it is nearly
#                   full, and the peak is what says so; a mean would hide it.
HISTORY_AGGREGATES = (
    ("online", "MIN({c})"),
    ("registered", "MIN({c})"),
    ("rssi", "CAST(ROUND(AVG({c})) AS INTEGER)"),
    ("dbm", "CAST(ROUND(AVG({c})) AS INTEGER)"),
    ("bars", "CAST(ROUND(AVG({c})) AS INTEGER)"),
    ("rsrp", "CAST(ROUND(AVG({c})) AS INTEGER)"),
    ("rsrq", "CAST(ROUND(AVG({c})) AS INTEGER)"),
    ("storage_used", "MAX({c})"),
    ("storage_cap", "MAX({c})"),
)


def history_columns(prefix: str = "") -> str:
    """Render the aggregate list, qualified by *prefix* where a join needs it.

    ``devices`` carries its own ``online`` and ``registered``, so the joined
    query must say which table it means.
    """
    return ", ".join(
        expression.format(c=f"{prefix}{name}") + f" AS {name}"
        for name, expression in HISTORY_AGGREGATES
    )

# Bucket start, rendered back into the same UTC ISO-8601 shape the raw column
# stores, so the response is indistinguishable in form from an unbucketed one.
_BUCKET_TS = (
    "strftime('%Y-%m-%dT%H:%M:%S+00:00', "
    "CAST(strftime('%s', {ts}) / {width} AS INTEGER) * {width}, 'unixepoch')"
)


# --------------------------------------------------------------------------
# paged list responses
# --------------------------------------------------------------------------


def paged(
    db: Any,
    *,
    select: str,
    source: str,
    order: str,
    limit: int,
    offset: int,
    where: str = "",
    params: tuple = (),
    count_source: str | None = None,
) -> dict[str, Any]:
    """One page of an append-only list, plus the unpaged total.

    The log views used to take a bare ``LIMIT`` with no offset, so they were
    not a first page but the *only* page: everything older than the newest N
    rows was unreachable from the UI however long retention kept it.  ``total``
    comes from the same filter as the page, so the UI can say where the window
    sits in the whole set.

    *count_source* narrows the count to the base table.  Every join here is a
    LEFT JOIN onto a primary key, so it cannot change the row count — counting
    across it would give the same number for more work.
    """
    clause = f" {where}" if where else ""
    items = db.query(
        f"SELECT {select} FROM {source}{clause} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    row = db.one(f"SELECT COUNT(*) AS n FROM {count_source or source}{clause}", params)
    return {"items": items, "total": int(row["n"]) if row else 0}


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


class RadioBody(BaseModel):
    enabled: bool


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


class ReadBody(BaseModel):
    sim_id: int | None = None
    peer: str = Field(min_length=1, max_length=32)


class RulePreviewBody(BaseModel):
    sim_id: int | None = None
    peer: str = ""
    body: str = Field(min_length=1, max_length=2000)


class NotifySettingsBody(BaseModel):
    # 0 keeps messages forever; the cap is a decade so a fat-fingered value
    # can never park the purge on a nonsense window.
    message_retention_days: int = Field(ge=0, le=3650)
    offline_alerts_enabled: bool


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


class IncidentStatusBody(BaseModel):
    status: Literal["active", "acknowledged", "resolved"]


class RotateAgentTokenBody(BaseModel):
    grace_minutes: int = Field(default=60, ge=0, le=7 * 24 * 60)


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
        # A trusted reverse proxy supplies X-Forwarded-Proto.
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

    @router.get("/devices/history", dependencies=guard)
    def all_device_history(
        hours: int = Query(24, ge=1, le=24 * 30),
    ) -> dict[str, list[dict[str, Any]]]:
        """Return every device series in one request for the dashboard.

        Downsampled into time buckets — see ``history_bucket_seconds``.  The
        response shape is the same as an unbucketed one, so a caller reading
        points off it needs no changes; only the density differs.
        """
        cutoff = (
            datetime.now(UTC) - timedelta(hours=hours)
        ).isoformat(timespec="seconds")
        width = history_bucket_seconds(hours)
        bucket = _BUCKET_TS.format(ts="s.ts", width=width)
        names = [row["name"] for row in state.db.query("SELECT name FROM devices")]
        grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
        rows = state.db.query(
            f"SELECT d.name, {bucket} AS ts, {history_columns('s.')} "
            "FROM device_status s JOIN devices d ON d.id = s.device_id "
            "WHERE s.ts >= ? "
            f"GROUP BY d.name, CAST(strftime('%s', s.ts) / {width} AS INTEGER) "
            "ORDER BY d.name, ts",
            (cutoff,),
        )
        for row in rows:
            grouped.setdefault(row.pop("name"), []).append(row)
        return grouped

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
            datetime.now(UTC) - timedelta(hours=hours)
        ).isoformat(timespec="seconds")
        width = history_bucket_seconds(hours)
        bucket = _BUCKET_TS.format(ts="ts", width=width)
        return state.db.query(
            f"SELECT {bucket} AS ts, {history_columns()} "
            "FROM device_status WHERE device_id = ? AND ts >= ? "
            f"GROUP BY CAST(strftime('%s', ts) / {width} AS INTEGER) "
            "ORDER BY ts",
            (row["id"], cutoff),
        )

    @router.post("/devices/{name}/refresh", dependencies=guard)
    async def refresh_device(name: str) -> dict[str, Any]:
        agent_id = state.gateway.agent_for_device(name)
        if agent_id is None:
            raise HTTPException(status_code=404, detail="no such device")
        return await _call(agent_id, {"type": "query", "device": name, "what": "status"})

    @router.post("/devices/{name}/radio", dependencies=guard)
    async def set_device_radio(name: str, body: RadioBody) -> dict[str, Any]:
        agent_id = state.gateway.agent_for_device(name)
        if agent_id is None:
            raise HTTPException(status_code=404, detail="no such device")
        return await _call(
            agent_id,
            {"type": "set_radio", "device": name, "enabled": body.enabled},
        )

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
        # 2000, not 500: the thread view reads a conversation back by growing
        # this window on demand.  Growing one window beats paging a transcript —
        # an offset page boundary can gap or repeat when an SMS lands mid-scroll.
        # Only an explicit "load older" raises it; nothing here polls.
        limit: int = Query(50, ge=1, le=2000),
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
            "total": state.db.count_messages(
                sim_id=sim_id, direction=direction, peer=peer, search=search
            ),
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

    @router.post("/messages/read", dependencies=guard)
    def mark_read(body: ReadBody) -> dict[str, Any]:
        """Mark one conversation's incoming messages as read (opening it)."""
        marked = state.db.mark_read(sim_id=body.sim_id, peer=body.peer)
        return {"ok": True, "marked": marked}

    @router.get("/messages/unread", dependencies=guard)
    def unread_total() -> dict[str, Any]:
        return {"total": state.db.unread_total()}

    @router.get("/messages/export", dependencies=guard)
    def export_messages(
        limit: int | None = Query(None, ge=1, le=1_000_000),
        sim_id: int | None = None,
        peer: str | None = None,
        search: str | None = None,
    ) -> StreamingResponse:
        """Stream stored messages as CSV without materialising the export."""
        import csv
        import io

        def generate() -> Iterable[str]:
            buffer = io.StringIO()
            writer = csv.writer(buffer)

            def line(values: list[Any]) -> str:
                buffer.seek(0)
                buffer.truncate(0)
                writer.writerow(values)
                return buffer.getvalue()

            # BOM makes Excel recognise the UTF-8 Chinese payload.
            yield "\ufeff"
            yield line([
                "id", "ts", "direction", "sim_id", "sim_label", "peer",
                "body", "status",
            ])
            for message in state.db.iter_messages(
                limit=limit, sim_id=sim_id, peer=peer, search=search
            ):
                yield line([
                    message["id"],
                    message["ts"],
                    message["direction"],
                    message["sim_id"] or "",
                    message.get("sim_label") or "",
                    message["peer"],
                    message["body"],
                    message["status"],
                ])

        return StreamingResponse(
            generate(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="messages.csv"',
                "Cache-Control": "no-store",
            },
        )

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

    @router.post("/rules/preview", dependencies=guard)
    def preview_rules(body: RulePreviewBody) -> list[dict[str, Any]]:
        """Which rules would fire for this message, with the rendered payload.

        The Notify page's debugger: paste a real message and see the exact
        pushes it would produce — rule name, channel, and rendered text —
        without touching any provider.
        """
        sim = (
            state.db.one("SELECT * FROM sims WHERE id = ?", (body.sim_id,))
            if body.sim_id is not None
            else None
        )
        context = state.notifier.context_for(
            {
                "peer": body.peer,
                "body": body.body,
                "ts": utcnow(),
                "sim_iccid": sim["iccid"] if sim else "",
                "sim_label": sim["label"] if sim else "",
                "phone_number": sim["phone_number"] if sim else "",
                "device": "",
            }
        )
        result: list[dict[str, Any]] = []
        for rule, channel in match_rules(
            state.db, sim_id=body.sim_id, body=body.body
        ):
            payload = state.notifier.render_payload(channel, context, rule=rule)
            result.append(
                {
                    "rule_id": rule["id"],
                    "rule_name": rule["name"] or f"规则 {rule['id']}",
                    "channel_id": channel["id"],
                    "channel_name": channel["name"],
                    "priority": rule["priority"],
                    "text": payload.text,
                    "title": payload.title,
                }
            )
        return result

    @router.get("/stats/messages", dependencies=guard)
    def message_stats(
        days: int = Query(30, ge=1, le=365),
    ) -> list[dict[str, Any]]:
        """Daily per-card message counts for the dashboard trend chart."""
        since = (datetime.now(UTC) - timedelta(days=days - 1)).date().isoformat()
        rows = state.db.query(
            "SELECT date(ts) AS day, sim_id, "
            "       SUM(CASE WHEN direction = 'in' THEN 1 ELSE 0 END) AS received, "
            "       SUM(CASE WHEN direction = 'out' THEN 1 ELSE 0 END) AS sent "
            "FROM messages WHERE date(ts) >= ? "
            "GROUP BY date(ts), sim_id ORDER BY day",
            (since,),
        )
        sims = {s["id"]: s for s in state.db.query("SELECT * FROM sims")}
        for row in rows:
            sim = sims.get(row["sim_id"])
            row["sim_label"] = sim["label"] or sim["iccid"] if sim else None
            row["received"] = int(row["received"] or 0)
            row["sent"] = int(row["sent"] or 0)
        return rows

    @router.get("/notify-logs", dependencies=guard)
    def notify_logs(
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return paged(
            state.db,
            select="n.*, c.name AS channel_name",
            source="notify_logs n LEFT JOIN channels c ON c.id = n.channel_id",
            count_source="notify_logs n",
            order="n.ts DESC, n.id DESC",
            limit=limit,
            offset=offset,
        )

    # -- notify settings ---------------------------------------------------
    # Operator-tunable knobs surfaced on the Notify page: how long SMS are
    # kept before the housekeeping purge deletes them, and whether a module
    # dropping off the bus pages anyone.  Both live in the settings table so a
    # change survives a restart and takes effect without redeploying.

    def _notify_settings() -> dict[str, Any]:
        return {
            "message_retention_days": state.message_retention_days,
            "offline_alerts_enabled": bool(state.db.get_setting(SETTING_ENABLED, True)),
        }

    @router.get("/notify-settings", dependencies=guard)
    def get_notify_settings() -> dict[str, Any]:
        return _notify_settings()

    @router.put("/notify-settings", dependencies=guard)
    def update_notify_settings(body: NotifySettingsBody) -> dict[str, Any]:
        state.db.set_setting(SETTING_MESSAGE_RETENTION_DAYS, body.message_retention_days)
        state.db.set_setting(SETTING_ENABLED, body.offline_alerts_enabled)
        return _notify_settings()

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

    @router.post("/tasks/{task_id}/run", dependencies=guard)
    async def run_task(task_id: int) -> dict[str, Any]:
        task = state.db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if task is None:
            raise HTTPException(status_code=404, detail="no such task")
        agent_id = task.get("agent_id") or state.gateway.agent_for_device(task["device"])
        if not agent_id:
            raise HTTPException(status_code=503, detail="task device has no agent")

        # Re-send the authoritative definition first. This keeps an immediate
        # run correct even when it follows an edit before the agent reconnects.
        await state.gateway.push_tasks(agent_id)
        return await _call(agent_id, {"type": "run_task", "task_id": task_id})

    @router.get("/task-logs", dependencies=guard)
    def task_logs(
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return paged(
            state.db,
            select="l.*, t.name AS task_name",
            source="task_logs l LEFT JOIN tasks t ON t.id = l.task_id",
            count_source="task_logs l",
            order="l.ts DESC, l.id DESC",
            limit=limit,
            offset=offset,
        )

    # -- logs and system ---------------------------------------------------

    @router.get("/logs", dependencies=guard)
    def agent_logs(
        limit: int = Query(200, ge=1, le=2000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return paged(
            state.db,
            select="*",
            source="agent_logs",
            order="ts DESC, id DESC",
            limit=limit,
            offset=offset,
        )

    # -- operations -------------------------------------------------------

    @router.get("/operations/diagnostics", dependencies=guard)
    def diagnostics() -> dict[str, Any]:
        db_path = state.settings.db_path
        wal_path = db_path.with_name(db_path.name + "-wal")
        disk = shutil.disk_usage(state.settings.data_dir)
        counts = {
            "messages": state.db.one("SELECT COUNT(*) AS n FROM messages")["n"],
            "status_samples": state.db.one(
                "SELECT COUNT(*) AS n FROM device_status"
            )["n"],
            "active_incidents": state.db.one(
                "SELECT COUNT(*) AS n FROM incidents WHERE status != 'resolved'"
            )["n"],
            "audit_events": state.db.one(
                "SELECT COUNT(*) AS n FROM audit_events"
            )["n"],
        }
        return {
            "server": {
                "version": __version__,
                "python": platform.python_version(),
                "started_at": state.started_at,
                "uptime_seconds": int(time.monotonic() - state.started_monotonic),
            },
            "runtime": {
                "agents_connected": len(state.gateway.connections),
                "pending_commands": state.gateway.pending_command_count,
                "notifications_inflight": state.notifier.inflight_count,
                "offline_timers": state.alerter.pending_count,
            },
            "storage": {
                "database_bytes": db_path.stat().st_size if db_path.exists() else 0,
                "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
                "disk_total_bytes": disk.total,
                "disk_free_bytes": disk.free,
            },
            "counts": counts,
            "activity": state.db.activity_stats(),
            "agents": state.db.query(
                "SELECT a.*, COUNT(d.id) AS device_count "
                "FROM agents a LEFT JOIN devices d ON d.agent_id = a.id "
                "GROUP BY a.id ORDER BY a.id"
            ),
        }

    @router.get("/operations/audit", dependencies=guard)
    def audit_events(
        limit: int = Query(200, ge=1, le=2000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return paged(
            state.db,
            select="*",
            source="audit_events",
            order="ts DESC, id DESC",
            limit=limit,
            offset=offset,
        )

    @router.get("/operations/incidents", dependencies=guard)
    def incidents(
        status: Literal["open", "all"] = "open",
        limit: int = Query(200, ge=1, le=2000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return paged(
            state.db,
            select="*",
            source="incidents",
            where="WHERE status != 'resolved'" if status == "open" else "",
            # Severity first, so paging never buries a critical incident on a
            # later page while warnings fill the first one.  id breaks ties in
            # last_seen_at, without which a page boundary could repeat a row.
            order=(
                "CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 "
                "ELSE 2 END, last_seen_at DESC, id DESC"
            ),
            limit=limit,
            offset=offset,
        )

    @router.get("/operations/incidents/count", dependencies=guard)
    def incident_count() -> dict[str, int]:
        row = state.db.one(
            "SELECT COUNT(*) AS n FROM incidents WHERE status != 'resolved'"
        )
        return {"total": int(row["n"]) if row else 0}

    @router.put("/operations/incidents/{incident_id}", dependencies=guard)
    def update_incident(
        incident_id: int, body: IncidentStatusBody
    ) -> dict[str, Any]:
        row = state.db.set_incident_status(incident_id, body.status)
        if row is None:
            raise HTTPException(status_code=404, detail="no such incident")
        return row

    @router.get("/system/agent-token", dependencies=guard)
    def agent_token(request: Request) -> dict[str, Any]:
        """The token the agent must present.  Shown so it can be copied once."""
        state.db.record_audit(
            "read agent token",
            target="agent-token",
            client_ip=request.client.host if request.client else "",
        )
        return {
            "token": state.settings.agent_token,
            "rotatable": not state.settings.agent_token_from_env,
            "previous_valid_until": state.db.get_setting(
                SETTING_PREVIOUS_AGENT_TOKEN_EXPIRES_AT, None
            ),
        }

    @router.post("/system/agent-token/rotate", dependencies=guard)
    def rotate_agent_token(body: RotateAgentTokenBody) -> dict[str, Any]:
        old_token = state.settings.agent_token
        new_token = secrets.token_urlsafe(32)
        try:
            state.settings.replace_agent_token(new_token)
        except ConfigError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if body.grace_minutes:
            expires = (
                datetime.now(UTC) + timedelta(minutes=body.grace_minutes)
            ).isoformat(timespec="seconds")
            state.db.set_setting(
                SETTING_PREVIOUS_AGENT_TOKEN_HASH, hash_agent_token(old_token)
            )
            state.db.set_setting(SETTING_PREVIOUS_AGENT_TOKEN_EXPIRES_AT, expires)
        else:
            expires = None
            state.db.set_setting(SETTING_PREVIOUS_AGENT_TOKEN_HASH, "")
            state.db.set_setting(SETTING_PREVIOUS_AGENT_TOKEN_EXPIRES_AT, "")

        return {
            "token": new_token,
            "previous_valid_until": expires,
        }

    @router.post("/system/purge", dependencies=guard)
    def purge() -> dict[str, Any]:
        return state.db.purge(
            message_days=state.message_retention_days,
            status_days=state.settings.status_retention_days,
            log_days=state.settings.log_retention_days,
            audit_days=state.settings.audit_retention_days,
            incident_days=state.settings.incident_retention_days,
            audit_max_rows=state.settings.audit_max_rows,
        )

    @router.get("/system/backup", dependencies=guard)
    def backup(request: Request) -> FileResponse:
        """Download a consistent snapshot of the whole database.

        A snapshot is written to a temp file (SQLite's online-backup API, so
        it is coherent even mid-write) and streamed back; the temp file is
        deleted once the response has been sent.
        """
        fd, tmp = tempfile.mkstemp(prefix="hub-backup-", suffix=".db")
        os.close(fd)
        try:
            state.db.backup_to(tmp)
        except Exception:
            os.unlink(tmp)
            raise
        state.db.record_audit(
            "download backup",
            target="database",
            client_ip=request.client.host if request.client else "",
        )
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        return FileResponse(
            tmp,
            media_type="application/octet-stream",
            filename=f"hub-backup-{stamp}.db",
            background=BackgroundTask(_safe_unlink, tmp),
        )

    @router.post("/system/restore", dependencies=guard)
    async def restore(request: Request) -> dict[str, Any]:
        """Restore the database from an uploaded backup.

        The body is the raw backup file (``application/octet-stream``).  It is
        streamed to a temp file, validated as a genuine hub database, then
        copied over the live data — no restart required.  A malformed or
        unrelated file is rejected before anything is overwritten.
        """
        fd, tmp = tempfile.mkstemp(prefix="hub-restore-", suffix=".db")
        try:
            size = 0
            with os.fdopen(fd, "wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    out.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="未收到备份文件")
            try:
                state.db.validate_backup(tmp)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                state.db.restore_from(tmp)
            except MigrationFailed as exc:
                # The backup's data is already in place by now; only bringing it
                # to the current schema failed.  The snapshot path is the way
                # back, so it has to reach the operator rather than dying in the
                # log behind a bare 500.
                log.exception("restore failed while migrating the restored data")
                detail = f"备份已写入但迁移失败: {exc}"
                if exc.snapshot is not None:
                    detail += f";迁移前的副本保存在 {exc.snapshot}"
                raise HTTPException(status_code=500, detail=detail) from exc
        finally:
            _safe_unlink(tmp)
        return {"ok": True}

    # -- helpers -----------------------------------------------------------

    def _safe_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

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
