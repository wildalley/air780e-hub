"""End-to-end tests for the dependency-free deployment checker."""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from hub_server.config import Settings
from hub_server.main import create_app


SCRIPT = Path(__file__).parents[2] / "deploy" / "self_check.py"
SPEC = importlib.util.spec_from_file_location("deploy_self_check", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
self_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(self_check)


@pytest.fixture
def live_server(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", agent_token="test-token")
    settings.data_dir.mkdir(parents=True)
    app = create_app(settings)

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", lifespan="on")
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    yield f"http://127.0.0.1:{port}", app

    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_self_check_reaches_health_and_authenticated_websocket(
    live_server, monkeypatch, capsys
):
    url, app = live_server
    monkeypatch.setenv("HUB_AGENT_TOKEN", "test-token")

    assert self_check.main(["--url", url, "--allow-http"]) == 0
    output = capsys.readouterr()
    assert "[PASS] health endpoint" in output.out
    assert "[PASS] WebSocket" in output.out
    assert "test-token" not in output.out + output.err
    assert app.state.hub.db.query("SELECT id FROM agents") == []


def test_self_check_reports_bad_token_without_printing_it(
    live_server, monkeypatch, capsys
):
    url, _ = live_server
    monkeypatch.setenv("HUB_AGENT_TOKEN", "wrong-token")

    assert self_check.main(["--url", url, "--allow-http"]) == 1
    output = capsys.readouterr()
    assert "code 4001" in output.err
    assert "wrong-token" not in output.out + output.err


def test_self_check_requires_https_unless_explicitly_allowed():
    with pytest.raises(self_check.CheckError, match="plain HTTP"):
        self_check.normalize_base_url("http://example.com", allow_http=False)


def test_self_check_rejects_unsafe_environment_values():
    with pytest.raises(self_check.CheckError, match="HUB_HOST_PORT"):
        self_check.validate_environment(
            {"HUB_HOST_PORT": "70000"}, using_https=True
        )
    with pytest.raises(self_check.CheckError, match="HUB_BEHIND_PROXY"):
        self_check.validate_environment(
            {"HUB_BEHIND_PROXY": "false"}, using_https=True
        )
