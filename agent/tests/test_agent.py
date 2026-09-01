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

# A real capture: the modem handed this over with octets missing between the
# originating address and the user data, so a spec-conformant decode reads
# message body as TP-DCS and TP-SCTS.  `test_pdu.py` owns the decoding side of
# it; here it is only a frame that must reach the Server with its salvage
# attached.  Kept as a literal rather than imported so a test-module rename
# cannot quietly disarm this one.
DELIVER_TRUNCATED_KRAKEN = (
    "0791448720003023240BD04B79785D7603B21B642FCBD3E6F4384C4FBFDDA0F19B5C06A5E73AD0EC"
    "46C3D9722E10F1ED3ED1417374585E06D1D1E93968FC269741F7341D0D0ABBF36F7779077AD7E5A0"
    "721BCE7EE7CBE539E89E66B341EEB2BD2C0785E76B90F9"
)

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

    async def wait_for_event_where(
        self, kind: str, predicate, timeout: float = 3.0
    ):
        """Wait for an event *matching* a condition, not merely for the Nth one.

        ``wait_for_events`` counts from the start of the run, so a test that
        mutates a mock and then asks for "one status event" is answered by a
        frame emitted before the mutation.  This waits for the frame that
        actually shows the change.
        """
        async with asyncio.timeout(timeout):
            while True:
                for event in self.events(kind):
                    if predicate(event.payload):
                        return event.payload
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


async def test_status_carries_the_port_it_actually_opened(agent):
    """`hello` can go out before discovery has resolved a port — the link and
    the workers come up in parallel — so the status frame has to carry it, or
    the server keeps displaying a stale path for ever."""
    await agent.wait_online()
    events = await agent.wait_for_events("status", 1)

    ports = {e.payload["device"]: e.payload.get("port") for e in events}
    assert ports.get("a") == "/dev/fake-a"
    assert all(p for p in ports.values()), "a blank port would erase the server's"


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


async def test_a_silent_but_present_module_is_reopened(agent):
    """A wedged firmware leaves the tty present, so port-loss alone is not enough."""
    await agent.wait_online()
    worker = agent.app.workers["a"]
    worker.health_check_timeout = 0.1
    worker.health_failure_threshold = 2
    agent.mocks["a"].silent.add("AT")

    async with asyncio.timeout(2.0):
        while worker.online:
            await asyncio.sleep(0.01)

    started = [
        event.payload
        for event in agent.events("log")
        if event.payload.get("event") == "device_recovery"
    ]
    assert started[-1]["action"] == "serial_reconnect"
    assert started[-1]["outcome"] == "started"
    assert agent.app.workers["b"].online is True

    agent.mocks["a"].silent.clear()
    await agent.wait_online()
    completed = [
        event.payload
        for event in agent.events("log")
        if event.payload.get("event") == "device_recovery"
    ]
    assert completed[-1]["outcome"] == "succeeded"


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


