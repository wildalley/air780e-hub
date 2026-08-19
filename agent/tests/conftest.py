"""Shared fixtures: an ATClient wired to a mock Air780E."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from air780e_agent.at import ATClient, PipeTransport
from air780e_agent.mock import MockAir780E
from air780e_agent.modem import Air780E, IncomingCall
from air780e_agent.pdu import DecodedSms, StatusReport


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--fault-cycles",
        action="store",
        type=int,
        default=1,
        help="repeat deterministic reconnect/re-enumeration fault scenarios",
    )


@pytest.fixture
def fault_cycles(pytestconfig) -> int:
    return max(1, pytestconfig.getoption("--fault-cycles"))


@dataclass
class Rig:
    client: ATClient
    mock: MockAir780E
    modem: Air780E | None = None
    received: list[DecodedSms] = field(default_factory=list)
    deliveries: list[StatusReport] = field(default_factory=list)
    calls: list[IncomingCall] = field(default_factory=list)
    _event: asyncio.Event = field(default_factory=asyncio.Event)

    def on_sms(self, sms: DecodedSms) -> None:
        self.received.append(sms)
        self._event.set()

    def on_delivery(self, report: StatusReport) -> None:
        self.deliveries.append(report)
        self._event.set()

    def on_call(self, call: IncomingCall) -> None:
        self.calls.append(call)
        self._event.set()

    async def wait_for_sms(self, count: int = 1, timeout: float = 2.0) -> None:
        """Block until ``count`` messages have surfaced, or fail the test."""
        async with asyncio.timeout(timeout):
            while len(self.received) < count:
                self._event.clear()
                if len(self.received) >= count:
                    return
                await self._event.wait()

    async def wait_for_call(self, count: int = 1, timeout: float = 2.0) -> None:
        """Block until ``count`` incoming calls have been reported."""
        async with asyncio.timeout(timeout):
            while len(self.calls) < count:
                self._event.clear()
                if len(self.calls) >= count:
                    return
                await self._event.wait()


@pytest.fixture
async def rig():
    agent_side, modem_side = PipeTransport.create_pair()
    mock = MockAir780E(transport=modem_side)
    client = ATClient(agent_side, name="test")
    await mock.start()
    await client.open()
    rig = Rig(client=client, mock=mock)
    try:
        yield rig
    finally:
        if rig.modem is not None:
            await rig.modem.close()
        await client.close()
        await mock.stop()


@pytest.fixture
async def modem(rig):
    """A fully initialized :class:`Air780E` on top of the mock."""
    rig.modem = Air780E(
        rig.client,
        on_sms=rig.on_sms,
        on_delivery=rig.on_delivery,
        on_call=rig.on_call,
    )
    await rig.modem.initialize()
    return rig.modem
