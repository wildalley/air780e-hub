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
from .config import AgentConfig
from .discovery import PortRegistry
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

        for device in config.devices:
            self.workers[device.name] = DeviceWorker(
                device,
                self.store,
                self.emit,
                status_interval=config.status_interval,
                reconnect_max_delay=config.reconnect_max_delay,
                transport_factory=transport_factory,
                registry=self.registry,
            )

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
            "agent %s starting: %d device(s), %d queued event(s), %d task(s)",
            self.config.agent_id, len(self.workers),
            self.store.unacked_count(), len(self.store.all_tasks()),
        )

        self._tasks = [
            asyncio.create_task(worker.run(), name=f"worker-{name}")
            for name, worker in self.workers.items()
        ]
        self._tasks.append(asyncio.create_task(self.link.run(), name="link"))
        self._tasks.append(
            asyncio.create_task(self.scheduler.run(), name="scheduler")
        )

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

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
