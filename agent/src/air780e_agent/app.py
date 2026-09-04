"""Agent application: workers, durable queue and the server link, wired up.

Everything the agent does flows through :meth:`AgentApp.emit` — one durable
append followed by a nudge to the link.  Nothing is sent directly to the
socket, which is what makes an outage indistinguishable from a slow server.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import __version__
from .config import AgentConfig, DeviceConfig
from .discovery import PortRegistry, ProbeResult
from .link import ServerLink
from .scheduler import KeepAliveScheduler
from .store import LocalStore
from .worker import DeviceOffline, DeviceWorker, TransportFactory, _default_transport

log = logging.getLogger(__name__)


class AgentApp:
    def __init__(
        self,
        config: AgentConfig,
        *,
        transport_factory: TransportFactory = _default_transport,
        store: LocalStore | None = None,
        registry: PortRegistry | None = None,
    ) -> None:
        self.config = config
        self.store = store or LocalStore(config.db_path)
        self.workers: dict[str, DeviceWorker] = {}

        # Shared, so two workers never probe at the same moment or claim the
        # same port.  Only needed by devices that did not pin one.
        self.registry = registry or PortRegistry(
            port_glob=config.port_glob,
            probe_timeout=config.probe_timeout,
            sole_device=len(config.devices) == 1,
        )

        self._transport_factory = transport_factory
        # Modules adopted by autodetect.  Kept alongside config.devices so a
        # later survey sees them as spoken for.
        self._adopted: list[DeviceConfig] = []

        for device in config.devices:
            self.workers[device.name] = self._build_worker(device)

        self.link = ServerLink(
            config.server,
            agent_id=config.agent_id,
            version=__version__,
            store=self.store,
            on_command=self.handle_command,
            describe_devices=self.describe_devices,
            max_delay=config.reconnect_max_delay,
        )

        # Keep-alive runs from the local clock and task table, so an outage
        # delays the receipt, never the task itself.
        self.scheduler = KeepAliveScheduler(
            self.store,
            self.workers,
            self.emit,
            tick=config.scheduler_tick,
            retry_delay=config.scheduler_retry_delay,
        )

        self._tasks: list[asyncio.Task] = []
        self._stopped = False

    def _build_worker(self, device: DeviceConfig) -> DeviceWorker:
        return DeviceWorker(
            device,
            self.store,
            self.emit,
            status_interval=self.config.status_interval,
            reconnect_max_delay=self.config.reconnect_max_delay,
            health_check_timeout=self.config.health_check_timeout,
            health_failure_threshold=self.config.health_failure_threshold,
            registration_recovery_delay=self.config.registration_recovery_delay,
            recovery_cooldown=self.config.recovery_cooldown,
            recovery_max_attempts_24h=self.config.recovery_max_attempts_24h,
            transport_factory=self._transport_factory,
            registry=self.registry,
        )

    # -- event plumbing ----------------------------------------------------

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        """Durably queue one outbound event, then wake the sender."""
        self.store.append_event(kind, payload)
        self.store.trim_events(self.config.max_queued_events)
        self.link.wake()

    def describe_devices(self) -> list[dict[str, Any]]:
        return [worker.describe() for worker in self.workers.values()]

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        log.info(
            "agent %s starting: %d device(s), %d queued event(s), %d task(s), "
            "autodetect %s",
            self.config.agent_id, len(self.workers),
            self.store.unacked_count(), len(self.store.all_tasks()),
            "on" if self.config.autodetect else "off",
        )

        self._tasks = [
            asyncio.create_task(worker.run(), name=f"worker-{name}")
            for name, worker in self.workers.items()
        ]
        self._tasks.append(asyncio.create_task(self.link.run(), name="link"))
        self._tasks.append(
            asyncio.create_task(self.scheduler.run(), name="scheduler")
        )
        if self.config.autodetect:
            self._tasks.append(
                asyncio.create_task(self._autodetect_loop(), name="autodetect")
            )

        try:
            # Copied, because adopting a module appends to self._tasks and a
            # gather over the live list would not notice either way.
            await asyncio.gather(*list(self._tasks))
        except asyncio.CancelledError:
            pass

    # -- autodetect --------------------------------------------------------

    async def _autodetect_loop(self) -> None:
        """Adopt modules that no [[devices]] block is waiting for.

        The first pass runs immediately so a module plugged in before startup
        does not wait out a whole interval.
        """
        while not self._stopped:
            try:
                await self._autodetect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A module mid-enumeration, a port that vanished between glob
                # and open: none of that should take down the loop.
                log.warning("autodetect scan failed: %s", exc)
            await asyncio.sleep(self.config.autodetect_interval)

    async def _autodetect_once(self) -> None:
        # A configured device with no port, imei or iccid means "the one module
        # that answers is mine" (see PortRegistry.sole_device).  Nothing in a
        # survey could be shown not to be that module, so adopting anything
        # would be taking it.
        if any(
            not (d.port or d.imei or d.iccid) for d in self.config.devices
        ):
            return

        reserved = self.config.devices + self._adopted
        seen_identities: set[str] = set()
        for found in await self.registry.survey(reserved):
            identity = self._probe_identity(found)
            if identity is None:
                # Identity is the whole point: a module that will not say who
                # it is cannot be given a name that survives a replug.
                log.debug("autodetect: %s has no IMEI/ICCID, leaving it alone", found.port)
                continue
            if identity in seen_identities:
                log.debug("autodetect: %s is a duplicate probe for %s", found.port, identity)
                continue
            seen_identities.add(identity)
            self._adopt(found)

    def _adopt(self, found: ProbeResult) -> None:
        identity = self._probe_identity(found)
        if identity is None:
            return
        if any(
            identity == self._device_identity(device)
            for device in self.config.devices + self._adopted
        ):
            log.debug("autodetect: %s is already represented by a worker", identity)
            return
        name = self._name_for(found.imei, found.iccid)
        device = DeviceConfig(name=name, imei=found.imei, label=found.model)
        worker = self._build_worker(device)

        # Both lists, so the next survey sees this module as spoken for even
        # before the worker has managed to open its port.
        self._adopted.append(device)
        self.workers[name] = worker
        self._tasks.append(asyncio.create_task(worker.run(), name=f"worker-{name}"))

        log.info("autodetect adopted %s as device %r", found.describe(), name)
        self.emit("log", {
            "level": "info",
            "device": name,
            "message": f"自动识别到新模块 {found.model or '?'}，已作为设备 {name} 接管",
            "event": "device_adopted",
            "imei": found.imei,
            "iccid": found.iccid,
        })

    @staticmethod
    def _probe_identity(found: ProbeResult) -> str | None:
        imei = found.imei.strip().lower()
        if imei:
            return f"imei:{imei}"
        iccid = found.iccid.strip().lower()
        return f"iccid:{iccid}" if iccid else None

    @staticmethod
    def _device_identity(device: DeviceConfig) -> str | None:
        imei = (device.imei or "").strip().lower()
        if imei:
            return f"imei:{imei}"
        iccid = (device.iccid or "").strip().lower()
        return f"iccid:{iccid}" if iccid else None

    def _name_for(self, imei: str, iccid: str = "") -> str:
        """A stable device name for this module, remembered across restarts.

        The name ends up on every event the module produces, so it must not
        drift: the server would see the history split in two.  Hence the kv
        table rather than deriving it fresh each time — the derivation could
        change, or collide differently, and the stored answer cannot.
        """
        identity = imei.strip() or iccid.strip()
        key = (
            f"autodetect:imei:{imei}"
            if imei.strip()
            else f"autodetect:iccid:{iccid}"
        )
        remembered = self.store.get(key)
        if remembered and remembered not in self.workers:
            return remembered

        # Suffix rather than the full IMEI: short enough to type into the web
        # UI, and the last digits are what differs between two modules from
        # the same batch.
        base = (
            f"auto-{identity[-6:]}"
            if len(identity) > 6
            else f"auto-{identity}"
        )
        name = base
        suffix = 2
        while name in self.workers:
            name = f"{base}-{suffix}"
            suffix += 1
        self.store.set(key, name)
        return name

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        log.info("shutting down")

        await self.link.stop()
        await self.scheduler.stop()
        for worker in self.workers.values():
            await worker.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.store.close()

    # -- commands from the server -----------------------------------------

    async def handle_command(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        cmd_id = frame.get("cmd_id")

        handlers = {
            "send_sms": self._cmd_send_sms,
            "sync_tasks": self._cmd_sync_tasks,
            "run_task": self._cmd_run_task,
            "query": self._cmd_query,
            "set_radio": self._cmd_set_radio,
            "set_data": self._cmd_set_data,
            "set_roaming_data": self._cmd_set_roaming_data,
            "scan_operators": self._cmd_scan_operators,
            "select_operator": self._cmd_select_operator,
            "network_diagnostics": self._cmd_network_diagnostics,
            "ussd": self._cmd_ussd,
            "raw_at": self._cmd_raw_at,
        }
        handler = handlers.get(kind or "")
        if handler is None:
            log.warning("unknown command from server: %s", kind)
            self._cmd_result(cmd_id, False, error=f"unknown command {kind!r}")
            return

        try:
            data = await handler(frame)
        except DeviceOffline as exc:
            self._cmd_result(cmd_id, False, error=str(exc))
        except Exception as exc:
            log.exception("command %s failed", kind)
            self._cmd_result(cmd_id, False, error=str(exc))
        else:
            self._cmd_result(cmd_id, True, data=data)

    def _cmd_result(
        self,
        cmd_id: Any,
        ok: bool,
        *,
        data: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if cmd_id is None:
            return  # fire-and-forget command; nothing to report against
        self.emit("cmd_result", {
            "cmd_id": cmd_id, "ok": ok, "data": data, "error": error,
        })

    def _worker(self, frame: dict[str, Any]) -> DeviceWorker:
        name = frame.get("device")
        worker = self.workers.get(name or "")
        if worker is None:
            raise DeviceOffline(f"no such device: {name!r}")
        return worker

    async def _cmd_send_sms(self, frame: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker(frame)
        number = str(frame.get("number", "")).strip()
        body = str(frame.get("body", ""))
        if not number:
            raise ValueError("send_sms needs a number")
        if not body:
            raise ValueError("send_sms needs a body")
        refs = await worker.send_sms(number, body, cmd_id=frame.get("cmd_id"))
        return {"refs": refs}

    async def _cmd_sync_tasks(self, frame: dict[str, Any]) -> dict[str, Any]:
        tasks = frame.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("sync_tasks needs a list of tasks")
        # Full-replace semantics; see docs/protocol.md.
        count = self.store.replace_tasks(tasks)
        log.info("synced %d keep-alive task(s)", count)
        return {"count": count}

    async def _cmd_run_task(self, frame: dict[str, Any]) -> dict[str, Any]:
        task_id = frame.get("task_id")
        if not isinstance(task_id, int) or isinstance(task_id, bool):
            raise ValueError("run_task needs an integer task_id")
        return self.scheduler.run_now(task_id)

    async def _cmd_query(self, frame: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker(frame)
        what = frame.get("what", "status")
        if what in ("status", "info"):
            return await worker.refresh()
        if what == "storage":
            state = await worker.refresh()
            return {
                "storage_used": state["storage_used"],
                "storage_capacity": state["storage_capacity"],
            }
        raise ValueError(f"unknown query {what!r}")

    async def _cmd_raw_at(self, frame: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker(frame)
        command = str(frame.get("command", "")).strip()
        if not command:
            raise ValueError("raw_at needs a command")
        lines = await worker.raw_at(command)
        return {"lines": lines}

    async def _cmd_set_radio(self, frame: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker(frame)
        enabled = frame.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("set_radio needs a boolean enabled value")
        return await worker.set_radio_enabled(enabled)

    async def _cmd_set_data(self, frame: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker(frame)
        enabled = frame.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("set_data needs a boolean enabled value")
        return await worker.set_data_enabled(enabled)

    async def _cmd_set_roaming_data(self, frame: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker(frame)
        allowed = frame.get("allowed")
        if not isinstance(allowed, bool):
            raise ValueError("set_roaming_data needs a boolean allowed value")
        return await worker.set_roaming_data_allowed(allowed)

    async def _cmd_scan_operators(self, frame: dict[str, Any]) -> dict[str, Any]:
        return await self._worker(frame).scan_operators()

    async def _cmd_select_operator(self, frame: dict[str, Any]) -> dict[str, Any]:
        numeric = frame.get("numeric")
        if numeric is not None:
            if (
                not isinstance(numeric, str)
                or not numeric.isascii()
                or not numeric.isdigit()
                or len(numeric) not in (5, 6)
            ):
                raise ValueError("select_operator needs a 5 or 6 digit numeric operator")
        return await self._worker(frame).select_operator(numeric)

    async def _cmd_network_diagnostics(self, frame: dict[str, Any]) -> dict[str, Any]:
        return await self._worker(frame).network_diagnostics()

    async def _cmd_ussd(self, frame: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker(frame)
        code = str(frame.get("code", "")).strip()
        if not code:
            raise ValueError("ussd needs a code")
        return await worker.ussd(code)
