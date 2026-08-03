"""The agent as a whole: two modules, a durable queue, commands from above."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from air780e_agent.app import AgentApp
from air780e_agent.at import PipeTransport
from air780e_agent.config import AgentConfig, DeviceConfig
from air780e_agent.discovery import PortRegistry, ProbeResult
from air780e_agent.mock import MockAir780E
from air780e_agent.store import LocalStore

TWO_DEVICES = b"""
[agent]
id = "test-agent"
status_interval = 0.05

[[devices]]
name = "a"
port = "/dev/fake-a"

[[devices]]
name = "b"
port = "/dev/fake-b"
"""


@dataclass
class AgentRig:
    app: AgentApp
    mocks: dict[str, MockAir780E]
    transports: dict[str, PipeTransport] = field(default_factory=dict)
    runner: asyncio.Task | None = None

    async def wait_online(self, timeout: float = 3.0) -> None:
        async with asyncio.timeout(timeout):
            while not all(w.online for w in self.app.workers.values()):
                await asyncio.sleep(0.01)

    async def wait_for_events(self, kind: str, count: int, timeout: float = 3.0):
        async with asyncio.timeout(timeout):
            while True:
                found = [
                    e for e in self.app.store.unacked_events(limit=1000)
                    if e.kind == kind
                ]
                if len(found) >= count:
                    return found
                await asyncio.sleep(0.01)

    def events(self, kind: str):
        return [e for e in self.app.store.unacked_events(limit=1000) if e.kind == kind]


@pytest.fixture
async def agent(tmp_path):
    config = AgentConfig.parse(TWO_DEVICES)
    config.db_path = tmp_path / "agent.db"

    mocks: dict[str, MockAir780E] = {}
    transports: dict[str, PipeTransport] = {}
    for index, device in enumerate(config.devices):
        agent_side, modem_side = PipeTransport.create_pair()
        mock = MockAir780E(
            transport=modem_side,
            iccid=f"8986062218001234567{index}",
            imei=f"86756704882549{index}",
        )
        await mock.start()
        mocks[device.name] = mock
        transports[device.name] = agent_side

    def factory(device: DeviceConfig) -> PipeTransport:
        return transports[device.name]

    app = AgentApp(config, transport_factory=factory)
    rig = AgentRig(app=app, mocks=mocks, transports=transports)
    rig.runner = asyncio.create_task(app.run())
    try:
        yield rig
    finally:
        await app.stop()
        if rig.runner:
            rig.runner.cancel()
            await asyncio.gather(rig.runner, return_exceptions=True)
        for mock in mocks.values():
            await mock.stop()


# --------------------------------------------------------------------------
# startup
# --------------------------------------------------------------------------


async def test_both_devices_come_up(agent):
    await agent.wait_online()
    described = {d["name"]: d for d in agent.app.describe_devices()}
    assert described["a"]["online"] is True
    assert described["b"]["online"] is True
    # Each module must be identified separately — this is what SimAdmin's
    # single-modem model could not express.
    assert described["a"]["iccid"] != described["b"]["iccid"]


async def test_an_unplugged_module_goes_offline_promptly(agent):
    """Pulling the USB cable must be noticed now, not at the next status poll.

    Found on real hardware: the read error surfaced inside the event loop's
    reader callback, where nothing was listening, and the status commands
    swallow AT errors — so the worker reported a healthy module for as long as
    it was left running, and never went back to look for it.
    """
    await agent.wait_online()
    assert agent.app.workers["a"].online is True

    agent.transports["a"].disconnect()

    async with asyncio.timeout(2.0):
        while agent.app.workers["a"].online:
            await asyncio.sleep(0.01)

    assert agent.app.workers["b"].online is True, "one module must not take out the other"
    described = {d["name"]: d for d in agent.app.describe_devices()}
    assert described["a"]["online"] is False


async def test_losing_the_port_frees_it_for_rediscovery(agent):
    """The claim has to be given back, or the module cannot be picked up
    again when it comes back under a different ttyACM number."""
    await agent.wait_online()
    worker = agent.app.workers["a"]
    worker._registry = agent.app.registry
    worker.config.port = ""  # pretend it was discovered rather than pinned
    worker._port = "/dev/ttyACM0"
    agent.app.registry._claimed["/dev/ttyACM0"] = "a"

    agent.transports["a"].disconnect()

    async with asyncio.timeout(2.0):
        while agent.app.registry.claimed_by("/dev/ttyACM0") is not None:
            await asyncio.sleep(0.01)


async def test_workers_come_up_on_discovered_ports(tmp_path, monkeypatch):
    """No ports in the config: each worker has to find its own module, and
    the one it reports must be the one it actually opened."""
    config = AgentConfig.parse(b"""
[agent]
id = "discovering-agent"
status_interval = 0.05

[[devices]]
name = "a"
imei = "867567048825490"

