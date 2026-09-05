"""Push engine: an inbound SMS becomes one or more notifications.

Four things here are less obvious than they look:

* **Dispatch must not block ingest.**  ``Gateway._ingest`` applies an event
  and *then* acks it.  A push with retries can take tens of seconds, so doing
  it inline would stall the ack — and the agent, having heard nothing, would
  replay the message on its next reconnect.  ``on_message`` therefore only
  records that a push is owed and returns.

* **What is owed outlives the process.**  The intent is written in the same
  transaction as the event (see ``Gateway._apply``), so a COMMIT is already a
  promise to notify.  Delivery is then a queue this module drains: a restart,
  a crash mid-send or a provider that is down for an hour resumes instead of
  losing the push with the process that held it in memory.

* **HTTP 200 is not success.**  Every Chinese bot platform here answers 200
  with an error code in the body (``errcode``, ``code``, ``StatusCode``).  A
  push that only checks the status line reports "delivered" for messages
  nobody ever received.

* **Nothing here may log the SMS body.**  This engine handles every
  verification code the user receives; ``notify_logs.detail`` is rendered in
  the web UI and must carry only status codes and the provider's own error
  text.  Request URLs are scrubbed too — a Telegram bot token lives in the
  URL path.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import random
import re
import smtplib
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .config import Settings
from .db import Database, utcnow

log = logging.getLogger(__name__)

# What a rule renders when the admin left the template empty.
DEFAULT_TEMPLATE = "【{card}】{sender}\n{message}"
# Used where a channel has a separate title field (Bark, mail subject).
DEFAULT_TITLE = "{card} · {sender}"

# Only ``{word}`` is a placeholder.  Everything else — including braces that
# happen to appear in an SMS — is literal text.
PLACEHOLDER = re.compile(r"\{(\w+)\}")

# A URL in an error string can carry a bot token; notify_logs is shown in the
# browser, so scrub before storing.
URL_PATTERN = re.compile(r"https?://\S+")

DETAIL_LIMIT = 300

# Deliveries already running.  Reached only if a provider hangs while SMS keep
# arriving; dropping is better than growing tasks without bound.
MAX_INFLIGHT = 200

# -- queue bounds ----------------------------------------------------------
# The queue is in the database, so backpressure is a matter of how much is taken
# out of it at once rather than what to throw away when memory fills.

# Deliveries attempted per pass, and how many of those may be in the air at
# once.  A pass that claimed everything due would hold leases it cannot service.
CLAIM_BATCH = 20
MAX_CONCURRENT_SENDS = 8
# ...and per channel, so one hanging provider cannot consume the whole budget
# and starve the channels that are answering.
MAX_PER_CHANNEL = 2

# Intents turned into deliveries per pass.  Rule matching runs a regex per rule
# and is deliberately outside the ingest transaction.
EXPAND_BATCH = 50

# How long a claim is held.  Longer than any single provider timeout, so a slow
# send is not re-claimed underneath itself; short enough that a killed process
# does not park its rows for long.
LEASE_SECONDS = 120.0

# How often to look for work with nothing waking us.  Deliveries are normally
# kicked the moment they are queued; this is the floor under a crash between
# COMMIT and kick, and under a retry that came due while the process was idle.
POLL_SECONDS = 5.0

# Never wait longer than this between attempts, whatever the provider asked for.
RETRY_AFTER_CAP_SECONDS = 300.0

# How long a push is still worth sending, by intent kind.  An SMS code is stale
# within minutes, but the queue is the wrong place to decide it is worthless:
# late is a nuisance, missing is what sends the operator to the web UI to read
# the message by hand.  A call or a task receipt ages out faster because both
# describe a moment, and neither carries anything the operator cannot re-read.
EXPIRY_SECONDS = {
    "message": 6 * 3600.0,
    "task_result": 3600.0,
    "call": 1800.0,
}
DEFAULT_EXPIRY_SECONDS = 3600.0

SAMPLE_CONTEXT = {
    "sender": "10086",
    "message": "这是一条来自 air780e-hub 的测试消息。",
    "card": "测试卡",
    "device": "test",
    "iccid": "",
}

TASK_STATUS_LABEL = {"ok": "执行成功", "failed": "执行失败", "skipped": "已跳过"}

CALL_OUTCOME_LABEL = {
    "missed": "未接",
    "answered": "已接通",
    "rejected": "已拒接",
    "no_answer": "无人接听",
    "busy": "占线",
    "failed": "呼叫失败",
}


class SendError(RuntimeError):
    """A channel refused the message.  ``detail`` is safe to store and show.

    The extra fields exist for the queue rather than for the log line: a durable
    delivery has to decide whether asking again can possibly work.  ``kind``
    separates the three answers — ``config`` is the operator's to fix, ``http``
    carries the provider's status code, and ``provider`` is a refusal returned
    alongside HTTP 200, which only the provider's own vocabulary explains.
    """

    def __init__(
        self,
        detail: str,
        *,
        kind: str = "provider",
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.detail = detail
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        super().__init__(detail)


@dataclass
class Payload:
    """One rendered notification, in the shapes the senders need."""

    text: str
    title: str
    context: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------


def render(template: str, context: dict[str, str]) -> str:
    """Substitute ``{name}`` placeholders.

    Deliberately not ``str.format``: the values here include SMS bodies, and a
    message containing a brace would raise instead of being delivered.  This
    walks the *template* only, and leaves unknown placeholders as written so a
    typo is visible in the push rather than silently blank.
    """

    def swap(match: re.Match[str]) -> str:
        value = context.get(match.group(1))
        return match.group(0) if value is None else str(value)

    return PLACEHOLDER.sub(swap, template or DEFAULT_TEMPLATE)


def scrub(text: str) -> str:
    """Strip URLs and clip, for anything headed to notify_logs."""
    cleaned = URL_PATTERN.sub("<url>", text or "").strip()
    return cleaned[:DETAIL_LIMIT]


def _salvaged(message: dict[str, Any], frame: dict[str, Any]) -> str | None:
    """What to push for a damaged message, or None if it is not damaged.

    The modem sometimes drops octets out of the middle of a frame before the
    agent ever sees it; the stored ``body`` is then mojibake and the agent's
    re-phased fragment is the only readable part.  The text says so, because a
    fragment presented as a whole message is worse than no message: the reader
    would take "no code here" at face value when the code was in the octets
    that went missing.

    Returns a string even when nothing was recovered.  "This SMS arrived
    damaged" is itself the notification — the alternative is silence, and the
    user goes looking for a code that never arrives.
    """
    if not (message.get("truncated") or frame.get("truncated")):
        return None

    body = (message.get("recovered_body") or frame.get("recovered_text") or "").strip()
    code = (message.get("recovered_code") or frame.get("code") or "").strip()

    lines = ["⚠️ 短信在模组内损坏，以下是可读部分（不是全文）"]
    if code:
        lines.append(f"可能的验证码：{code}")
    if body:
        lines.append(body)
    if not body and not code:
        lines.append("正文未能恢复，请到管理界面查看原始 PDU。")
    return "\n".join(lines)


def notification_body(
    message: dict[str, Any], frame: dict[str, Any] | None = None
) -> str:
    """The text a channel should receive for this message.

    Normally the decoded body.  For a damaged frame it is the salvage notice
    instead, so what a rule matched on is what the channel renders — the two
    drifting apart is how a push ends up saying it matched a keyword the reader
    cannot find anywhere in the text.
    """
    frame = frame or {}
    return _salvaged(message, frame) or message.get("body") or frame.get("body") or ""


# --------------------------------------------------------------------------
# rule matching
# --------------------------------------------------------------------------


def rule_matches(rule: dict[str, Any], *, sim_id: int | None, body: str) -> bool:
    if rule["sim_id"] is not None and rule["sim_id"] != sim_id:
        return False

    mode = rule["match"]
    if mode == "all":
        return True

    pattern = rule["pattern"] or ""
    if not pattern:
        # A keyword rule with no keyword matches nothing.  Treating it as
        # "everything" would turn a half-filled form into a firehose.
        return False
    if mode == "keyword":
        return pattern.lower() in body.lower()
    if mode == "regex":
        try:
            return re.search(pattern, body) is not None
        except re.error as exc:
            # One bad regex must not stop the other rules from delivering.
            log.warning("rule %s has an invalid regex, skipped: %s", rule["id"], exc)
            return False

    log.warning("rule %s has unknown match mode %r, skipped", rule["id"], mode)
    return False


def match_rules(
    db: Database, *, sim_id: int | None, body: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Rules that fire for this message, at most one per channel.

    Overlap is the normal case — a catch-all rule plus a keyword rule for the
    same Bark device — and the user wants one push, not two.  Highest priority
    wins, so its template is the one that renders.
    """
    channels = {
        row["id"]: row
        for row in db.query("SELECT * FROM channels WHERE enabled = 1")
    }
    rules = db.query(
        "SELECT * FROM rules WHERE enabled = 1 ORDER BY priority DESC, id"
    )

    chosen: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    for rule in rules:
        channel = channels.get(rule["channel_id"])
        if channel is None or rule["channel_id"] in chosen:
            continue
        if rule_matches(rule, sim_id=sim_id, body=body):
            chosen[rule["channel_id"]] = (rule, channel)
    return list(chosen.values())


