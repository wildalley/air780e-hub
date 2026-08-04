"""Outbound link to the server.

The agent always dials out, so nothing here listens on a port and no inbound
firewall rule is needed.  The link is *optional*: if it never
comes up, messages are still received, stored and keep-alive tasks still run
— they just queue until there is somewhere to send them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .config import ServerConfig
from .store import LocalStore

log = logging.getLogger(__name__)

CommandHandler = Callable[[dict[str, Any]], Awaitable[None]]
DescribeDevices = Callable[[], list[dict[str, Any]]]

BATCH = 200
PING_INTERVAL = 30
PING_TIMEOUT = 90

CLOSE_AUTH_FAILED = 4001
CLOSE_PROTOCOL_ERROR = 4002
CLOSE_AGENT_CONFLICT = 4003


class ServerLink:
    def __init__(
        self,
        config: ServerConfig,
        *,
        agent_id: str,
        version: str,
        store: LocalStore,
        on_command: CommandHandler,
        describe_devices: DescribeDevices,
        max_delay: float = 60.0,
    ) -> None:
        self.config = config
        self.agent_id = agent_id
        self.version = version
        self.store = store
        self.on_command = on_command
        self.describe_devices = describe_devices
        self.max_delay = max_delay

        self._wake = asyncio.Event()
        self._stopped = False
        self._connected = False
        self._sent_through = 0

    @property
    def connected(self) -> bool:
        return self._connected

    def wake(self) -> None:
        """Nudge the sender — a new event is waiting in the queue."""
        self._wake.set()

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    async def run(self) -> None:
        if not self.config.enabled:
            log.info("no server configured; running standalone")
            return

        delay = 1.0
        while not self._stopped:
            try:
                await self._connect_once()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except InvalidStatus as exc:
                status = getattr(exc.response, "status_code", "?")
                # A bad token will never fix itself by retrying quickly.
                delay = self.max_delay
                log.error("server rejected the connection (HTTP %s); "
                          "check server.token", status)
            except ConnectionClosed as exc:
                if exc.rcvd is not None and exc.rcvd.code == CLOSE_AUTH_FAILED:
                    delay = self.max_delay
                    log.error("server closed the link: authentication failed")
                else:
                    log.warning("link closed: %s", exc)
            except OSError as exc:
                log.warning("cannot reach server: %s", exc)
            except Exception:
                log.exception("unexpected link failure")
            finally:
                self._connected = False

            if self._stopped:
                break
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, self.max_delay)

    async def _connect_once(self) -> None:
        headers = {"Authorization": f"Bearer {self.config.token}"}
        log.info("connecting to %s", self.config.url)

        async with connect(
            self.config.url,
            additional_headers=headers,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            max_size=4 * 1024 * 1024,
        ) as ws:
            self._connected = True
            self._sent_through = 0
            log.info("link established (%d event(s) queued)",
                     self.store.unacked_count())

            await self._send(ws, {
                "type": "hello",
                "agent_id": self.agent_id,
                "version": self.version,
                "last_seq": self.store.last_seq(),
                "devices": self.describe_devices(),
            })

            sender = asyncio.create_task(self._sender(ws), name="link-sender")
            try:
                await self._receiver(ws)
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    # -- outbound ----------------------------------------------------------

    async def _sender(self, ws) -> None:
        while not self._stopped:
            events = [
                event
                for event in self.store.unacked_events(limit=BATCH)
                if event.seq > self._sent_through
            ]
            if not events:
                self._wake.clear()
                try:
                    # The timeout is a safety net: if a wake is ever missed,
                    # the queue still drains within a few seconds.
                    await asyncio.wait_for(self._wake.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                continue

            for event in events:
                await self._send(ws, event.to_frame())
                self._sent_through = event.seq

    async def _send(self, ws, frame: dict[str, Any]) -> None:
        await ws.send(json.dumps(frame, ensure_ascii=False))

    # -- inbound -----------------------------------------------------------

    async def _receiver(self, ws) -> None:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except (ValueError, TypeError):
                log.warning("ignoring non-JSON frame from server")
                continue
            if not isinstance(frame, dict) or "type" not in frame:
                log.warning("ignoring malformed frame from server")
                continue

            kind = frame["type"]
            if kind == "ack":
                self._handle_ack(frame)
            elif kind == "resend_from":
                self._handle_resend(frame)
            else:
                try:
                    await self.on_command(frame)
                except Exception:
                    log.exception("command handler failed for %s", kind)

    def _handle_ack(self, frame: dict[str, Any]) -> None:
        seq = frame.get("seq")
        if not isinstance(seq, int):
            return
        removed = self.store.ack_through(seq)
        if removed:
            log.debug("acked through %d (%d event(s) cleared)", seq, removed)

    def _handle_resend(self, frame: dict[str, Any]) -> None:
        seq = frame.get("seq")
        if not isinstance(seq, int):
            return
        # Rewinding the in-session marker is enough; the sender picks it up.
        log.info("server asked for a resend from seq %d", seq)
        self._sent_through = max(0, seq - 1)
        self.wake()
