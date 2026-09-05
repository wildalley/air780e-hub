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
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from . import PROTOCOL_VERSION
from .commands import CommandDispatcher, CommandHandler, RejectHandler
from .config import ServerConfig
from .store import LocalStore

log = logging.getLogger(__name__)

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
        on_command_rejected: RejectHandler,
        describe_devices: DescribeDevices,
        max_delay: float = 60.0,
    ) -> None:
        self.config = config
        self.agent_id = agent_id
        self.version = version
        self.store = store
        self._commands = CommandDispatcher(on_command, on_command_rejected)
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
        await self._commands.stop()

    async def run(self) -> None:
        try:
            await self._reconnect()
        finally:
            await self.stop()

    async def _reconnect(self) -> None:
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
                "protocol_version": PROTOCOL_VERSION,
                "last_seq": self.store.last_seq(),
                # Tells the server which sequence-number space these events
                # belong to, so a rebuilt local queue is read as a new stream
                # rather than as a replay of numbers it has already seen.
                "stream_id": self.store.stream_id(),
                "devices": self.describe_devices(),
            })

            sender = asyncio.create_task(self._sender(ws), name="link-sender")
            try:
                await self._receiver(ws)
            finally:
                sender.cancel()
                try:
                    self._commands.discard_queued()
                finally:
                    await asyncio.gather(sender, return_exceptions=True)

    # -- outbound ----------------------------------------------------------

    async def _sender(self, ws) -> None:
        while not self._stopped:
            # Clear before looking at the queue.  An append or ACK that races
            # the query then leaves the event set and cannot strand work until
            # the five-second safety timeout.
            self._wake.clear()
            events = [
                event
                for event in self.store.unacked_events(limit=BATCH)
                if event.seq > self._sent_through
            ]
            if not events:
                try:
                    # The timeout is a safety net: if a wake is ever missed,
                    # the queue still drains within a few seconds.
                    async with asyncio.timeout(5.0):
                        await self._wake.wait()
                except TimeoutError:
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
            if self._stopped:
                return
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
                accepted = self._commands.submit(frame)
                if not accepted and frame.get("cmd_id") is None:
                    # sync_tasks has no receipt. Reconnect so the server sends
                    # its full configuration again instead of silently losing it.
                    await ws.close(code=1013, reason="command queue is full")
                    return

    def _handle_ack(self, frame: dict[str, Any]) -> None:
        seq = frame.get("seq")
        if not isinstance(seq, int):
            return
        removed = self.store.ack_through(seq)
        if removed:
            log.debug("acked through %d (%d event(s) cleared)", seq, removed)
            # unacked_events() reads the oldest BATCH rows.  Once their ACK
            # removes them, wake the sender so the next batch is visible now
            # rather than after the safety timeout.
            self.wake()

    def _handle_resend(self, frame: dict[str, Any]) -> None:
        seq = frame.get("seq")
        if not isinstance(seq, int):
            return
        # Rewinding the in-session marker is enough; the sender picks it up.
        log.info("server asked for a resend from seq %d", seq)
        self._sent_through = max(0, seq - 1)
        self.wake()
