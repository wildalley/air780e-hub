"""Server tests: auth, ingest idempotency, and the API surface."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from hub_server.config import Settings
from hub_server.db import Database
from hub_server.gateway import Gateway
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


def test_calendar_today_uses_the_configured_timezone(tmp_path):
    east = Settings(data_dir=tmp_path, timezone="Pacific/Kiritimati")
    west = Settings(data_dir=tmp_path, timezone="Pacific/Pago_Pago")
    assert east.calendar_today() == datetime.now(
        ZoneInfo("Pacific/Kiritimati")
    ).date()
    assert west.calendar_today() == datetime.now(
        ZoneInfo("Pacific/Pago_Pago")
    ).date()
    assert east.calendar_today() > west.calendar_today()


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
    "protocol_version": 1,
    "last_seq": 0,
    "devices": [
        {"name": "a", "label": "移动卡", "port": "/dev/air780e-a", "online": True,
         "iccid": "89860622180012345670", "imei": "111", "model": "AirM2M_780E",
         "operator": "CHINA MOBILE", "smsc": "+8613800210500", "registered": True,
         "radio_enabled": True},
        {"name": "b", "label": "联通卡", "port": "/dev/air780e-b", "online": True,
         "iccid": "89860622180012345671", "imei": "222", "model": "AirM2M_780E",
         "operator": "CHINA UNICOM", "smsc": "+8613010200500", "registered": True,
         "radio_enabled": True},
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


def _items(response) -> list[dict]:
    """The rows out of a paged list response."""
    return response.json()["items"]


class _AckSocket:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_text(self, raw: str) -> None:
        self.frames.append(json.loads(raw))


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

    agent = admin.app.state.hub.db.one(
        "SELECT version, protocol_version FROM agents WHERE id = 'test-agent'"
    )
    assert agent == {"version": "0.1.0", "protocol_version": 1}


def test_agent_version_mismatch_opens_and_then_resolves_an_incident(admin):
    incompatible = {
        **HELLO,
        "version": "0.0.9",
        "protocol_version": 99,
    }
    with _connect(admin) as ws:
        ws.send_json(incompatible)
        assert ws.receive_json()["type"] == "sync_tasks"

        diagnostics = admin.get("/api/operations/diagnostics").json()
        agent = diagnostics["agents"][0]
        assert agent["version_matches"] is False
        assert agent["protocol_compatible"] is False
        incident = _items(admin.get("/api/operations/incidents"))[0]
        assert incident["kind"] == "agent_version_mismatch"
        assert incident["severity"] == "critical"
        assert "协议 99" in incident["detail"]

    with _connect(admin) as ws:
        _greet(ws)
        diagnostics = admin.get("/api/operations/diagnostics").json()
        agent = diagnostics["agents"][0]
        assert agent["version_matches"] is True
        assert agent["protocol_compatible"] is True

    assert _items(admin.get("/api/operations/incidents")) == []


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


def test_the_pdu_and_dcs_the_agent_sends_are_kept(admin):
    """These are what make a garbled message diagnosable after the fact.

    The agent had always sent ``pdu``; the server dropped it. Once the modem's
    own copy is deleted (``delete_after_read`` defaults on) the decoded body was
    all that survived, and a body alone cannot tell a decoder bug from a payload
    that was never text.
    """
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_in", "seq": 11, "device": "a",
            "iccid": "89860622180012345670", "peer": "10086",
            "body": "验证码 123456", "ts": "2026-08-02T18:00:00+08:00",
            "segments": 1,
            "pdu": "0791261010101010040C91261019283746000052806151713140",
            "dcs": 0,
        })
        assert ws.receive_json() == {"type": "ack", "seq": 11}

    row = admin.app.state.hub.db.one(
        "SELECT raw_pdu, dcs, is_binary FROM messages WHERE peer = '10086'"
    )
    assert row["raw_pdu"].startswith("079126101010")
    assert row["dcs"] == 0
    assert row["is_binary"] == 0


def test_a_binary_sms_is_stored_flagged_and_previewed_as_data(admin):
    """An operator data SMS must not read as a message someone sent.

    This is the giffgaff case: an OTA payload decoded as text produced a wall of
    mojibake in the conversation, and one that decoded to nothing produced a
    blank bubble with "(空)" in the list.
    """
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_in", "seq": 12, "device": "a",
            "iccid": "89860622180012345670", "peer": "giffgaff",
            "body": "鼠S耸盘涌羹",          # what the text decode produced
            "ts": "2026-08-06T21:15:24+08:00",
            "segments": 1,
            "pdu": "0705912143F5040BC8329BFD0600F5",
            "dcs": 4,
            "binary": True,
        })
        assert ws.receive_json() == {"type": "ack", "seq": 12}

    items = admin.get("/api/messages").json()["items"]
    assert items[0]["is_binary"] == 1
    # The body is kept rather than blanked: the raw PDU plus what the decode
    # produced is the whole evidence trail. The UI is what stops showing it.
    assert items[0]["body"] == "鼠S耸盘涌羹"
    assert items[0]["dcs"] == 4

    # The conversation list carries the flag too, or its preview still shows
    # the mojibake even once the bubble stops.
    thread = admin.get("/api/conversations").json()[0]
    assert thread["last_is_binary"] == 1


def test_server_reclassifies_a_data_pdu_from_an_older_agent(admin):
    """Server-side fallback covers the window before every Agent is upgraded."""
    pdu = "0791448720003023000ED0E7B4D97C0E9BCDDD00BA0E740E9BCD2E00"
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_in", "seq": 14, "device": "a",
            "iccid": "89860622180012345670", "peer": "giffgaff",
            "body": "", "ts": "2026-08-06T21:15:24+08:00",
            "segments": 1, "pdu": pdu, "dcs": 0,
            "binary": False,
        })
        assert ws.receive_json() == {"type": "ack", "seq": 14}

    row = admin.app.state.hub.db.one(
        "SELECT raw_pdu, is_binary FROM messages WHERE peer = 'giffgaff'"
    )
    assert row == {"raw_pdu": pdu, "is_binary": 1}


def test_a_frame_with_a_malformed_dcs_still_stores_the_message(admin):
    """A diagnostic column is not worth refusing a message over."""
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_in", "seq": 13, "device": "a",
            "iccid": "89860622180012345670", "peer": "10086",
            "body": "hello", "ts": "2026-08-02T18:00:00+08:00",
            "dcs": "not-a-number",
        })
        assert ws.receive_json() == {"type": "ack", "seq": 13}

    row = admin.app.state.hub.db.one("SELECT body, dcs FROM messages")
    assert row["body"] == "hello"
    assert row["dcs"] is None


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


async def test_failed_event_application_rolls_back_and_is_replayable(tmp_path):
    """A partial write must neither claim nor acknowledge the sequence."""
    settings = Settings(data_dir=tmp_path, agent_token="test-token")
    db = Database(settings.db_path)
    db.upsert_agent("test-agent", "0.1.0", 1, connected=True)
    gateway = Gateway(db, settings)
    socket = _AckSocket()
    event = {
        "type": "sms_in", "seq": 17, "device": "a",
        "iccid": "89860622180012345670", "peer": "10086",
        "body": "survives retry", "ts": "2026-08-02T18:00:00+08:00",
    }
    original = gateway._apply_sms_in
    attempts = 0

    def fail_once(agent_id: str, frame: dict) -> int:
        nonlocal attempts
        attempts += 1
        message_id = original(agent_id, frame)
        if attempts == 1:
            raise RuntimeError("injected failure after message insert")
        return message_id

    gateway._apply_sms_in = fail_once  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            await gateway._ingest("test-agent", event, socket)

        assert socket.frames == [], "a failed sequence must not be acknowledged"
        assert db.query("SELECT id FROM messages") == []
        assert db.query("SELECT seq FROM ingested") == []

        await gateway._ingest("test-agent", event, socket)
        await gateway._ingest("test-agent", event, socket)

        assert socket.frames == [
            {"type": "ack", "seq": 17},
            {"type": "ack", "seq": 17},
        ]
        assert len(db.query("SELECT id FROM messages")) == 1
        assert db.query("SELECT seq, kind FROM ingested") == [
            {"seq": 17, "kind": "sms_in"}
        ]
        assert db.one("SELECT last_seq FROM agents WHERE id = 'test-agent'")[
            "last_seq"
        ] == 17
    finally:
        db.close()


def test_failed_event_application_closes_the_ordered_stream(admin, monkeypatch):
    """The next frame must not produce a cumulative ACK past a failed one."""
    from starlette.websockets import WebSocketDisconnect

    gateway = admin.app.state.hub.gateway
    original = gateway._apply_sms_in

    def fail_after_insert(agent_id: str, frame: dict) -> int:
        original(agent_id, frame)
        raise RuntimeError("injected ordered-stream failure")

    monkeypatch.setattr(gateway, "_apply_sms_in", fail_after_insert)
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with _connect(admin) as ws:
            _greet(ws)
            ws.send_json({
                "type": "sms_in", "seq": 18, "device": "a",
                "iccid": "89860622180012345670", "peer": "10086",
                "body": "must roll back", "ts": "2026-08-02T18:00:00+08:00",
            })
            ws.receive_json()

    assert excinfo.value.code == 1011
    db = admin.app.state.hub.db
    assert db.query("SELECT id FROM messages WHERE seq = 18") == []
    assert db.query("SELECT seq FROM ingested WHERE seq = 18") == []


def test_lost_ack_replay_survives_repeated_server_restarts(settings, fault_cycles):
    """Persist once, lose its ACK, then replay across fresh Server processes."""
    event = {
        "type": "sms_in", "seq": 23, "device": "a",
        "iccid": "89860622180012345670", "peer": "10086",
        "body": "one durable copy", "ts": "2026-08-02T18:00:00+08:00",
    }

    first_app = create_app(settings)
    with TestClient(first_app) as first:
        with _connect(first) as ws:
            _greet(ws)
            ws.send_json(event)
            # Wait for the durable write but deliberately never consume the
            # ACK from this connection, exactly as a network cut would do.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if first.app.state.hub.db.one(
                    "SELECT COUNT(*) AS n FROM ingested WHERE agent_id = ? AND seq = ?",
                    ("test-agent", event["seq"]),
                )["n"] == 1:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("event was not persisted before the injected disconnect")

    for _cycle in range(fault_cycles):
        restarted_app = create_app(settings)
        with TestClient(restarted_app) as restarted:
            with _connect(restarted) as ws:
                _greet(ws)
                ws.send_json(event)
                assert ws.receive_json() == {"type": "ack", "seq": event["seq"]}

            db = restarted.app.state.hub.db
            assert db.one("SELECT COUNT(*) AS n FROM messages")["n"] == 1
            assert db.one("SELECT COUNT(*) AS n FROM ingested")["n"] == 1
            assert db.one("SELECT last_seq FROM agents WHERE id = 'test-agent'")[
                "last_seq"
            ] == event["seq"]


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


def test_conversation_preview_breaks_equal_timestamp_ties_by_message_id(admin):
    db = admin.app.state.hub.db
    stamp = _minutes_ago(1)
    db.insert_message(
        agent_id="home-arch", device="a", direction="in", peer="10086",
        body="先到的短信", ts=stamp, iccid="89860622180012345670",
    )
    db.insert_message(
        agent_id="home-arch", device="a", direction="in", peer="10086",
        body="后到的短信", ts=stamp, iccid="89860622180012345670",
    )

    thread = admin.get("/api/conversations").json()[0]
    assert thread["last_body"] == "后到的短信"
    assert thread["message_count"] == 2


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
        "is_binary", "dcs", "raw_pdu",
    ]
    assert rows[1][5:7] == ["10086", body]
    # New diagnostic columns are present; values are empty for a pre-upgrade row.
    assert rows[1][8] == "0"    # is_binary
    assert rows[1][9] == ""     # dcs (None → "")
    assert rows[1][10] == ""    # raw_pdu (None → "")


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


def test_message_content_filter_separates_text_and_data_sms(admin):
    db = admin.app.state.hub.db
    common = {
        "agent_id": "home-arch", "device": "a", "direction": "in",
        "peer": "giffgaff", "ts": _minutes_ago(1),
        "iccid": "89860622180012345670",
    }
    db.insert_message(**common, body="plain text")
    db.insert_message(**common, body="鼠S耸盘涌羹", is_binary=True)

    text = admin.get("/api/messages?content=text").json()
    data = admin.get("/api/messages?content=data").json()
    assert text["total"] == 1
    assert [row["body"] for row in text["items"]] == ["plain text"]
    assert data["total"] == 1
    assert [row["is_binary"] for row in data["items"]] == [1]

    text_threads = admin.get("/api/conversations?content=text").json()
    data_threads = admin.get("/api/conversations?content=data").json()
    assert text_threads[0]["last_body"] == "plain text"
    assert text_threads[0]["message_count"] == 1
    assert text_threads[0]["unread_count"] == 1
    assert data_threads[0]["last_is_binary"] == 1
    assert data_threads[0]["message_count"] == 1
    assert data_threads[0]["unread_count"] == 1

    text_export = admin.get("/api/messages/export?content=text").text
    data_export = admin.get("/api/messages/export?content=data").text
    assert "plain text" in text_export and "鼠S耸盘涌羹" not in text_export
    assert "鼠S耸盘涌羹" in data_export and "plain text" not in data_export

    assert admin.get("/api/messages?content=unknown").status_code == 422


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


def test_a_thread_window_can_grow_to_reach_older_messages(admin):
    """The thread view reads history back by asking for a bigger window.

    It grows one window rather than paging: a transcript has no page boundary
    to land on, and an offset page could gap or repeat if an SMS arrives while
    the operator is scrolling back.
    """
    db = admin.app.state.hub.db
    seen = _minutes_ago(60)
    sim = db.execute(
        "INSERT INTO sims (iccid, label, first_seen_at, last_seen_at) "
        "VALUES ('8986062218001234567', 'card', ?, ?)",
        (seen, seen),
    ).lastrowid
    for index in range(30):
        stamp = _minutes_ago(30 - index)
        db.execute(
            "INSERT INTO messages "
            "(agent_id, device, sim_id, direction, peer, body, ts, status, created_at) "
            "VALUES ('agent-a', 'a', ?, 'in', '10086', ?, ?, 'received', ?)",
            (sim, f"msg-{index:02d}", stamp, stamp),
        )

    base = f"/api/messages?peer=10086&sim_id={sim}"
    first = admin.get(f"{base}&limit=10").json()
    # total describes the thread, not the window — it is what tells the UI
    # there is more to reach for.
    assert first["total"] == 30
    assert [m["body"] for m in first["items"]] == [
        f"msg-{index:02d}" for index in reversed(range(20, 30))
    ]

    # A bigger window reaches further back and still ends at the newest.
    grown = admin.get(f"{base}&limit=25").json()
    assert grown["total"] == 30
    assert len(grown["items"]) == 25
    assert grown["items"][0]["body"] == "msg-29"
    assert grown["items"][-1]["body"] == "msg-05"


def test_the_thread_window_cap_allows_a_long_conversation(admin):
    """2000 is the bound the thread view grows into; past it must still fail."""
    assert admin.get("/api/messages?limit=2000").status_code == 200
    assert admin.get("/api/messages?limit=2001").status_code == 422


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

    # A short window buckets at 30s, below the 1-minute spacing of these
    # samples, so each is still its own point and the raw values read back.
    history = admin.get("/api/devices/a/history?hours=1").json()
    assert [row["rssi"] for row in history] == [24, 18, 11]
    histories = admin.get("/api/devices/history?hours=1").json()
    assert [row["rssi"] for row in histories["a"]] == [24, 18, 11]
    assert histories["b"] == []


def test_history_bucket_width_bounds_the_row_count():
    """Every window must fit the point target, and widen monotonically."""
    from hub_server.api import (
        HISTORY_BUCKETS,
        HISTORY_TARGET_POINTS,
        history_bucket_seconds,
    )

    previous = 0
    for hours in (1, 3, 6, 12, 24, 24 * 7, 24 * 30):
        width = history_bucket_seconds(hours)
        assert hours * 3600 / width <= HISTORY_TARGET_POINTS, hours
        assert width >= previous, "a longer window must not bucket finer"
        previous = width

    # Short windows stay effectively raw: modules report every ~30s.
    assert history_bucket_seconds(1) == 30
    # The longest window the API accepts still has to land on the ladder.
    assert history_bucket_seconds(24 * 30) in HISTORY_BUCKETS


def test_history_collapses_a_long_window_into_buckets(admin):
    """A 7-day window must not return one point per stored sample.

    The regression this guards: the endpoint used to return every row in the
    window, so ten modules reporting every 30s produced a six-figure response
    that the dashboard rebuilt every 15 seconds.
    """
    from hub_server.api import history_bucket_seconds

    width = history_bucket_seconds(24 * 7)
    assert width >= 1800, "a 7-day window should bucket coarsely"

    # Twelve samples one minute apart, all inside a single bucket.
    with _connect(admin) as ws:
        _greet(ws)
        for seq in range(1, 13):
            ws.send_json({
                "type": "status", "seq": seq, "device": "a", "online": True,
                "registered": True, "rssi": 20, "dbm": -70,
                "bars": 3, "storage_used": 0, "storage_capacity": 50,
                "ts": _minutes_ago(seq),
            })
            ws.receive_json()

    points = admin.get("/api/devices/a/history?hours=168").json()
    assert 0 < len(points) <= 2, f"12 samples collapsed to {len(points)} points"


def test_history_bucket_keeps_an_outage_and_the_storage_peak(admin):
    """Aggregation must not smooth away the things an operator looks for.

    A module that dropped out inside a bucket has to read as down, and storage
    has to read as its high-water mark — an average would hide both.
    """
    with _connect(admin) as ws:
        _greet(ws)
        samples = [
            (1, True, True, 10),
            (2, False, False, 90),   # the outage, and the storage peak
            (3, True, True, 20),
        ]
        for seq, online, registered, used in samples:
            ws.send_json({
                "type": "status", "seq": seq, "device": "a",
                "online": online, "registered": registered,
                "rssi": 20, "dbm": -70, "bars": 3,
                "storage_used": used, "storage_capacity": 100,
                "ts": _minutes_ago(seq),
            })
            ws.receive_json()

    # One bucket wide enough to hold all three samples.
    points = admin.get("/api/devices/a/history?hours=168").json()
    assert len(points) >= 1
    collapsed = points[0]
    assert collapsed["online"] == 0, "an outage inside the bucket must survive"
    assert collapsed["registered"] == 0
    assert collapsed["storage_used"] == 90, "storage must read as the peak"


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

    logs = _items(admin.get("/api/logs"))
    assert logs[0]["message"] == "storage 48/50, draining"
    assert logs[0]["level"] == "warning"


def test_device_recovery_logs_open_escalate_and_resolve_an_incident(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "log", "seq": 1, "device": "a", "level": "warning",
            "message": "automatic recovery started: operator_reselect",
            "event": "device_recovery", "action": "operator_reselect",
            "outcome": "started", "reason": "unregistered for 300s", "attempt": 1,
        })
        assert ws.receive_json() == {"type": "ack", "seq": 1}

        incident = _items(admin.get("/api/operations/incidents"))[0]
        assert incident["kind"] == "device_recovery"
        assert incident["severity"] == "warning"
        assert "自动选择运营商" in incident["detail"]

        ws.send_json({
            "type": "log", "seq": 2, "device": "a", "level": "error",
            "message": "automatic registration recovery reached its 24-hour limit",
            "event": "device_recovery", "action": "registration_recovery",
            "outcome": "exhausted", "reason": "6 actions in 24 hours", "attempt": 6,
        })
        assert ws.receive_json() == {"type": "ack", "seq": 2}
        incident = _items(admin.get("/api/operations/incidents"))[0]
        assert incident["severity"] == "critical"
        assert "限频" in incident["title"]

        ws.send_json({
            "type": "log", "seq": 3, "device": "a", "level": "info",
            "message": "automatic recovery succeeded: registration_watch",
            "event": "device_recovery", "action": "registration_watch",
            "outcome": "succeeded", "reason": "network registration restored",
            "attempt": 6,
        })
        assert ws.receive_json() == {"type": "ack", "seq": 3}

    assert _items(admin.get("/api/operations/incidents")) == []
    closed = _items(admin.get("/api/operations/incidents?status=all"))[0]
    assert closed["kind"] == "device_recovery"
    assert closed["status"] == "resolved"
    assert len(_items(admin.get("/api/logs"))) == 3


def test_delivery_report_updates_a_single_segment_message(admin):
    sent_at = _minutes_ago(1)
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_out", "seq": 1, "device": "a", "peer": "10086",
            "body": "CXHF", "status": "sent", "refs": [7], "ts": sent_at,
        })
        assert ws.receive_json() == {"type": "ack", "seq": 1}
        assert admin.get("/api/messages").json()["items"][0]["status"] == "pending"

        ws.send_json({
            "type": "sms_delivery", "seq": 2, "device": "a", "reference": 7,
            "peer": "10086", "status": "delivered", "status_code": 0,
            "service_center_ts": sent_at, "discharge_ts": _minutes_ago(0),
            "ts": _minutes_ago(0), "pdu": "000207",
        })
        assert ws.receive_json() == {"type": "ack", "seq": 2}

    message = admin.get("/api/messages").json()["items"][0]
    assert message["status"] == "delivered"
    assert message["error"] is None
    segment = admin.app.state.hub.db.one("SELECT * FROM sms_delivery_segments")
    assert segment["message_id"] == message["id"]
    assert segment["segment_index"] == 1
    assert segment["modem_reference"] == 7
    assert segment["status"] == "delivered"
    assert segment["status_code"] == 0
    assert segment["raw_pdu"] == "000207"


def test_temporary_report_does_not_overwrite_a_terminal_receipt(admin):
    sent_at = _minutes_ago(2)
    delivered_at = _minutes_ago(1)
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_out", "seq": 1, "device": "a", "peer": "10086",
            "body": "CXHF", "status": "sent", "refs": [8], "ts": sent_at,
        })
        ws.receive_json()
        ws.send_json({
            "type": "sms_delivery", "seq": 2, "device": "a", "reference": 8,
            "peer": "10086", "status": "delivered", "status_code": 0,
            "service_center_ts": sent_at, "discharge_ts": delivered_at,
            "ts": delivered_at, "pdu": "final-report",
        })
        ws.receive_json()
        terminal = admin.app.state.hub.db.one(
            "SELECT * FROM sms_delivery_segments"
        )

        # Some networks replay an older temporary TP-ST after the final report.
        # It must not leave terminal status paired with temporary metadata.
        ws.send_json({
            "type": "sms_delivery", "seq": 3, "device": "a", "reference": 8,
            "peer": "10086", "status": "pending", "status_code": 0x20,
            "service_center_ts": sent_at, "discharge_ts": None,
            "ts": _minutes_ago(0), "pdu": "stale-temporary-report",
        })
        assert ws.receive_json() == {"type": "ack", "seq": 3}

    assert admin.app.state.hub.db.one(
        "SELECT * FROM sms_delivery_segments"
    ) == terminal
    message = admin.get("/api/messages").json()["items"][0]
    assert message["status"] == "delivered"
    assert message["error"] is None


def test_multipart_delivery_aggregates_pending_partial_and_delivered(admin):
    sent_at = _minutes_ago(1)
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_out", "seq": 1, "device": "a", "peer": "10086",
            "body": "x" * 400, "status": "sent", "refs": [10, 11], "ts": sent_at,
        })
        ws.receive_json()

        ws.send_json({
            "type": "sms_delivery", "seq": 2, "device": "a", "reference": 10,
            "peer": "10086", "status": "delivered", "status_code": 0,
            "service_center_ts": sent_at, "ts": _minutes_ago(0),
        })
        ws.receive_json()
        assert admin.get("/api/messages").json()["items"][0]["status"] == "partial"

        # A temporary service-centre error leaves the outstanding segment pending.
        ws.send_json({
            "type": "sms_delivery", "seq": 3, "device": "a", "reference": 11,
            "peer": "10086", "status": "pending", "status_code": 0x20,
            "service_center_ts": sent_at, "ts": _minutes_ago(0),
        })
        ws.receive_json()
        assert admin.get("/api/messages").json()["items"][0]["status"] == "partial"

        ws.send_json({
            "type": "sms_delivery", "seq": 4, "device": "a", "reference": 11,
            "peer": "10086", "status": "delivered", "status_code": 0,
            "service_center_ts": sent_at, "ts": _minutes_ago(0),
        })
        ws.receive_json()

    assert admin.get("/api/messages").json()["items"][0]["status"] == "delivered"


def test_delivery_status_code_overrides_an_incorrect_claimed_state(admin):
    sent_at = _minutes_ago(1)
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_out", "seq": 1, "device": "a", "peer": "10086",
            "body": "CXHF", "status": "sent", "refs": [12], "ts": sent_at,
        })
        ws.receive_json()
        ws.send_json({
            "type": "sms_delivery", "seq": 2, "device": "a", "reference": 12,
            "peer": "10086", "status": "delivered", "status_code": 0x40,
            "service_center_ts": sent_at, "ts": _minutes_ago(0),
        })
        ws.receive_json()

    message = admin.get("/api/messages").json()["items"][0]
    assert message["status"] == "failed"
    assert "TP-ST 0x40" in message["error"]


def test_delivery_report_that_arrives_before_sms_out_is_reconciled(admin):
    sent_at = _minutes_ago(1)
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_delivery", "seq": 1, "device": "a", "reference": 42,
            "peer": "+8613800138000", "status": "delivered", "status_code": 0,
            "service_center_ts": sent_at, "ts": _minutes_ago(0),
        })
        ws.receive_json()
        unmatched = admin.app.state.hub.db.one("SELECT * FROM sms_delivery_segments")
        assert unmatched["message_id"] is None

        ws.send_json({
            "type": "sms_delivery", "seq": 2, "device": "a", "reference": 42,
            "peer": "+8613800138000", "status": "pending", "status_code": 0x20,
            "service_center_ts": sent_at, "ts": _minutes_ago(0),
            "pdu": "stale-temporary-report",
        })
        ws.receive_json()
        assert admin.app.state.hub.db.one(
            "SELECT * FROM sms_delivery_segments"
        ) == unmatched

        ws.send_json({
            "type": "sms_out", "seq": 3, "device": "a", "peer": "13800138000",
            "body": "CXHF", "status": "sent", "refs": [42], "ts": sent_at,
        })
        ws.receive_json()

    message = admin.get("/api/messages").json()["items"][0]
    assert message["status"] == "delivered"
    rows = admin.app.state.hub.db.query("SELECT * FROM sms_delivery_segments")
    assert len(rows) == 1
    assert rows[0]["message_id"] == message["id"]


def test_reused_modem_reference_matches_the_closest_submission_time(admin):
    older, newer = _minutes_ago(5), _minutes_ago(1)
    with _connect(admin) as ws:
        _greet(ws)
        for seq, stamp, body in ((1, older, "older"), (2, newer, "newer")):
            ws.send_json({
                "type": "sms_out", "seq": seq, "device": "a", "peer": "10086",
                "body": body, "status": "sent", "refs": [9], "ts": stamp,
            })
            ws.receive_json()

        ws.send_json({
            "type": "sms_delivery", "seq": 3, "device": "a", "reference": 9,
            "peer": "10086", "status": "delivered", "status_code": 0,
            "service_center_ts": older, "ts": _minutes_ago(0),
        })
        ws.receive_json()

    rows = admin.app.state.hub.db.query(
        "SELECT body, status FROM messages ORDER BY ts"
    )
    assert rows == [
        {"body": "older", "status": "delivered"},
        {"body": "newer", "status": "pending"},
    ]


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


def test_set_radio_routes_a_typed_command_to_the_owning_agent(admin, monkeypatch):
    gateway = admin.app.state.hub.gateway
    seen = {}

    monkeypatch.setattr(gateway, "agent_for_device", lambda name: "test-agent")

    async def fake_call(agent_id, frame, **_kwargs):
        seen.update(agent_id=agent_id, frame=frame)
        return {"radio_enabled": False, "registered": False}

    monkeypatch.setattr(gateway, "call", fake_call)

    response = admin.post("/api/devices/a/radio", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["radio_enabled"] is False
    assert seen == {
        "agent_id": "test-agent",
        "frame": {"type": "set_radio", "device": "a", "enabled": False},
    }


def test_set_radio_rejects_unknown_devices_and_non_booleans(admin):
    assert admin.post("/api/devices/nope/radio", json={"enabled": True}).status_code == 404
    assert admin.post("/api/devices/a/radio", json={"enabled": []}).status_code == 422


def test_set_radio_requires_an_admin_session(client):
    assert client.post("/api/devices/a/radio", json={"enabled": False}).status_code == 401


# --------------------------------------------------------------------------
# CRUD used by the web UI
# --------------------------------------------------------------------------


def test_sim_payg_details_and_lifecycle_dates_can_be_managed(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({"type": "status", "seq": 1, "device": "a"})
        ws.receive_json()

    sim_id = admin.get("/api/sims").json()[0]["id"]
    today = admin.app.state.hub.settings.calendar_today()
    expiry = (today + timedelta(days=20)).isoformat()
    activity_due = (today + timedelta(days=5)).isoformat()
    response = admin.patch(f"/api/sims/{sim_id}", json={
        "label": "移动主卡", "phone_number": "13800138000",
        "billing_type": "payg", "plan_name": "30GB 月包",
        "balance": "12.50", "low_balance_threshold": "10.00", "currency": "usd",
        "expires_at": expiry, "activity_due_at": activity_due,
    })
    assert response.status_code == 200
    assert response.json()["label"] == "移动主卡"
    assert response.json()["phone_number"] == "13800138000"
    assert response.json()["billing_type"] == "payg"
    assert response.json()["plan_name"] == "30GB 月包"
    assert response.json()["balance"] == "12.50"
    assert response.json()["low_balance_threshold"] == "10.00"
    assert response.json()["currency"] == "USD"
    balance_updated_at = response.json()["balance_updated_at"]
    assert datetime.fromisoformat(balance_updated_at).tzinfo is not None
    assert response.json()["expires_at"] == expiry
    assert response.json()["activity_due_at"] == activity_due

    incidents = _items(admin.get("/api/operations/incidents"))
    assert {incident["fingerprint"] for incident in incidents} == {
        f"sim-expiry:{sim_id}",
        f"sim-activity:{sim_id}",
    }
    by_fingerprint = {incident["fingerprint"]: incident for incident in incidents}
    assert by_fingerprint[f"sim-expiry:{sim_id}"]["severity"] == "warning"
    assert by_fingerprint[f"sim-activity:{sim_id}"]["kind"] == "sim_activity_due"
    assert by_fingerprint[f"sim-activity:{sim_id}"]["severity"] == "critical"

    unchanged = admin.patch(
        f"/api/sims/{sim_id}", json={"balance": "12.50", "expires_at": None}
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["balance_updated_at"] == balance_updated_at
    remaining = _items(admin.get("/api/operations/incidents"))
    assert [incident["fingerprint"] for incident in remaining] == [
        f"sim-activity:{sim_id}"
    ]

    low = admin.patch(f"/api/sims/{sim_id}", json={"balance": "8.00"})
    assert low.status_code == 200
    assert low.json()["balance"] == "8.00"
    remaining = _items(admin.get("/api/operations/incidents"))
    by_fingerprint = {incident["fingerprint"]: incident for incident in remaining}
    assert set(by_fingerprint) == {
        f"sim-activity:{sim_id}",
        f"sim-balance:{sim_id}",
    }
    assert by_fingerprint[f"sim-balance:{sim_id}"]["kind"] == "sim_low_balance"
    assert by_fingerprint[f"sim-balance:{sim_id}"]["severity"] == "warning"

    cleared = admin.patch(
        f"/api/sims/{sim_id}",
        json={
            "billing_type": None,
            "plan_name": None,
            "balance": None,
            "low_balance_threshold": None,
            "currency": None,
            "activity_due_at": None,
        },
    )
    assert cleared.status_code == 200
    assert cleared.json()["billing_type"] == "unknown"
    assert cleared.json()["plan_name"] == ""
    assert cleared.json()["balance"] is None
    assert cleared.json()["low_balance_threshold"] is None
    assert cleared.json()["currency"] == ""
    assert cleared.json()["balance_updated_at"] is None
    assert cleared.json()["expires_at"] is None
    assert cleared.json()["activity_due_at"] is None
    assert _items(admin.get("/api/operations/incidents")) == []


@pytest.mark.parametrize(
    ("field", "value", "unchanged"),
    (
        ("expires_at", "2026-02-30", None),
        ("activity_due_at", "2026-02-30", None),
        ("balance", "1e3", None),
        ("low_balance_threshold", "-1.00", None),
        ("currency", "US", ""),
        ("billing_type", "metered", "unknown"),
    ),
)
def test_sim_billing_rejects_invalid_values(admin, field, value, unchanged):
    with _connect(admin) as ws:
        _greet(ws)

    sim_id = admin.get("/api/sims").json()[0]["id"]
    response = admin.patch(f"/api/sims/{sim_id}", json={field: value})
    assert response.status_code == 422
    assert admin.get("/api/sims").json()[0][field] == unchanged


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


def test_task_can_be_triggered_manually(admin, monkeypatch):
    task = admin.post("/api/tasks", json={"device": "a", "name": "手动保号"}).json()
    gateway = admin.app.state.hub.gateway
    seen = []

    async def fake_push(agent_id):
        seen.append(("sync", agent_id))

    async def fake_call(agent_id, frame, **_kwargs):
        seen.append(("call", agent_id, frame))
        return {"task_id": task["id"], "status": "started"}

    monkeypatch.setattr(gateway, "agent_for_device", lambda _name: "test-agent")
    monkeypatch.setattr(gateway, "push_tasks", fake_push)
    monkeypatch.setattr(gateway, "call", fake_call)

    response = admin.post(f"/api/tasks/{task['id']}/run")
    assert response.status_code == 200
    assert response.json() == {"task_id": task["id"], "status": "started"}
    assert seen == [
        ("sync", "test-agent"),
        ("call", "test-agent", {"type": "run_task", "task_id": task["id"]}),
    ]


def test_running_an_unknown_task_is_404(admin):
    assert admin.post("/api/tasks/999/run").status_code == 404


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
        "protocol_version": 1,
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


def test_deliberate_flight_mode_is_stored_without_a_network_incident(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "status", "seq": 1, "device": "a", "online": True,
            "registered": False, "radio_enabled": False, "ts": _minutes_ago(1),
        })
        assert ws.receive_json() == {"type": "ack", "seq": 1}

    device = next(d for d in admin.get("/api/devices").json() if d["name"] == "a")
    assert device["radio_enabled"] == 0
    incidents = _items(admin.get("/api/operations/incidents?status=open"))
    assert not any(i["kind"] == "network_unregistered" for i in incidents)


def test_radio_on_but_unregistered_still_opens_a_network_incident(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "status", "seq": 1, "device": "a", "online": True,
            "registered": False, "radio_enabled": True, "ts": _minutes_ago(1),
        })
        ws.receive_json()

    incidents = _items(admin.get("/api/operations/incidents?status=open"))
    assert any(i["kind"] == "network_unregistered" for i in incidents)


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
    assert body["server"]["protocol_version"] == 1
    assert body["runtime"]["agents_connected"] == 0
    assert body["storage"]["disk_free_bytes"] > 0

    admin.post("/api/system/purge")
    events = _items(admin.get("/api/operations/audit"))
    assert any(event["action"] == "POST /api/system/purge" for event in events)
    assert "hunter2hunter" not in json.dumps(events)


def test_activity_stats_split_windows_and_scope_failures_to_outbound(admin):
    """The 24h window must not leak 7d rows, and inbound must not count as a
    send failure — the UI subtracts `failed` from `outbound`, so an inbound
    failure landing in that bucket would render a negative success rate."""
    db = admin.app.state.hub.db
    now = datetime.now(UTC)

    def at(hours: float) -> str:
        return (now - timedelta(hours=hours)).isoformat(timespec="seconds")

    # Inside 24h.
    db.insert_message(
        agent_id="a", device="d", direction="in", peer="10086",
        body="x", ts=at(1),
    )
    db.insert_message(
        agent_id="a", device="d", direction="out", peer="10086",
        body="x", ts=at(2), status="sent",
    )
    db.insert_message(
        agent_id="a", device="d", direction="out", peer="10086",
        body="x", ts=at(3), status="failed",
    )
    # Older than 24h but inside 7d.
    db.insert_message(
        agent_id="a", device="d", direction="in", peer="10086",
        body="x", ts=at(48),
    )
    # An inbound row marked failed: must stay out of the outbound failure bucket.
    db.insert_message(
        agent_id="a", device="d", direction="in", peer="10086",
        body="x", ts=at(4), status="failed",
    )
    # Older than 7d: outside both windows.
    db.insert_message(
        agent_id="a", device="d", direction="out", peer="10086",
        body="x", ts=at(24 * 9), status="sent",
    )

    stats = db.activity_stats()
    assert stats["messages"]["inbound"] == {"day": 2, "week": 3}
    assert stats["messages"]["outbound"] == {"day": 2, "week": 2}
    assert stats["messages"]["failed"] == {"day": 1, "week": 1}
    # The value the UI divides by must never go negative.
    assert (
        stats["messages"]["outbound"]["day"] - stats["messages"]["failed"]["day"] >= 0
    )

    assert stats["rows"]["messages"] == 6
    # Every table in the map is reported, so a growing one cannot hide.
    assert set(stats["rows"]) >= {"messages", "notify_logs", "task_logs", "audit_events"}

    # And it is reachable through the endpoint.
    body = admin.get("/api/operations/diagnostics").json()
    assert body["activity"]["messages"]["inbound"]["day"] == 2


def test_activity_stats_keep_skipped_tasks_out_of_both_outcomes(admin):
    """`skipped` means nothing was attempted. Folding it into either bucket
    would misreport the success rate the operator reads off the card."""
    db = admin.app.state.hub.db
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    task_id = db.execute(
        "INSERT INTO tasks (name, device, agent_id, created_at) VALUES (?, ?, ?, ?)",
        ("keepalive", "d", "a", ts),
    ).lastrowid
    for status in ("ok", "ok", "failed", "skipped", "skipped", "skipped"):
        db.execute(
            "INSERT INTO task_logs (task_id, ts, status, attempts, detail) "
            "VALUES (?, ?, ?, 1, '')",
            (task_id, ts, status),
        )

    tasks = db.activity_stats()["tasks"]
    assert tasks["ok"]["day"] == 2
    assert tasks["failed"]["day"] == 1
    assert tasks["skipped"]["day"] == 3
    # 2/(2+1), not 2/6 — the three skips are not failures.
    assert tasks["ok"]["day"] + tasks["failed"]["day"] == 3


def test_incidents_can_be_acknowledged_and_resolved(admin):
    incident = admin.app.state.hub.db.open_incident(
        "test:device-a",
        kind="test",
        severity="warning",
        source="device-a",
        title="test incident",
        detail="safe detail",
    )

    open_rows = _items(admin.get("/api/operations/incidents"))
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
    assert _items(admin.get("/api/operations/incidents")) == []
    assert _items(admin.get("/api/operations/incidents?status=all"))[0]["id"] == incident["id"]


def test_network_registration_incident_tracks_recovery(admin):
    with _connect(admin) as ws:
        _greet(ws)
        ws.send_json({
            "type": "status", "seq": 1, "device": "a", "online": True,
            "registered": False, "ts": _minutes_ago(1),
        })
        ws.receive_json()
        assert _items(admin.get("/api/operations/incidents"))[0]["kind"] == "network_unregistered"

        ws.send_json({
            "type": "status", "seq": 2, "device": "a", "online": True,
            "registered": True, "ts": _minutes_ago(0),
        })
        ws.receive_json()

    assert _items(admin.get("/api/operations/incidents")) == []


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

        open_rows = _items(admin.get("/api/operations/incidents"))
        assert len(open_rows) == 1
        assert open_rows[0]["kind"] == "sms_send_failed"
        assert open_rows[0]["occurrences"] == 3

        ws.send_json({
            "type": "sms_out", "seq": 4, "device": "a", "peer": "10086",
            "body": "finally", "status": "sent", "ts": _minutes_ago(0),
        })
        ws.receive_json()

    assert _items(admin.get("/api/operations/incidents")) == []


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
        assert len(_items(admin.get("/api/operations/incidents"))) == 2

        # A real offline notification need not restate registration at all —
        # the module is gone, so there is nothing to report about it.
        ws.send_json({
            "type": "status", "seq": 3, "device": "a", "online": False,
            "ts": _minutes_ago(0),
        })
        ws.receive_json()

    kinds = {row["kind"] for row in _items(admin.get("/api/operations/incidents"))}
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


def test_restore_reports_the_snapshot_when_migrating_the_backup_fails(
    admin, monkeypatch, tmp_path
):
    """A failed post-restore migration must name the copy that can undo it.

    By this point the uploaded data already overwrote the live database, so a
    bare 500 would leave the operator with a half-migrated file and no hint
    that a recoverable snapshot exists.
    """
    import sqlite3

    from hub_server.db import Database

    snapshot = admin.get("/api/system/backup")
    assert snapshot.status_code == 200
    legacy = tmp_path / "legacy.db"
    legacy.write_bytes(snapshot.content)
    connection = sqlite3.connect(legacy)
    try:
        connection.execute("PRAGMA user_version = 4")
    finally:
        connection.close()

    def explode(self) -> None:
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(Database, "_migration_data_messages", explode)

    response = admin.post(
        "/api/system/restore",
        content=legacy.read_bytes(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "迁移失败" in detail
    expected = admin.app.state.hub.db.path.with_name("hub.db.v4.bak")
    assert str(expected) in detail
    assert expected.exists()


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


# --------------------------------------------------------------------------
# log pagination
# --------------------------------------------------------------------------


def _seed_agent_logs(admin, count: int, *, ts: str | None = None) -> None:
    """Insert *count* agent log rows, newest message last.

    When *ts* is given every row shares it, which is the case that exposes an
    unstable sort: without a tiebreak, SQLite may return equal-``ts`` rows in
    any order and paging over them repeats or drops rows.
    """
    db = admin.app.state.hub.db
    for index in range(count):
        db.execute(
            "INSERT INTO agent_logs (agent_id, device, level, message, ts) "
            "VALUES ('agent-a', 'a', 'info', ?, ?)",
            (
                f"line-{index:03d}",
                ts or _minutes_ago(count - index),
            ),
        )


def test_log_pages_carry_the_full_total_not_the_page_size(admin):
    """``total`` is what makes a pager able to render its last page.

    Returning the page length instead would read as "one page, done" for every
    window, which is exactly the bug that hid the older records: the UI cannot
    know more exist.
    """
    _seed_agent_logs(admin, 25)

    body = admin.get("/api/logs?limit=10").json()
    assert len(body["items"]) == 10
    assert body["total"] == 25


def test_paging_through_logs_visits_every_row_exactly_once(admin):
    _seed_agent_logs(admin, 25)

    seen: list[str] = []
    for offset in (0, 10, 20):
        page = admin.get(f"/api/logs?limit=10&offset={offset}").json()["items"]
        seen.extend(row["message"] for row in page)

    # Every seeded row, once, newest first — no gaps and no repeats.
    assert seen == [f"line-{index:03d}" for index in reversed(range(25))]


def test_rows_sharing_a_timestamp_still_read_newest_first(admin):
    """Bulk-written logs share a second, and ``ts`` alone cannot order them.

    Ordering by ``ts DESC`` alone, SQLite walks the ts index, whose entries are
    (key, rowid) — so rows with an equal ts come back *rowid ascending*, which
    is oldest-first inside the tie group.  A log view whose entire contract is
    newest-first then shows those rows backwards.  The ``id DESC`` tiebreak is
    what fixes it; paging over them was already deterministic without it.
    """
    stamp = _minutes_ago(5)
    _seed_agent_logs(admin, 30, ts=stamp)

    seen: list[str] = []
    for offset in (0, 10, 20):
        page = admin.get(f"/api/logs?limit=10&offset={offset}").json()["items"]
        seen.extend(row["message"] for row in page)

    # Newest (highest id, seeded last) first, all the way down.
    assert seen == [f"line-{index:03d}" for index in reversed(range(30))]
    assert len(set(seen)) == 30, "a row was repeated across pages"


def test_an_offset_past_the_end_returns_an_empty_page_not_an_error(admin):
    """The pager can land here by holding a page while rows age out."""
    _seed_agent_logs(admin, 5)

    body = admin.get("/api/logs?limit=10&offset=500").json()
    assert body["items"] == []
    # total still describes the collection, so the UI can recover to a real page.
    assert body["total"] == 5


def test_incident_totals_track_the_status_filter(admin):
    """``total`` must count what the filter selects, not the whole table.

    A pager sized from an unfiltered count would offer pages that come back
    empty as soon as the operator narrows to open incidents.
    """
    db = admin.app.state.hub.db
    for index in range(4):
        db.open_incident(
            f"fp-{index}",
            kind="test",
            severity="warning",
            source="a",
            title=f"incident {index}",
        )
    # Resolve half of them, so the two counts genuinely differ.
    for row in db.query("SELECT id FROM incidents LIMIT 2"):
        db.set_incident_status(row["id"], "resolved")

    assert admin.get("/api/operations/incidents?status=open").json()["total"] == 2
    assert admin.get("/api/operations/incidents?status=all").json()["total"] == 4


def test_every_paged_log_endpoint_answers_with_items_and_total(admin):
    """One shape across all five, so the frontend pager is written once."""
    for path in (
        "/api/logs",
        "/api/notify-logs",
        "/api/task-logs",
        "/api/operations/audit",
        "/api/operations/incidents",
    ):
        body = admin.get(f"{path}?limit=5&offset=0").json()
        assert isinstance(body, dict), f"{path} did not return an object"
        assert isinstance(body["items"], list), f"{path} has no items list"
        assert isinstance(body["total"], int), f"{path} has no integer total"
