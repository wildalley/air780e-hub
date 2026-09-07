"""Bounded, private runtime measurements without changing delivery semantics."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import hub_server.metrics as metrics_module
from hub_server.config import Settings
from hub_server.db import Database
from hub_server.gateway import AgentConnection, Gateway
from hub_server.main import create_app
from hub_server.metrics import (
    GATEWAY_CLOSE_SERIES_LIMIT,
    HTTP_SERIES_LIMIT,
    SAMPLE_LIMIT,
    MeasuredRLock,
    MetricsMiddleware,
    RuntimeMetrics,
    monitor_loop,
)


def test_samples_are_bounded_but_counts_and_maximum_survive_eviction():
    metrics = RuntimeMetrics()
    metrics.observe("gateway_ack", 10)
    for index in range(1, SAMPLE_LIMIT + 1):
        metrics.observe("gateway_ack", index / 1000)
    snapshot = metrics.snapshot()
    timing = snapshot["timings"]["gateway_ack"]
    assert timing == {
        "count": 257, "sample_count": 256, "total_ms": 42896.0, "max_ms": 10000.0,
        "p50_ms": 128.0, "p95_ms": 244.0, "p99_ms": 254.0,
    }
    assert snapshot["timings"]["loop_lag"]["p95_ms"] is None


def test_parallel_observations_are_not_lost_and_snapshots_are_detached():
    metrics = RuntimeMetrics()

    def collect(_):
        for _ in range(500):
            metrics.observe("db_worker", 0.001)
            metrics.increment("events_committed")
            metrics.snapshot()

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(collect, range(4)))
    snapshot = metrics.snapshot()
    assert snapshot["timings"]["db_worker"]["count"] == 2000
    assert snapshot["counters"]["events_committed"] == 2000
    snapshot["counters"]["events_committed"] = -1
    assert metrics.snapshot()["counters"]["events_committed"] == 2000


def test_http_labels_and_series_remain_bounded():
    metrics = RuntimeMetrics()
    for index in range(1000):
        metrics.observe_http({
            "type": "http", "method": f"CUSTOM{index}", "path": f"/api/private-{index}",
            "query_string": b"token=private", "route": SimpleNamespace(path="/{path:path}"),
        }, 404, "complete", 0.001)
    snapshot = metrics.snapshot()
    assert len(snapshot["http"]) == 1
    assert snapshot["http"][0]["method"] == "OTHER"
    assert snapshot["http"][0]["route"] == "<unmatched>"
    assert snapshot["http"][0]["count"] == 1000
    assert "private" not in json.dumps(snapshot)

    for index in range(HTTP_SERIES_LIMIT + 10):
        metrics.observe_http({
            "method": "GET", "route": SimpleNamespace(path=f"/api/static-route-{index}"),
        }, 200, "complete", 0.001)
    snapshot = metrics.snapshot()
    assert len(snapshot["http"]) == HTTP_SERIES_LIMIT
    assert snapshot["http_overflow"]["count"] == 11
    assert sum(item["count"] for item in snapshot["http"]) + 11 == 1266


def test_gateway_close_reasons_are_bounded_and_do_not_keep_peer_text():
    metrics = RuntimeMetrics()
    metrics.note_gateway_close("auth_failed", 4001)
    metrics.note_gateway_close("peer_disconnect", 1000)
    metrics.note_gateway_close("not-a-real-category", 999)
    bounded = RuntimeMetrics()
    for code in range(3000, 3000 + GATEWAY_CLOSE_SERIES_LIMIT):
        bounded.note_gateway_close("protocol", code)
    bounded.note_gateway_close("protocol", 4999)

    snapshot = metrics.snapshot()
    bounded_snapshot = bounded.snapshot()
    assert {row["category"] for row in snapshot["gateway_closes"]} >= {
        "auth_failed", "peer_disconnect", "other",
    }
    assert next(
        row for row in snapshot["gateway_closes"]
        if row["category"] == "auth_failed"
    ) == {"category": "auth_failed", "code": 4001, "count": 1}
    assert bounded_snapshot["gateway_close_overflow"] == 1
    assert len(bounded_snapshot["gateway_closes"]) == GATEWAY_CLOSE_SERIES_LIMIT
    assert "not-a-real-category" not in json.dumps(snapshot)


async def test_http_streaming_records_after_the_last_body_and_does_not_buffer(monkeypatch):
    metrics = RuntimeMetrics()
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(metrics_module, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    sent = []

    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/messages/export")
        await send({"type": "http.response.start", "status": 200})
        clock.now = 0.05
        await send({"type": "http.response.body", "body": b"private body", "more_body": True})
        assert len(sent) == 2
        assert metrics.snapshot()["http"] == []
        clock.now = 0.25
        await send({"type": "http.response.body", "body": b"final"})

    async def send(message):
        sent.append(message)

    await MetricsMiddleware(app, metrics=metrics)({
        "type": "http", "method": "GET", "path": "/api/messages/export",
    }, None, send)
    snapshot = metrics.snapshot()
    assert snapshot["http"][0]["count"] == 1
    assert snapshot["http"][0]["total_ms"] == 250
    assert snapshot["http"][0]["outcome"] == "complete"
    assert "private body" not in json.dumps(snapshot)


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("headers_sent", [False, True])
async def test_http_interruption_preserves_errors_and_records_one_sample(failure, headers_sent):
    metrics = RuntimeMetrics()

    async def app(scope, receive, send):
        if headers_sent:
            await send({"type": "http.response.start", "status": 200})
        raise failure("private error")

    async def send(message):
        pass

    with pytest.raises(failure, match="private error"):
        await MetricsMiddleware(app, metrics=metrics)({
            "type": "http", "method": "GET", "path": "/api/unknown",
        }, None, send)
    snapshot = metrics.snapshot()
    assert snapshot["http"][0]["count"] == 1
    assert snapshot["http"][0]["status"] == (200 if headers_sent else 500)
    assert snapshot["http"][0]["outcome"] == (
        "error" if failure is RuntimeError else "cancelled"
    )
    assert "private error" not in json.dumps(snapshot)


async def test_loop_lag_uses_monotonic_delay_and_propagates_cancellation(monkeypatch):
    metrics = RuntimeMetrics()
    clock = SimpleNamespace(now=0.0, sleeps=0)
    monkeypatch.setattr(metrics_module, "time", SimpleNamespace(perf_counter=lambda: clock.now))

    async def sleep(interval):
        clock.sleeps += 1
        if clock.sleeps == 2:
            raise asyncio.CancelledError
        clock.now += interval + 0.125

    monkeypatch.setattr(metrics_module, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        await monitor_loop(metrics)
    lag = metrics.snapshot()["timings"]["loop_lag"]
    assert lag["count"] == 1
    assert lag["p95_ms"] == 125


def test_nested_lock_wait_and_hold_are_counted_once_and_release_on_error(monkeypatch):
    metrics = RuntimeMetrics()
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(metrics_module, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    lock = MeasuredRLock(metrics)
    error = sqlite3.OperationalError("private database name")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY | (2 << 8)
    with pytest.raises(sqlite3.OperationalError):
        with lock:
            clock.now = 0.05
            with lock:
                clock.now = 0.10
                raise error
    snapshot = metrics.snapshot()
    assert snapshot["timings"]["db_lock_wait"]["count"] == 1
    assert snapshot["timings"]["db_lock_hold"]["total_ms"] == 100
    assert snapshot["counters"]["db_busy"] == 1
    with ThreadPoolExecutor(max_workers=1) as worker:
        def acquire():
            with lock:
                return True
        assert worker.submit(acquire).result(timeout=1)


def test_real_sqlite_busy_is_counted_without_treating_other_errors_as_busy(tmp_path):
    db = Database(tmp_path / "hub.db")
    other = sqlite3.connect(db.path, isolation_level=None)
    try:
        db.execute("PRAGMA busy_timeout = 0")
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.apply_event("site", 1, "log", lambda: None)
        assert db.metrics.snapshot()["counters"]["db_busy"] == 1
        assert db.metrics.snapshot()["counters"]["events_failed"] == 1
        other.execute("ROLLBACK")
        with pytest.raises(sqlite3.OperationalError):
            db.read_one("SELECT * FROM private_missing_table")
        snapshot = db.metrics.snapshot()
        assert snapshot["counters"]["db_busy"] == 1
        assert snapshot["timings"]["db_read_session"]["count"] == 1
        assert "private_missing_table" not in json.dumps(snapshot)
    finally:
        other.close()
        db.close()


async def test_failed_ack_counts_commit_then_replay_without_double_applying(tmp_path):
    db = Database(tmp_path / "hub.db")

    class Socket:
        failing = True

        async def send_text(self, text):
            assert db.count_messages() == 1
            if self.failing:
                raise OSError("private socket address")

    socket = Socket()
    gateway = Gateway(db, Settings(data_dir=tmp_path, agent_token="synthetic"))
    connection = AgentConnection("private-agent", socket)
    frame = {"type": "sms_in", "seq": 1, "device": "private-device", "body": "private-body"}
    try:
        with pytest.raises(OSError):
            await gateway._ingest(connection, frame)
        snapshot = db.metrics.snapshot()
        assert snapshot["counters"]["events_committed"] == 1
        assert snapshot["counters"]["ack_send_failed"] == 1
        assert snapshot["timings"]["gateway_ack"]["count"] == 0
        socket.failing = False
        await gateway._ingest(connection, frame)
        snapshot = db.metrics.snapshot()
        assert snapshot["counters"]["events_committed"] == 1
        assert snapshot["counters"]["events_duplicate"] == 1
        assert snapshot["counters"]["events_failed"] == 0
        assert snapshot["timings"]["gateway_ack"]["count"] == 1
        assert snapshot["timings"]["event_transaction"]["count"] == 2
        assert "private" not in json.dumps(snapshot)
    finally:
        db.close()


def test_diagnostics_requires_authentication_and_groups_real_requests_by_template(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, agent_token="synthetic"))
    with TestClient(app) as client:
        assert client.get("/api/operations/diagnostics").status_code == 401
        assert client.post("/api/auth/setup", json={"password": "private-password123"}).is_success
        for name in ("private-device-a", "private-device-b"):
            assert client.get(f"/api/devices/{name}/history").status_code == 404
        assert client.get("/api/messages?limit=0&peer=private-phone").status_code == 422
        response = client.get("/api/operations/diagnostics")
        assert response.headers["Cache-Control"] == "no-store"
        metrics = response.json()["runtime"]["metrics"]
        history = [row for row in metrics["http"] if row["route"] == "/api/devices/{name}/history"]
        assert len(history) == 1
        assert history[0]["count"] == 2
        assert history[0]["status"] == 404
        assert any(row["status"] == 401 for row in metrics["http"])
        assert any(row["status"] == 422 for row in metrics["http"])
        assert "private" not in json.dumps(metrics)
        app.state.hub.restore.active = True
        try:
            assert client.get("/api/messages").status_code == 503
            rows = app.state.hub.db.metrics.snapshot()["http"]
            assert any(row["status"] == 503 and row["route"] == "<unmatched>" for row in rows)
        finally:
            app.state.hub.restore.active = False


async def test_loop_monitor_follows_background_restart_and_keeps_history(tmp_path, monkeypatch):
    import hub_server.main as main_module

    def fast_monitor(metrics):
        return monitor_loop(metrics, interval=0.001)

    monkeypatch.setattr(main_module, "monitor_loop", fast_monitor)
    app = create_app(Settings(data_dir=tmp_path, agent_token="synthetic"))
    state = app.state.hub

    async def wait_for_sample(after):
        async with asyncio.timeout(2):
            while state.db.metrics.snapshot()["timings"]["loop_lag"]["count"] <= after:
                await asyncio.sleep(0.001)

    async with app.router.lifespan_context(app):
        await wait_for_sample(0)
        old_tasks = list(state.background_tasks)
        await state.stop_background()
        assert all(task.done() for task in old_tasks)
        count = state.db.metrics.snapshot()["timings"]["loop_lag"]["count"]
        await asyncio.sleep(0.005)
        assert state.db.metrics.snapshot()["timings"]["loop_lag"]["count"] == count
        state.rebuild_gateway()
        state.start_background()
        await wait_for_sample(count)
        new_tasks = list(state.background_tasks)
    assert all(task.done() for task in new_tasks)