def channel_config(channel: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(channel.get("config") or "{}")
    except ValueError:
        log.warning("channel %s has unparseable config", channel.get("id"))
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------
# senders
# --------------------------------------------------------------------------


def _require(config: dict[str, Any], key: str) -> str:
    value = str(config.get(key) or "").strip()
    if not value:
        raise SendError(
            f"channel is missing the {key!r} setting", kind="config"
        )
    return value


def _provider_message(response: httpx.Response) -> str:
    """Whatever the provider called its error text."""
    body = _json_body(response)
    for key in ("msg", "errmsg", "description", "message", "StatusMessage", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return response.text


def _json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """How long the provider asked us to wait, from its ``Retry-After`` header.

    RFC 9110 allows two shapes — delta-seconds and an HTTP-date — and a date
    already in the past means "now", not a negative wait.  Guessing a backoff
    when the provider named its own window is how a throttled channel keeps
    arriving early and stays throttled.
    """
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        seconds = float(header.strip())
    except ValueError:
        pass
    else:
        return max(0.0, seconds)
    try:
        at = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, (at - datetime.now(UTC)).total_seconds())


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code // 100 != 2:
        raise SendError(
            scrub(f"HTTP {response.status_code}: {_provider_message(response)}"),
            kind="http",
            status=response.status_code,
            retry_after=_retry_after_seconds(response),
        )


def _raise_for_code(response: httpx.Response, code: Any, *, ok: Any = 0) -> None:
    """Fail on a provider error code returned alongside HTTP 200."""
    if code != ok:
        raise SendError(scrub(f"code {code}: {_provider_message(response)}"))


def _ok(response: httpx.Response) -> str:
    return f"HTTP {response.status_code}"


async def _send_bark(
    client: httpx.AsyncClient, config: dict[str, Any], payload: Payload
) -> str:
    response = await client.post(
        _require(config, "url"),
        json={"title": payload.title, "body": payload.text},
    )
    _raise_for_status(response)
    _raise_for_code(response, _json_body(response).get("code", 200), ok=200)
    return _ok(response)


async def _send_telegram(
    client: httpx.AsyncClient, config: dict[str, Any], payload: Payload
) -> str:
    token = _require(config, "token")
    response = await client.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": _require(config, "chat_id"),
            "text": payload.text,
            "disable_web_page_preview": True,
        },
    )
    _raise_for_status(response)
    if _json_body(response).get("ok") is not True:
        raise SendError(scrub(_provider_message(response)))
    return _ok(response)


