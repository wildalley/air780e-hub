"""Push engine: an inbound SMS becomes one or more notifications.

Three things here are less obvious than they look:

* **Dispatch must not block ingest.**  ``Gateway._ingest`` applies an event
  and *then* acks it.  A push with retries can take tens of seconds, so doing
  it inline would stall the ack — and the agent, having heard nothing, would
  replay the message on its next reconnect.  ``on_message`` therefore only
  spawns a task and returns.

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
import hashlib
import hmac
import json
import logging
import re
import smtplib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Callable
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

SAMPLE_CONTEXT = {
    "sender": "10086",
    "message": "这是一条来自 air780e-hub 的测试消息。",
    "card": "测试卡",
    "device": "test",
    "iccid": "",
}

TASK_STATUS_LABEL = {"ok": "执行成功", "failed": "执行失败", "skipped": "已跳过"}


class SendError(RuntimeError):
    """A channel refused the message.  ``detail`` is safe to store and show."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
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
        raise SendError(f"channel is missing the {key!r} setting")
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


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code // 100 != 2:
        raise SendError(
            scrub(f"HTTP {response.status_code}: {_provider_message(response)}")
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


async def _send_feishu(
    client: httpx.AsyncClient, config: dict[str, Any], payload: Payload
) -> str:
    body: dict[str, Any] = {"msg_type": "text", "content": {"text": payload.text}}
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
    return await asyncio.to_thread(_send_smtp_blocking, config, payload, timeout)


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


def _load_timezone(name: str) -> Any:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # Slim base images ship no zoneinfo database; timestamps in UTC are
        # wrong-looking but harmless, a crashed push is not.
        log.warning("timezone %r unavailable (tzdata missing?); using UTC", name)
        return timezone.utc


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

    # -- lifecycle ---------------------------------------------------------

    async def drain(self) -> None:
        """Wait for in-flight deliveries (shutdown, and determinism in tests)."""
        while self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- entry points ------------------------------------------------------

    async def on_message(self, message_id: int, frame: dict[str, Any]) -> None:
        """Gateway hook.  Returns immediately; the ack must not wait on a push."""
        self._spawn(
            self.deliver(message_id, frame),
            label=f"message {message_id}",
            on_dropped=lambda: self._record(
                message_id, None, None, "failed", 0, "push queue full"
            ),
        )

    async def on_task_result(self, task_id: int, frame: dict[str, Any]) -> None:
        """Gateway hook for keep-alive task receipts."""
        task = self.db.one(
            "SELECT t.*, s.label AS sim_label FROM tasks t "
            "LEFT JOIN sims s ON s.id = t.sim_id WHERE t.id = ?",
            (task_id,),
        ) or {}
        wanted = task.get("notify_on_result", frame.get("notify", True))
        if not wanted:
            return

        self._spawn(
            self.notify_text(self._task_summary(task_id, task, frame), title="保号任务"),
            label=f"task {task_id}",
        )

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

        body = message.get("body") or frame.get("body") or ""
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

        context = {**SAMPLE_CONTEXT, "message": text, "timestamp": self._local(utcnow())}
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
        self.db.execute(
            "INSERT INTO notify_logs "
            "(message_id, channel_id, rule_id, status, attempts, detail, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, channel_id, rule_id, status, attempts,
             scrub(detail), utcnow()),
        )

    # -- context -----------------------------------------------------------

    def context_for(
        self, message: dict[str, Any], frame: dict[str, Any] | None = None
    ) -> dict[str, str]:
        frame = frame or {}
        iccid = message.get("sim_iccid") or frame.get("iccid") or ""
        device = message.get("device") or frame.get("device") or ""
        return {
            "sender": message.get("peer") or frame.get("peer") or "",
            "message": message.get("body") or frame.get("body") or "",
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
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(self._tz).strftime("%Y-%m-%d %H:%M:%S")
