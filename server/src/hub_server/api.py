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
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import PROTOCOL_VERSION, __version__
from .alerts import SETTING_ENABLED
from .auth import SESSION_COOKIE, AuthError, hash_agent_token
from .config import ConfigError
from .csv_export import iter_message_csv
from .db import (
    SETTING_MESSAGE_RETENTION_DAYS,
    BadCursor,
    MessageScope,
    MigrationFailed,
    OperationConflict,
    OperationQueueFull,
    utcnow,
)
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
#   voltage         MIN — a low-water mark, for the same reason inverted.  The
#                   supply matters when it sags, and a sag is brief by nature:
#                   a transmit burst that pulls the module down for two seconds
#                   is exactly the event being looked for, and an average over
#                   half an hour would report it as a perfectly healthy supply.
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
    ("voltage_mv", "MIN({c})"),
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
# message scope
# --------------------------------------------------------------------------


def message_scope(
    sim_id: int | None = None,
    # The third case, spelled out.  Omitting `sim_id` means every card — that
    # is what the list has always done, and what the totals and the export
    # assume — so "the messages with no card" needs a name of its own rather
    # than a null that also reads as "unfiltered".
    sim_scope: Literal["unassigned"] | None = None,
    direction: Literal["in", "out"] | None = None,
    peer: str | None = None,
    search: str | None = None,
    content: Literal["text", "data"] | None = None,
) -> MessageScope:
    """Build the one condition object every message read shares.

    A dependency rather than per-route parameters: the list, the total, the
    cursor and the export have to mean the same thing by construction, and the
    way they stopped meaning the same thing was each spelling its own filter.
    """
    if sim_scope == "unassigned" and sim_id is not None:
        raise HTTPException(
            422, detail="sim_id and sim_scope=unassigned are mutually exclusive"
        )
    return MessageScope(
        sim="unassigned" if sim_scope == "unassigned" else (
            "all" if sim_id is None else sim_id
        ),
        direction=direction,
        peer=peer,
        search=search,
        content=content,
    )