def _feishu_card(payload: Payload) -> dict[str, Any]:
    """Build a compact Feishu card without interpreting SMS text as Markdown."""
    context = payload.context
    # The shared default template already includes card and sender.  Once the
    # card gives those values dedicated fields, repeating that first line in
    # the body looks like a rendering bug.  Custom templates remain verbatim.
    default_text = render(DEFAULT_TEMPLATE, context)
    content = (
        context.get("message", "")
        if payload.text == default_text and context.get("message")
        else payload.text
    )

    elements: list[dict[str, Any]] = []
    fields = []
    for label, key in (("卡片", "card"), ("发件人", "sender")):
        value = str(context.get(key) or "").strip()
        if value:
            fields.append({
                "is_short": True,
                "text": {"tag": "plain_text", "content": f"{label}\n{value}"},
            })
    if fields:
        elements.extend(({"tag": "div", "fields": fields}, {"tag": "hr"}))

    elements.append({
        "tag": "div",
        "text": {"tag": "plain_text", "content": content or " "},
    })

    timestamp = str(context.get("timestamp") or "").strip()
    if timestamp:
        elements.extend((
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": timestamp}],
            },
        ))

    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": payload.title or "air780e-hub"},
        },
        "elements": elements,
    }


async def _send_feishu(
    client: httpx.AsyncClient, config: dict[str, Any], payload: Payload
) -> str:
    body: dict[str, Any] = {"msg_type": "interactive", "card": _feishu_card(payload)}
    secret = str(config.get("secret") or "").strip()
    if secret:
        # Feishu signs an *empty* message with "<timestamp>\n<secret>" as the key.
        stamp = str(int(time.time()))
        digest = hmac.new(
            f"{stamp}\n{secret}".encode(), b"", hashlib.sha256
        ).digest()
        body |= {"timestamp": stamp, "sign": base64.b64encode(digest).decode()}

    response = await client.post(_require(config, "webhook"), json=body)
    _raise_for_status(response)
    parsed = _json_body(response)
    # v2 webhooks answer {"code": 0}; older ones {"StatusCode": 0}.
    _raise_for_code(response, parsed.get("code", parsed.get("StatusCode", 0)))
    return _ok(response)


async def _send_wecom(
    client: httpx.AsyncClient, config: dict[str, Any], payload: Payload
) -> str:
    response = await client.post(
        _require(config, "webhook"),
        json={"msgtype": "text", "text": {"content": payload.text}},
    )
    _raise_for_status(response)
    _raise_for_code(response, _json_body(response).get("errcode", 0))
    return _ok(response)


async def _send_dingtalk(
    client: httpx.AsyncClient, config: dict[str, Any], payload: Payload
) -> str:
    url = httpx.URL(_require(config, "webhook"))
    secret = str(config.get("secret") or "").strip()
    if secret:
        # DingTalk signs "<millis>\n<secret>" keyed by the secret, in the query.
        stamp = str(int(time.time() * 1000))
        digest = hmac.new(
            secret.encode(), f"{stamp}\n{secret}".encode(), hashlib.sha256
        ).digest()
        url = url.copy_merge_params(
            {"timestamp": stamp, "sign": base64.b64encode(digest).decode()}
        )

    response = await client.post(
        url, json={"msgtype": "text", "text": {"content": payload.text}}
    )
    _raise_for_status(response)
    _raise_for_code(response, _json_body(response).get("errcode", 0))
    return _ok(response)


async def _send_post(
    client: httpx.AsyncClient, config: dict[str, Any], payload: Payload
) -> str:
    headers = config.get("headers")
    response = await client.post(
        _require(config, "url"),
        json={**payload.context, "title": payload.title, "text": payload.text},
        headers={str(k): str(v) for k, v in headers.items()}
        if isinstance(headers, dict)
        else None,
    )
    _raise_for_status(response)
    return _ok(response)


async def _send_get(
    client: httpx.AsyncClient, config: dict[str, Any], payload: Payload
) -> str:
    # Merge rather than replace: the URL may already carry an API key.
    url = httpx.URL(_require(config, "url")).copy_merge_params(
        {**payload.context, "text": payload.text}
    )
    response = await client.get(url)
    _raise_for_status(response)
    return _ok(response)


def _send_smtp_blocking(config: dict[str, Any], payload: Payload, timeout: float) -> str:
    host = _require(config, "host")
    recipients = [
        part.strip() for part in _require(config, "to").split(",") if part.strip()
    ]
    if not recipients:
        raise SendError("channel is missing the 'to' setting")

    security = str(config.get("security") or "ssl").lower()
    port = int(config.get("port") or (465 if security == "ssl" else 587))
    username = str(config.get("username") or "")
    password = str(config.get("password") or "")

    message = EmailMessage()
    message["Subject"] = payload.title
    message["From"] = str(config.get("from") or username or f"air780e-hub@{host}")
    message["To"] = ", ".join(recipients)
    message.set_content(payload.text)

    try:
        if security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        with server:
            if security == "starttls":
                server.starttls()
            if username:
                server.login(username, password)
            server.send_message(message)
    except smtplib.SMTPException as exc:
        raise SendError(scrub(f"{type(exc).__name__}: {exc}")) from exc
    except OSError as exc:
        raise SendError(scrub(f"{type(exc).__name__}: {exc}")) from exc
    return f"sent to {len(recipients)} recipient(s)"


async def _send_smtp(
    client: httpx.AsyncClient, config: dict[str, Any], payload: Payload
) -> str:
    # smtplib is blocking, and a slow relay would otherwise freeze the whole
    # server — the WebSocket gateway shares this event loop.
    timeout = client.timeout.connect or 10.0
    completed = threading.Event()
    result: list[str] = []
    errors: list[Exception] = []

    def send() -> None:
        try:
            result.append(_send_smtp_blocking(config, payload, timeout))
        except Exception as exc:
            errors.append(exc)
        finally:
            completed.set()

    threading.Thread(target=send, name="smtp-notify", daemon=True).start()
    while not completed.is_set():
        await asyncio.sleep(0.01)
    if errors:
        raise errors[0]
    return result[0]


