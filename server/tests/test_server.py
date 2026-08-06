"""Server tests: auth, ingest idempotency, and the API surface."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hub_server.config import Settings
from hub_server.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path, agent_token="test-token")
    s.ensure_agent_token()
    return s


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client):
    """A client that has completed first-run setup and is signed in."""
    response = client.post("/api/auth/setup", json={"password": "hunter2hunter"})
    assert response.status_code == 200
    return client


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


def test_first_run_reports_unconfigured(client):
    body = client.get("/api/auth/status").json()
    assert body["configured"] is False
    assert body["authenticated"] is False


def test_setup_then_authenticated(client):
    client.post("/api/auth/setup", json={"password": "hunter2hunter"})
    body = client.get("/api/auth/status").json()
    assert body["configured"] is True
    assert body["authenticated"] is True


def test_setup_is_one_shot(admin):
    response = admin.post("/api/auth/setup", json={"password": "another1pass"})
    assert response.status_code == 409


def test_weak_password_rejected(client):
    response = client.post("/api/auth/setup", json={"password": "short"})
    assert response.status_code == 400
    response = client.post("/api/auth/setup", json={"password": "alllowercase"})
    assert response.status_code == 400, "needs at least two character classes"


def test_login_and_logout(admin):
    admin.post("/api/auth/logout")
    assert admin.get("/api/overview").status_code == 401

    assert admin.post("/api/auth/login", json={"password": "wrong-one1"}).status_code == 401
    assert admin.post("/api/auth/login", json={"password": "hunter2hunter"}).status_code == 200
    assert admin.get("/api/overview").status_code == 200


def test_password_change_signs_everyone_out(admin):
    response = admin.post(
        "/api/auth/password", json={"current": "hunter2hunter", "new": "brandNew123"}
    )
    assert response.status_code == 200
    assert admin.get("/api/overview").status_code == 401


def test_password_change_requires_the_current_one(admin):
    response = admin.post(
        "/api/auth/password", json={"current": "nope1234", "new": "brandNew123"}
    )
    assert response.status_code == 400


def test_protected_routes_reject_anonymous(client):
    for path in ("/api/overview", "/api/devices", "/api/messages", "/api/tasks"):
        assert client.get(path).status_code == 401, path


def test_there_is_no_password_free_mode(admin):
    """SimAdmin ships a 'disable password' switch; this project must not."""
    paths = [
        getattr(route, "path", "") for route in admin.app.routes
    ]
    assert not any("disable" in p or "no-auth" in p for p in paths)


def test_healthz_is_public(client):
    assert client.get("/healthz").json()["ok"] is True


def test_openapi_is_not_exposed(client):
    response = client.get("/openapi.json")
    # Either absent, or shadowed by the SPA shell — never the actual schema.
    assert "openapi" not in response.text.lower()


# --------------------------------------------------------------------------
# agent websocket
# --------------------------------------------------------------------------


HELLO = {
    "type": "hello",
    "agent_id": "test-agent",
    "version": "0.1.0",
    "last_seq": 0,
    "devices": [
        {"name": "a", "label": "移动卡", "port": "/dev/air780e-a", "online": True,
         "iccid": "89860622180012345670", "imei": "111", "model": "AirM2M_780E",
         "operator": "CHINA MOBILE", "smsc": "+8613800210500", "registered": True},
        {"name": "b", "label": "联通卡", "port": "/dev/air780e-b", "online": True,
         "iccid": "89860622180012345671", "imei": "222", "model": "AirM2M_780E",
         "operator": "CHINA UNICOM", "smsc": "+8613010200500", "registered": True},
    ],
}


def _connect(client, token: str = "test-token"):
    return client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}"})


def _greet(ws) -> list[dict]:
    """Complete the handshake and return the keep-alive tasks pushed with it.

    Every accepted connection is answered with a full ``sync_tasks`` frame, so
    a test that says hello must read it before it can see its own acks.
    """
    ws.send_json(HELLO)
    frame = ws.receive_json()
    assert frame["type"] == "sync_tasks", f"expected the task push, got {frame}"
    return frame["tasks"]


def _minutes_ago(minutes: int) -> str:
    from datetime import datetime, timedelta

    return (
        datetime.now(UTC) - timedelta(minutes=minutes)
    ).isoformat(timespec="seconds")


def test_agent_needs_a_valid_token(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with _connect(client, token="wrong") as ws:
            ws.send_json(HELLO)
            ws.receive_json()
    assert excinfo.value.code == 4001


def test_websocket_self_check_authenticates_without_registering_agent(client):
    with client.websocket_connect(
        "/ws?self_check=1",
        headers={"Authorization": "Bearer test-token"},
    ) as ws:
        assert ws.receive_json() == {"type": "self_check", "ok": True}

    assert client.app.state.hub.gateway.connections == {}
    assert client.app.state.hub.db.query("SELECT id FROM agents") == []


def test_websocket_self_check_rejects_a_bad_token(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/ws?self_check=1",
            headers={"Authorization": "Bearer wrong"},
        ) as ws:
            ws.receive_json()
    assert excinfo.value.code == 4001


def test_hello_registers_devices_and_sims(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({"type": "status", "seq": 1, "device": "a", "online": True})
        ws.receive_json()  # ack

    devices = admin.get("/api/devices").json()
    assert {d["name"] for d in devices} == {"a", "b"}

    sims = admin.get("/api/sims").json()
    assert len(sims) == 2, "two cards must be tracked separately"
    assert {s["iccid"] for s in sims} == {
        "89860622180012345670", "89860622180012345671",
    }


def test_hello_must_come_first(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with _connect(client) as ws:
            ws.send_json({"type": "status", "seq": 1, "device": "a"})
            ws.receive_json()
    assert excinfo.value.code == 4002


def test_sms_in_is_stored_and_acked(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_in", "seq": 7, "device": "a",
            "iccid": "89860622180012345670", "peer": "10086",
            "body": "验证码 123456", "ts": "2026-08-02T18:00:00+08:00",
            "segments": 1,
        })
        assert ws.receive_json() == {"type": "ack", "seq": 7}

    items = admin.get("/api/messages").json()["items"]
    assert len(items) == 1
    assert items[0]["body"] == "验证码 123456"
    assert items[0]["peer"] == "10086"
    assert items[0]["direction"] == "in"
    assert items[0]["sim_iccid"] == "89860622180012345670"


def test_replayed_event_is_not_duplicated(admin):
    """The agent replays after a lost ack; that must not double a message."""
    event = {
        "type": "sms_in", "seq": 9, "device": "a",
        "iccid": "89860622180012345670", "peer": "10086",
        "body": "only once", "ts": "2026-08-02T18:00:00+08:00",
    }
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json(event)
        ws.receive_json()
        ws.send_json(event)  # same seq again
        assert ws.receive_json() == {"type": "ack", "seq": 9}, "duplicates still ack"

    items = admin.get("/api/messages").json()["items"]
    assert len(items) == 1


def test_messages_from_two_cards_are_kept_apart(admin):
    with _connect(admin) as ws:
        _greet(ws)
        for seq, (device, iccid, body) in enumerate([
            ("a", "89860622180012345670", "from card a"),
            ("b", "89860622180012345671", "from card b"),
        ], start=1):
            ws.send_json({
                "type": "sms_in", "seq": seq, "device": device, "iccid": iccid,
                "peer": "10086", "body": body, "ts": "2026-08-02T18:00:00+08:00",
            })
            ws.receive_json()

    sims = {s["iccid"]: s for s in admin.get("/api/sims").json()}
    for sim in sims.values():
        assert sim["message_count"] == 1

    sim_a = sims["89860622180012345670"]["id"]
    filtered = admin.get(f"/api/messages?sim_id={sim_a}").json()["items"]
    assert [m["body"] for m in filtered] == ["from card a"]


def test_conversations_group_by_card_and_correspondent(admin):
    """The messages UI opens on threads, so the grouping has to be right even
    when the same number is reached through both cards."""
    with _connect(admin) as ws:
        _greet(ws)
        for seq, (device, iccid, peer, body, ts) in enumerate([
            ("a", "89860622180012345670", "10086", "第一条", "2026-08-02T10:00:00+08:00"),
            ("a", "89860622180012345670", "10086", "第二条", "2026-08-02T11:00:00+08:00"),
            ("a", "89860622180012345670", "955555", "别的号", "2026-08-02T09:00:00+08:00"),
            ("b", "89860622180012345671", "10086", "另一张卡", "2026-08-02T12:00:00+08:00"),
        ], start=1):
            ws.send_json({
                "type": "sms_in", "seq": seq, "device": device, "iccid": iccid,
                "peer": peer, "body": body, "ts": ts,
            })
            ws.receive_json()

    threads = admin.get("/api/conversations").json()
    assert len(threads) == 3, "same peer on two cards is two threads"

    # Newest activity first, and the preview is the newest message in each.
    assert [t["peer"] for t in threads] == ["10086", "10086", "955555"]
    assert threads[0]["last_body"] == "另一张卡"
    assert threads[0]["sim_iccid"] == "89860622180012345671"
    assert threads[1]["last_body"] == "第二条", "preview must be the latest, not the first"
    assert threads[1]["message_count"] == 2
    assert threads[2]["message_count"] == 1

def test_unread_receipts_are_isolated_by_card_and_correspondent(admin):
    db = admin.app.state.hub.db
    now = _minutes_ago(1)
    common = {
        "agent_id": "home-arch",
        "device": "a",
        "ts": now,
    }
    db.insert_message(
        **common, direction="in", peer="10086", body="card a",
        iccid="89860622180012345670",
    )
    db.insert_message(
        **common, direction="in", peer="95555", body="another peer",
        iccid="89860622180012345670",
    )
    db.insert_message(
        **common, direction="in", peer="10086", body="card b",
        iccid="89860622180012345671",
    )
    db.insert_message(
        **common, direction="out", peer="10086", body="reply",
        iccid="89860622180012345670",
    )

    assert admin.get("/api/messages/unread").json() == {"total": 3}
    sims = {row["iccid"]: row["id"] for row in admin.get("/api/sims").json()}
    card_a = sims["89860622180012345670"]
    response = admin.post(
        "/api/messages/read", json={"sim_id": card_a, "peer": "10086"}
    )
    assert response.json() == {"ok": True, "marked": 1}
    assert admin.get("/api/messages/unread").json() == {"total": 2}

    unread = {
        (row["sim_id"], row["peer"]): row["unread_count"]
        for row in admin.get("/api/conversations").json()
    }
    assert unread[(card_a, "10086")] == 0
    assert unread[(card_a, "95555")] == 1
    assert unread[(sims["89860622180012345671"], "10086")] == 1


def test_message_export_is_utf8_csv_with_intact_multiline_bodies(admin):
    import csv
    import io

    body = "验证码,123456\n请勿泄漏"
    admin.app.state.hub.db.insert_message(
        agent_id="home-arch",
        device="a",
        direction="in",
        peer="10086",
        body=body,
        ts=_minutes_ago(1),
        iccid="89860622180012345670",
    )

    response = admin.get("/api/messages/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == [
        "id", "ts", "direction", "sim_id", "sim_label", "peer", "body", "status",
    ]
    assert rows[1][5:7] == ["10086", body]


def test_message_list_total_uses_the_same_filters(admin):
    db = admin.app.state.hub.db
    for direction, peer, body in [
        ("in", "10086", "balance 42"),
        ("out", "10086", "query"),
        ("in", "95555", "other"),
    ]:
        db.insert_message(
            agent_id="home-arch", device="a", direction=direction, peer=peer,
            body=body, ts=_minutes_ago(1), iccid="89860622180012345670",
        )

    result = admin.get(
        "/api/messages?direction=in&peer=10086&search=balance"
    ).json()
    assert result["total"] == 1
    assert [item["body"] for item in result["items"]] == ["balance 42"]


def test_message_stats_count_each_card_for_the_requested_window(admin):
    db = admin.app.state.hub.db
    for device, iccid in [
        ("a", "89860622180012345670"),
        ("b", "89860622180012345671"),
    ]:
        db.insert_message(
            agent_id="home-arch",
            device=device,
            direction="in",
            peer="10086",
            body=device,
            ts=_minutes_ago(1),
            iccid=iccid,
        )

    rows = admin.get("/api/stats/messages?days=2").json()
    assert len(rows) == 2
    assert {row["sim_id"] for row in rows} == {
        sim["id"] for sim in admin.get("/api/sims").json()
    }
    assert sum(row["received"] for row in rows) == 2
    assert sum(row["sent"] for row in rows) == 0


def test_a_thread_can_be_read_back_in_full(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_in", "seq": 1, "device": "a",
            "iccid": "89860622180012345670", "peer": "10086",
            "body": "验证码 123456", "ts": "2026-08-02T18:00:00+08:00",
        })
        ws.receive_json()

    sim_id = admin.get("/api/conversations").json()[0]["sim_id"]
    items = admin.get(f"/api/messages?peer=10086&sim_id={sim_id}").json()["items"]
    assert [m["body"] for m in items] == ["验证码 123456"]


def test_status_is_recorded_for_the_history_graph(admin):
    with _connect(admin) as ws:
        _greet(ws)
        for seq, rssi in enumerate([24, 18, 11], start=1):
            ws.send_json({
                "type": "status", "seq": seq, "device": "a", "online": True,
                "registered": True, "rssi": rssi, "dbm": -113 + 2 * rssi,
                "bars": 3, "storage_used": 0, "storage_capacity": 50,
                "ts": _minutes_ago(30 - seq),
            })
            ws.receive_json()

    history = admin.get("/api/devices/a/history?hours=24").json()
    assert [row["rssi"] for row in history] == [24, 18, 11]
    histories = admin.get("/api/devices/history?hours=24").json()
    assert [row["rssi"] for row in histories["a"]] == [24, 18, 11]
    assert histories["b"] == []


def test_history_window_respects_offset_timestamps(admin):
    """A +08:00 timestamp must not be compared as if it were UTC.

    Stored naively, an eight-hour offset shifts a sample right across a
    24-hour window boundary — the graph then shows points it should have
    dropped, or drops points it should show.
    """
    from datetime import datetime, timedelta, timezone

    shanghai = timezone(timedelta(hours=8))
    recent = datetime.now(shanghai) - timedelta(hours=1)
    ancient = datetime.now(shanghai) - timedelta(days=5)

    with _connect(admin) as ws:
        _greet(ws)
        for seq, (when, rssi) in enumerate([(ancient, 9), (recent, 22)], start=1):
            ws.send_json({
                "type": "status", "seq": seq, "device": "a", "online": True,
                "rssi": rssi, "dbm": -113 + 2 * rssi,
                "ts": when.isoformat(timespec="seconds"),
            })
            ws.receive_json()

    day = admin.get("/api/devices/a/history?hours=24").json()
    assert [row["rssi"] for row in day] == [22], "the 5-day-old sample must be excluded"

    week = admin.get("/api/devices/a/history?hours=168").json()
    assert [row["rssi"] for row in week] == [9, 22], "and included in a wider window"


def test_message_timestamps_are_normalised_to_utc(admin):
    from datetime import datetime

    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_in", "seq": 1, "device": "a",
            "iccid": "89860622180012345670", "peer": "10086", "body": "x",
            "ts": "2026-08-02T18:00:00+08:00",
        })
        ws.receive_json()

    stored = admin.get("/api/messages").json()["items"][0]["ts"]
    parsed = datetime.fromisoformat(stored)
    assert parsed.utcoffset() == UTC.utcoffset(None)
    assert parsed == datetime.fromisoformat("2026-08-02T18:00:00+08:00")


def test_agent_log_is_stored_without_message_bodies(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "log", "seq": 1, "device": "a", "level": "warning",
            "message": "storage 48/50, draining",
        })
        ws.receive_json()

    logs = admin.get("/api/logs").json()
    assert logs[0]["message"] == "storage 48/50, draining"
    assert logs[0]["level"] == "warning"


def test_devices_go_offline_when_the_agent_disconnects(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({"type": "status", "seq": 1, "device": "a", "online": True})
        ws.receive_json()
        assert any(d["online"] for d in admin.get("/api/devices").json())

    assert not any(d["online"] for d in admin.get("/api/devices").json())


def test_second_connection_for_the_same_agent_is_refused(admin):
    from starlette.websockets import WebSocketDisconnect

    with _connect(admin) as first:
        _greet(first)
        first.send_json({"type": "status", "seq": 1, "device": "a"})
        first.receive_json()

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with _connect(admin) as second:
                second.send_json(HELLO)
                second.receive_json()
        assert excinfo.value.code == 4003


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def test_send_without_an_agent_is_503(admin):
    admin.post("/api/messages/send", json={
        "device": "a", "number": "10086", "body": "x",
    })
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({"type": "status", "seq": 1, "device": "a"})
        ws.receive_json()

    # Agent has gone; the device is known but unreachable.
    response = admin.post("/api/messages/send", json={
        "device": "a", "number": "10086", "body": "x",
    })
    assert response.status_code == 503


def test_send_to_unknown_device_is_404(admin):
    response = admin.post("/api/messages/send", json={
        "device": "nope", "number": "10086", "body": "x",
    })
    assert response.status_code == 404


# --------------------------------------------------------------------------
# CRUD used by the web UI
# --------------------------------------------------------------------------


def test_sim_can_be_labelled(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({"type": "status", "seq": 1, "device": "a"})
        ws.receive_json()

    sim_id = admin.get("/api/sims").json()[0]["id"]
    response = admin.patch(f"/api/sims/{sim_id}", json={
        "label": "移动主卡", "phone_number": "13800138000",
    })
    assert response.status_code == 200
    assert response.json()["label"] == "移动主卡"
    assert response.json()["phone_number"] == "13800138000"


def test_channel_and_rule_crud(admin):
    channel = admin.post("/api/channels", json={
        "name": "我的 Bark", "type": "bark",
        "config": {"url": "https://api.day.app/xxx"},
    }).json()
    assert channel["type"] == "bark"

    rule = admin.post("/api/rules", json={
        "name": "验证码", "channel_id": channel["id"],
        "match": "keyword", "pattern": "验证码",
    }).json()
    assert rule["pattern"] == "验证码"

    rules = admin.get("/api/rules").json()
    assert rules[0]["channel_name"] == "我的 Bark"

    admin.delete(f"/api/rules/{rule['id']}")
    assert admin.get("/api/rules").json() == []


def test_task_crud_defaults_match_the_plan(admin):
    task = admin.post("/api/tasks", json={"device": "a", "name": "移动保号"}).json()
    assert task["target_number"] == "10086"
    assert task["content"] == "1"
    assert task["schedule_type"] == "interval"
    assert task["schedule_expr"] == "25"
    assert task["jitter_seconds"] == 1800
    assert task["random_suffix"] == 1
    assert task["retry_max"] == 3


def test_task_can_be_customised(admin):
    """The whole point of decision 4: number, content and period are editable."""
    task = admin.post("/api/tasks", json={
        "device": "a", "name": "联通保号",
        "target_number": "10010", "content": "CXHF",
        "schedule_type": "cron", "schedule_expr": "0 3 * * 2",
        "jitter_seconds": 600, "random_suffix": False, "retry_max": 5,
    }).json()
    assert task["target_number"] == "10010"
    assert task["content"] == "CXHF"
    assert task["schedule_expr"] == "0 3 * * 2"
    assert task["random_suffix"] == 0

    updated = admin.put(f"/api/tasks/{task['id']}", json={
        "device": "a", "name": "联通保号", "target_number": "10010",
        "content": "CXHF2", "schedule_type": "interval", "schedule_expr": "30",
    }).json()
    assert updated["content"] == "CXHF2"
    assert updated["schedule_expr"] == "30"

    admin.delete(f"/api/tasks/{task['id']}")
    assert admin.get("/api/tasks").json() == []


def test_a_connecting_agent_is_given_its_tasks(admin):
    """The agent keeps tasks locally (D3), so the connect push is what makes
    an edit it slept through take effect."""
    admin.post("/api/tasks", json={
        "device": "a", "name": "移动卡保号", "target_number": "10086",
        "content": "CXHF", "schedule_expr": "25",
    })

    with _connect(admin) as ws:
        tasks = _greet(ws)

    assert len(tasks) == 1
    assert tasks[0]["device"] == "a"
    assert tasks[0]["target_number"] == "10086"
    assert tasks[0]["content"] == "CXHF"
    assert tasks[0]["enabled"] is True, "wire form is JSON booleans, not 0/1"
    assert tasks[0]["random_suffix"] is True
    assert "last_run_at" not in tasks[0], "run times belong to the agent's clock"


def test_a_task_deleted_while_the_agent_was_away_is_cleared_on_reconnect(admin):
    task = admin.post("/api/tasks", json={"device": "a", "name": "废弃任务"}).json()

    with _connect(admin) as ws:
        assert len(_greet(ws)) == 1

    admin.delete(f"/api/tasks/{task['id']}")

    with _connect(admin) as ws:
        assert _greet(ws) == [], "full replace is what clears the agent's copy"


def test_each_agent_is_only_given_its_own_tasks(admin):
    """Two agents must not run each other's keep-alives."""
    other_hello = {
        "type": "hello", "agent_id": "other-agent", "version": "0.1.0",
        "last_seq": 0,
        "devices": [{"name": "c", "label": "电信卡", "port": "/dev/air780e-c",
                     "online": True, "iccid": "89860622180012345672"}],
    }
    admin.post("/api/tasks", json={"device": "a", "name": "给 test-agent"})
    admin.post("/api/tasks", json={"device": "c", "name": "给 other-agent"})

    with _connect(admin) as other:
        other.send_json(other_hello)
        assert [t["name"] for t in other.receive_json()["tasks"]] == ["给 other-agent"]

        with _connect(admin) as mine:
            assert [t["name"] for t in _greet(mine)] == ["给 test-agent"]


