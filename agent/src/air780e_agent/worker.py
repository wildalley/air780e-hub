"""One worker per module.

Owns the serial port, keeps the modem initialized, and turns everything the
modem does into events for the outbound queue.  A worker never gives up: a
module that is unplugged, reset by a brownout or reflashed comes back on its
own once the port reappears.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .at import ATClient, ATError, SerialTransport, Transport
from .config import DeviceConfig
from .modem import Air780E, Signal
from .pdu import DecodedSms
from .store import LocalStore

log = logging.getLogger(__name__)

EmitCallback = Callable[[str, dict[str, Any]], None]

# Below this change in +CSQ a sample is not worth a row in the graph.
RSSI_NOISE_FLOOR = 2
# Re-send an unchanged status at least this often, so "still alive" is visible.
STATUS_HEARTBEAT = 900.0


class TransportFactory(Protocol):
    def __call__(self, config: DeviceConfig) -> Transport: ...


def _default_transport(config: DeviceConfig) -> Transport:
    return SerialTransport(config.port, baudrate=config.baudrate)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class DeviceState:
    online: bool = False
    registered: bool = False
    operator: str = ""
    iccid: str = ""
    imei: str = ""
    model: str = ""
    smsc: str = ""
    signal: Signal = field(default_factory=Signal)
    storage_used: int = 0
    storage_capacity: int = 0
    last_error: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "registered": self.registered,
            "operator": self.operator,
            "iccid": self.iccid,
            "imei": self.imei,
            "model": self.model,
            "smsc": self.smsc,
            "rssi": self.signal.rssi,
            "dbm": self.signal.dbm,
            "bars": self.signal.bars,
            "rsrp": self.signal.rsrp,
            "rsrq": self.signal.rsrq,
            "storage_used": self.storage_used,
            "storage_capacity": self.storage_capacity,
        }


class DeviceOffline(RuntimeError):
    """Raised when a command arrives for a module that is not currently up."""


class DeviceWorker:
    def __init__(
        self,
        config: DeviceConfig,
        store: LocalStore,
        emit: EmitCallback,
        *,
        status_interval: float = 60.0,
        reconnect_max_delay: float = 60.0,
        transport_factory: TransportFactory = _default_transport,
    ) -> None:
        self.config = config
        self.store = store
        self.emit = emit
        self.status_interval = status_interval
        self.reconnect_max_delay = reconnect_max_delay
        self._transport_factory = transport_factory

        self.state = DeviceState()
        self._client: ATClient | None = None
        self._modem: Air780E | None = None
        self._ready = asyncio.Event()
        self._stopped = False
        self._last_status_sent = 0.0
        self._last_status_payload: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def online(self) -> bool:
        return self.state.online

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.config.label,
            "port": self.config.port,
            **self.state.describe(),
        }

    # -- supervision -------------------------------------------------------

    async def run(self) -> None:
        delay = 1.0
        while not self._stopped:
            try:
                await self._connect()
                delay = 1.0
                await self._serve()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._go_offline(str(exc))
                log.warning("[%s] %s; retrying in %.0fs", self.name, exc, delay)
            finally:
                await self._teardown()

            if self._stopped:
                break
            # Jitter keeps two modules from retrying in lockstep after a
            # USB controller reset takes both down at once.
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, self.reconnect_max_delay)

    async def stop(self) -> None:
        self._stopped = True
        await self._teardown()

    async def _connect(self) -> None:
        log.info("[%s] opening %s", self.name, self.config.port)
        transport = self._transport_factory(self.config)
        client = ATClient(transport, name=self.name)
        await client.open()
        self._client = client

        modem = Air780E(
            client,
            on_sms=self._on_sms,
            storage=self.config.storage,
            delete_after_read=self.config.delete_after_read,
        )
        info = await modem.initialize()
        self._modem = modem

        self.state.online = True
        self.state.last_error = ""
        self.state.model = info.model
        self.state.imei = info.imei
        self.state.iccid = info.iccid
        self.state.smsc = info.smsc
        self.state.operator = info.operator
        self.state.registered = info.registered
        self._ready.set()

        log.info(
            "[%s] up: %s iccid=%s operator=%s",
            self.name, info.model or "?", info.iccid or "?", info.operator or "?",
        )
        if not info.smsc:
            self._log_event("warning", "no SMSC configured; sending will fail")

        # Anything that arrived while we were down is still in the modem's
        # store — collect it, and free the slots.
        recovered = await modem.drain_inbox()
        if recovered:
            log.info("[%s] recovered %d stored message(s)", self.name, len(recovered))

        await self._sample_status(force=True)

    async def _serve(self) -> None:
        while not self._stopped:
            await asyncio.sleep(self.status_interval)
            await self._sample_status()

    async def _teardown(self) -> None:
        self._ready.clear()
        modem, self._modem = self._modem, None
        client, self._client = self._client, None
        if modem is not None:
            await modem.close()
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    def _go_offline(self, reason: str) -> None:
        was_online = self.state.online
        self.state.online = False
        self.state.last_error = reason
        self._ready.clear()
        if was_online:
            self._emit_status(force=True)

    # -- status ------------------------------------------------------------

    async def _sample_status(self, *, force: bool = False) -> None:
        modem = self._modem
        if modem is None:
            return
        self.state.signal = await modem.read_signal()
        used, capacity = await modem.storage_usage()
        self.state.storage_used = used
        self.state.storage_capacity = capacity

        if capacity and used >= capacity * 0.8:
            # Approaching full means messages are about to start vanishing.
            self._log_event(
                "warning", f"storage {used}/{capacity}, draining"
            )
            await modem.drain_inbox()

        self._emit_status(force=force)

    def _emit_status(self, *, force: bool = False) -> None:
        payload = {"device": self.name, "ts": _now(), **self.state.describe()}
        if not force and not self._status_worth_sending(payload):
            return
        self._last_status_payload = payload
        self._last_status_sent = asyncio.get_running_loop().time()
        self.emit("status", payload)

    def _status_worth_sending(self, payload: dict[str, Any]) -> bool:
        previous = self._last_status_payload
        if previous is None:
            return True
        loop_time = asyncio.get_running_loop().time()
        if loop_time - self._last_status_sent >= STATUS_HEARTBEAT:
            return True

        for key in ("online", "registered", "operator", "storage_used"):
            if previous.get(key) != payload.get(key):
                return True

        old, new = previous.get("rssi"), payload.get("rssi")
        if (old is None) != (new is None):
            return True
        if old is not None and new is not None:
            return abs(old - new) >= RSSI_NOISE_FLOOR
        return False

    # -- incoming ----------------------------------------------------------

    def _on_sms(self, sms: DecodedSms) -> None:
        ts = sms.timestamp.isoformat() if sms.timestamp else _now()
        segments = sms.concat.total if sms.concat else 1

        self.store.record_message(
            device=self.name,
            direction="in",
            peer=sms.address,
            body=sms.text,
            ts=ts,
            iccid=self.state.iccid or None,
            status="received",
            segments=segments,
        )
        self.emit(
            "sms_in",
            {
                "device": self.name,
                "iccid": self.state.iccid,
                "peer": sms.address,
                "body": sms.text,
                "ts": ts,
                "ts_source": "scts" if sms.timestamp else "local",
                "segments": segments,
                "pdu": sms.raw,
            },
        )
        # Deliberately no message body in the log (PLAN.md section 10).
        log.info("[%s] sms from %s (%d chars)", self.name, sms.address, len(sms.text))

    # -- commands ----------------------------------------------------------

    def _require_modem(self) -> Air780E:
        if self._modem is None or not self.state.online:
            raise DeviceOffline(
                f"device {self.name} is offline: {self.state.last_error or 'not connected'}"
            )
        return self._modem

    async def send_sms(
        self, number: str, body: str, *, cmd_id: str | None = None
    ) -> list[int]:
        modem = self._require_modem()
        ts = _now()
        try:
            refs = await modem.send_sms(number, body)
        except ATError as exc:
            self.store.record_message(
                device=self.name, direction="out", peer=number, body=body,
                ts=ts, iccid=self.state.iccid or None, status="failed",
            )
            self.emit("sms_out", {
                "device": self.name, "iccid": self.state.iccid, "peer": number,
                "body": body, "ts": ts, "status": "failed", "refs": [],
                "cmd_id": cmd_id, "error": str(exc),
            })
            raise

        self.store.record_message(
            device=self.name, direction="out", peer=number, body=body,
            ts=ts, iccid=self.state.iccid or None, status="sent",
            segments=len(refs),
        )
        self.emit("sms_out", {
            "device": self.name, "iccid": self.state.iccid, "peer": number,
            "body": body, "ts": ts, "status": "sent", "refs": refs,
            "cmd_id": cmd_id, "error": None,
        })
        log.info("[%s] sent to %s (%d segment(s))", self.name, number, len(refs))
        return refs

    async def ping(self, host: str = "www.baidu.com") -> bool:
        return await self._require_modem().ping(host)

    async def raw_at(self, command: str) -> list[str]:
        client = self._client
        if client is None or not self.state.online:
            raise DeviceOffline(f"device {self.name} is offline")
        response = await client.execute(command, timeout=30.0)
        return response.lines

    async def refresh(self) -> dict[str, Any]:
        await self._sample_status(force=True)
        return self.describe()

    # -- logging -----------------------------------------------------------

    def _log_event(self, level: str, message: str) -> None:
        self.emit("log", {"device": self.name, "level": level, "message": message})