SENDERS = {
    "bark": _send_bark,
    "telegram": _send_telegram,
    "feishu": _send_feishu,
    "wecom": _send_wecom,
    "dingtalk": _send_dingtalk,
    "post": _send_post,
    "get": _send_get,
    "smtp": _send_smtp,
}


async def send_via_channel(
    client: httpx.AsyncClient, channel: dict[str, Any], payload: Payload
) -> str:
    sender = SENDERS.get(channel.get("type", ""))
    if sender is None:
        raise SendError(f"unknown channel type {channel.get('type')!r}")
    return await sender(client, channel_config(channel), payload)


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------


def _suppressed(message: dict[str, Any], frame: dict[str, Any]) -> bool:
    """Whether this message has no human-readable notification at all.

    A damaged message is ``is_binary`` too — its decoded body is mojibake for the
    same reason — but unlike a data SMS it was written by a person for a person,
    and the agent salvaged part of it.  Push what survived: suppressing it is how
    a verification code goes missing silently, which is the worse failure of the
    two.  Data SMS (OTA provisioning, WAP push, SIM toolkit, malformed UDH) is
    kept for diagnosis and suppressed before matching even an ``all`` rule.
    """
    if _salvaged(message, frame) is not None:
        return False
    return bool(message.get("is_binary") or frame.get("binary"))