def test_an_edit_reaches_the_connected_agent(admin):
    with _connect(admin) as ws:
        assert _greet(ws) == []

        admin.post("/api/tasks", json={"device": "a", "name": "新任务"})
        frame = ws.receive_json()
        assert frame["type"] == "sync_tasks"
        assert [t["name"] for t in frame["tasks"]] == ["新任务"]


def test_overview_counters(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_in", "seq": 1, "device": "a",
            "iccid": "89860622180012345670", "peer": "10086",
            "body": "hello", "ts": "2026-08-02T18:00:00+08:00",
        })
        ws.receive_json()
        overview = admin.get("/api/overview").json()
        assert overview["counters"]["devices_total"] == 2
        assert overview["counters"]["messages_total"] == 1
        assert len(overview["recent_messages"]) == 1


def test_status_events_do_not_erase_device_identity(admin):
    """A status frame carries a subset of what hello did.

    Treating a missing field as "set it to blank" wipes the card's label,
    port and SIM every 60 seconds — the dashboard then shows bare slot names
    and "no card" for a perfectly healthy module.
    """
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "status", "seq": 1, "device": "a",
            "online": True, "registered": True, "rssi": 22, "dbm": -69,
            "ts": _minutes_ago(1),
        })
        ws.receive_json()

    device = next(d for d in admin.get("/api/devices").json() if d["name"] == "a")
    assert device["label"] == "移动卡"
    assert device["port"] == "/dev/air780e-a"
    assert device["imei"] == "111"
    assert device["iccid"] == "89860622180012345670"
    # …while the state the frame *did* carry is applied.
    assert device["rssi"] == 22
    assert device["dbm"] == -69


