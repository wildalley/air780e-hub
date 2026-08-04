"""Offline alerts: a module dropping off the bus becomes a push.

The agent already reports the drop — a serial port that dies turns into a
``status`` frame with ``online: false`` (worker ``_go_offline``), and a whole
agent losing its link marks all its devices offline
(:meth:`Gateway._unregister`).  What was missing is the other half: turning
that transition into a notification.  This is where the two meet.

Two things here are deliberate:

* **A grace period, not an instant page.**  A USB brown-out that re-enumerates
  in a second, or a home-broadband blip that drops the agent's WebSocket for
  two, must not wake anyone at 3am.  A drop is *armed* the moment it is seen and
  only *fires* if the module is still down ``grace`` seconds later; a recovery
  inside the window cancels it in silence.  Waiting is also the only way to tell
  "the host died" from "the link blipped" — until the grace elapses the two are
  indistinguishable.

* **Recovery is announced only if the drop was.**  A blip that never paged is
  not followed by an all-clear either — that would just be two notifications
  where the right number was zero.

State is per module, keyed by ``(agent_id, device)``.  It lives in memory: on a
server restart the worst case is a missed recovery notice, never a duplicate
page (a restart arms no timers).
"""

from __future__ import annotations

import asyncio
import logging

from .db import Database, utcnow
from .notify import Notifier

log = logging.getLogger(__name__)

# Settings key for the runtime on/off switch (Notify page, /api/notify-settings).
SETTING_ENABLED = "offline_alerts_enabled"

Key = tuple[str, str]


class OfflineAlerter:
    """Turns module online/offline transitions into debounced pushes."""

    def __init__(
        self, db: Database, notifier: Notifier, *, grace: float = 120.0
    ) -> None:
        self.db = db
        self.notifier = notifier
        self.grace = grace
        # Modules already paged as offline — kept so their recovery is
        # announced, and so a heartbeat repeating "offline" does not re-page.
        self._alerted: set[Key] = set()
        # Armed-but-not-yet-fired offline timers.
        self._pending: dict[Key, asyncio.Task] = {}
        # Every spawned task (timers and pushes), so shutdown can wait on them.
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.db.get_setting(SETTING_ENABLED, True))

    # -- the one entry point ----------------------------------------------

    def note(self, agent_id: str, device: str, online: bool) -> None:
        """Record a module's current up/down state.

        The gateway calls this on every state it learns — hello, each status
        frame that carries ``online``, and an agent disconnect.  It is
        idempotent per state: only a genuine edge (first offline, or a recovery
        after a page) does anything, so repeats and heartbeats are free.
        """
        key = (agent_id, device)
        if online:
            self._note_online(key)
        else:
            self._note_offline(key)

    def _note_offline(self, key: Key) -> None:
        if not self.enabled:
            return
        if key in self._pending or key in self._alerted:
            return  # already counting down, or already paged
        self._pending[key] = self._spawn(
            self._fire_after_grace(key), label=f"offline {key[0]}/{key[1]}"
        )

    def _note_online(self, key: Key) -> None:
        pending = self._pending.pop(key, None)
        if pending is not None:
            pending.cancel()  # recovered inside the grace window — stay quiet
        if key in self._alerted:
            self._alerted.discard(key)
            self._spawn(
                self._deliver(
                    key, title="模块恢复", tag="恢复",
                    phrase="已恢复在线", time_label="恢复时间",
                ),
                label=f"online {key[0]}/{key[1]}",
            )

    async def _fire_after_grace(self, key: Key) -> None:
        await asyncio.sleep(self.grace)
        # We now own this key: past the sleep, a cancel can no longer reach us.
        self._pending.pop(key, None)
        # Re-read the store rather than trust the timer — the module may have
        # come back in a way that raced the cancel, and the toggle may have
        # been flipped off while we waited.
        if not self.enabled or self.db.device_online(*key):
            return
        self._alerted.add(key)
        await self._deliver(
            key, title="模块掉线", tag="掉线", phrase="已离线", time_label="掉线时间"
        )

    async def _deliver(
        self, key: Key, *, title: str, tag: str, phrase: str, time_label: str
    ) -> None:
        label = self._device_label(*key)
        text = f"【{tag}】{label} {phrase}\n{time_label}:{self.notifier._local(utcnow())}"
        try:
            await self.notifier.notify_text(text, title=title)
        except Exception:
            # notify_text already absorbs per-channel failures; this guards the
            # unexpected so one bad push can never wedge the alerter.
            log.exception("offline alert delivery failed for %s/%s", *key)

    def _device_label(self, agent_id: str, device: str) -> str:
        """A phone-readable name for the module, best handle first.

        The SIM's label survives being moved between modules, so it wins; the
        module's own label ("移动卡") reads better than an ICCID tail until the
        card is named.  The slot is always appended so two modules never render
        the same.
        """
        row = self.db.one(
            "SELECT d.label AS device_label, s.label AS sim_label, "
            "s.phone_number, s.iccid FROM devices d "
            "LEFT JOIN sims s ON s.id = d.sim_id "
            "WHERE d.agent_id = ? AND d.name = ?",
            (agent_id, device),
        ) or {}
        iccid = row.get("iccid") or ""
        name = (
            row.get("sim_label")
            or row.get("phone_number")
            or row.get("device_label")
            or (f"…{iccid[-4:]}" if iccid else "")
        )
        return f"{name}({device})" if name else device

    # -- task plumbing -----------------------------------------------------

    def _spawn(self, coro, *, label: str) -> asyncio.Task:
        task = asyncio.create_task(self._quietly(coro, label), name=f"alert-{label}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    async def _quietly(coro, label: str) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise  # a cancelled offline timer (recovery in the window) is normal
        except Exception:
            log.exception("offline alerter task failed: %s", label)

    async def drain(self) -> None:
        """Wait for armed timers and in-flight pushes (tests, and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self.drain()
