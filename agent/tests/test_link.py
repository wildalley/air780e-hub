"""Agent/Server WebSocket contract tests."""

from __future__ import annotations

import json
from typing import Any

import air780e_agent.link as link_module
from air780e_agent import PROTOCOL_VERSION
from air780e_agent.config import ServerConfig
from air780e_agent.link import ServerLink
from air780e_agent.store import LocalStore


class _Socket:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.frames.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _Connection:
    def __init__(self, socket: _Socket) -> None:
        self.socket = socket

    async def __aenter__(self) -> _Socket:
        return self.socket

    async def __aexit__(self, *_args: object) -> None:
        return None


async def test_hello_advertises_the_wire_protocol_version(tmp_path, monkeypatch):
    socket = _Socket()
    monkeypatch.setattr(
        link_module,
        "connect",
        lambda *_args, **_kwargs: _Connection(socket),
    )
    store = LocalStore(tmp_path / "agent.db")

    async def on_command(_frame: dict[str, Any]) -> None:
        return None

    link = ServerLink(
        ServerConfig(url="wss://hub.test/ws", token="secret"),
        agent_id="site-a",
        version="0.1.0",
        store=store,
        on_command=on_command,
        describe_devices=lambda: [{"name": "a"}],
    )
    try:
        await link._connect_once()
    finally:
        store.close()

    assert socket.frames == [{
        "type": "hello",
        "agent_id": "site-a",
        "version": "0.1.0",
        "protocol_version": PROTOCOL_VERSION,
        "last_seq": 0,
        "devices": [{"name": "a"}],
    }]