def test_status_can_still_set_falsy_state(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "status", "seq": 1, "device": "a",
            "online": False, "registered": False, "rssi": 0,
            "storage_used": 0, "storage_capacity": 50, "ts": _minutes_ago(1),
        })
        ws.receive_json()
        device = next(d for d in admin.get("/api/devices").json() if d["name"] == "a")
        assert device["registered"] == 0
        assert device["rssi"] == 0


def test_overview_carries_the_sim_label(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({"type": "status", "seq": 1, "device": "a", "ts": _minutes_ago(1)})
        ws.receive_json()

        sim = next(
            s for s in admin.get("/api/sims").json()
            if s["iccid"] == "89860622180012345670"
        )
        admin.patch(f"/api/sims/{sim['id']}", json={"label": "移动主卡"})

        device = next(d for d in admin.get("/api/overview").json()["devices"]
                      if d["name"] == "a")
        assert device["sim_label"] == "移动主卡"
        assert device["iccid"] == "89860622180012345670"


def test_agent_token_is_readable_for_setup(admin):
    assert admin.get("/api/system/agent-token").json()["token"] == "test-token"


def test_agent_token_rotation_supports_a_bounded_grace_period(admin):
    state = admin.app.state.hub
    response = admin.post(
        "/api/system/agent-token/rotate", json={"grace_minutes": 5}
    )
    assert response.status_code == 200
    replacement = response.json()["token"]
    assert replacement != "test-token"
    assert state.settings.token_path.read_text().strip() == replacement
    assert state.gateway.authenticate(f"Bearer {replacement}")
    assert state.gateway.authenticate("Bearer test-token")

    second = admin.post(
        "/api/system/agent-token/rotate", json={"grace_minutes": 0}
    ).json()["token"]
    assert state.gateway.authenticate(f"Bearer {second}")
    assert not state.gateway.authenticate(f"Bearer {replacement}")
    assert not state.gateway.authenticate("Bearer test-token")


def test_agent_token_file_is_never_briefly_world_readable(tmp_path):
    """The token is a bearer credential, so it must be born private.

    Creating the file and then chmod-ing it leaves a window in which any local
    user can read it; under a 0022 umask that window is real.
    """
    import os

    previous = os.umask(0o022)  # the permissive umask that made the window real
    try:
        settings = Settings(data_dir=tmp_path)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.ensure_agent_token()

        assert settings.token_path.stat().st_mode & 0o777 == 0o600

        # A rewrite tightens a file that was already loose, and truncates
        # rather than leaving a longer previous token's tail behind.
        settings.token_path.chmod(0o644)
        settings.replace_agent_token("L" * 80)
        settings.replace_agent_token("short-token")
        assert settings.token_path.stat().st_mode & 0o777 == 0o600
        assert settings.token_path.read_text() == "short-token\n"
        assert list(tmp_path.glob(".agent_token*")) == []
    finally:
        os.umask(previous)


def test_environment_controlled_agent_token_cannot_rotate_online(admin):
    admin.app.state.hub.settings.agent_token_from_env = True
    response = admin.post(
        "/api/system/agent-token/rotate", json={"grace_minutes": 5}
    )
    assert response.status_code == 409
    assert "HUB_AGENT_TOKEN" in response.json()["detail"]


def test_operations_diagnostics_and_audit_are_available_to_admin(admin):
    diagnostics = admin.get("/api/operations/diagnostics")
    assert diagnostics.status_code == 200
    body = diagnostics.json()
    assert body["server"]["version"] == "0.1.0"
    assert body["runtime"]["agents_connected"] == 0
    assert body["storage"]["disk_free_bytes"] > 0

    admin.post("/api/system/purge")
    events = admin.get("/api/operations/audit").json()
    assert any(event["action"] == "POST /api/system/purge" for event in events)
    assert "hunter2hunter" not in json.dumps(events)


def test_incidents_can_be_acknowledged_and_resolved(admin):
    incident = admin.app.state.hub.db.open_incident(
        "test:device-a",
        kind="test",
        severity="warning",
        source="device-a",
        title="test incident",
        detail="safe detail",
    )

    open_rows = admin.get("/api/operations/incidents").json()
    assert [row["id"] for row in open_rows] == [incident["id"]]

    acknowledged = admin.put(
        f"/api/operations/incidents/{incident['id']}",
        json={"status": "acknowledged"},
    ).json()
    assert acknowledged["status"] == "acknowledged"
    assert acknowledged["acknowledged_at"] is not None

    resolved = admin.put(
        f"/api/operations/incidents/{incident['id']}",
        json={"status": "resolved"},
    ).json()
    assert resolved["status"] == "resolved"
    assert admin.get("/api/operations/incidents").json() == []
    assert admin.get("/api/operations/incidents?status=all").json()[0]["id"] == incident["id"]


def test_network_registration_incident_tracks_recovery(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "status", "seq": 1, "device": "a", "online": True,
            "registered": False, "ts": _minutes_ago(1),
        })
        ws.receive_json()
        assert admin.get("/api/operations/incidents").json()[0]["kind"] == "network_unregistered"

        ws.send_json({
            "type": "status", "seq": 2, "device": "a", "online": True,
            "registered": True, "ts": _minutes_ago(0),
        })
        ws.receive_json()

    assert admin.get("/api/operations/incidents").json() == []