[[devices]]
name = "b"
imei = "867567048825491"
""")
    config.db_path = tmp_path / "agent.db"

    mocks: dict[str, MockAir780E] = {}
    transports: dict[str, PipeTransport] = {}
    # Deliberately crossed: the module with device a's IMEI sits on the port
    # that would have been b's under any positional scheme.
    for port, index in (("/dev/ttyACM0", 1), ("/dev/ttyACM1", 0)):
        agent_side, modem_side = PipeTransport.create_pair()
        mock = MockAir780E(
            transport=modem_side,
            iccid=f"8986062218001234567{index}",
            imei=f"86756704882549{index}",
        )
        await mock.start()
        mocks[port] = mock
        transports[port] = agent_side

    async def prober(port: str, *, timeout: float):
        mock = mocks.get(port)
        return None if mock is None else ProbeResult(
            port=port, model=mock.model, imei=mock.imei, iccid=mock.iccid
        )

    # Never look at the real /dev — the suite must pass on a machine with no
    # modules plugged in.
    monkeypatch.setattr(
        "air780e_agent.discovery.globmodule.glob",
        lambda pattern: ["/dev/ttyACM0", "/dev/ttyACM1"],
    )

    app = AgentApp(
        config,
        transport_factory=lambda device: transports[device.port],
        registry=PortRegistry(prober=prober),
    )
    runner = asyncio.create_task(app.run())
    try:
        async with asyncio.timeout(3.0):
            while not all(w.online for w in app.workers.values()):
                await asyncio.sleep(0.01)

        described = {d["name"]: d for d in app.describe_devices()}
        # a's module (imei ...90) was put on ttyACM1 and b's on ttyACM0, so
        # anything that went by position would have these the other way round.
        assert described["a"]["port"] == "/dev/ttyACM1"
        assert described["b"]["port"] == "/dev/ttyACM0"
        assert described["a"]["imei"] == "867567048825490"
        assert described["b"]["imei"] == "867567048825491"
    finally:
        await app.stop()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        for mock in mocks.values():
            await mock.stop()


async def test_startup_emits_status_for_each_device(agent):
    await agent.wait_online()
    events = await agent.wait_for_events("status", 2)
    assert {e.payload["device"] for e in events} >= {"a", "b"}


# --------------------------------------------------------------------------
# receiving
# --------------------------------------------------------------------------


async def test_incoming_message_is_stored_and_queued(agent):
    await agent.wait_online()
    agent.mocks["a"].deliver("10086", "验证码 123456")

    events = await agent.wait_for_events("sms_in", 1)
    payload = events[0].payload
    assert payload["device"] == "a"
    assert payload["peer"] == "10086"
    assert payload["body"] == "验证码 123456"
    assert payload["iccid"] == agent.mocks["a"].iccid

    rows = agent.app.store.recent_messages()
    assert rows[0]["body"] == "验证码 123456"
    assert rows[0]["direction"] == "in"


async def test_messages_from_both_devices_are_distinguished(agent):
    await agent.wait_online()
    agent.mocks["a"].deliver("10086", "from a")
    agent.mocks["b"].deliver("10010", "from b")

    events = await agent.wait_for_events("sms_in", 2)
    by_device = {e.payload["device"]: e.payload for e in events}
    assert by_device["a"]["body"] == "from a"
    assert by_device["b"]["body"] == "from b"
    assert by_device["a"]["iccid"] != by_device["b"]["iccid"]


async def test_long_message_is_queued_once(agent):
    await agent.wait_online()
    text = "y" * 400
    agent.mocks["a"].deliver("10086", text)

    events = await agent.wait_for_events("sms_in", 1)
    await asyncio.sleep(0.1)
    sms_events = agent.events("sms_in")
    assert len(sms_events) == 1, "segments must be merged before queueing"
    assert sms_events[0].payload["body"] == text
    assert sms_events[0].payload["segments"] == 3


async def test_queued_events_survive_a_restart(agent, tmp_path):
    await agent.wait_online()
    agent.mocks["a"].deliver("10086", "must survive")
    await agent.wait_for_events("sms_in", 1)

    # Nothing has acked these, so a fresh store on the same file still has them.
    path = agent.app.store.path
    reopened = LocalStore(path)
    try:
        kinds = [e.kind for e in reopened.unacked_events(limit=1000)]
        assert "sms_in" in kinds
    finally:
        reopened.close()


async def test_message_body_is_not_written_to_logs(agent, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="air780e_agent.worker")
    await agent.wait_online()
    secret = "verification code 999888"
    agent.mocks["a"].deliver("10086", secret)
    await agent.wait_for_events("sms_in", 1)

    assert secret not in caplog.text, "message bodies must stay out of the logs"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


async def test_send_sms_command(agent):
    await agent.wait_online()
    await agent.app.handle_command({
        "type": "send_sms", "cmd_id": "c-1", "device": "a",
        "number": "10086", "body": "CXHF",
    })

    assert len(agent.mocks["a"].sent) == 1
    assert agent.mocks["a"].sent[0].text == "CXHF"
    assert agent.mocks["b"].sent == [], "must go out the addressed device only"

    results = agent.events("cmd_result")
    assert results[-1].payload["ok"] is True
    out = agent.events("sms_out")[-1].payload
    assert out["status"] == "sent"
    assert out["cmd_id"] == "c-1"


async def test_send_sms_failure_is_reported(agent):
    await agent.wait_online()
    agent.mocks["a"].fail_next_send = True
    await agent.app.handle_command({
        "type": "send_sms", "cmd_id": "c-2", "device": "a",
        "number": "10086", "body": "will fail",
    })

    result = agent.events("cmd_result")[-1].payload
    assert result["ok"] is False
    assert "CMS ERROR" in result["error"]

    out = agent.events("sms_out")[-1].payload
    assert out["status"] == "failed"


async def test_send_to_unknown_device_is_rejected(agent):
    await agent.wait_online()
    await agent.app.handle_command({
        "type": "send_sms", "cmd_id": "c-3", "device": "nope",
        "number": "10086", "body": "x",
    })
    result = agent.events("cmd_result")[-1].payload
    assert result["ok"] is False
    assert "no such device" in result["error"]


async def test_send_sms_validates_arguments(agent):
    await agent.wait_online()
    await agent.app.handle_command({
        "type": "send_sms", "cmd_id": "c-4", "device": "a", "number": "", "body": "x",
    })
    assert agent.events("cmd_result")[-1].payload["ok"] is False


async def test_sync_tasks_is_persisted(agent):
    await agent.wait_online()
    await agent.app.handle_command({
        "type": "sync_tasks", "cmd_id": "c-5",
        "tasks": [{
            "id": 1, "device": "a", "name": "移动保号", "enabled": True,
            "action": "send_sms", "target_number": "10086", "content": "1",
            "schedule_type": "interval", "schedule_expr": "25",
            "jitter_seconds": 1800, "random_suffix": True,
            "retry_max": 3, "notify_on_result": True,
        }],
    })

    tasks = agent.app.store.all_tasks()
    assert len(tasks) == 1
    assert tasks[0]["name"] == "移动保号"
    assert agent.events("cmd_result")[-1].payload["ok"] is True


async def test_the_connect_time_task_push_needs_no_cmd_id(agent):
    """The server pushes the full list on connect without asking for a receipt
    (it cannot wait for one inside its own receive loop), so a frame with no
    cmd_id must still apply — and must not answer."""
    await agent.wait_online()
    await agent.app.handle_command({
        "type": "sync_tasks",
        "tasks": [{
            "id": 4, "device": "a", "name": "连接时下发", "enabled": True,
            "action": "send_sms", "target_number": "10086", "content": "1",
            "schedule_type": "interval", "schedule_expr": "25",
            "jitter_seconds": 1800, "random_suffix": True,
            "retry_max": 3, "notify_on_result": True,
        }],
    })

    assert [t["name"] for t in agent.app.store.all_tasks()] == ["连接时下发"]
    assert agent.events("cmd_result") == [], "nothing to report against"

    # An empty list is how the server clears tasks deleted while we were away.
    await agent.app.handle_command({"type": "sync_tasks", "tasks": []})
    assert agent.app.store.all_tasks() == []


async def test_query_returns_device_state(agent):
    await agent.wait_online()
    await agent.app.handle_command({
        "type": "query", "cmd_id": "c-6", "device": "b", "what": "status",
    })
    result = agent.events("cmd_result")[-1].payload
    assert result["ok"] is True
    assert result["data"]["name"] == "b"
    assert result["data"]["online"] is True


async def test_raw_at_command(agent):
    await agent.wait_online()
    await agent.app.handle_command({
        "type": "raw_at", "cmd_id": "c-7", "device": "a", "command": "AT+CSQ",
    })
    result = agent.events("cmd_result")[-1].payload
    assert result["ok"] is True
    assert any("+CSQ:" in line for line in result["data"]["lines"])


async def test_unknown_command_is_reported_not_ignored(agent):
    await agent.wait_online()
    await agent.app.handle_command({"type": "nonsense", "cmd_id": "c-8"})
    result = agent.events("cmd_result")[-1].payload
    assert result["ok"] is False
    assert "unknown command" in result["error"]


# --------------------------------------------------------------------------
# status throttling
# --------------------------------------------------------------------------


async def test_unchanged_status_is_not_re_sent(agent):
    await agent.wait_online()
    await agent.wait_for_events("status", 2)
    before = len(agent.events("status"))

    # status_interval is 50ms in this rig, so several samples happen here.
    await asyncio.sleep(0.3)

    after = len(agent.events("status"))
    assert after == before, "identical samples must not flood the queue"


async def test_signal_change_is_reported(agent):
    await agent.wait_online()
    await agent.wait_for_events("status", 2)
    before = len(agent.events("status"))

    agent.mocks["a"].rssi = 5  # a big drop, well past the noise floor
    await asyncio.sleep(0.3)

    assert len(agent.events("status")) > before
