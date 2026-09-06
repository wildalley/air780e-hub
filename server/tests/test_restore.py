"""Restore boundaries using disposable databases and local ASGI clients."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from hub_server.auth import SESSION_COOKIE
from hub_server.config import Settings
from hub_server.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, agent_token="restore-test"))
    with TestClient(app) as client:
        assert (
            client.post("/api/auth/setup", json={"password": "RestoreTest123"}).status_code == 200
        )
        yield client


def backup(client):
    response = client.get("/api/system/backup")
    assert response.status_code == 200
    return response.content


def upload(client, content):
    return client.post(
        "/api/system/restore", content=content, headers={"Content-Type": "application/octet-stream"}
    )


def test_declared_oversize_is_rejected_without_creating_upload(client):
    state = client.app.state.hub
    state.settings.restore_max_bytes = 4
    assert upload(client, b"too large").status_code == 413
    assert state.restore.outcome == "unchanged"
    assert not list(state.settings.data_dir.glob("restore-upload-*"))


def test_actual_chunked_size_is_limited(client):
    state = client.app.state.hub
    state.settings.restore_max_bytes = 4
    response = upload(client, iter([b"123", b"456"]))
    assert response.status_code == 413
    assert not list(state.settings.data_dir.glob("restore-upload-*"))


def test_disk_preflight_refuses_before_mutating_database(client, monkeypatch):
    import hub_server.restore as module

    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    assert upload(client, b"backup").status_code == 507
    assert client.get("/api/overview").status_code == 200


def test_candidate_foreign_key_errors_are_rejected(client, tmp_path):
    candidate = tmp_path / "bad-reference.db"
    candidate.write_bytes(backup(client))
    with sqlite3.connect(candidate) as conn:
        # The backup is WAL-mode.  Switch this diagnostic copy to a rollback
        # journal so the invalid row is present in the bytes uploaded below.
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("INSERT INTO devices (agent_id,name,sim_id) VALUES ('site','a',999)")
    with sqlite3.connect(candidate) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchone() is not None
    response = upload(client, candidate.read_bytes())
    assert response.status_code == 400
    assert "引用" in response.json()["detail"]
    assert client.app.state.hub.db.one("SELECT id FROM devices") is None


def test_switch_failure_rolls_back_online_data_and_keeps_session(client, monkeypatch):
    data = backup(client)
    state = client.app.state.hub
    state.db.set_setting("after-backup", "online-value")

    def fail_reset():
        state.db.set_setting("after-backup", "partially-restored")
        raise RuntimeError("injected failure after copy")

    monkeypatch.setattr(state.db, "reset_restored_runtime", fail_reset)
    response = upload(client, data)
    assert response.status_code == 500
    assert "已回退" in response.json()["detail"]
    assert state.db.get_setting("after-backup") == "online-value"
    assert state.restore.outcome == "rolled_back"
    assert not state.restore.active
    assert client.get("/api/overview").status_code == 200
    assert state.gateway.on_message.__self__ is state.notifier


def test_double_failure_keeps_maintenance_and_exposes_recovery_snapshot(client, monkeypatch):
    data = backup(client)
    state = client.app.state.hub

    def fail_copy(path):
        raise RuntimeError("injected copy failure")

    monkeypatch.setattr(state.db, "replace_prepared", fail_copy)
    response = upload(client, data)
    assert response.status_code == 500
    assert "需要人工恢复" in response.json()["detail"]
    assert client.get("/api/overview").status_code == 503
    status = client.get("/api/system/restore/status")
    assert status.json()["outcome"] == "manual_recovery"
    snapshot = state.settings.data_dir / status.json()["snapshot"]
    assert snapshot.is_file()
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert not state.background_tasks


def test_success_clears_sessions_token_grace_operations_and_stale_online_flags(client):
    state = client.app.state.hub
    db = state.db
    db.upsert_device("site", {"name": "a", "online": True})
    db.upsert_agent("site", "test", 1, connected=True)
    db.set_setting("previous_agent_token_hash", "old-hash")
    db.set_setting("previous_agent_token_expires_at", "2099-01-01T00:00:00+00:00")
    operation = db.create_operation(
        actor="admin",
        target=db.one("SELECT * FROM devices"),
        frame={"type": "send_sms"},
        idempotency_key="restore-operation",
        deadline="2099-01-01T00:00:00+00:00",
        stream_id="s",
        connection_id="old",
    )
    assert upload(client, backup(client)).status_code == 200
    assert db.one("SELECT online FROM devices")["online"] == 0
    assert db.one("SELECT connected FROM agents")["connected"] == 0
    assert db.get_setting("previous_agent_token_hash") is None
    assert db.operation(operation["id"])["status"] == "cancelled"
    assert not db.query("SELECT * FROM sessions")
    assert state.settings.agent_token == "restore-test"
    assert client.get("/api/overview").status_code == 401


def test_snapshot_retention_keeps_three_private_copies(client):
    data = backup(client)
    for _ in range(4):
        assert upload(client, data).status_code == 200
        assert (
            client.post("/api/auth/login", json={"password": "RestoreTest123"}).status_code == 200
        )
    snapshots = list((client.app.state.hub.settings.data_dir / "restore-snapshots").glob("*.db"))
    assert len(snapshots) == 3
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in snapshots)


async def wait_for(event):
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(0.002)


async def test_maintenance_blocks_http_and_drains_admitted_requests(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path, agent_token="test"))
    state = app.state.hub
    snapshot = tmp_path / "snapshot.db"
    state.auth.set_password("RestoreTest123")
    state.db.backup_to(snapshot)
    entered, release = threading.Event(), threading.Event()
    real_query = state.db.read_query

    def slow_query(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_query(*args, **kwargs)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            assert (
                await c.post("/api/auth/login", json={"password": "RestoreTest123"})
            ).status_code == 200
            monkeypatch.setattr(state.db, "read_query", slow_query)
            reader = asyncio.create_task(c.get("/api/sims"))
            await wait_for(entered)
            restoring = asyncio.create_task(
                c.post("/api/system/restore", content=snapshot.read_bytes())
            )
            try:
                async with asyncio.timeout(5):
                    while not state.restore.active:
                        await asyncio.sleep(0.002)
                assert (await c.get("/api/overview")).status_code == 503
                assert (
                    await c.post("/api/auth/setup", json={"password": "UnsafeReset123"})
                ).status_code == 503
                assert (await c.get("/healthz")).status_code == 200
                assert (await c.get("/api/system/restore/status")).json()["phase"] == "draining"
                assert not restoring.done()
            finally:
                release.set()
                await reader
            assert (await restoring).status_code == 200


async def test_cancelled_upload_cleans_file_and_releases_lock(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, agent_token="test"))
    state = app.state.hub
    started = asyncio.Event()

    async def receive():
        if not started.is_set():
            started.set()
            return {"type": "http.request", "body": b"partial", "more_body": True}
        await asyncio.Event().wait()

    request = Request(
        {
            "type": "http",
            "headers": [(b"cookie", f"{SESSION_COOKIE}=test-cookie".encode())],
        },
        receive,
    )
    task = asyncio.create_task(state.restore.restore(state, request))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not state.restore.lock.locked()
    assert state.restore.outcome == "unchanged"
    assert not list(tmp_path.glob("restore-upload-*"))
    await state.notifier.client.aclose()
    state.close()


async def test_repeated_cancel_during_switch_still_finishes_before_cleanup(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path, agent_token="test"))
    state = app.state.hub
    state.auth.set_password("RestoreTest123")
    snapshot = tmp_path / "snapshot.db"
    state.db.backup_to(snapshot)
    started, release = threading.Event(), threading.Event()
    real_replace = state.db.replace_prepared

    def slow_replace(path):
        started.set()
        assert release.wait(5)
        assert Path(path).exists()
        real_replace(path)

    monkeypatch.setattr(state.db, "replace_prepared", slow_replace)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await c.post("/api/auth/login", json={"password": "RestoreTest123"})
            task = asyncio.create_task(c.post("/api/system/restore", content=snapshot.read_bytes()))
            await wait_for(started)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert state.restore.outcome == "restored"
            assert not state.restore.active
            assert not list(tmp_path.glob("restore-upload-*"))
            assert (await c.get("/api/overview")).status_code == 401


def test_restore_disconnects_agent_before_rebuilding_registry(client):
    data = backup(client)
    state = client.app.state.hub
    old_gateway = state.gateway
    with client.websocket_connect("/ws", headers={"Authorization": "Bearer restore-test"}) as ws:
        ws.send_json({
            "type": "hello", "agent_id": "site",
            "devices": [{"name": "a", "online": True}],
        })
        assert ws.receive_json()["type"] == "sync_tasks"
        old_connection = old_gateway.connections["site"]
        assert upload(client, data).status_code == 200
        assert ws.receive()["code"] == 1012
        assert state.restore.sockets == {}
        assert old_gateway.connections == {}
        assert state.gateway.connections == {}

    with client.websocket_connect("/ws", headers={"Authorization": "Bearer restore-test"}) as ws:
        ws.send_json({
            "type": "hello", "agent_id": "site",
            "devices": [{"name": "a", "online": True}],
        })
        assert ws.receive_json()["type"] == "sync_tasks"
        assert state.gateway.connections["site"].ready
        assert state.db.one("SELECT connected FROM agents WHERE id = 'site'")["connected"] == 1
        assert state.db.one("SELECT online FROM devices WHERE name = 'a'")["online"] == 1

        # The retired ASGI task may unwind after this new connection is live.
        # Its old cleanup must not write offline state into the restored DB.
        client.portal.call(old_gateway._unregister, old_connection)
        assert state.db.one("SELECT connected FROM agents WHERE id = 'site'")["connected"] == 1
        assert state.db.one("SELECT online FROM devices WHERE name = 'a'")["online"] == 1


def test_drain_timeout_rebuilds_gateway_without_switching_database(client):
    data = backup(client)
    state = client.app.state.hub
    old_gateway = state.gateway
    state.db.set_setting("after-backup", "online-value")
    state.settings.restore_drain_timeout = 0.01
    state.restore.idle.clear()

    response = upload(client, data)

    assert response.status_code == 409
    assert state.db.get_setting("after-backup") == "online-value"
    assert not state.restore.active
    assert state.gateway is not old_gateway
    assert old_gateway.retiring_for_restore
    assert not state.gateway.retiring_for_restore
    assert state.background_tasks


async def test_concurrent_restore_returns_conflict_without_replacing_progress(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, agent_token="test"))
    state = app.state.hub
    state.restore.phase = "uploading"
    async with state.restore.lock:
        with pytest.raises(Exception) as error:
            await state.restore.restore(state, None)
        assert error.value.status_code == 409
        assert state.restore.phase == "uploading"
    await state.notifier.client.aclose()
    state.close()