def test_failed_sends_aggregate_per_module_and_recover(admin):
    """A failed send must not leave an incident nothing can ever close.

    Fingerprinting on message_id opened a fresh permanently-active incident per
    failure, so a card with no balance would light the nav badge forever and an
    admin could only clear it by hand, one row at a time.
    """
    with _connect(admin) as ws:
        _greet(ws)
        for seq in (1, 2, 3):
            ws.send_json({
                "type": "sms_out", "seq": seq, "device": "a",
                "peer": "10086", "body": f"attempt {seq}", "status": "failed",
                "error": "CMS ERROR 500", "ts": _minutes_ago(3 - seq),
            })
            ws.receive_json()

        open_rows = admin.get("/api/operations/incidents").json()
        assert len(open_rows) == 1
        assert open_rows[0]["kind"] == "sms_send_failed"
        assert open_rows[0]["occurrences"] == 3

        ws.send_json({
            "type": "sms_out", "seq": 4, "device": "a", "peer": "10086",
            "body": "finally", "status": "sent", "ts": _minutes_ago(0),
        })
        ws.receive_json()

    assert admin.get("/api/operations/incidents").json() == []


def test_going_offline_closes_the_module_specific_incidents(admin):
    """device_offline already speaks for a module that is gone.

    An unregistered or failing module that then drops off would otherwise strand
    its incidents: neither can recover while the module is offline.
    """
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "status", "seq": 1, "device": "a", "online": True,
            "registered": False, "ts": _minutes_ago(2),
        })
        ws.receive_json()
        ws.send_json({
            "type": "sms_out", "seq": 2, "device": "a", "peer": "10086",
            "body": "x", "status": "failed", "ts": _minutes_ago(2),
        })
        ws.receive_json()
        assert len(admin.get("/api/operations/incidents").json()) == 2

        # A real offline notification need not restate registration at all —
        # the module is gone, so there is nothing to report about it.
        ws.send_json({
            "type": "status", "seq": 3, "device": "a", "online": False,
            "ts": _minutes_ago(0),
        })
        ws.receive_json()

    kinds = {row["kind"] for row in admin.get("/api/operations/incidents").json()}
    assert "network_unregistered" not in kinds
    assert "sms_send_failed" not in kinds