async def test_worker_reclaims_the_same_module_after_usb_reenumeration(
    tmp_path, monkeypatch, fault_cycles
):
    """A running worker follows its IMEI when ttyACM numbering changes."""
    imei = "867567048825490"
    iccid = "89860622180012345670"
    config = AgentConfig.parse(f"""
[agent]
id = "reenumeration-agent"
status_interval = 0.05
reconnect_max_delay = 0.05

[[devices]]
name = "a"
imei = "{imei}"
iccid = "{iccid}"
""".encode())
    config.db_path = tmp_path / "agent.db"

    visible_ports = ["/dev/ttyACM0"]
    mocks: dict[str, MockAir780E] = {}
    transports: dict[str, PipeTransport] = {}

    async def add_module(port: str) -> None:
        agent_side, modem_side = PipeTransport.create_pair()
        mock = MockAir780E(transport=modem_side, imei=imei, iccid=iccid)
        await mock.start()
        transports[port] = agent_side
        mocks[port] = mock

    await add_module(visible_ports[0])

    async def prober(port: str, *, timeout: float):
        mock = mocks.get(port)
        if mock is None or port not in visible_ports:
            return None
        return ProbeResult(port=port, model=mock.model, imei=mock.imei, iccid=mock.iccid)

    monkeypatch.setattr(
        "air780e_agent.discovery.globmodule.glob",
        lambda _pattern: list(visible_ports),
    )
    # Keep deterministic fault cycles fast while preserving the production
    # backoff implementation itself.
    monkeypatch.setattr("air780e_agent.worker.random.uniform", lambda *_args: 0.01)

    registry = PortRegistry(prober=prober)
    app = AgentApp(
        config,
        transport_factory=lambda device: transports[device.port],
        registry=registry,
    )
    runner = asyncio.create_task(app.run())
    current_port = visible_ports[0]
    try:
        async with asyncio.timeout(3.0):
            while not app.workers["a"].online:
                await asyncio.sleep(0.01)

        for cycle in range(fault_cycles):
            next_port = f"/dev/ttyACM{cycle + 3}"
            await add_module(next_port)
            visible_ports[:] = [next_port]
            transports[current_port].disconnect()

            async with asyncio.timeout(3.0):
                while True:
                    described = app.workers["a"].describe()
                    if app.workers["a"].online and described["port"] == next_port:
                        break
                    await asyncio.sleep(0.01)

            assert described["imei"] == imei
            assert described["iccid"] == iccid
            assert registry.claimed_by(current_port) is None
            assert registry.claimed_by(next_port) == "a"
            await mocks[current_port].stop()
            current_port = next_port
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

    await agent.wait_for_events("sms_in", 1)
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


async def test_a_damaged_message_carries_its_salvage_to_the_server(agent):
    """The salvage has to be on the wire, or it exists only in the agent's log.

    `body` here is mojibake — the modem dropped octets before the agent saw the
    frame, so the decoder read message body as TP-DCS/SCTS.  Without these three
    fields the Server would store that mojibake, the UI would file it as an
    operator data SMS, and the push engine would suppress it: a verification
    code lost with nothing anywhere saying so.
    """
    await agent.wait_online()
    agent.mocks["a"].deliver_pdu(DELIVER_TRUNCATED_KRAKEN)

    events = await agent.wait_for_events("sms_in", 1)
    payload = events[0].payload
    assert payload["truncated"] is True
    assert payload["code"] == "374869"
    assert "verification code is: 374869" in payload["recovered_text"]
    # The spec-conformant decode still travels untouched, and still says
    # "not text" — the salvage is a bypass, not a repair.
    assert payload["binary"] is True
    assert payload["body"] != payload["recovered_text"]


async def test_an_undamaged_message_claims_no_salvage(agent):
    """The fields are always present, and false/empty on a healthy frame.

    A `truncated` that is merely absent would make the Server's default the
    thing under test on every upgrade; asserting it here keeps the damaged
    branch reachable only by actually damaged frames.
    """
    await agent.wait_online()
    agent.mocks["a"].deliver("10086", "验证码 123456")

    events = await agent.wait_for_events("sms_in", 1)
    payload = events[0].payload
    assert payload["truncated"] is False
    assert payload["recovered_text"] == ""
    assert payload["code"] == ""


async def test_a_damaged_message_keeps_its_salvage_out_of_the_logs(agent, caplog):
    """The damaged path logs too, and must not become the body's way out.

    It warns instead of logging at info, which is a second place the no-body
    rule has to hold — a recovered verification code is still a verification
    code.
    """
    import logging

    caplog.set_level(logging.DEBUG, logger="air780e_agent.worker")
    await agent.wait_online()
    agent.mocks["a"].deliver_pdu(DELIVER_TRUNCATED_KRAKEN)
    await agent.wait_for_events("sms_in", 1)

    assert "374869" not in caplog.text
    assert "verification code is" not in caplog.text
    # It does say a message arrived damaged: silence here is how the fault went
    # unnoticed in the first place.
    assert "damaged" in caplog.text


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

    reference = out["refs"][0]
    agent.mocks["a"].report_delivery(reference, "10086")
    deliveries = await agent.wait_for_events("sms_delivery", 1)
    assert deliveries[-1].payload["reference"] == reference
    assert deliveries[-1].payload["status"] == "delivered"


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
    assert "IMS not registered" in result["error"]
    assert agent.mocks["a"].firmware in result["error"]

    out = agent.events("sms_out")[-1].payload
    assert out["status"] == "failed"
    assert out["error"] == result["error"]


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


