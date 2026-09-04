"""One worker per module.

Owns the serial port, keeps the modem initialized, and turns everything the
modem does into events for the outbound queue.  A worker never gives up: a
module that is unplugged, reset by a brownout or reflashed comes back on its
own once the port reappears.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from .at import ATClient, ATError, SerialTransport, Transport
from .config import DeviceConfig
from .modem import Air780E, IncomingCall, Signal
from .pdu import DecodedSms, StatusReport
from .store import LocalStore

if TYPE_CHECKING:  # imported for typing only — discovery imports config, not us
    from .discovery import PortRegistry

log = logging.getLogger(__name__)

EmitCallback = Callable[[str, dict[str, Any]], None]
ClockCallback = Callable[[], float]

# Below this change in +CSQ a sample is not worth a row in the graph.
RSSI_NOISE_FLOOR = 2
# Same idea for the supply reading, which wanders by single millivolts between
# polls even on a steady supply.  Measured spread on two idle V1011 modules was
# under 30 mV, so a 50 mV step is movement rather than noise.
VOLTAGE_NOISE_MV = 50
# A module fed from USB sits near 4.0 V; the EC618 datasheet floor is 3.3 V.
# Below this the module still runs but a transmit burst can brown it out, which
# looks like random unregistrations rather than a power problem — which is the
# whole reason for reporting the voltage at all.
VOLTAGE_LOW_MV = 3500
# Re-send an unchanged status at least this often, so "still alive" is visible.
STATUS_HEARTBEAT = 900.0
# Registration actions are counted in a rolling window and persisted locally,
# so restarting a flapping Agent cannot bypass the protection.
RECOVERY_WINDOW = 24 * 60 * 60
REGISTRATION_RECOVERY_ACTIONS = (
    "operator_reselect",
    "radio_cycle",
    "module_reset",
)

# These policies live on the Agent because they must still be enforced while
# the Server is unreachable.  New devices intentionally start with packet data
# disabled; an operator has to opt in explicitly from the UI.
DATA_POLICY_KEY_PREFIX = "device-data:"
ROAMING_DATA_POLICY_KEY_PREFIX = "device-roaming-data:"


class TransportFactory(Protocol):
    def __call__(self, config: DeviceConfig) -> Transport: ...


def _default_transport(config: DeviceConfig) -> Transport:
    return SerialTransport(config.port, baudrate=config.baudrate)


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


@dataclass
class DeviceState:
    online: bool = False
    registered: bool = False
    radio_enabled: bool | None = None
    operator: str = ""
    iccid: str = ""
    imei: str = ""
    model: str = ""
    hardware_model: str = ""
    firmware: str = ""
    smsc: str = ""
    eps_registered: bool | None = None
    cs_registered: bool | None = None
    ims_registered: bool | None = None
    # Effective local policy: whether the Agent may allow data operations.
    # This is deliberately separate from ``data_attached`` and ``pdp_active``;
    # an attached modem can still have no user-data session.
    data_enabled: bool = False
    data_attached: bool | None = None
    pdp_active: bool | None = None
    roaming: bool | None = None
    roaming_data_allowed: bool = False
    data_blocked_by_roaming: bool = False
    signal: Signal = field(default_factory=Signal)
    storage_used: int = 0
    storage_capacity: int = 0
    # Supply voltage in millivolts, None while the module has not answered
    # AT+CBC or answered in a shape the parser does not trust.
    voltage_mv: int | None = None
    last_error: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "registered": self.registered,
            "radio_enabled": self.radio_enabled,
            "operator": self.operator,
            "iccid": self.iccid,
            "imei": self.imei,
            "model": self.model,
            "hardware_model": self.hardware_model,
            "firmware": self.firmware,
            "smsc": self.smsc,
            "eps_registered": self.eps_registered,
            "cs_registered": self.cs_registered,
            "ims_registered": self.ims_registered,
            "data_enabled": self.data_enabled,
            "data_attached": self.data_attached,
            "pdp_active": self.pdp_active,
            "roaming": self.roaming,
            "roaming_data_allowed": self.roaming_data_allowed,
            "data_blocked_by_roaming": self.data_blocked_by_roaming,
            "rssi": self.signal.rssi,
            "dbm": self.signal.dbm,
            "bars": self.signal.bars,
            "rsrp": self.signal.rsrp,
            "rsrq": self.signal.rsrq,
            "storage_used": self.storage_used,
            "storage_capacity": self.storage_capacity,
            "voltage_mv": self.voltage_mv,
        }


class DeviceOffline(RuntimeError):
    """Raised when a command arrives for a module that is not currently up."""


class DeviceRecoveryReconnect(RuntimeError):
    """A recovery action intentionally asks the supervisor to reopen the port."""


class DeviceWorker:
    def __init__(
        self,
        config: DeviceConfig,
        store: LocalStore,
        emit: EmitCallback,
        *,
        status_interval: float = 60.0,
        reconnect_max_delay: float = 60.0,
        health_check_timeout: float = 5.0,
        health_failure_threshold: int = 3,
        registration_recovery_delay: float = 300.0,
        recovery_cooldown: float = 300.0,
        recovery_max_attempts_24h: int = 6,
        transport_factory: TransportFactory = _default_transport,
        registry: PortRegistry | None = None,
        clock: ClockCallback = time.time,
    ) -> None:
        self.config = config
        self.store = store
        self.emit = emit
        self.status_interval = status_interval
        self.reconnect_max_delay = reconnect_max_delay
        self.health_check_timeout = max(0.1, health_check_timeout)
        self.health_failure_threshold = max(1, health_failure_threshold)
        self.registration_recovery_delay = max(0.0, registration_recovery_delay)
        self.recovery_cooldown = max(0.0, recovery_cooldown)
        self.recovery_max_attempts_24h = max(0, recovery_max_attempts_24h)
        self._clock = clock
        self._transport_factory = transport_factory
        # Absent for a pinned port; required to find an unpinned module.
        self._registry = registry
        # The port actually in use, which for a discovered module is only
        # known once it has answered.
        self._port = config.port

        self.state = DeviceState()
        self._client: ATClient | None = None
        self._modem: Air780E | None = None
        self._ready = asyncio.Event()
        self._stopped = False
        self._last_status_sent = 0.0
        self._last_status_payload: dict[str, Any] | None = None
        self._health_failures = 0
        self._unregistered_since: float | None = None
        self._recovery_stage = 0
        self._recovery_attempt_times: deque[float] = deque()
        self._recovery_sequence = 0
        self._recovery_inflight: tuple[str, int] | None = None
        self._recovery_issue_open = False
        self._last_recovery_action = ""
        self._last_recovery_attempt = 0
        self._recovery_limit_reported = False
        self._load_recovery_state()
        self.state.data_enabled = self._desired_data_enabled()
        self.state.roaming_data_allowed = self._desired_roaming_data_allowed()

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def online(self) -> bool:
        return self.state.online

    @property
    def radio_enabled(self) -> bool | None:
        return self.state.radio_enabled

    @property
    def data_enabled(self) -> bool:
        """Whether the local policy permits operations that may use data."""
        return self.state.data_enabled

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.config.label,
            "port": self._port or self.config.port,
            **self.state.describe(),
        }

    # -- durable recovery state ------------------------------------------

    @property
    def _recovery_store_key(self) -> str:
        return f"device-recovery:{self.name}"

    def _load_recovery_state(self) -> None:
        raw = self.store.get(self._recovery_store_key)
        if not raw:
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("recovery state is not an object")
            now = self._clock()
            attempts = [
                float(value)
                for value in data.get("attempts", [])
                if 0 <= now - float(value) < RECOVERY_WINDOW
            ]
            stage = int(data.get("stage", 0))
            self._recovery_attempt_times = deque(sorted(attempts))
            self._recovery_stage = (
                stage if 0 <= stage < len(REGISTRATION_RECOVERY_ACTIONS) else 0
            )
            self._recovery_sequence = max(0, int(data.get("sequence", 0)))
            self._recovery_issue_open = bool(data.get("issue_open", False))
            self._last_recovery_action = str(data.get("last_action", ""))
            self._last_recovery_attempt = max(0, int(data.get("last_attempt", 0)))
            self._recovery_limit_reported = bool(data.get("limit_reported", False))
            inflight = data.get("inflight")
            if isinstance(inflight, dict):
                action = str(inflight.get("action", ""))
                attempt = int(inflight.get("attempt", 0))
                if action and attempt > 0:
                    self._recovery_inflight = (action, attempt)
        except (TypeError, ValueError, json.JSONDecodeError):
            log.warning("[%s] ignoring invalid persisted recovery state", self.name)

    def _persist_recovery_state(self) -> None:
        inflight = None
        if self._recovery_inflight is not None:
            action, attempt = self._recovery_inflight
            inflight = {"action": action, "attempt": attempt}
        self.store.set(
            self._recovery_store_key,
            json.dumps(
                {
                    "attempts": list(self._recovery_attempt_times),
                    "stage": self._recovery_stage,
                    "sequence": self._recovery_sequence,
                    "inflight": inflight,
                    "issue_open": self._recovery_issue_open,
                    "last_action": self._last_recovery_action,
                    "last_attempt": self._last_recovery_attempt,
                    "limit_reported": self._recovery_limit_reported,
                },
                separators=(",", ":"),
            ),
        )

    # -- packet-data policy ------------------------------------------------

    @property
    def _data_policy_store_key(self) -> str:
        return f"{DATA_POLICY_KEY_PREFIX}{self.name}"

    @property
    def _roaming_data_policy_store_key(self) -> str:
        return f"{ROAMING_DATA_POLICY_KEY_PREFIX}{self.name}"

    def _desired_data_enabled(self) -> bool:
        return self.store.get(self._data_policy_store_key, "0") == "1"

    def _desired_roaming_data_allowed(self) -> bool:
        return self.store.get(self._roaming_data_policy_store_key, "0") == "1"

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
        # Resolving the port here rather than at startup is what makes a
        # module survive being moved to another socket, or coming back from a
        # USB reset under a different ttyACM number: every reconnect attempt
        # goes looking again.
        if self.config.is_pinned:
            self._port = self.config.port
        elif self._registry is not None:
            self._port = await self._registry.acquire(self.config)
        else:
            raise RuntimeError(
                f"device {self.name!r} has no port and no way to discover one"
            )

        log.info("[%s] opening %s", self.name, self._port)
        transport = self._transport_factory(replace(self.config, port=self._port))
        client = ATClient(transport, name=self.name)
        await client.open()
        self._client = client

        modem = Air780E(
            client,
            on_sms=self._on_sms,
            on_delivery=self._on_delivery,
            on_call=self._on_incoming_call,
            storage=self.config.storage,
            delete_after_read=self.config.delete_after_read,
        )
        info = await modem.initialize()
        self._modem = modem

        self.state.online = True
        self.state.last_error = ""
        self.state.model = info.model
        self.state.hardware_model = info.hardware_model
        self.state.firmware = info.firmware
        self.state.imei = info.imei
        self.state.iccid = info.iccid
        self.state.smsc = info.smsc
        self.state.operator = info.operator
        self.state.registered = info.registered
        self.state.eps_registered = info.eps_registered
        self.state.cs_registered = info.cs_registered
        self.state.ims_registered = info.ims_registered
        self.state.radio_enabled = info.radio_enabled
        self.state.data_enabled = self._desired_data_enabled()
        self.state.data_attached = info.data_attached
        self.state.pdp_active = info.pdp_active
        self.state.roaming = info.roaming
        self.state.roaming_data_allowed = self._desired_roaming_data_allowed()
        self._ready.set()
        self._health_failures = 0

        log.info(
            "[%s] up: %s iccid=%s operator=%s",
            self.name, info.model or "?", info.iccid or "?", info.operator or "?",
        )
        self._settle_recovery_after_connect()
        # Enforce the persisted policy before the first status is advertised.
        # The default is off, so a restart also deactivates any PDP context
        # left active by an older Agent or by a manual AT command.
        await self._enforce_data_policy(modem, force=True)
        if not info.smsc:
            self._log_event("warning", "no SMSC configured; sending will fail")

        # Anything that arrived while we were down is still in the modem's
        # store — collect it, and free the slots.
        recovered = await modem.drain_inbox()
        if recovered:
            log.info("[%s] recovered %d stored message(s)", self.name, len(recovered))

        await self._sample_status(force=True)

    async def _serve(self) -> None:
        """Sample status until the port dies.

        Waiting on the port's own death rather than only polling matters: the
        status commands swallow AT errors (a single timed-out +CSQ is not
        worth dropping the link over), so without this a module that was
        unplugged would leave the worker reporting stale "online" for ever.
        """
        client = self._client
        lost = (
            asyncio.create_task(client.wait_lost(), name=f"lost-{self.name}")
            if client is not None
            else None
        )
        try:
            while not self._stopped:
                if lost is not None and lost.done():
                    raise await lost
                await self._sleep_unless_lost(lost, self.status_interval)
                if lost is not None and lost.done():
                    raise await lost
                await self._sample_status()
        finally:
            if lost is not None and not lost.done():
                lost.cancel()

    async def _sleep_unless_lost(
        self, lost: asyncio.Task | None, seconds: float
    ) -> None:
        if lost is None:
            await asyncio.sleep(seconds)
            return
        await asyncio.wait({lost}, timeout=seconds)

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
        # Give the port back before retrying: after a USB reset the module
        # usually reappears under a different name, and holding the old claim
        # would keep this worker (and any other) from taking the new one.
        if self._registry is not None and not self.config.is_pinned and self._port:
            self._registry.release(self._port)
            self._port = ""

    def _go_offline(self, reason: str) -> None:
        was_online = self.state.online
        self.state.online = False
        self.state.last_error = reason
        # An unplugged module cannot be inspected.  Do not leave the last
        # positive reading on the page and accidentally call it current.
        self.state.data_attached = None
        self.state.pdp_active = None
        self.state.data_enabled = False
        self.state.data_blocked_by_roaming = False
        self._ready.clear()
        if was_online:
            self._emit_status(force=True)

    # -- status ------------------------------------------------------------

    async def _check_health(self) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            await client.execute("AT", timeout=self.health_check_timeout)
        except ATError as exc:
            self._health_failures += 1
            if self._health_failures < self.health_failure_threshold:
                log.warning(
                    "[%s] AT health check failed (%d/%d): %s",
                    self.name,
                    self._health_failures,
                    self.health_failure_threshold,
                    exc,
                )
                return False
            reason = (
                f"AT health check failed {self._health_failures} consecutive times: {exc}"
            )
            self._start_recovery("serial_reconnect", reason)
            raise DeviceRecoveryReconnect(reason) from exc

        if self._health_failures:
            log.info(
                "[%s] AT health check recovered after %d failure(s)",
                self.name,
                self._health_failures,
            )
        self._health_failures = 0
        return True

    async def _maybe_recover_registration(self, modem: Air780E) -> None:
        if self.state.radio_enabled is False:
            self._cancel_registration_recovery("radio was deliberately disabled")
            return
        if modem.operator_selection_mode == 1:
            self._cancel_registration_recovery("manual operator selection is active")
            return
        if self.state.registered:
            self._registration_restored("network registration restored")
            return
        if self.recovery_max_attempts_24h == 0:
            self._cancel_registration_recovery(
                "automatic registration recovery is disabled by configuration"
            )
            return

        now = self._clock()
        if self._unregistered_since is None:
            self._unregistered_since = now
        if now - self._unregistered_since < self.registration_recovery_delay:
            return

        self._prune_recovery_attempts(now)
        if len(self._recovery_attempt_times) >= self.recovery_max_attempts_24h:
            self._report_recovery_limit()
            return
        if (
            self._recovery_attempt_times
            and now - self._recovery_attempt_times[-1] < self.recovery_cooldown
        ):
            return

        action = REGISTRATION_RECOVERY_ACTIONS[self._recovery_stage]
        self._recovery_attempt_times.append(now)
        attempt = self._start_recovery(
            action,
            f"module remained unregistered for {now - self._unregistered_since:.0f}s",
        )

        if action == "operator_reselect":
            recovered = await modem.reselect_operator()
        elif action == "radio_cycle":
            recovered = await modem.cycle_radio()
        else:
            reset_error = ""
            try:
                await modem.reset()
            except ATError as exc:
                # The module may reboot before writing its final OK.  Closing
                # and reopening the port is still the correct next step.
                reset_error = f"; reset acknowledgement failed: {exc}"
            self._recovery_stage = 0
            self._unregistered_since = now
            self._persist_recovery_state()
            raise DeviceRecoveryReconnect(
                f"module reset requested for registration recovery{reset_error}"
            )

        if recovered:
            self.state.registered = True
            self._recovery_stage = 0
            self._unregistered_since = None
            self._recovery_limit_reported = False
            self._finish_recovery(
                action,
                attempt,
                "succeeded",
                "mobile network registration recovered",
            )
            return

        self._recovery_stage = (self._recovery_stage + 1) % len(
            REGISTRATION_RECOVERY_ACTIONS
        )
        self._finish_recovery(
            action,
            attempt,
            "failed",
            "module is still unregistered after the recovery action",
        )

    def _start_recovery(self, action: str, reason: str) -> int:
        self._recovery_sequence += 1
        attempt = self._recovery_sequence
        self._recovery_inflight = (action, attempt)
        self._recovery_issue_open = True
        self._last_recovery_action = action
        self._last_recovery_attempt = attempt
        self._persist_recovery_state()
        self._log_event(
            "warning",
            f"automatic recovery started: {action}",
            event="device_recovery",
            action=action,
            outcome="started",
            reason=reason,
            attempt=attempt,
        )
        return attempt

    def _finish_recovery(
        self,
        action: str,
        attempt: int,
        outcome: str,
        reason: str,
    ) -> None:
        level = "info" if outcome in {"succeeded", "cancelled"} else "warning"
        self._log_event(
            level,
            f"automatic recovery {outcome}: {action}; {reason}",
            event="device_recovery",
            action=action,
            outcome=outcome,
            reason=reason,
            attempt=attempt,
        )
        self._recovery_inflight = None
        if outcome in {"succeeded", "cancelled"}:
            self._recovery_issue_open = False
        self._persist_recovery_state()

    def _settle_recovery_after_connect(self) -> None:
        inflight = self._recovery_inflight
        if inflight is not None:
            action, attempt = inflight
            if action == "serial_reconnect":
                self._finish_recovery(
                    action, attempt, "succeeded", "AT connection reopened successfully"
                )
            elif self.state.radio_enabled is False:
                self._finish_recovery(
                    action, attempt, "cancelled", "radio is deliberately disabled"
                )
            elif self.state.registered:
                self._finish_recovery(
                    action, attempt, "succeeded", "module registered after reconnect"
                )
            else:
                self._finish_recovery(
                    action, attempt, "failed", "module reopened but remains unregistered"
                )

        if self.state.registered:
            self._registration_restored("network registration restored after reconnect")
        elif self.state.radio_enabled is False:
            self._cancel_registration_recovery("radio is deliberately disabled")
        else:
            # A reset gets a fresh attachment grace period instead of
            # immediately starting the next escalation stage.
            self._unregistered_since = self._clock()

    def _registration_restored(self, reason: str) -> None:
        changed = (
            self._unregistered_since is not None
            or self._recovery_stage != 0
            or self._recovery_limit_reported
        )
        self._unregistered_since = None
        self._recovery_stage = 0
        self._recovery_limit_reported = False
        if self._recovery_issue_open:
            self._finish_recovery(
                self._last_recovery_action or "registration_watch",
                self._last_recovery_attempt,
                "succeeded",
                reason,
            )
        elif changed:
            self._persist_recovery_state()

    def _cancel_registration_recovery(self, reason: str) -> None:
        changed = (
            self._unregistered_since is not None
            or self._recovery_stage != 0
            or self._recovery_limit_reported
        )
        self._unregistered_since = None
        self._recovery_stage = 0
        self._recovery_limit_reported = False
        if self._recovery_issue_open:
            self._finish_recovery(
                self._last_recovery_action or "registration_watch",
                self._last_recovery_attempt,
                "cancelled",
                reason,
            )
        elif changed:
            self._persist_recovery_state()

    def _prune_recovery_attempts(self, now: float) -> None:
        changed = False
        while self._recovery_attempt_times:
            age = now - self._recovery_attempt_times[0]
            if 0 <= age < RECOVERY_WINDOW:
                break
            self._recovery_attempt_times.popleft()
            changed = True
        if (
            self._recovery_limit_reported
            and len(self._recovery_attempt_times) < self.recovery_max_attempts_24h
        ):
            self._recovery_limit_reported = False
            changed = True
        if changed:
            self._persist_recovery_state()

    def _report_recovery_limit(self) -> None:
        if self._recovery_limit_reported:
            return
        self._recovery_limit_reported = True
        self._recovery_issue_open = True
        self._last_recovery_action = "registration_recovery"
        attempt = self._last_recovery_attempt
        self._log_event(
            "error",
            "automatic registration recovery reached its 24-hour limit",
            event="device_recovery",
            action="registration_recovery",
            outcome="exhausted",
            reason=(
                f"{len(self._recovery_attempt_times)} actions in the last 24 hours; "
                "waiting for the rolling window"
            ),
            attempt=attempt,
        )
        self._persist_recovery_state()

    async def _sample_status(self, *, force: bool = False) -> None:
        modem = self._modem
        if modem is None:
            return
        if not await self._check_health():
            return
        radio_enabled = await modem.read_radio_enabled()
        if radio_enabled is not None:
            self.state.radio_enabled = radio_enabled
        self.state.registered = (
            False
            if self.state.radio_enabled is False
            else await modem.read_registration()
        )
        if self.state.radio_enabled is False:
            self.state.eps_registered = False
            self.state.cs_registered = False
        else:
            self.state.eps_registered = modem.info.eps_registered
            self.state.cs_registered = modem.info.cs_registered
        self.state.roaming = getattr(modem.info, "roaming", None)
        self.state.ims_registered = await modem.read_ims_registration()
        modem.info.ims_registered = self.state.ims_registered
        await self._enforce_data_policy(modem)
        await self._maybe_recover_registration(modem)
        self.state.signal = await modem.read_signal()
        self.state.voltage_mv = await modem.read_voltage()
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
        # The port belongs in here, not only in `hello`: with discovery it is
        # not known until the module has answered, which can be *after* the
        # link comes up — and it changes on its own whenever the module is
        # moved or renumbered.  Without it the server keeps showing whichever
        # path was true when the agent last said hello.
        payload = {
            "device": self.name,
            "ts": _now(),
            "port": self._port or self.config.port,
            # The threshold travels with the reading so the Server never keeps a
            # second copy of the default: the voltage that counts as low is a
            # property of this module's supply, which only the Agent's config
            # knows.  Sent even when the reading is None, so the Server can say
            # what it was comparing against.
            "low_voltage_mv": self._low_voltage_threshold,
            **self.state.describe(),
        }
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

        for key in (
            "online",
            "registered",
            "radio_enabled",
            "eps_registered",
            "cs_registered",
            "ims_registered",
            "data_enabled",
            "data_attached",
            "pdp_active",
            "roaming",
            "roaming_data_allowed",
            "data_blocked_by_roaming",
            "operator",
            "storage_used",
            "port",
        ):
            if previous.get(key) != payload.get(key):
                return True

        if self._metric_changed(previous, payload, "rssi", RSSI_NOISE_FLOOR):
            return True
        # A supply reading wanders by a few millivolts between polls, so an
        # exact comparison would make every single poll "worth sending" and
        # defeat the whole filter.  Crossing the alert threshold is exempt from
        # the noise floor: that edge is the one sample that must not wait for
        # the heartbeat.
        if self._crossed_voltage_threshold(previous, payload):
            return True
        return self._metric_changed(previous, payload, "voltage_mv", VOLTAGE_NOISE_MV)

    @staticmethod
    def _metric_changed(
        previous: dict[str, Any], payload: dict[str, Any], key: str, floor: int
    ) -> bool:
        """True when *key* moved by at least *floor*, or appeared/disappeared."""
        old, new = previous.get(key), payload.get(key)
        if (old is None) != (new is None):
            return True
        if old is None or new is None:
            return False
        return abs(old - new) >= floor

    @property
    def _low_voltage_threshold(self) -> int:
        """Millivolts below which this module's supply counts as low."""
        return self.config.low_voltage_mv or VOLTAGE_LOW_MV

    def _crossed_voltage_threshold(
        self, previous: dict[str, Any], payload: dict[str, Any]
    ) -> bool:
        old, new = previous.get("voltage_mv"), payload.get("voltage_mv")
        if old is None or new is None:
            return False
        threshold = self._low_voltage_threshold
        return (old < threshold) != (new < threshold)

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
                # The PDU alone cannot say how it was read: the alphabet comes
                # from TP-DCS, and a wrong reading is exactly the failure these
                # two make diagnosable after the fact.
                "dcs": sms.dcs,
                "alphabet": sms.alphabet,
                "binary": sms.is_binary,
                # The frame reached us with octets missing, so `body` above is
                # mojibake — it was decoded under header fields that are really
                # message body.  These three are the only readable thing left,
                # and without them on the wire the salvage would exist only in
                # the agent's log: the Server would store the mojibake, the UI
                # would call it an operator data SMS, and the push engine would
                # suppress it.  A lost verification code has to be visible.
                "truncated": sms.truncated,
                "recovered_text": sms.recovered_text,
                "code": sms.code,
            },
        )
        # Deliberately no message body in logs: verification codes are sensitive.
        if sms.truncated:
            log.warning(
                "[%s] sms from %s arrived damaged; recovered %d char(s), code %s",
                self.name,
                sms.address,
                len(sms.recovered_text),
                "found" if sms.code else "not recoverable",
            )
        else:
            log.info(
                "[%s] sms from %s (%d chars)", self.name, sms.address, len(sms.text)
            )

    def _on_delivery(self, report: StatusReport) -> None:
        """Persist a modem delivery report in the outbound event queue."""
        self.emit(
            "sms_delivery",
            {
                "device": self.name,
                "iccid": self.state.iccid,
                "reference": report.message_reference,
                "peer": report.recipient,
                "status": report.state,
                "status_code": report.status,
                "service_center_ts": (
                    report.service_center_timestamp.isoformat()
                    if report.service_center_timestamp else None
                ),
                "discharge_ts": (
                    report.discharge_time.isoformat() if report.discharge_time else None
                ),
                "ts": _now(),
                "pdu": report.raw,
            },
        )
        log.info(
            "[%s] delivery report mr=%d status=0x%02X (%s)",
            self.name, report.message_reference, report.status, report.state,
        )

    def _on_incoming_call(self, call: IncomingCall) -> None:
        """Record an incoming call.  It is never answered.

        Some plans count a received call as activity, so the record is worth
        keeping on its own; it also shows whether a card can be reached at all,
        which is the other half of the keep-alive question.
        """
        # Emitted as its own frame, not just log text.  An inbound call is the
        # only direct evidence that the card is reachable *from* the network,
        # and a log line cannot be filtered, counted, or notified on — which is
        # exactly what someone watching a keep-alive card needs to do with it.
        self.emit(
            "call_event",
            {
                "device": self.name,
                "iccid": self.state.iccid,
                "direction": "in",
                "peer": call.number,
                "ts": call.ts or _now(),
                "outcome": "missed",
                # An unanswered inbound call reached us by definition: the
                # module could only ring because the network delivered it.
                "reached_network": True,
                "ring_seconds": 0.0,
                "detail": "",
            },
        )
        self._log_event(
            "info",
            f"来电 {call.number or '未知号码'}（未接听，仅记录）",
        )
        log.info("[%s] incoming call from %s", self.name, call.number or "unknown")

    # -- commands ----------------------------------------------------------

    def _require_modem(self) -> Air780E:
        if self._modem is None or not self.state.online:
            raise DeviceOffline(
                f"device {self.name} is offline: {self.state.last_error or 'not connected'}"
            )
        return self._modem

    def _require_radio(self) -> Air780E:
        modem = self._require_modem()
        if self.state.radio_enabled is False:
            raise DeviceOffline(f"device {self.name} radio is disabled")
        return modem

    async def send_sms(
        self, number: str, body: str, *, cmd_id: str | None = None
    ) -> list[int]:
        modem = self._require_radio()
        ts = _now()
        try:
            refs = await modem.send_sms(number, body)
        except ATError as exc:
            error = f"{exc}; modem status: {self._sms_diagnostic_context()}"
            self.store.record_message(
                device=self.name, direction="out", peer=number, body=body,
                ts=ts, iccid=self.state.iccid or None, status="failed",
            )
            self.emit("sms_out", {
                "device": self.name, "iccid": self.state.iccid, "peer": number,
                "body": body, "ts": ts, "status": "failed", "refs": [],
                "cmd_id": cmd_id, "error": error,
            })
            raise ATError(error) from exc

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

    def _sms_diagnostic_context(self) -> str:
        domains = []
        if self.state.eps_registered:
            domains.append("EPS/LTE")
        if self.state.cs_registered:
            domains.append("CS")
        if domains:
            network = "+".join(domains) + " registered"
        elif self.state.registered:
            network = "registered (domain unavailable)"
        else:
            network = "not registered"

        if self.state.ims_registered is True:
            ims = "IMS registered"
        elif self.state.ims_registered is False:
            ims = "IMS not registered"
        else:
            ims = "IMS status unavailable"

        firmware = self.state.firmware or "unknown firmware"
        return f"{network}, {ims}, firmware={firmware}"

    async def ping(self, host: str = "www.baidu.com") -> bool:
        if not self.data_enabled:
            raise DeviceOffline("移动数据已关闭，未发送 ping")
        return await self._require_radio().ping(host)

    async def call_keepalive(self, number: str) -> dict[str, Any]:
        """Place a keep-alive call and report what the network did with it.

        Raises when the call could not be attempted at all, and returns a
        result — including an unsuccessful one — whenever the modem got as far
        as dialing.  ``reached_network`` is what says whether the carrier
        actually booked an attempt; on a roaming SIM with no working CS path a
        call fails the same way an SMS does, so the failure carries the same
        registration context that makes it diagnosable.
        """
        modem = self._require_radio()
        try:
            result = await modem.call_keepalive(number)
        except ATError as exc:
            # Same context the SMS path attaches: on a roaming card the useful
            # question is always "which domain was actually registered".
            error = f"{exc}; modem status: {self._sms_diagnostic_context()}"
            self._log_event("error", f"保号呼叫 {number} 失败: {error}")
            raise ATError(error) from exc

        # Also emitted as a call record.  `task_result` carries the outcome of a
        # scheduled call and this method's return value carries a manual one, but
        # neither accumulates: the question a keep-alive card raises is "when did
        # this last reach the network", and answering it needs a row per attempt.
        self.emit(
            "call_event",
            {
                "device": self.name,
                "iccid": self.state.iccid,
                "direction": "out",
                "peer": number,
                "ts": _now(),
                "outcome": result.outcome,
                "reached_network": result.reached_network,
                "ring_seconds": result.ring_seconds,
                "detail": result.detail,
            },
        )
        if result.reached_network:
            self._log_event("info", f"保号呼叫 {number}: {result.describe()}")
        else:
            self._log_event(
                "warning",
                f"保号呼叫 {number} 未到达网络: {result.describe()}; "
                f"modem status: {self._sms_diagnostic_context()}",
            )
        log.info("[%s] keep-alive call to %s: %s", self.name, number, result.describe())
        return {
            "outcome": result.outcome,
            "reached_network": result.reached_network,
            "ring_seconds": round(result.ring_seconds, 1),
            "detail": result.describe(),
        }

    async def set_radio_enabled(self, enabled: bool) -> dict[str, Any]:
        modem = self._require_modem()
        radio_enabled, registered = await modem.set_radio_enabled(enabled)
        self.state.radio_enabled = radio_enabled
        self.state.registered = registered
        self.state.eps_registered = modem.info.eps_registered
        self.state.cs_registered = modem.info.cs_registered
        self.state.ims_registered = modem.info.ims_registered
        if not radio_enabled:
            self.state.signal = Signal()
            self.state.data_attached = False
            self.state.pdp_active = False
            self.state.data_blocked_by_roaming = False
            self._cancel_registration_recovery("radio was deliberately disabled")
        elif registered:
            self._registration_restored("network registration restored")
        else:
            self._unregistered_since = self._clock()
        self._emit_status(force=True)
        return self.describe()

    async def _refresh_data_state(self, modem: Air780E) -> bool:
        """Read the modem's packet-data states when the driver supports them."""
        reader = getattr(modem, "read_data_status", None)
        if reader is None:
            # Keeps older test doubles and rolling Agent upgrades usable; a
            # real Air780E always has this method.
            return False
        attached, active = await reader()
        self.state.data_attached = attached
        self.state.pdp_active = active
        return True

    async def _enforce_data_policy(self, modem: Air780E, *, force: bool = False) -> None:
        """Apply local data and roaming policy, then retain actual state."""
        self.state.roaming_data_allowed = self._desired_roaming_data_allowed()
        supported = await self._refresh_data_state(modem)
        if not supported:
            return

        desired = self._desired_data_enabled()
        # Only an explicit home-network result (False) is safe when roaming
        # data is not allowed.  Unknown is fail-closed so an unsupported or
        # temporarily unavailable registration query cannot cause a surprise
        # roaming charge.
        blocked = (
            desired
            and self.state.roaming is not False
            and not self.state.roaming_data_allowed
        )
        self.state.data_blocked_by_roaming = blocked
        effective = desired and not blocked
        self.state.data_enabled = effective
        needs_disable = not effective
        # ``data_attached`` is only the packet-service control-plane state.  It
        # is safe to keep it true while data is off; only an active PDP context
        # represents a user-data session that must be torn down.
        data_is_on = self.state.pdp_active is True
        needs_attach = (
            self.state.registered and self.state.data_attached is False
        )
        if needs_disable and (
            data_is_on
            or needs_attach
            or (force and self.state.pdp_active is None)
        ):
            attached, active = await modem.set_data_enabled(False)
            self.state.data_attached = attached
            self.state.pdp_active = active
            message = "移动数据已关闭（PDP 已停用，保留网络附着）"
            self._log_event("info", message)
            log.info("[%s] %s", self.name, message)
        elif (
            desired
            and not blocked
            and self.state.registered
            and self.state.data_attached is False
        ):
            attached, active = await modem.set_data_enabled(True)
            self.state.data_attached = attached
            self.state.pdp_active = active
            self.state.data_enabled = True
            message = "已允许移动数据（不主动激活 PDP）"
            self._log_event("info", message)
            log.info("[%s] %s", self.name, message)

    async def set_data_enabled(self, enabled: bool) -> dict[str, Any]:
        """Persist and apply the packet-data preference for this device."""
        modem = self._require_modem()
        self.store.set(self._data_policy_store_key, "1" if enabled else "0")
        # Refresh the registration code before deciding whether roaming blocks
        # an enable request; a stale status sample must not bypass the guard.
        if self.state.radio_enabled is not False:
            self.state.registered = await modem.read_registration()
            self.state.eps_registered = modem.info.eps_registered
            self.state.cs_registered = modem.info.cs_registered
            self.state.roaming = getattr(modem.info, "roaming", None)
        await self._enforce_data_policy(modem, force=True)
        self._emit_status(force=True)
        return self.describe()

    async def set_roaming_data_allowed(self, allowed: bool) -> dict[str, Any]:
        """Persist whether packet data may remain attached while roaming."""
        modem = self._require_modem()
        self.store.set(
            self._roaming_data_policy_store_key, "1" if allowed else "0"
        )
        if self.state.radio_enabled is not False:
            self.state.registered = await modem.read_registration()
            self.state.eps_registered = modem.info.eps_registered
            self.state.cs_registered = modem.info.cs_registered
            self.state.roaming = getattr(modem.info, "roaming", None)
        await self._enforce_data_policy(modem, force=not allowed)
        self._emit_status(force=True)
        return self.describe()

    async def scan_operators(self) -> dict[str, Any]:
        """Scan visible operators; the modem command may take several minutes."""
        modem = self._require_radio()
        return {"operators": await modem.scan_operators()}

    async def select_operator(self, numeric: str | None) -> dict[str, Any]:
        """Manually select an operator, or return to automatic selection."""
        modem = self._require_radio()
        current = await modem.select_operator(numeric)
        self.state.operator = str(current.get("operator") or "")
        self.state.registered = await modem.read_registration()
        self.state.eps_registered = modem.info.eps_registered
        self.state.cs_registered = modem.info.cs_registered
        self.state.ims_registered = await modem.read_ims_registration()
        modem.info.ims_registered = self.state.ims_registered
        if self.state.registered:
            self._registration_restored("network registration restored")
        elif modem.operator_selection_mode == 1:
            self._cancel_registration_recovery("manual operator selection is active")
        else:
            self._unregistered_since = self._clock()
        self._emit_status(force=True)
        return {"operator": current, "device": self.describe()}

    async def network_diagnostics(self) -> dict[str, Any]:
        """Return raw, read-only cell engineering diagnostics."""
        return {"diagnostics": await self._require_modem().read_network_diagnostics()}

    async def ussd(self, code: str) -> dict[str, Any]:
        """Send a USSD code and return the raw response."""
        client = self._client
        if client is None or not self.state.online:
            raise DeviceOffline(f"device {self.name} is offline")
        # AT+CUSD=1,<code>,15 — action=1 (send), dcs=15 (GSM-7 default alphabet)
        response = await client.execute(f'AT+CUSD=1,"{code}",15', timeout=30.0)
        return {"response": "\n".join(response.lines)}

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

    def _log_event(self, level: str, message: str, **fields: Any) -> None:
        self.emit(
            "log",
            {
                "device": self.name,
                "level": level,
                "message": message,
                "ts": _now(),
                **fields,
            },
        )