# --------------------------------------------------------------------------
# operational retention
# --------------------------------------------------------------------------


def test_unmatched_api_paths_are_not_audited(admin):
    """The audit middleware runs ahead of authentication.

    Without this, anyone who can reach the port could append a row per request
    by spraying invented paths, and the table has no natural ceiling.
    """
    db = admin.app.state.hub.db
    before = db.one("SELECT COUNT(*) AS n FROM audit_events")["n"]

    for index in range(20):
        admin.post(f"/api/not-a-route-{index}", json={})
        admin.put(f"/api/nope/{index}", json={})
        admin.delete(f"/api/nothing/{index}")

    assert db.one("SELECT COUNT(*) AS n FROM audit_events")["n"] == before

    # A real route still records, including when it rejects: a failed login is
    # exactly what an audit trail is for.
    admin.post("/api/auth/login", json={"password": "wrong-password"})
    rows = db.query("SELECT action, status FROM audit_events ORDER BY id DESC LIMIT 1")
    assert rows[0]["action"] == "POST /api/auth/login"
    assert rows[0]["status"] == "rejected"

    # So does a real route that answers 404.
    admin.put("/api/operations/incidents/999999", json={"status": "resolved"})
    rows = db.query("SELECT action, status FROM audit_events ORDER BY id DESC LIMIT 1")
    assert rows[0]["action"] == "PUT /api/operations/incidents/999999"
    assert rows[0]["status"] == "rejected"