def _loads(raw: Any) -> dict[str, Any]:
    """A stored JSON frame, or an empty one — never a raise on a queue row."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_id(row: dict[str, Any] | None) -> int | None:
    value = (row or {}).get("id")
    return int(value) if value is not None else None


def _shift(moment: str, seconds: float) -> str:
    """``moment`` moved forward, in the same string form as ``utcnow``."""
    try:
        parsed = datetime.fromisoformat(moment)
    except (TypeError, ValueError):
        parsed = datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (parsed + timedelta(seconds=seconds)).astimezone(UTC).isoformat(
        timespec="seconds"
    )


def _past(moment: Any) -> bool:
    """Whether a stored deadline has gone by.  No deadline is never past."""
    if not moment:
        return False
    return str(moment) <= utcnow()


def _error_code(failure: SendError) -> str:
    """A short, stable reason, for grouping failures on the operations page."""
    if failure.kind == "config":
        return "channel_config"
    if failure.kind == "network":
        return "network"
    if failure.status is not None:
        return f"http_{failure.status}"
    return "provider_rejected"


def _transient(failure: SendError) -> bool:
    """Whether asking again could plausibly succeed.

    A missing setting is the operator's to fix and will refuse identically for as
    long as the queue is willing to ask.  A 4xx is the request being wrong, with
    the two exceptions that are about timing rather than content.  Everything
    else — 5xx, a timeout, a refused connection, a provider saying "busy" with
    HTTP 200 — is retried, which is also what this engine did before the queue
    existed.
    """
    if failure.kind == "config":
        return False
    if failure.status is None:
        return True
    if failure.status in (408, 429):
        return True
    return failure.status // 100 != 4


def _load_timezone(name: str) -> Any:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # Slim base images ship no zoneinfo database; timestamps in UTC are
        # wrong-looking but harmless, a crashed push is not.
        log.warning("timezone %r unavailable (tzdata missing?); using UTC", name)
        return UTC


class Notifier:
    """Turns stored messages into pushes, with retries and an audit trail."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        retries: int | None = None,
        backoff: tuple[float, ...] = (1.0, 3.0),
    ) -> None:
        self.db = db
        self.settings = settings
        self.retries = settings.notify_retries if retries is None else retries
        self.backoff = backoff or (0.0,)
        self.client = client or httpx.AsyncClient(
            timeout=settings.notify_timeout, follow_redirects=True
        )
        self._inflight: set[asyncio.Task] = set()
        self._tz = _load_timezone(settings.timezone)
        # Who holds a lease, so a row claimed by a process that died can be told
        # apart from one this process is working on.  The pid alone would repeat
        # after a restart on a container that reuses pid 1.
        self._owner = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._closing = False
        self._sends = asyncio.Semaphore(MAX_CONCURRENT_SENDS)
        self._channel_gates: dict[int, asyncio.Semaphore] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Begin draining the queue.  Idempotent; needs a running event loop."""
        if self._worker is not None and not self._worker.done():
            return
        self._closing = False
        self._worker = asyncio.create_task(self._run(), name="notify-outbox")

    def kick(self) -> None:
        """Say that something was queued.  Safe to call from any coroutine.

        Only ever an optimisation: the worker polls anyway, so a kick lost to a
        crash between COMMIT and here costs latency, not the notification.
        """
        self._wake.set()

    async def _run(self) -> None:
        """Drain the queue, then wait for a kick or for the next row to come due."""
        while not self._closing:
            try:
                await self.pump()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A pass that raises must not end the worker: the next one has
                # a fresh claim and the failed rows kept their leases.
                log.exception("notification queue pass failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._idle_seconds())

    def _idle_seconds(self) -> float:
        """How long to wait before looking again, absent a kick.

        Sleeping a fixed interval would make a retry that is already due wait it
        out — three quick retries would take three polls rather than three
        attempts.  The queue knows when its earliest row comes due, so wait for
        that, and never longer than a poll so a write from another process (or a
        lost kick) is still noticed.
        """
        due = self.db.next_delivery_due_at()
        if due is None:
            return POLL_SECONDS
        try:
            deadline = datetime.fromisoformat(due)
        except (TypeError, ValueError):
            return POLL_SECONDS
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        waiting = (deadline - datetime.now(UTC)).total_seconds()
        return min(POLL_SECONDS, max(0.0, waiting))

    async def drain(self) -> None:
        """Settle everything owed right now (shutdown, and determinism in tests).

        Passes repeat because one pass can only claim ``CLAIM_BATCH`` rows and a
        retry with no backoff comes due immediately.  A retry scheduled into the
        future is deliberately left in the queue — it is durable, and waiting for
        it here would hold a shutdown open for as long as the backoff.
        """
        for _ in range(max(4, self.retries + 2)):
            while self._inflight:
                await asyncio.gather(*list(self._inflight), return_exceptions=True)
            if not await self.pump():
                break
        while self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    async def aclose(self) -> None:
        self._closing = True
        worker, self._worker = self._worker, None
        if worker is not None:
            self._wake.set()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        await self.drain()
        await self.client.aclose()

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    # -- entry points ------------------------------------------------------

    async def on_message(
        self, message_id: int, frame: dict[str, Any], *, event_key: str | None = None
    ) -> None:
        """Gateway hook.  Records that a push is owed; the ack must not wait.

        ``event_key`` names the agent event behind the message.  The gateway has
        already written this intent inside the event's own transaction and passes
        the same key, so this insert is a no-op there and the queue cannot hold
        the same push twice — including after a replay of a lost ack.
        """
        self._queue("message", ref_id=message_id, frame=frame, event_key=event_key)

    async def on_task_result(
        self, task_id: int, frame: dict[str, Any], *, event_key: str | None = None
    ) -> None:
        """Gateway hook for keep-alive task receipts.

        Whether the task wants a push at all is decided when the intent is
        expanded, not here: reading ``tasks`` is a query, and this runs on the
        path that owes the agent an ack.
        """
        self._queue("task_result", ref_id=task_id, frame=frame, event_key=event_key)

    async def on_call(
        self, call_id: int, frame: dict[str, Any], *, event_key: str | None = None
    ) -> None:
        """Gateway hook for call attempts.

        Only inbound calls push, which is also decided at expansion.  An
        outbound keep-alive dial is something the operator scheduled and already
        hears about through the task receipt; pushing it again would train them
        to ignore the channel.  An incoming call is the opposite — nobody asked
        for it, and on a card kept alive for one service it is the only sign
        someone is trying to reach the number.
        """
        self._queue("call", ref_id=call_id, frame=frame, event_key=event_key)

    def _queue(
        self,
        kind: str,
        *,
        ref_id: int | None,
        frame: dict[str, Any],
        event_key: str | None,
    ) -> int | None:
        outbox_id = self.db.enqueue_notification(
            kind, ref_id=ref_id, frame=frame, event_key=event_key
        )
        self.kick()
        return outbox_id

    # -- queue -------------------------------------------------------------

    async def pump(self) -> int:
        """One pass over the queue.  Returns how many deliveries were attempted.

        Expansion first, so a push queued a moment ago is attempted in the same
        pass; then one bounded batch of due deliveries.  Everything a pass learns
        is written back to the row, so the next pass — in this process or in the
        one that replaces it — starts from the same place.
        """
        self._expand_intents()
        claimed = self.db.claim_deliveries(
            owner=self._owner, lease_seconds=LEASE_SECONDS, limit=CLAIM_BATCH
        )
        if not claimed:
            return 0
        await asyncio.gather(*[self._deliver_claimed(row) for row in claimed])
        return len(claimed)

    def _expand_intents(self) -> None:
        """Turn queued intents into one delivery per channel that wants them.

        Deliveries are written *before* the intent is marked expanded, so a crash
        in between leaves the intent pending and the next pass repeats the work —
        which is safe because the unique index on (intent, channel, rule) turns a
        repeat into a no-op rather than a second push.
        """
        for intent in self.db.pending_intents(EXPAND_BATCH):
            try:
                targets, title, body = self._targets_for(intent)
            except Exception:
                # A bad rule pattern or a row shaped unexpectedly must not stall
                # every later notification behind it.
                log.exception("cannot expand notification intent %s", intent["id"])
                self.db.finish_intent(intent["id"], "skipped")
                continue
            expires_at = _shift(
                intent.get("created_at") or utcnow(),
                EXPIRY_SECONDS.get(str(intent["kind"]), DEFAULT_EXPIRY_SECONDS),
            )
            for channel_id, rule_id in targets:
                self.db.add_delivery(
                    intent["id"], channel_id, rule_id=rule_id, expires_at=expires_at
                )
            self.db.finish_intent(
                intent["id"],
                "expanded" if targets else "skipped",
                title=title,
                body=body,
            )

    def _targets_for(
        self, intent: dict[str, Any]
    ) -> tuple[list[tuple[int, int | None]], str, str]:
        """Which (channel, rule) pairs this intent owes, and the text to send.

        The text is returned for the kinds whose wording is fixed by the event —
        a task receipt, a call — so a later attempt sends what happened rather
        than re-rendering from rows that have moved on.  A message renders per
        channel template at send time instead, and gets no text here.
        """
        kind = str(intent["kind"])
        frame = _loads(intent.get("frame"))
        ref_id = intent.get("ref_id")
        if kind == "message":
            return self._message_targets(int(ref_id or 0), frame), "", ""
        if kind == "task_result":
            return self._task_targets(ref_id, frame)
        if kind == "call":
            return self._call_targets(ref_id, frame)
        log.warning("unknown notification intent kind %r", kind)
        return [], "", ""

    def _message_targets(
        self, message_id: int, frame: dict[str, Any]
    ) -> list[tuple[int, int | None]]:
        message = self.db.one(
            "SELECT m.*, s.label AS sim_label FROM messages m "
            "LEFT JOIN sims s ON s.id = m.sim_id WHERE m.id = ?",
            (message_id,),
        )
        if message is None:
            # Deleted by retention before the queue reached it.  Nothing to push
            # and nothing to complain about.
            log.debug("message %s is gone; dropping its notification", message_id)
            return []
        if _suppressed(message, frame):
            log.debug("suppressing notification for data SMS %s", message_id)
            return []
        body = notification_body(message, frame)
        targets = match_rules(self.db, sim_id=message.get("sim_id"), body=body)
        if not targets:
            log.debug("no rule matched message %s", message_id)
        return [(int(channel["id"]), _optional_id(rule)) for rule, channel in targets]

    def _task_targets(
        self, task_id: Any, frame: dict[str, Any]
    ) -> tuple[list[tuple[int, int | None]], str, str]:
        task = self.db.one(
            "SELECT t.*, s.label AS sim_label FROM tasks t "
            "LEFT JOIN sims s ON s.id = t.sim_id WHERE t.id = ?",
            (task_id,),
        ) or {}
        if not task.get("notify_on_result", frame.get("notify", True)):
            return [], "", ""
        body = self._task_summary(int(task_id or 0), task, frame)
        return self._every_enabled_channel(), "保号任务", body

    def _call_targets(
        self, call_id: Any, frame: dict[str, Any]
    ) -> tuple[list[tuple[int, int | None]], str, str]:
        if str(frame.get("direction") or "") != "in":
            return [], "", ""
        call = self.db.one(
            "SELECT c.*, s.label AS sim_label FROM calls c "
            "LEFT JOIN sims s ON s.id = c.sim_id WHERE c.id = ?",
            (call_id,),
        ) or {}
        return self._every_enabled_channel(), "来电", self._call_summary(call, frame)

    def _every_enabled_channel(self) -> list[tuple[int, int | None]]:
        """Every channel, no rule — the audience for a hub notice.

        Rules describe which SMS a channel wants.  A task receipt or a call is
        not an SMS, so there is nothing for a keyword pattern to match and every
        channel the operator turned on is the intended audience.
        """
        return [
            (int(row["id"]), None)
            for row in self.db.query(
                "SELECT id FROM channels WHERE enabled = 1 ORDER BY id"
            )
        ]

    async def _deliver_claimed(self, row: dict[str, Any]) -> None:
        """One attempt at one claimed delivery, under both concurrency bounds."""
        try:
            async with self._sends, self._gate(int(row["channel_id"])):
                await self._attempt_claimed(row)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The lease expires on its own, so the row comes back rather than
            # being lost; what must not happen is one row ending the pass.
            log.exception("delivery %s failed outside the sender", row.get("id"))

    def _gate(self, channel_id: int) -> asyncio.Semaphore:
        gate = self._channel_gates.get(channel_id)
        if gate is None:
            gate = self._channel_gates[channel_id] = asyncio.Semaphore(MAX_PER_CHANNEL)
        return gate

    async def _attempt_claimed(self, row: dict[str, Any]) -> None:
        delivery_id = int(row["id"])
        channel_id = int(row["channel_id"])
        rule_id = row.get("rule_id")
        kind = str(row.get("kind") or "")
        message_id = int(row["ref_id"]) if kind == "message" and row.get("ref_id") else None
        attempts = int(row.get("attempts") or 0) + 1

        channel = self.db.one(
            "SELECT * FROM channels WHERE id = ? AND enabled = 1", (channel_id,)
        )
        if channel is None:
            # Turned off after the push was queued.  Nothing was attempted, so
            # this is not a delivery failure and must not raise an incident.
            self.db.settle_delivery(
                delivery_id, status="failed", attempts=attempts - 1,
                error_code="channel_disabled", safe_detail="渠道已停用",
            )
            return

        payload = self._payload_for(row, channel)
        if payload is None:
            self.db.settle_delivery(
                delivery_id, status="failed", attempts=attempts - 1,
                error_code="source_gone", safe_detail="来源记录已不存在",
            )
            return

        try:
            detail = await send_via_channel(self.client, channel, payload)
        except SendError as exc:
            failure = exc
        except Exception as exc:
            # Timeouts, DNS failures, TLS errors — httpx puts the URL in some of
            # these, and a Telegram URL carries the bot token.
            failure = SendError(
                scrub(f"{type(exc).__name__}: {exc}"), kind="network"
            )
        else:
            self.db.settle_delivery(
                delivery_id, status="ok", attempts=attempts, safe_detail=detail
            )
            self._record(message_id, channel_id, rule_id, "ok", attempts, detail)
            return

        code = _error_code(failure)
        expired = _past(row.get("expires_at"))
        if _transient(failure) and attempts < self.retries + 1 and not expired:
            delay = self._retry_delay(attempts - 1, failure.retry_after)
            self.db.settle_delivery(
                delivery_id, status="pending", attempts=attempts,
                error_code=code, safe_detail=failure.detail,
                next_attempt_at=_shift(utcnow(), delay),
            )
            log.info(
                "channel %s attempt %d failed (%s); retrying in %.1fs",
                channel_id, attempts, code, delay,
            )
            return

        log.warning(
            "channel %s (%s) gave up after %d attempt(s): %s",
            channel_id, channel.get("type"), attempts, failure.detail,
        )
        self.db.settle_delivery(
            delivery_id, status="expired" if expired else "failed",
            attempts=attempts, error_code="expired" if expired else code,
            safe_detail=failure.detail,
        )
        self._record(
            message_id, channel_id, rule_id, "failed", attempts, failure.detail
        )

    def _payload_for(
        self, row: dict[str, Any], channel: dict[str, Any]
    ) -> Payload | None:
        """What to send, or None if the event it describes is gone.

        A message is rendered here rather than at expansion because the template
        belongs to the rule and the channel, and the row it renders from is the
        one the operator can still read in the web UI.
        """
        if str(row.get("kind")) != "message":
            body = str(row.get("body") or "")
            if not body:
                return None
            context = {
                "sender": "", "card": "", "device": "", "iccid": "",
                "message": body, "timestamp": self._local(utcnow()),
            }
            return Payload(
                text=body, title=str(row.get("title") or "air780e-hub"),
                context=context,
            )

        message = self.db.one(
            "SELECT m.*, s.label AS sim_label, s.phone_number, "
            "s.iccid AS sim_iccid, d.label AS device_label FROM messages m "
            "LEFT JOIN sims s ON s.id = m.sim_id "
            "LEFT JOIN devices d ON d.agent_id = m.agent_id AND d.name = m.device "
            "WHERE m.id = ?",
            (row.get("ref_id"),),
        )
        if message is None:
            return None
        frame = _loads(row.get("frame"))
        rule = (
            self.db.one("SELECT * FROM rules WHERE id = ?", (row["rule_id"],))
            if row.get("rule_id")
            else None
        )
        return self.render_payload(channel, self.context_for(message, frame), rule=rule)

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        """How long before the next attempt.

        A provider that named its own window wins outright — coming back before
        it reopens earns the same refusal.  One whole second is added on that
        path because ``next_attempt_at`` is stored at second resolution, and the
        truncation alone would otherwise put the retry just inside the window it
        was told to wait out.  Otherwise the configured backoff, jittered: two
        deliveries that failed together against the same provider must not
        return together.
        """
        if retry_after is not None:
            return min(retry_after, RETRY_AFTER_CAP_SECONDS) + 1.0 + random.random()
        base = self._delay(attempt)
        return base * (0.5 + random.random()) if base else 0.0

    def _call_summary(self, call: dict[str, Any], frame: dict[str, Any]) -> str:
        """An incoming call as someone would want to read it on a phone."""
        peer = call.get("peer") or frame.get("peer") or "未知号码"
        outcome = str(call.get("outcome") or frame.get("outcome") or "")
        lines = [f"【来电】{peer} {CALL_OUTCOME_LABEL.get(outcome, outcome or '来电')}"]
        where = call.get("sim_label") or call.get("device") or frame.get("device")
        if where:
            lines.append(f"卡:{where}")
        # The stored row wins outright, and `or` would not give it that: the
        # gateway coerces a malformed duration to 0.0, which is falsy, so an
        # `or` chain would fall through the coerced value and back to the raw
        # frame field that needed coercing in the first place.
        ring = call["ring_seconds"] if "ring_seconds" in call else frame.get("ring_seconds")
        try:
            ring = float(ring)
        except (TypeError, ValueError):
            ring = 0.0
        # Worth a line only when it actually rang: "响铃 0 秒" reads like a bug
        # rather than like a call that was rejected before it ever rang.
        if ring:
            lines.append(f"响铃:{round(ring)} 秒")
        return "\n".join(lines)

    def _task_summary(
        self, task_id: int, task: dict[str, Any], frame: dict[str, Any]
    ) -> str:
        """A keep-alive receipt as someone would want to read it on a phone."""
        status = str(frame.get("status") or "unknown")
        lines = [
            f"【保号】{task.get('name') or f'任务 {task_id}'} "
            f"{TASK_STATUS_LABEL.get(status, status)}"
        ]
        where = task.get("sim_label") or task.get("device") or frame.get("device")
        if where:
            lines.append(f"卡:{where}")
        detail = frame.get("error") or frame.get("detail")
        if detail:
            lines.append(str(detail))
        attempts = int(frame.get("attempts") or 1)
        if attempts > 1:
            lines.append(f"尝试 {attempts} 次")
        if frame.get("next_run_at"):
            lines.append(f"下次:{self._local(str(frame['next_run_at']))}")
        return "\n".join(lines)

    def _spawn(
        self,
        coro: Any,
        *,
        label: str,
        on_dropped: Callable[[], None] | None = None,
    ) -> None:
        if len(self._inflight) >= MAX_INFLIGHT:
            log.warning(
                "%d deliveries in flight; dropping push for %s",
                len(self._inflight), label,
            )
            coro.close()
            if on_dropped is not None:
                on_dropped()
            return

        task = asyncio.create_task(self._quietly(coro, label), name=f"notify-{label}")
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    @staticmethod
    async def _quietly(coro: Any, label: str) -> None:
        try:
            await coro
        except Exception:
            # Nothing above catches this — a failed push must not be able to
            # take the gateway's connection down with it.
            log.exception("delivery failed for %s", label)

    async def deliver(
        self, message_id: int, frame: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Match rules for one stored message and push to every matched channel."""
        frame = frame or {}
        message = self.db.one(
            "SELECT m.*, s.label AS sim_label, s.phone_number, "
            "s.iccid AS sim_iccid, d.label AS device_label FROM messages m "
            "LEFT JOIN sims s ON s.id = m.sim_id "
            "LEFT JOIN devices d ON d.agent_id = m.agent_id AND d.name = m.device "
            "WHERE m.id = ?",
            (message_id,),
        ) or {}

        if _suppressed(message, frame):
            log.debug("suppressing notification for data SMS %s", message_id)
            return []

        # Matched on exactly what the channel will render, salvage included: a
        # keyword rule for "GitHub" should fire when the recovered fragment says
        # GitHub, and must not fire on mojibake the reader never sees.
        body = notification_body(message, frame)
        targets = match_rules(self.db, sim_id=message.get("sim_id"), body=body)
        if not targets:
            log.debug("no rule matched message %s", message_id)
            return []

        context = self.context_for(message, frame)
        results = await asyncio.gather(*[
            self.push(channel, context, rule=rule, message_id=message_id)
            for rule, channel in targets
        ])
        return list(results)

    async def notify_text(
        self, text: str, *, title: str = "air780e-hub", channel_ids: list[int] | None = None
    ) -> list[dict[str, Any]]:
        """Push a plain message not tied to an SMS — task results (M5) use this."""
        channels = self.db.query("SELECT * FROM channels WHERE enabled = 1 ORDER BY id")
        if channel_ids is not None:
            channels = [c for c in channels if c["id"] in set(channel_ids)]
        if not channels:
            return []

        # This is a system/task notification, not the sample SMS shown by the
        # test button.  Fake sample card/sender values here would leak into a
        # structured provider payload and look like real metadata.
        context = {
            "sender": "", "card": "", "device": "", "iccid": "",
            "message": text, "timestamp": self._local(utcnow()),
        }
        payload = Payload(text=text, title=title, context=context)
        results = await asyncio.gather(*[
            self._attempt(channel, payload, message_id=None, rule_id=None,
                          retries=self.retries)
            for channel in channels
        ])
        return list(results)

    async def test_channel(self, channel: dict[str, Any]) -> dict[str, Any]:
        """Send a sample through one channel.

        One attempt only: this is a button in the UI, and an admin waiting on
        an answer wants the provider's complaint now, not after two backoffs.
        """
        context = {**SAMPLE_CONTEXT, "timestamp": self._local(utcnow())}
        payload = Payload(
            text=render(DEFAULT_TEMPLATE, context),
            title=render(DEFAULT_TITLE, context),
            context=context,
        )
        return await self._attempt(
            channel, payload, message_id=None, rule_id=None, retries=0
        )

    @staticmethod
    def render_payload(
        channel: dict[str, Any],
        context: dict[str, str],
        *,
        rule: dict[str, Any] | None = None,
    ) -> Payload:
        """Render exactly what a channel will receive, without sending it."""
        return Payload(
            text=render((rule or {}).get("template") or DEFAULT_TEMPLATE, context),
            title=render(
                channel_config(channel).get("title") or DEFAULT_TITLE, context
            ),
            context=context,
        )

    async def push(
        self,
        channel: dict[str, Any],
        context: dict[str, str],
        *,
        rule: dict[str, Any] | None = None,
        message_id: int | None = None,
    ) -> dict[str, Any]:
        payload = self.render_payload(channel, context, rule=rule)
        return await self._attempt(
            channel, payload,
            message_id=message_id,
            rule_id=(rule or {}).get("id"),
            retries=self.retries,
        )

    # -- delivery ----------------------------------------------------------

    async def _attempt(
        self,
        channel: dict[str, Any],
        payload: Payload,
        *,
        message_id: int | None,
        rule_id: int | None,
        retries: int,
    ) -> dict[str, Any]:
        detail = ""
        attempts = 0
        for attempt in range(retries + 1):
            attempts = attempt + 1
            try:
                detail = await send_via_channel(self.client, channel, payload)
            except SendError as exc:
                detail = exc.detail
            except Exception as exc:
                # Timeouts, DNS failures, TLS errors — httpx puts the URL in
                # some of these, and a Telegram URL carries the bot token.
                detail = scrub(f"{type(exc).__name__}: {exc}")
            else:
                self._record(message_id, channel["id"], rule_id, "ok", attempts, detail)
                return {"channel_id": channel["id"], "status": "ok",
                        "attempts": attempts, "detail": detail}

            if attempt < retries:
                await asyncio.sleep(self._delay(attempt))

        log.warning(
            "channel %s (%s) failed after %d attempt(s): %s",
            channel["id"], channel.get("type"), attempts, detail,
        )
        self._record(message_id, channel["id"], rule_id, "failed", attempts, detail)
        return {"channel_id": channel["id"], "status": "failed",
                "attempts": attempts, "detail": detail}

    def _delay(self, attempt: int) -> float:
        return self.backoff[min(attempt, len(self.backoff) - 1)]

    def _record(
        self,
        message_id: int | None,
        channel_id: int | None,
        rule_id: int | None,
        status: str,
        attempts: int,
        detail: str,
    ) -> None:
        safe_detail = scrub(detail)
        self.db.execute(
            "INSERT INTO notify_logs "
            "(message_id, channel_id, rule_id, status, attempts, detail, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, channel_id, rule_id, status, attempts,
             safe_detail, utcnow()),
        )
        fingerprint = (
            f"notify-channel:{channel_id}" if channel_id is not None else "notify-queue"
        )
        if status == "failed":
            channel = (
                self.db.one("SELECT name FROM channels WHERE id = ?", (channel_id,))
                if channel_id is not None
                else None
            )
            label = (channel or {}).get("name") or (
                f"渠道 {channel_id}" if channel_id is not None else "通知队列"
            )
            self.db.open_incident(
                fingerprint,
                kind="notification_failed",
                severity="warning",
                source=str(label),
                title=f"{label} 投递失败",
                detail=safe_detail or f"尝试 {attempts} 次后失败",
            )
        elif status == "ok":
            self.db.resolve_incident(fingerprint, detail="通知投递已恢复")

    # -- context -----------------------------------------------------------

    def context_for(
        self, message: dict[str, Any], frame: dict[str, Any] | None = None
    ) -> dict[str, str]:
        frame = frame or {}
        iccid = message.get("sim_iccid") or frame.get("iccid") or ""
        device = message.get("device") or frame.get("device") or ""
        return {
            "sender": message.get("peer") or frame.get("peer") or "",
            # A damaged message renders its salvaged fragment, not the mojibake
            # in ``body``.  Decided here rather than at the call site so the
            # text a rule matched on is the text the channel receives.
            "message": notification_body(message, frame),
            "timestamp": self._local(message.get("ts") or frame.get("ts") or utcnow()),
            "card": self._card_name(message, iccid, device),
            "device": device,
            "iccid": iccid,
        }

    @staticmethod
    def _card_name(message: dict[str, Any], iccid: str, device: str) -> str:
        """What to call the card in a push, best available name first.

        The SIM's own label wins because it survives being moved between
        modules, but it starts out empty — until the admin names the card, the
        module's label ("移动卡", from the agent's config) reads far better in
        a notification than the tail of an ICCID.
        """
        return (
            message.get("sim_label")
            or message.get("phone_number")
            or message.get("device_label")
            or (f"…{iccid[-4:]}" if iccid else "")
            or device
        )

    def _local(self, ts: str) -> str:
        """Stored timestamps are UTC; a push should read in the user's zone."""
        try:
            parsed = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            return ts
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(self._tz).strftime("%Y-%m-%d %H:%M:%S")