MessageScopeQuery = Annotated[MessageScope, Depends(message_scope)]


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
    items = db.read_query(
        f"SELECT {select} FROM {source}{clause} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    row = db.read_one(f"SELECT COUNT(*) AS n FROM {count_source or source}{clause}", params)
    return {"items": items, "total": int(row["n"]) if row else 0}


def _ussd_code(body: dict[str, str]) -> str:
    """The USSD code out of a free-form body, or a 400."""
    code = str(body.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    return code


# --------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------


class PasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordBody(BaseModel):
    current: str
    new: str = Field(min_length=1, max_length=128)


class DeviceRef(BaseModel):
    """How a body-addressed command names its module.

    ``device_id`` is the unambiguous form and wins when both are given; the
    name is kept because existing clients send it, and is only accepted when it
    resolves to a single module fleet-wide.
    """

    device: str = ""
    device_id: int | None = None


class SendSmsBody(DeviceRef):
    number: str = Field(min_length=1, max_length=32)
    body: str = Field(min_length=1, max_length=2000)


class OperationBody(BaseModel):
    command_type: Literal["scan_operators", "network_diagnostics", "send_sms", "run_task"]
    device_id: int | None = Field(default=None, gt=0)
    task_id: int | None = Field(default=None, gt=0)
    number: str | None = Field(default=None, min_length=1, max_length=32, pattern=r"^[+0-9*#]+$")
    body: str | None = Field(default=None, min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")

    @model_validator(mode="after")
    def validate_command(self):
        if self.command_type == "run_task":
            if self.task_id is None or self.device_id is not None:
                raise ValueError("run_task requires task_id and no device_id")
        elif self.device_id is None or self.task_id is not None:
            raise ValueError("device command requires device_id and no task_id")
        if self.command_type == "send_sms":
            if self.number is None or self.body is None:
                raise ValueError("send_sms requires number and body")
        elif self.number is not None or self.body is not None:
            raise ValueError("number and body are only valid for send_sms")
        return self


class RawAtBody(DeviceRef):
    command: str = Field(min_length=2, max_length=200)


class RadioBody(BaseModel):
    enabled: bool


class RoamingDataBody(BaseModel):
    allowed: bool


class OperatorSelectionBody(BaseModel):
    """A 3GPP numeric MCC/MNC, or null to restore automatic selection."""

    numeric: str | None = Field(
        default=None,
        max_length=6,
        pattern=r"^[0-9]{5,6}$",
    )


class SimPatch(BaseModel):
    label: str | None = None
    phone_number: str | None = None
    billing_type: Literal["unknown", "payg", "prepaid", "postpaid"] | None = None
    plan_name: str | None = Field(default=None, max_length=128)
    balance: str | None = Field(
        default=None,
        max_length=32,
        pattern=r"^-?\d+(?:\.\d{1,6})?$",
    )
    low_balance_threshold: str | None = Field(
        default=None,
        max_length=32,
        pattern=r"^\d+(?:\.\d{1,6})?$",
    )
    currency: str | None = Field(
        default=None, max_length=3, pattern=r"^(?:[A-Za-z]{3})?$"
    )
    expires_at: date | None = None
    activity_due_at: date | None = None
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
    """One thread, and how far into it the operator has actually read.

    ``sim_scope="unassigned"`` is the explicit spelling for the thread of
    messages with no card; a bare null ``sim_id`` still means the same thing,
    which is what this endpoint always did.  What it must never mean here is
    "every card" — that would mark a number's history read across the fleet.
    """

    sim_id: int | None = None
    sim_scope: Literal["unassigned"] | None = None
    peer: str = Field(min_length=1, max_length=32)
    # The newest message the client had actually rendered.  Anything that
    # arrives after that stays unread; see ``Database.mark_read``.
    through_id: int | None = Field(default=None, ge=1)
    # The filter the transcript was being read under.  A view showing only text
    # must not mark the data messages it hid — they were never on screen.
    content: Literal["text", "data"] | None = None

    @model_validator(mode="after")
    def _one_identity(self) -> ReadBody:
        if self.sim_scope == "unassigned" and self.sim_id is not None:
            raise ValueError("sim_id and sim_scope=unassigned are exclusive")
        return self

    def scope(self) -> MessageScope:
        return MessageScope(
            sim="unassigned" if self.sim_id is None else self.sim_id,
            peer=self.peer,
            content=self.content,
        )


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
    device: str = ""
    # The module this task runs on.  ``device``/``agent_id`` are still accepted
    # so existing clients keep working, but they are resolved and rewritten
    # server-side: a task that fires an SMS every day must not be able to drift
    # onto another host's module because two of them share a name.
    device_id: int | None = None
    agent_id: str = ""
    enabled: bool = True
    action: Literal["send_sms", "ping", "raw_at", "voice_call"] = "send_sms"
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
                "messages_total": db.read_one(
                    "SELECT COUNT(*) AS n FROM messages"
                )["n"],
                "messages_today": db.read_one(
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

    # A module's identity is its row id.  Names are unique per agent, not per
    # fleet: two hosts each carrying a module called "a" is the ordinary shape
    # of a multi-site deployment, and every name-addressed route then had to
    # choose between them — the old resolver took whichever connection it met
    # first, so "send from a" could leave from the wrong SIM in the wrong
    # building.  Name addressing stays for existing clients, but only where the
    # name resolves to exactly one module; anything else is refused with the
    # candidates rather than answered with a guess.

    def _device_by_id(device_id: int) -> dict[str, Any]:
        row = state.db.one(
            "SELECT d.id, d.agent_id, d.name, d.imei, s.iccid "
            "FROM devices d LEFT JOIN sims s ON s.id = d.sim_id WHERE d.id = ?",
            (device_id,),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="no such device")
        return row

    def _devices_named(name: str) -> list[dict[str, Any]]:
        return state.db.query(
            "SELECT id, agent_id, name FROM devices WHERE name = ? ORDER BY agent_id",
            (name,),
        )

    def _ambiguous_device(name: str, rows: list[dict[str, Any]]) -> HTTPException:
        return HTTPException(
            status_code=409,
            detail={
                "error": "ambiguous_device_name",
                "message": (
                    f"{len(rows)} 台 Agent 都有名为 {name} 的模组，"
                    "无法判断指向哪一个；请改用模组 ID 寻址。"
                ),
                "candidates": [
                    {"device_id": row["id"], "agent_id": row["agent_id"]}
                    for row in rows
                ],
            },
        )

    def _device_by_name(name: str) -> dict[str, Any]:
        rows = _devices_named(name)
        if not rows:
            raise HTTPException(status_code=404, detail="no such device")
        if len(rows) > 1:
            raise _ambiguous_device(name, rows)
        return rows[0]

    def _body_device(body: DeviceRef) -> dict[str, Any]:
        """Resolve a body-addressed module: id if given, else a unique name."""
        if body.device_id is not None:
            return _device_by_id(body.device_id)
        if not body.device:
            raise HTTPException(
                status_code=422, detail="device or device_id is required"
            )
        return _device_by_name(body.device)

    async def _device_call(
        target: dict[str, Any], frame: dict[str, Any], *, timeout: float = 30.0
    ) -> dict[str, Any]:
        """Run a command on *target*, addressed the way its own agent sees it.

        The wire keeps using the agent-local module name: it is unambiguous
        inside one agent, and the agent has no notion of the server's row ids.
        """
        return await _call(
            target["agent_id"], {**frame, "device": target["name"]}, timeout=timeout
        )

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

        Keyed by module id, as a string because that is what a JSON object key
        is.  Grouping by name merged two same-named modules on different hosts
        into one series — a chart in which neither module's signal was shown.

        Downsampled into time buckets — see ``history_bucket_seconds``.  The
        point shape is the same as an unbucketed one, so a caller reading points
        off it needs no changes; only the density differs.
        """
        cutoff = (
            datetime.now(UTC) - timedelta(hours=hours)
        ).isoformat(timespec="seconds")
        width = history_bucket_seconds(hours)
        bucket = _BUCKET_TS.format(ts="s.ts", width=width)
        grouped: dict[str, list[dict[str, Any]]] = {
            str(row["id"]): [] for row in state.db.query("SELECT id FROM devices")
        }
        rows = state.db.read_query(
            f"SELECT s.device_id, {bucket} AS ts, {history_columns('s.')} "
            "FROM device_status s WHERE s.ts >= ? "
            f"GROUP BY s.device_id, CAST(strftime('%s', s.ts) / {width} AS INTEGER) "
            "ORDER BY s.device_id, ts",
            (cutoff,),
        )
        for row in rows:
            grouped.setdefault(str(row.pop("device_id")), []).append(row)
        return grouped

    def _device_history(device_id: int, hours: int) -> list[dict[str, Any]]:
        # Compute the cutoff here rather than with SQLite's datetime('now'):
        # that function formats with a space separator, which does not compare
        # correctly against the ISO-8601 'T' timestamps stored in the column.
        cutoff = (
            datetime.now(UTC) - timedelta(hours=hours)
        ).isoformat(timespec="seconds")
        width = history_bucket_seconds(hours)
        bucket = _BUCKET_TS.format(ts="ts", width=width)
        return state.db.read_query(
            f"SELECT {bucket} AS ts, {history_columns()} "
            "FROM device_status WHERE device_id = ? AND ts >= ? "
            f"GROUP BY CAST(strftime('%s', ts) / {width} AS INTEGER) "
            "ORDER BY ts",
            (device_id, cutoff),
        )

    @router.get("/devices/by-id/{device_id}/history", dependencies=guard)
    def device_history_by_id(
        device_id: int, hours: int = Query(24, ge=1, le=24 * 30)
    ) -> list[dict[str, Any]]:
        return _device_history(_device_by_id(device_id)["id"], hours)

    @router.post("/devices/by-id/{device_id}/refresh", dependencies=guard)
    async def refresh_device_by_id(device_id: int) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_device_by_id, device_id), {"type": "query", "what": "status"}
        )

    @router.post("/devices/by-id/{device_id}/radio", dependencies=guard)
    async def set_device_radio_by_id(
        device_id: int, body: RadioBody
    ) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_device_by_id, device_id),
            {"type": "set_radio", "enabled": body.enabled},
        )

    @router.post("/devices/by-id/{device_id}/data", dependencies=guard)
    async def set_device_data_by_id(
        device_id: int, body: RadioBody
    ) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_device_by_id, device_id),
            {"type": "set_data", "enabled": body.enabled},
            timeout=60.0,
        )

    @router.post("/devices/by-id/{device_id}/roaming-data", dependencies=guard)
    async def set_device_roaming_data_by_id(
        device_id: int, body: RoamingDataBody
    ) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_device_by_id, device_id),
            {"type": "set_roaming_data", "allowed": body.allowed},
            timeout=60.0,
        )

    @router.post("/devices/by-id/{device_id}/operators/scan", dependencies=guard)
    async def scan_device_operators_by_id(device_id: int) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_device_by_id, device_id), {"type": "scan_operators"}, timeout=210.0
        )

    @router.post("/devices/by-id/{device_id}/operator", dependencies=guard)
    async def select_device_operator_by_id(
        device_id: int, body: OperatorSelectionBody
    ) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_device_by_id, device_id),
            {"type": "select_operator", "numeric": body.numeric},
            timeout=210.0,
        )

    @router.post("/devices/by-id/{device_id}/network-diagnostics", dependencies=guard)
    async def device_network_diagnostics_by_id(device_id: int) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_device_by_id, device_id),
            {"type": "network_diagnostics"},
            timeout=165.0,
        )

    @router.post("/devices/by-id/{device_id}/ussd", dependencies=guard)
    async def send_ussd_by_id(
        device_id: int, body: dict[str, str]
    ) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_device_by_id, device_id),
            {"type": "ussd", "code": _ussd_code(body)},
            timeout=60.0,
        )

    @router.get("/devices/{name}/history", dependencies=guard)
    def device_history(
        name: str, hours: int = Query(24, ge=1, le=24 * 30)
    ) -> list[dict[str, Any]]:
        return _device_history(_device_by_name(name)["id"], hours)

    @router.post("/devices/{name}/refresh", dependencies=guard)
    async def refresh_device(name: str) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_device_by_name, name), {"type": "query", "what": "status"}
        )

    @router.post("/devices/{name}/radio", dependencies=guard)
    async def set_device_radio(name: str, body: RadioBody) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_device_by_name, name),
            {"type": "set_radio", "enabled": body.enabled},
        )

    @router.post("/devices/{name}/data", dependencies=guard)
    async def set_device_data(name: str, body: RadioBody) -> dict[str, Any]:
        """Enable or fully disable packet data on the modem."""
        return await _device_call(
            await state.db.run(_device_by_name, name),
            {"type": "set_data", "enabled": body.enabled},
            timeout=60.0,
        )

    @router.post("/devices/{name}/roaming-data", dependencies=guard)
    async def set_device_roaming_data(
        name: str, body: RoamingDataBody
    ) -> dict[str, Any]:
        """Set the local safety policy for data while the SIM is roaming."""
        return await _device_call(
            await state.db.run(_device_by_name, name),
            {"type": "set_roaming_data", "allowed": body.allowed},
            timeout=60.0,
        )

    @router.post("/devices/{name}/operators/scan", dependencies=guard)
    async def scan_device_operators(name: str) -> dict[str, Any]:
        # AT+COPS=? is allowed to take several minutes while the modem scans.
        return await _device_call(
            await state.db.run(_device_by_name, name), {"type": "scan_operators"}, timeout=210.0
        )

    @router.post("/devices/{name}/operator", dependencies=guard)
    async def select_device_operator(
        name: str, body: OperatorSelectionBody
    ) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_device_by_name, name),
            {"type": "select_operator", "numeric": body.numeric},
            timeout=210.0,
        )

    @router.post("/devices/{name}/network-diagnostics", dependencies=guard)
    async def device_network_diagnostics(name: str) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_device_by_name, name),
            {"type": "network_diagnostics"},
            # The agent reads five optional diagnostics serially, each with a
            # 30-second AT timeout. Leave room for all of them before the
            # gateway gives up and drops the pending command: a firmware that
            # hangs on one command must not cost the sections after it.
            timeout=165.0,
        )

    @router.post("/devices/{name}/ussd", dependencies=guard)
    async def send_ussd(name: str, body: dict[str, str]) -> dict[str, Any]:
        """Send a USSD code and return the raw response."""
        return await _device_call(
            await state.db.run(_device_by_name, name),
            {"type": "ussd", "code": _ussd_code(body)},
            timeout=60.0,
        )

    @router.get("/sims", dependencies=guard)
    def list_sims() -> list[dict[str, Any]]:
        return state.db.read_query(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM messages m WHERE m.sim_id = s.id) AS message_count, "
            "(SELECT ts FROM calls WHERE sim_id = s.id AND reached_network = 1 "
            "ORDER BY ts DESC LIMIT 1) AS last_reached_network_at "
            "FROM sims s ORDER BY s.id"
        )

    @router.patch("/sims/{sim_id}", dependencies=guard)
    def patch_sim(sim_id: int, body: SimPatch) -> dict[str, Any]:
        current = state.db.one(
            "SELECT id, balance FROM sims WHERE id = ?", (sim_id,)
        )
        if current is None:
            raise HTTPException(status_code=404, detail="no such SIM")

        fields = body.model_dump(exclude_unset=True)
        if not fields:
            raise HTTPException(status_code=400, detail="nothing to update")
        for key in ("label", "phone_number", "plan_name", "currency", "note"):
            if key in fields and fields[key] is None:
                fields[key] = ""
        if "billing_type" in fields and fields["billing_type"] is None:
            fields["billing_type"] = "unknown"
        if "currency" in fields:
            fields["currency"] = fields["currency"].upper()
        if "balance" in fields:
            if fields["balance"] is None:
                fields["balance_updated_at"] = None
            elif fields["balance"] != current["balance"]:
                fields["balance_updated_at"] = utcnow()
        for key in ("expires_at", "activity_due_at"):
            if isinstance(fields.get(key), date):
                fields[key] = fields[key].isoformat()
        assignments = ",".join(f"{key} = :{key}" for key in fields)
        state.db.execute(
            f"UPDATE sims SET {assignments} WHERE id = :id", {**fields, "id": sim_id}
        )
        state.db.reconcile_sim_incidents(state.settings.calendar_today())
        row = state.db.one("SELECT * FROM sims WHERE id = ?", (sim_id,))
        assert row is not None
        return row

    # -- messages ----------------------------------------------------------

    @router.get("/messages", dependencies=guard)
    def list_messages(
        scope: MessageScopeQuery,
        # 2000, not 500: the thread view reads a conversation back by growing
        # this window on demand.  A longer history is walked with `before`
        # instead — an offset page boundary can gap or repeat when an SMS lands
        # mid-scroll, while a (ts,id) cursor cannot.
        limit: int = Query(50, ge=1, le=2000),
        offset: int = Query(0, ge=0),
        before: str | None = Query(
            None,
            max_length=160,
            description="Cursor from a previous page; returns older messages.",
        ),
        # The transcript re-reads itself every few seconds for delivery
        # reports. Counting a 10,000-message history each time buys nothing
        # once `has_more` answers the only question the view asks.
        count: bool = True,
    ) -> dict[str, Any]:
        if before is not None and offset:
            raise HTTPException(
                422, detail="before and offset are mutually exclusive"
            )
        try:
            # One row past the page: enough to know whether an older page
            # exists without counting the rest of the history.
            rows = state.db.messages(
                scope, limit=limit + 1, offset=offset, before=before
            )
        except BadCursor as exc:
            raise HTTPException(
                422, detail=f"invalid cursor ({exc})"
            ) from exc
        has_more = len(rows) > limit
        items = rows[:limit]
        return {
            "items": items,
            "total": state.db.count_messages(scope) if count else None,
            "has_more": has_more,
            "next_cursor": scope.cursor(items[-1]) if has_more and items else None,
        }

    @router.get("/conversations", dependencies=guard)
    def list_conversations(
        limit: int = Query(200, ge=1, le=1000),
        content: Literal["text", "data"] | None = None,
    ) -> list[dict[str, Any]]:
        """One row per (card, correspondent), newest first.

        The UI is a messaging app, so the list it opens with is threads, not
        rows.  Grouping here rather than in the browser keeps it correct once
        the history is longer than one page.
        """
        return state.db.conversations(limit=limit, content=content)

    @router.get("/calls", dependencies=guard)
    def list_calls(
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        sim_id: int | None = None,
        direction: Literal["in", "out"] | None = None,
    ) -> dict[str, Any]:
        """Call attempts, newest first.

        Both directions in one list on purpose: for a keep-alive card the
        question is when it last touched the network at all, and splitting
        inbound from outbound buries half the answer.

        Wrapped like ``/messages`` rather than returned bare: this log only
        grows, so the UI needs a total it can show without reading every row.
        """
        return {
            "items": state.db.calls(
                limit=limit, offset=offset, sim_id=sim_id, direction=direction
            ),
            "total": state.db.count_calls(sim_id=sim_id, direction=direction),
        }

    @router.post("/messages/send", dependencies=guard)
    async def send_message(body: SendSmsBody) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_body_device, body),
            {"type": "send_sms", "number": body.number, "body": body.body},
        )

    @router.post("/messages/read", dependencies=guard)
    def mark_read(body: ReadBody) -> dict[str, Any]:
        """Mark one thread's incoming messages read, up to what was rendered."""
        marked = state.db.mark_read(body.scope(), through_id=body.through_id)
        return {"ok": True, "marked": marked}

    @router.get("/messages/unread", dependencies=guard)
    def unread_total() -> dict[str, Any]:
        return {"total": state.db.unread_total()}

    @router.get("/messages/export", dependencies=guard)
    def export_messages(
        scope: MessageScopeQuery,
        limit: int | None = Query(None, ge=1, le=1_000_000),
    ) -> StreamingResponse:
        """Stream stored messages as CSV without materialising the export."""
        return StreamingResponse(
            iter_message_csv(state.db, scope, limit=limit),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="messages.csv"',
                "Cache-Control": "no-store",
            },
        )

    @router.post("/at", dependencies=guard)
    async def raw_at(body: RawAtBody) -> dict[str, Any]:
        return await _device_call(
            await state.db.run(_body_device, body), {"type": "raw_at", "command": body.command}
        )

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
        row = await state.db.run(
            state.db.one, "SELECT * FROM channels WHERE id = ?", (channel_id,)
        )
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
        since_day = (datetime.now(UTC) - timedelta(days=days - 1)).date()
        since = f"{since_day.isoformat()}T00:00:00+00:00"
        rows = state.db.message_trend(since=since)
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

    @router.get("/notify-queue", dependencies=guard)
    def notify_queue() -> dict[str, Any]:
        """The push queue as an operator needs to read it.

        ``notify_logs`` records what already happened; this is what has not.
        The two answer different questions — "did the code arrive" versus "is
        anything stuck" — and the second one is only visible here.
        """
        return {
            "backlog": state.db.notify_backlog(),
            "stuck": state.db.stuck_deliveries(),
        }

    @router.post("/notify-queue/{delivery_id}/retry", dependencies=guard)
    async def retry_delivery(delivery_id: int, request: Request) -> dict[str, Any]:
        """Put one given-up push back in the queue.

        Audited because it re-sends a message body to a third party: whoever
        looks at this later should be able to see that a person asked for it.
        """
        await state.db.run(
            _retry_delivery, delivery_id, request.client.host if request.client else ""
        )
        state.notifier.kick()
        return {"ok": True}

    def _retry_delivery(delivery_id: int, client_ip: str) -> None:
        if not state.db.retry_delivery(delivery_id):
            raise HTTPException(
                status_code=404, detail="no such failed delivery"
            )
        state.db.record_audit(
            "retry notification",
            target=f"delivery:{delivery_id}",
            client_ip=client_ip,
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

    def _task_fields(body: TaskBody) -> dict[str, Any]:
        """A task row's columns, with its module pinned to one identity.

        A keep-alive task sends real SMS on a schedule, unattended, so the
        module it names is resolved at write time: stored as a row id, with the
        owning agent and the module's name written alongside so a later read
        never has to guess which host was meant.

        A name the server has not seen yet is kept as a name — tasks are often
        configured before the agent's first connection, and ``tasks_for`` binds
        those at push time.  An *ambiguous* name is refused outright: a schedule
        that fires unattended must not be able to drift onto another host's
        module.
        """
        target: dict[str, Any]
        if body.device_id is not None:
            target = _device_by_id(body.device_id)
        elif not body.device:
            raise HTTPException(
                status_code=422, detail="device or device_id is required"
            )
        else:
            rows = _devices_named(body.device)
            if len(rows) > 1:
                raise _ambiguous_device(body.device, rows)
            target = rows[0] if rows else {
                "id": None, "agent_id": body.agent_id, "name": body.device,
            }

        data = body.model_dump()
        data["enabled"] = int(data["enabled"])
        data["random_suffix"] = int(data["random_suffix"])
        data["notify_on_result"] = int(data["notify_on_result"])
        data["device_id"] = target["id"]
        data["device"] = target["name"]
        data["agent_id"] = target["agent_id"]
        return data

    @router.post("/tasks", dependencies=guard)
    async def create_task(body: TaskBody) -> dict[str, Any]:
        row = await state.db.run(_create_task, body)
        await _sync_tasks()
        return row

    def _create_task(body: TaskBody) -> dict[str, Any]:
        data = _task_fields(body)
        data["created_at"] = utcnow()
        columns = ",".join(data)
        placeholders = ",".join(f":{key}" for key in data)
        cursor = state.db.execute(
            f"INSERT INTO tasks ({columns}) VALUES ({placeholders})", data
        )
        return state.db.one("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,))

    @router.put("/tasks/{task_id}", dependencies=guard)
    async def update_task(task_id: int, body: TaskBody) -> dict[str, Any]:
        row = await state.db.run(_update_task, task_id, body)
        await _sync_tasks()
        return row

    def _update_task(task_id: int, body: TaskBody) -> dict[str, Any]:
        data = _task_fields(body)
        assignments = ",".join(f"{key} = :{key}" for key in data)
        state.db.execute(
            f"UPDATE tasks SET {assignments} WHERE id = :id", {**data, "id": task_id}
        )
        row = state.db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="no such task")
        return row

    @router.delete("/tasks/{task_id}", dependencies=guard)
    async def delete_task(task_id: int) -> dict[str, Any]:
        await state.db.run(state.db.execute, "DELETE FROM tasks WHERE id = ?", (task_id,))
        await _sync_tasks()
        return {"ok": True}

    @router.post("/tasks/{task_id}/run", dependencies=guard)
    async def run_task(task_id: int) -> dict[str, Any]:
        agent_id = await state.db.run(_task_agent, task_id)
        # Send the authoritative definition before the immediate run.
        revision = await state.gateway.push_tasks(agent_id)
        frame: dict[str, Any] = {"type": "run_task", "task_id": task_id}
        if revision is not None:
            frame["revision"] = revision
        return await _call(agent_id, frame)

    def _task_agent(task_id: int) -> str:
        task = state.db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if task is None:
            raise HTTPException(status_code=404, detail="no such task")
        # Prefer the identities written with the row; fall back to resolving the
        # name only for rows that predate them — and refuse an ambiguous one
        # rather than run it on whichever module answers first.
        agent_id = str(task.get("agent_id") or "")
        if not agent_id and task.get("device_id"):
            agent_id = str(_device_by_id(int(task["device_id"]))["agent_id"])
        if not agent_id:
            agent_id = str(_device_by_name(str(task.get("device") or ""))["agent_id"])
        if not agent_id:
            raise HTTPException(status_code=503, detail="task device has no agent")

        return agent_id

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
            "messages": state.db.read_one("SELECT COUNT(*) AS n FROM messages")["n"],
            "status_samples": state.db.read_one(
                "SELECT COUNT(*) AS n FROM device_status"
            )["n"],
            "active_incidents": state.db.read_one(
                "SELECT COUNT(*) AS n FROM incidents WHERE status != 'resolved'"
            )["n"],
            "audit_events": state.db.read_one(
                "SELECT COUNT(*) AS n FROM audit_events"
            )["n"],
        }
        agents = state.db.query(
            "SELECT a.*, COUNT(d.id) AS device_count "
            "FROM agents a LEFT JOIN devices d ON d.agent_id = a.id "
            "GROUP BY a.id ORDER BY a.id"
        )
        for agent in agents:
            agent["version_matches"] = agent["version"] == __version__
            agent["protocol_compatible"] = (
                agent["protocol_version"] == PROTOCOL_VERSION
            )
        return {
            "server": {
                "version": __version__,
                "protocol_version": PROTOCOL_VERSION,
                "python": platform.python_version(),
                "started_at": state.started_at,
                "uptime_seconds": int(time.monotonic() - state.started_monotonic),
            },
            "runtime": {
                "agents_connected": len(state.gateway.connections),
                "pending_commands": state.gateway.pending_command_count,
                "notifications_inflight": state.notifier.inflight_count,
                # In-flight counts only what this process is holding; the queue
                # is what a restart would inherit, which is the number that
                # says whether pushes are actually moving.
                "notify_queue": state.db.notify_backlog(),
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
            "agents": agents,
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
    async def purge() -> dict[str, Any]:
        message_days = await state.db.run(lambda: state.message_retention_days)
        return await state.db.purge_async(
            message_days=message_days,
            status_days=state.settings.status_retention_days,
            log_days=state.settings.log_retention_days,
            audit_days=state.settings.audit_retention_days,
            incident_days=state.settings.incident_retention_days,
            audit_max_rows=state.settings.audit_max_rows,
            ingested_days=state.settings.ingested_retention_days,
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
                    await run_in_threadpool(out.write, chunk)
                await run_in_threadpool(out.flush)
            if size == 0:
                raise HTTPException(status_code=400, detail="未收到备份文件")
            try:
                await run_in_threadpool(state.db.validate_backup, tmp)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                await state.db.run(state.db.restore_from, tmp)
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

    # Registered after /operations/diagnostics, /incidents and /audit.

    def _operation_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **{key: row[key] for key in (
                "id", "device_id", "agent_id", "device", "command_type", "status",
                "deadline", "result", "error", "created_at", "updated_at",
            )},
            "operation_id": row["id"], "status_url": f"/api/operations/{row['id']}",
            "run_id": row["id"] if row["command_type"] == "run_task" else None,
        }

    def _operation(operation_id: str) -> dict[str, Any]:
        row = state.db.operation(operation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such operation")
        return _operation_view(row)

    def _operation_target(body: OperationBody) -> tuple[dict[str, Any], dict[str, Any]]:
        frame: dict[str, Any] = {"type": body.command_type}
        if body.command_type == "run_task":
            agent_id = _task_agent(body.task_id)
            task = state.db.one("SELECT * FROM tasks WHERE id = ?", (body.task_id,))
            target = state.db.one(
                "SELECT * FROM devices WHERE agent_id = ? AND name = ?",
                (agent_id, task["device"]),
            )
            if target is None:
                raise HTTPException(status_code=404, detail="task device does not exist")
            frame["task_id"] = body.task_id
        else:
            target = _device_by_id(body.device_id)
        if body.command_type == "send_sms":
            frame.update(number=body.number, body=body.body)
        return target, frame

    @router.post("/operations", dependencies=guard, status_code=202)
    async def create_operation(body: OperationBody, response: Response) -> dict[str, Any]:
        target, frame = await state.db.run(_operation_target, body)
        timeout = {
            "scan_operators": 210, "network_diagnostics": 165,
            "send_sms": 180, "run_task": 210,
        }[body.command_type]
        if body.command_type == "run_task":
            revision = await state.gateway.push_tasks(target["agent_id"])
            if revision is not None:
                frame["revision"] = revision
        try:
            row = await state.gateway.submit_operation(
                target, frame, idempotency_key=body.idempotency_key, timeout=timeout,
            )
        except OperationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OperationQueueFull as exc:
            raise HTTPException(
                status_code=429, detail=str(exc), headers={"Retry-After": "2"},
            ) from exc
        except AgentUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        view = _operation_view(row)
        response.headers["Location"] = view["status_url"]
        response.headers["Cache-Control"] = "no-store"
        return view

    @router.get("/operations", dependencies=guard)
    def list_operations(
        limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
        device_id: int | None = None,
    ) -> dict[str, Any]:
        where = "WHERE device_id = ?" if device_id is not None else ""
        params = (device_id,) if device_id is not None else ()
        rows = state.db.read_query(
            f"SELECT id FROM command_operations {where} ORDER BY created_at DESC, rowid DESC "
            "LIMIT ? OFFSET ?", (*params, limit, offset),
        )
        return {
            "items": [_operation(row["id"]) for row in rows],
            "total": state.db.read_one(
                f"SELECT COUNT(*) AS n FROM command_operations {where}", params,
            )["n"],
        }

    @router.get("/operations/{operation_id}", dependencies=guard)
    def get_operation(operation_id: str, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return _operation(operation_id)

    @router.post("/operations/{operation_id}/cancel", dependencies=guard)
    async def cancel_operation(operation_id: str) -> dict[str, Any]:
        await state.db.run(_operation, operation_id)
        if not await state.db.run(state.db.cancel_operation, operation_id):
            raise HTTPException(
                status_code=409, detail="only an undispatched operation can be cancelled",
            )
        return await state.db.run(_operation, operation_id)

    # -- helpers -----------------------------------------------------------

    def _safe_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    async def _call(
        agent_id: str, frame: dict[str, Any], *, timeout: float = 30.0
    ) -> dict[str, Any]:
        try:
            return await state.gateway.call(agent_id, frame, timeout=timeout)
        except AgentUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except CommandFailed as exc:
            # The agent's own error text (a +CMS code, "device offline", …) is
            # far more useful than a generic failure.
            headers = None
            if exc.operation_id:
                headers = {
                    "X-Operation-ID": exc.operation_id,
                    "Location": f"/api/operations/{exc.operation_id}",
                }
            raise HTTPException(status_code=502, detail=exc.error, headers=headers) from exc

    async def _sync_tasks() -> None:
        """Update desired versions, send online snapshots, and let receipts confirm them."""
        for agent in await state.db.run(state.db.query, "SELECT id FROM agents"):
            await state.gateway.push_tasks(agent["id"])

    router.sync_tasks = _sync_tasks  # type: ignore[attr-defined]
    return router