def test_purge_bounds_every_append_only_table(admin):
    """Each operational table needs a horizon of its own.

    Only notify_logs rows tied to a message follow it out via ON DELETE
    CASCADE; task receipts and channel tests carry no message_id, and
    agent_logs, task_logs, audit_events and closed incidents were unbounded.
    """
    db = admin.app.state.hub.db
    old = _minutes_ago(400 * 24 * 60)

    db.execute(
        "INSERT INTO agent_logs (agent_id, device, level, message, ts) "
        "VALUES ('a', 'a', 'warning', 'ancient', ?)", (old,),
    )
    db.execute(
        "INSERT INTO task_logs (task_id, ts, status) VALUES (NULL, ?, 'ok')", (old,)
    )
    # message_id NULL: a task receipt or channel test, with nothing to cascade
    # from.
    db.execute(
        "INSERT INTO notify_logs (message_id, status, attempts, detail, ts) "
        "VALUES (NULL, 'ok', 1, '', ?)", (old,),
    )
    db.execute("INSERT INTO audit_events (ts, action) VALUES (?, 'ancient')", (old,))

    db.open_incident("closed", kind="test", severity="warning", source="s",
                     title="closed long ago")
    db.resolve_incident("closed", detail="done")
    db.execute("UPDATE incidents SET resolved_at = ? WHERE fingerprint = 'closed'", (old,))
    db.open_incident("still-open", kind="test", severity="critical", source="s",
                     title="unresolved and old")
    db.execute(
        "UPDATE incidents SET first_seen_at = ?, last_seen_at = ? "
        "WHERE fingerprint = 'still-open'", (old, old),
    )

    removed = db.purge(
        message_days=90, status_days=30, log_days=30,
        audit_days=180, incident_days=90,
    )
    assert removed["agent_logs"] == 1
    assert removed["task_logs"] == 1
    assert removed["notify_logs"] == 1
    assert removed["audit_events"] == 1
    assert removed["incidents"] == 1

    # An unresolved incident stays however old it is — it is still the truth
    # about the system.
    assert db.one(
        "SELECT status FROM incidents WHERE fingerprint = 'still-open'"
    )["status"] == "active"