async def test_run_task_command_starts_the_local_scheduler(agent):
    await agent.wait_online()
    await agent.app.handle_command({
        "type": "sync_tasks",
        "tasks": [{
            "id": 9, "device": "a", "name": "手动测试", "enabled": False,
            "action": "send_sms", "target_number": "10086", "content": "1",
            "schedule_type": "interval", "schedule_expr": "25",
            "jitter_seconds": 0, "random_suffix": False,
            "retry_max": 0, "notify_on_result": True,
        }],
    })

    await agent.app.handle_command({
        "type": "run_task", "cmd_id": "c-run", "task_id": 9,
    })
    result = agent.events("cmd_result")[-1].payload
    assert result["ok"] is True
    assert result["data"] == {"task_id": 9, "status": "started"}

    await agent.app.scheduler.drain()
    assert agent.events("task_result")[-1].payload["task_id"] == 9


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


async def test_set_radio_command_updates_state_and_can_turn_it_back_on(agent):
    await agent.wait_online()

    await agent.app.handle_command({
        "type": "set_radio", "cmd_id": "c-radio-off", "device": "a", "enabled": False,
    })
    result = agent.events("cmd_result")[-1].payload
    assert result["ok"] is True
    assert result["data"]["radio_enabled"] is False
    assert result["data"]["registered"] is False
    assert agent.mocks["a"].radio_enabled is False

    await agent.app.handle_command({
        "type": "set_radio", "cmd_id": "c-radio-on", "device": "a", "enabled": True,
    })
    result = agent.events("cmd_result")[-1].payload
    assert result["ok"] is True
    assert result["data"]["radio_enabled"] is True
    assert result["data"]["registered"] is True
    assert agent.mocks["a"].radio_enabled is True


async def test_set_radio_requires_a_boolean(agent):
    await agent.wait_online()
    await agent.app.handle_command({
        "type": "set_radio", "cmd_id": "c-radio-bad", "device": "a", "enabled": "false",
    })
    result = agent.events("cmd_result")[-1].payload
    assert result["ok"] is False
    assert "boolean" in result["error"]


async def test_operator_scan_selection_and_network_diagnostics_commands(agent):
    await agent.wait_online()

    await agent.app.handle_command({
        "type": "scan_operators", "cmd_id": "c-operators", "device": "a",
    })
    scan = agent.events("cmd_result")[-1].payload
    assert scan["ok"] is True
    assert scan["data"]["operators"][0]["numeric"] == "46000"

    await agent.app.handle_command({
        "type": "select_operator", "cmd_id": "c-select", "device": "a",
        "numeric": "46001",
    })
    selected = agent.events("cmd_result")[-1].payload
    assert selected["ok"] is True
    assert agent.mocks["a"].operator == "CHINA UNICOM"

    await agent.app.handle_command({
        "type": "network_diagnostics", "cmd_id": "c-diagnostics", "device": "a",
    })
    diagnostics = agent.events("cmd_result")[-1].payload
    assert diagnostics["ok"] is True
    sections = diagnostics["data"]["diagnostics"]
    # All five reach the wire, including the two query-only V1011 reads.
    assert sections["cced"]["lines"]
    assert len(sections["cced_neighbors"]["lines"]) == 2
    assert sections["bandind"]["lines"] == ["*BANDIND: 0, 39, 7"]
    assert sections["sysinfo"]["lines"] == ["^SYSINFO: 2,2,1,17,1,7"]
    # V1011 has no AT+EEMGINFO at all, so that one section carries the refusal
    # while the other four still report — the point of keeping them separate.
    assert sections["eemginfo"]["lines"] == []
    assert sections["eemginfo"]["error"]


async def test_select_operator_command_validates_numeric(agent):
    await agent.wait_online()
    await agent.app.handle_command({
        "type": "select_operator", "cmd_id": "c-invalid", "device": "a",
        "numeric": "not-a-network",
    })
    result = agent.events("cmd_result")[-1].payload
    assert result["ok"] is False
    assert "5 or 6 digit" in result["error"]


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


