"""Bounded command execution, independent of the WebSocket receive loop."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

CommandHandler = Callable[[dict[str, Any]], Awaitable[None]]
RejectHandler = Callable[[dict[str, Any], str], None]

MAX_PENDING_COMMANDS = 200
MAX_DEVICE_COMMANDS = 16
MAX_COMMAND_WORKERS = 32


@dataclass
class _DeviceQueue:
    # The running command stays at the head and counts toward both limits.
    frames: deque[dict[str, Any]] = field(default_factory=deque)
    running: bool = False
    task: asyncio.Task[None] | None = None


class CommandDispatcher:
    def __init__(
        self,
        execute: CommandHandler,
        reject: RejectHandler,
        *,
        max_pending: int = MAX_PENDING_COMMANDS,
        max_per_device: int = MAX_DEVICE_COMMANDS,
        max_workers: int = MAX_COMMAND_WORKERS,
    ) -> None:
        self.execute = execute
        self.reject = reject
        self.max_pending = max_pending
        self.max_per_device = max_per_device
        self.max_workers = max_workers
        self._queues: dict[str | None, _DeviceQueue] = {}
        self._pending = 0
        self._stopped = False

    def submit(self, frame: dict[str, Any]) -> bool:
        if self._stopped:
            self.reject(frame, "agent is stopping; command was not started")
            return False

        device = frame.get("device")
        # Task configuration and run_task share a FIFO control queue, so a
        # manual run cannot overtake the sync_tasks that defines its task.
        key = device if isinstance(device, str) and device else None
        if frame.get("type") in ("sync_tasks", "run_task"):
            key = None
        queue = self._queues.get(key)
        if (
            self._pending >= self.max_pending
            or (queue is not None and len(queue.frames) >= self.max_per_device)
            or (queue is None and len(self._queues) >= self.max_workers)
        ):
            self.reject(frame, "command queue is full; command was not started")
            return False

        if queue is None:
            queue = _DeviceQueue()
            self._queues[key] = queue
            queue.task = asyncio.create_task(
                self._worker(key, queue), name="link-command"
            )
        queue.frames.append(frame)
        self._pending += 1
        return True

    async def _worker(self, key: str | None, queue: _DeviceQueue) -> None:
        try:
            while queue.frames:
                frame = queue.frames[0]
                queue.running = True
                try:
                    await self.execute(frame)
                except asyncio.CancelledError:
                    self.reject(
                        frame,
                        "agent stopped while command was running; execution result is unknown",
                    )
                    raise
                except Exception:
                    log.exception("command handler failed for %s", frame.get("type"))
                    self.reject(frame, "command handler failed; execution result is unknown")
                finally:
                    queue.running = False
                    queue.frames.popleft()
                    self._pending -= 1
        finally:
            self._queues.pop(key, None)

    def discard_queued(self, reason: str = "server disconnected before command started") -> None:
        # A socket loss must not cancel an AT operation that may already have
        # affected the modem. Its result still goes to the durable event queue.
        for queue in self._queues.values():
            while len(queue.frames) > int(queue.running):
                frame = queue.frames.pop()
                self._pending -= 1
                self.reject(frame, reason)

    async def stop(self) -> None:
        tasks = [queue.task for queue in self._queues.values() if queue.task is not None]
        if not self._stopped:
            self._stopped = True
            self.discard_queued("agent is stopping; command was not started")
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first turn never enters _worker's finally.
        self._queues.clear()
        self._pending = 0