def test_audit_row_cap_keeps_the_newest_rows(admin):
    """Age alone cannot bound a table an unauthenticated caller can append to."""
    db = admin.app.state.hub.db
    db.execute("DELETE FROM audit_events")
    for index in range(50):
        db.record_audit(f"POST /api/thing/{index}", status="ok")

    removed = db.purge(message_days=0, status_days=0, audit_max_rows=10)

    assert removed["audit_events"] == 40
    remaining = db.query("SELECT action FROM audit_events ORDER BY id")
    assert len(remaining) == 10
    assert remaining[-1]["action"] == "POST /api/thing/49"
    assert remaining[0]["action"] == "POST /api/thing/40"


def test_purge_endpoint_reports_every_table(admin):
    body = admin.post("/api/system/purge").json()
    for table in (
        "messages", "status", "ingested", "agent_logs", "task_logs",
        "notify_logs", "audit_events", "incidents",
    ):
        assert table in body


# --------------------------------------------------------------------------
# frontend hosting
# --------------------------------------------------------------------------


def test_app_starts_with_a_frontend_bundle_present(tmp_path, monkeypatch):
    """The SPA catch-all only runs when the bundle exists.

    Without this, the whole class of failures in that branch is invisible to
    the suite and only shows up in the built container.
    """
    import hub_server.main as main_module

    bundle = tmp_path / "www"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text("<!doctype html><title>hub</title>")
    (bundle / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(main_module, "FRONTEND_DIR", bundle)

    settings = Settings(data_dir=tmp_path / "data", agent_token="t")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = main_module.create_app(settings)

    with TestClient(app) as client:
        assert client.get("/").text.startswith("<!doctype html>")
        # Unknown client-side routes fall through to index.html…
        assert client.get("/messages").text.startswith("<!doctype html>")
        # …but the catch-all must never shadow the API or the socket.
        assert client.get("/api/nope").status_code == 404
        assert client.get("/healthz").json()["ok"] is True
        assert client.get("/assets/app.js").status_code == 200


def test_spa_catch_all_does_not_escape_the_bundle(tmp_path, monkeypatch):
    import hub_server.main as main_module

    bundle = tmp_path / "www"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text("<!doctype html><title>hub</title>")
    (tmp_path / "secret.txt").write_text("not for the web")
    monkeypatch.setattr(main_module, "FRONTEND_DIR", bundle)

    settings = Settings(data_dir=tmp_path / "data2", agent_token="t")
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    with TestClient(main_module.create_app(settings)) as client:
        response = client.get("/../secret.txt")
        assert "not for the web" not in response.text


# --------------------------------------------------------------------------
# backup and restore
# --------------------------------------------------------------------------


def test_backup_restore_round_trip(admin):
    """A backup this server produced must be accepted back.

    The happy path was the one nobody exercised: ``validate_backup`` reached
    its table check only for files that pass the integrity check, so every
    *genuine* backup hit it and every malformed upload was rejected earlier.
    A 500 on the one input that must work is invisible without this test.
    """
    snapshot = admin.get("/api/system/backup")
    assert snapshot.status_code == 200
    assert len(snapshot.content) > 0

    restored = admin.post(
        "/api/system/restore",
        content=snapshot.content,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["ok"] is True

    # The session and the data survive the swap.
    assert admin.get("/api/overview").status_code == 200


def test_restore_rejects_unrelated_sqlite(admin, tmp_path):
    """A valid SQLite file that is not a hub database is refused."""
    import sqlite3

    other = tmp_path / "unrelated.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE greetings (word TEXT)")
    conn.commit()
    conn.close()

    response = admin.post(
        "/api/system/restore",
        content=other.read_bytes(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 400
    assert "缺少表" in response.json()["detail"]


def test_restore_rejects_garbage_and_empty_uploads(admin):
    """Neither a non-database blob nor an empty body may reach the live data."""
    garbage = admin.post(
        "/api/system/restore",
        content=b"this is definitely not a sqlite file" * 32,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert garbage.status_code == 400

    empty = admin.post(
        "/api/system/restore",
        content=b"",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert empty.status_code == 400
    assert "未收到备份文件" in empty.json()["detail"]

    # The server is still serving after both rejections.
    assert admin.get("/api/overview").status_code == 200