# --------------------------------------------------------------------------
# supply voltage
# --------------------------------------------------------------------------


async def test_status_carries_the_supply_reading_and_its_threshold(agent):
    """The threshold travels with the reading.

    The Server judges the voltage but does not own the number: what counts as
    low is a property of this module's supply, which only the Agent's config
    knows.  A frame carrying the reading alone would force the Server to keep a
    second copy of the default.
    """
    await agent.wait_online()
    agent.mocks["a"].voltage_mv = 3968
    payload = await agent.wait_for_event_where(
        "status", lambda p: p["device"] == "a" and p.get("voltage_mv") is not None
    )

    assert payload["voltage_mv"] == 3968
    assert payload["low_voltage_mv"] > 0


async def test_a_drifting_supply_reading_does_not_flood_the_queue(agent):
    """A few millivolts of wander is not an event.

    Without a noise floor this metric alone would defeat the whole status
    filter: the reading moves by single millivolts between polls, so every
    single sample would count as "changed" and reach the wire.
    """
    await agent.wait_online()
    await agent.wait_for_events("status", 2)
    before = len(agent.events("status"))

    for millivolts in (3970, 3966, 3971, 3969):
        agent.mocks["a"].voltage_mv = millivolts
        await asyncio.sleep(0.08)

    assert len(agent.events("status")) == before


async def test_crossing_the_low_voltage_threshold_is_reported_at_once(agent):
    """The threshold edge is exempt from the noise floor.

    A supply sagging past the alert line one millivolt at a time is exactly the
    case the noise floor would swallow, and it is the one sample that must not
    wait up to fifteen minutes for the heartbeat.
    """
    await agent.wait_online()
    worker = agent.app.workers["a"]
    threshold = worker._low_voltage_threshold

    agent.mocks["a"].voltage_mv = threshold + 1
    await agent.wait_for_events("status", 1)
    before = len(agent.events("status"))

    agent.mocks["a"].voltage_mv = threshold - 1
    await asyncio.sleep(0.3)

    payload = agent.events("status")[-1].payload
    assert len(agent.events("status")) > before
    assert payload["voltage_mv"] == threshold - 1


async def test_a_firmware_without_cbc_reports_no_voltage(agent):
    """A module that refuses AT+CBC stays online and unjudged.

    None means "no reading", which must not read as 0 mV — that would look like
    a dead supply on a perfectly healthy module.
    """
    await agent.wait_online()
    agent.mocks["a"].unsupported.add("AT+CBC")
    payload = await agent.wait_for_event_where(
        "status", lambda p: p["device"] == "a" and p.get("voltage_mv") is None
    )

    assert payload["voltage_mv"] is None
    assert agent.app.workers["a"].online is True


# --------------------------------------------------------------------------
# autodetect: a module nobody configured
# --------------------------------------------------------------------------


ONE_PINNED_DEVICE = b"""
[agent]
id = "test-agent"
status_interval = 0.05
autodetect_interval = 0.05

[[devices]]
name = "a"
port = "/dev/fake-a"
"""


@dataclass
class AdoptRig:
    """An agent whose /dev holds one configured module and one stranger."""

    app: AgentApp
    mocks: dict[str, MockAir780E]
    runner: asyncio.Task

    async def wait_for_worker(self, predicate, timeout: float = 3.0) -> str:
        async with asyncio.timeout(timeout):
            while True:
                for name, worker in list(self.app.workers.items()):
                    if predicate(name, worker):
                        return name
                await asyncio.sleep(0.01)


