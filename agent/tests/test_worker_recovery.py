"""Device self-healing policy: escalation, cooldown and durable limits."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from air780e_agent.at import ATResponse, ATTimeout
from air780e_agent.config import DeviceConfig
from air780e_agent.modem import Signal
from air780e_agent.store import LocalStore
from air780e_agent.worker import RECOVERY_WINDOW, DeviceRecoveryReconnect, DeviceWorker


class Clock:
    def __init__(self, value: float = 1_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeClient:
    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.calls: list[str] = []

    async def execute(self, command: str, *, timeout: float | None = None) -> ATResponse:
        self.calls.append(command)
        if not self.healthy:
            raise ATTimeout("silent modem", command=command)
        return ATResponse(command)


class FakeModem:
    def __init__(
        self,
        *,
        registered: bool = False,
        radio_enabled: bool = True,
        operator_recovers: bool = False,
        radio_cycle_recovers: bool = False,
        reset_recovers: bool = False,
        operator_selection_mode: int | None = 0,
    ) -> None:
        self.registered = registered
        self.radio_enabled = radio_enabled
        self.operator_recovers = operator_recovers
        self.radio_cycle_recovers = radio_cycle_recovers
        self.reset_recovers = reset_recovers
        self.operator_selection_mode = operator_selection_mode
        self.actions: list[str] = []
        self.info = SimpleNamespace(
            registered=registered,
            radio_enabled=radio_enabled,
            eps_registered=registered,
            cs_registered=False,
            ims_registered=False,
        )

    async def read_radio_enabled(self) -> bool:
        return self.radio_enabled

    async def read_registration(self) -> bool:
        self.info.registered = self.registered
        self.info.eps_registered = self.registered
        return self.registered

    async def read_ims_registration(self) -> bool:
        return self.info.ims_registered

    async def read_signal(self) -> Signal:
        return Signal(rssi=20)

    async def read_voltage(self) -> int:
        return 3968

    async def storage_usage(self) -> tuple[int, int]:
        return 0, 10

    async def drain_inbox(self) -> list[object]:
        return []

    async def reselect_operator(self) -> bool:
        self.actions.append("operator_reselect")
        self.registered = self.operator_recovers
        self.info.registered = self.registered
        self.info.eps_registered = self.registered
        return self.registered

    async def cycle_radio(self) -> bool:
        self.actions.append("radio_cycle")
        self.registered = self.radio_cycle_recovers
        self.info.registered = self.registered
        self.info.eps_registered = self.registered
        return self.registered

    async def reset(self) -> None:
        self.actions.append("module_reset")
        self.registered = self.reset_recovers


@pytest.fixture
def store(tmp_path):
    value = LocalStore(tmp_path / "agent.db")
    yield value
    value.close()


def build_worker(
    store: LocalStore,
    clock: Clock,
    modem: FakeModem,
    *,
    client: FakeClient | None = None,
    events: list[tuple[str, dict]] | None = None,
    **policy,
) -> tuple[DeviceWorker, list[tuple[str, dict]]]:
    captured = events if events is not None else []
    worker = DeviceWorker(
        DeviceConfig(name="a", port="/dev/fake-a"),
        store,
        lambda kind, payload: captured.append((kind, payload)),
        clock=clock,
        **policy,
    )
    worker._client = client or FakeClient()
    worker._modem = modem
    worker.state.online = True
    worker.state.radio_enabled = modem.radio_enabled
    worker.state.registered = modem.registered
    return worker, captured


def recovery_logs(events: list[tuple[str, dict]]) -> list[dict]:
    return [payload for kind, payload in events if kind == "log" and payload.get("event")]


async def test_registration_recovery_waits_then_escalates_after_cooldown(store):
    clock = Clock()
    modem = FakeModem(radio_cycle_recovers=True)
    worker, events = build_worker(
        store,
        clock,
        modem,
        registration_recovery_delay=10.0,
        recovery_cooldown=20.0,
    )

    await worker._sample_status()
    clock.advance(9)
    await worker._sample_status()
    assert modem.actions == []

    clock.advance(1)
    await worker._sample_status()
    assert modem.actions == ["operator_reselect"]

    clock.advance(19)
    await worker._sample_status()
    assert modem.actions == ["operator_reselect"]

    clock.advance(1)
    await worker._sample_status()
    assert modem.actions == ["operator_reselect", "radio_cycle"]
    assert worker.state.registered is True
    assert [row["outcome"] for row in recovery_logs(events)] == [
        "started",
        "failed",
        "started",
        "succeeded",
    ]


async def test_module_reset_reconnect_is_completed_after_worker_restart(store):
    clock = Clock()
    modem = FakeModem(reset_recovers=True)
    worker, events = build_worker(
        store,
        clock,
        modem,
        registration_recovery_delay=0.0,
        recovery_cooldown=0.0,
    )

    await worker._sample_status()
    await worker._sample_status()
    with pytest.raises(DeviceRecoveryReconnect, match="module reset requested"):
        await worker._sample_status()
    assert modem.actions == ["operator_reselect", "radio_cycle", "module_reset"]

    # A process restart must not forget that the reset was in flight.  The
    # first successful initialization settles the durable recovery event.
    restarted, _ = build_worker(store, clock, modem, events=events)
    restarted.state.registered = True
    restarted._settle_recovery_after_connect()

    logs = recovery_logs(events)
    assert logs[-1]["action"] == "module_reset"
    assert logs[-1]["outcome"] == "succeeded"
    assert restarted._recovery_inflight is None


async def test_registration_recovery_limit_is_rolling_and_reported_once(store):
    clock = Clock()
    modem = FakeModem()
    worker, events = build_worker(
        store,
        clock,
        modem,
        registration_recovery_delay=0.0,
        recovery_cooldown=0.0,
        recovery_max_attempts_24h=1,
    )

    await worker._sample_status()
    restarted, _ = build_worker(
        store,
        clock,
        modem,
        events=events,
        registration_recovery_delay=0.0,
        recovery_cooldown=0.0,
        recovery_max_attempts_24h=1,
    )
    await restarted._sample_status()
    await restarted._sample_status()
    exhausted = [row for row in recovery_logs(events) if row["outcome"] == "exhausted"]
    assert len(exhausted) == 1
    assert modem.actions == ["operator_reselect"]

    clock.advance(RECOVERY_WINDOW)
    await restarted._sample_status()
    assert modem.actions == ["operator_reselect", "radio_cycle"]


async def test_deliberate_flight_mode_never_starts_recovery(store):
    clock = Clock()
    modem = FakeModem(radio_enabled=False)
    worker, events = build_worker(
        store,
        clock,
        modem,
        registration_recovery_delay=0.0,
        recovery_cooldown=0.0,
    )

    await worker._sample_status()

    assert modem.actions == []
    assert recovery_logs(events) == []


async def test_manual_operator_selection_is_not_undone_by_recovery(store):
    clock = Clock()
    modem = FakeModem(operator_selection_mode=1)
    worker, events = build_worker(
        store,
        clock,
        modem,
        registration_recovery_delay=0.0,
        recovery_cooldown=0.0,
    )

    await worker._sample_status()

    assert modem.actions == []
    assert recovery_logs(events) == []


async def test_repeated_health_timeouts_force_a_serial_reconnect(store):
    clock = Clock()
    modem = FakeModem(registered=True)
    client = FakeClient(healthy=False)
    worker, events = build_worker(
        store,
        clock,
        modem,
        client=client,
        health_check_timeout=0.1,
        health_failure_threshold=2,
    )

    await worker._sample_status()
    with pytest.raises(DeviceRecoveryReconnect, match="2 consecutive"):
        await worker._sample_status()

    assert recovery_logs(events)[-1]["action"] == "serial_reconnect"
    assert recovery_logs(events)[-1]["outcome"] == "started"

    client.healthy = True
    restarted, _ = build_worker(store, clock, modem, client=client, events=events)
    restarted.state.registered = True
    restarted._settle_recovery_after_connect()
    assert recovery_logs(events)[-1]["outcome"] == "succeeded"