@pytest.fixture
async def adopting(tmp_path, monkeypatch):
    config = AgentConfig.parse(ONE_PINNED_DEVICE)
    config.db_path = tmp_path / "agent.db"

    mocks: dict[str, MockAir780E] = {}
    transports: dict[str, PipeTransport] = {}
    # "/dev/fake-a" is device a, pinned.  "/dev/ttyACM7" is a module that no
    # [[devices]] block mentions — the one autodetect exists for.
    for port, imei in (
        ("/dev/fake-a", "867567048825490"),
        ("/dev/ttyACM7", "862323088372050"),
    ):
        agent_side, modem_side = PipeTransport.create_pair()
        mock = MockAir780E(
            transport=modem_side, imei=imei, iccid=f"89860622180012345{imei[-2:]}"
        )
        await mock.start()
        mocks[port] = mock
        transports[port] = agent_side

    async def prober(port: str, *, timeout: float):
        mock = mocks.get(port)
        return None if mock is None else ProbeResult(
            port=port, model=mock.model, imei=mock.imei, iccid=mock.iccid
        )

    monkeypatch.setattr(
        "air780e_agent.discovery.globmodule.glob", lambda pattern: ["/dev/ttyACM7"]
    )

    def factory(device: DeviceConfig) -> PipeTransport:
        return transports[device.port]

    app = AgentApp(
        config,
        transport_factory=factory,
        registry=PortRegistry(prober=prober, probe_timeout=0.1),
    )
    runner = asyncio.create_task(app.run())
    rig = AdoptRig(app=app, mocks=mocks, runner=runner)
    try:
        yield rig
    finally:
        await app.stop()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        for mock in mocks.values():
            await mock.stop()


async def test_a_new_module_is_adopted_without_being_configured(adopting):
    """The point of the feature: plug in a module, it works.

    Swapping hardware used to mean reading the new IMEI by hand and editing
    the config before the agent would touch it.
    """
    name = await adopting.wait_for_worker(lambda n, w: n != "a")

    assert name == "auto-372050", "the name must be derived from the IMEI"
    worker = adopting.app.workers[name]
    assert worker.config.imei == "862323088372050"

    async with asyncio.timeout(3.0):
        while not worker.online:
            await asyncio.sleep(0.01)
    assert worker.describe()["port"] == "/dev/ttyACM7"


async def test_adoption_is_announced_to_the_server(adopting):
    """A module that starts sending SMS under a name nobody typed has to be
    visible, or the server shows an unexplained device."""
    await adopting.wait_for_worker(lambda n, w: n != "a")

    async with asyncio.timeout(3.0):
        while True:
            adopted = [
                e.payload
                for e in adopting.app.store.unacked_events(limit=1000)
                if e.kind == "log"
                and e.payload.get("event") == "device_adopted"
            ]
            if adopted:
                break
            await asyncio.sleep(0.01)

    assert adopted[0]["imei"] == "862323088372050"
    assert adopted[0]["device"] == "auto-372050"


async def test_a_module_is_adopted_only_once(adopting):
    """The scan repeats every interval; a claimed port must drop out of it."""
    await adopting.wait_for_worker(lambda n, w: n != "a")
    # Several more scan intervals.
    await asyncio.sleep(0.3)

    auto = [n for n in adopting.app.workers if n.startswith("auto-")]
    assert auto == ["auto-372050"]


async def test_the_configured_device_keeps_its_own_module(adopting):
    """Adoption must not touch a pinned port, even though the glob could
    return it — the pinned worker already holds that tty open."""
    await adopting.wait_for_worker(lambda n, w: n != "a")
    assert adopting.app.workers["a"].config.port == "/dev/fake-a"
    assert "a" in adopting.app.workers


async def test_an_adopted_name_survives_a_restart(adopting):
    """The name is on every event the module produces, so it must not drift.

    A name derived fresh each run would be fine while the derivation stays
    put — but the server keys history by device name, and a rename splits one
    module's history into two devices.  Hence the kv table: the stored answer
    cannot change even if the derivation does.
    """
    name = await adopting.wait_for_worker(lambda n, w: n != "a")
    key = "autodetect:imei:862323088372050"
    assert adopting.app.store.get(key) == name

    # Rewritten to something the derivation would never produce, standing in
    # for a name this module was adopted under by an older version.  Asserting
    # only that a second run agrees would prove nothing — the derivation is
    # deterministic, so it agrees with itself even with the lookup removed.
    adopting.app.store.set(key, "auto-from-an-older-agent")
    revived = AgentApp(
        adopting.app.config,
        transport_factory=lambda device: None,
        store=adopting.app.store,
    )
    assert revived._name_for("862323088372050") == "auto-from-an-older-agent"
