"""Database migrations that do not need the HTTP application."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from hub_server import db as db_module
from hub_server.db import (
    SCHEMA,
    SCHEMA_VERSION,
    Database,
    MigrationFailed,
    SchemaTooNew,
    utcnow,
)


def _user_version(path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _set_user_version(path, version: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"PRAGMA user_version = {version}")
    finally:
        connection.close()


def _write_pre_radio_schema(path) -> None:
    legacy_schema = SCHEMA.replace("    radio_enabled INTEGER,\n", "")
    connection = sqlite3.connect(path)
    try:
        connection.executescript(legacy_schema)
    finally:
        connection.close()


def test_restoring_a_pre_radio_backup_applies_current_additive_columns(tmp_path):
    backup = tmp_path / "legacy.db"
    _write_pre_radio_schema(backup)

    database = Database(tmp_path / "live.db")
    try:
        database.restore_from(backup)
        columns = {
            row["name"] for row in database.query("PRAGMA table_info(devices)")
        }
        assert "radio_enabled" in columns

        database.upsert_device(
            "agent-a",
            {"name": "modem-a", "online": True, "radio_enabled": False},
        )
        row = database.one("SELECT radio_enabled FROM devices")
        assert row == {"radio_enabled": 0}
    finally:
        database.close()


def test_unknown_radio_state_remains_null(tmp_path):
    database = Database(tmp_path / "hub.db")
    try:
        database.upsert_device(
            "agent-a",
            {"name": "modem-a", "online": True, "radio_enabled": None},
        )
        row = database.one("SELECT radio_enabled FROM devices")
        assert row == {"radio_enabled": None}
    finally:
        database.close()


def test_registration_domains_preserve_false_and_unknown(tmp_path):
    database = Database(tmp_path / "hub.db")
    try:
        database.upsert_device(
            "agent-a",
            {
                "name": "modem-a",
                "registered": True,
                "eps_registered": True,
                "cs_registered": False,
                "ims_registered": None,
            },
        )
        row = database.one(
            "SELECT registered, eps_registered, cs_registered, ims_registered "
            "FROM devices"
        )
        assert row == {
            "registered": 1,
            "eps_registered": 1,
            "cs_registered": 0,
            "ims_registered": None,
        }
    finally:
        database.close()


# -- schema versioning ----------------------------------------------------


def test_a_fresh_database_lands_on_the_current_version_without_a_snapshot(tmp_path):
    path = tmp_path / "hub.db"
    database = Database(path)
    try:
        assert _user_version(path) == SCHEMA_VERSION
    finally:
        database.close()
    # A file we just created holds nothing worth preserving; snapshotting every
    # first start would litter the data volume.
    assert list(tmp_path.glob("*.bak")) == []


def test_a_pre_versioning_database_is_reconciled_and_snapshotted(tmp_path):
    path = tmp_path / "hub.db"
    _write_pre_radio_schema(path)
    assert _user_version(path) == 0

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO devices (agent_id, name) VALUES ('agent-a', 'modem-a')"
        )
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    try:
        assert _user_version(path) == SCHEMA_VERSION
        columns = {row["name"] for row in database.query("PRAGMA table_info(devices)")}
        assert "radio_enabled" in columns
        # The row that was already there survives the migration.
        assert database.one("SELECT name FROM devices") == {"name": "modem-a"}
    finally:
        database.close()

    snapshot = path.with_name(f"{path.name}.v0.bak")
    assert snapshot.exists()
    # The snapshot is the pre-migration state: same row, no new column.
    connection = sqlite3.connect(snapshot)
    try:
        names = {
            row[1] for row in connection.execute("PRAGMA table_info(devices)")
        }
        assert "radio_enabled" not in names
        assert connection.execute("SELECT name FROM devices").fetchone() == ("modem-a",)
    finally:
        connection.close()


def test_reopening_an_up_to_date_database_migrates_nothing(tmp_path):
    path = tmp_path / "hub.db"
    Database(path).close()
    for stale in tmp_path.glob("*.bak"):
        stale.unlink()

    database = Database(path)
    try:
        assert _user_version(path) == SCHEMA_VERSION
    finally:
        database.close()
    # Already current: no step ran, so no snapshot was taken.
    assert list(tmp_path.glob("*.bak")) == []


def test_a_database_from_a_newer_server_is_refused_before_schema_writes(tmp_path):
    path = tmp_path / "hub.db"
    Database(path).close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE sms_delivery_segments")
        connection.commit()
    finally:
        connection.close()
    _set_user_version(path, SCHEMA_VERSION + 1)

    with pytest.raises(SchemaTooNew):
        Database(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'sms_delivery_segments'"
        ).fetchone() is None
    finally:
        connection.close()


def test_a_failing_migration_reports_the_snapshot_and_keeps_the_old_version(
    tmp_path, monkeypatch
):
    path = tmp_path / "hub.db"
    _write_pre_radio_schema(path)

    def explode(self) -> None:
        self._db.execute("ALTER TABLE devices ADD COLUMN half_applied TEXT")
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(Database, "_reconcile_to_baseline", explode)

    with pytest.raises(MigrationFailed) as caught:
        Database(path)

    # The snapshot is reported so an operator knows what to restore.
    assert caught.value.snapshot == path.with_name(f"{path.name}.v0.bak")
    assert caught.value.snapshot.exists()
    # Nothing stuck: neither the partial column nor the version bump.
    assert _user_version(path) == 0
    connection = sqlite3.connect(path)
    try:
        names = {row[1] for row in connection.execute("PRAGMA table_info(devices)")}
        assert "half_applied" not in names
    finally:
        connection.close()


def test_a_failed_restore_migration_leaves_the_live_connection_usable(
    tmp_path, monkeypatch
):
    """The rollback matters most here, not on a failed open.

    A failed ``Database(...)`` throws the half-built object away and SQLite
    discards its transaction with the connection.  ``restore_from`` runs on a
    *live* connection the server keeps using, so a migration that raised without
    rolling back would leave an open write transaction holding the lock — every
    later write would then block instead of failing.
    """
    backup = tmp_path / "legacy.db"
    _write_pre_radio_schema(backup)

    database = Database(tmp_path / "live.db")
    try:
        def explode(self) -> None:
            raise sqlite3.OperationalError("boom")

        monkeypatch.setattr(Database, "_reconcile_to_baseline", explode)
        with pytest.raises(MigrationFailed):
            database.restore_from(backup)

        monkeypatch.undo()
        # No transaction left open: a write goes through instead of blocking on
        # the lock the failed migration would otherwise still hold.
        assert not database._db.in_transaction
        database.execute(
            "INSERT INTO devices (agent_id, name) VALUES ('agent-a', 'modem-a')"
        )
        assert database.one("SELECT name FROM devices") == {"name": "modem-a"}
    finally:
        database.close()


def test_an_ordered_migration_applies_and_bumps_the_version(tmp_path, monkeypatch):
    path = tmp_path / "hub.db"
    Database(path).close()
    assert _user_version(path) == SCHEMA_VERSION

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    monkeypatch.setattr(
        Database,
        "_probe_step",
        lambda self: self._db.execute("ALTER TABLE devices ADD COLUMN probe TEXT"),
        raising=False,
    )
    monkeypatch.setattr(
        Database,
        "MIGRATIONS",
        ((SCHEMA_VERSION + 1, "add a probe column", "_probe_step"),),
    )

    database = Database(path)
    try:
        assert _user_version(path) == SCHEMA_VERSION + 1
        columns = {row["name"] for row in database.query("PRAGMA table_info(devices)")}
        assert "probe" in columns
    finally:
        database.close()


def test_a_partly_failing_ordered_migration_rolls_back_completely(tmp_path, monkeypatch):
    """A step that fails midway must leave nothing behind.

    The regression this guards: running a step's statements through
    ``executescript``.  It COMMITs before it runs, which closes the transaction
    the step opened — the first statement would then stick, the version bump
    would commit on its own, and the rollback would raise inside the error
    handler and discard the snapshot path.
    """
    path = tmp_path / "hub.db"
    Database(path).close()

    def half_apply(self) -> None:
        self._db.execute("ALTER TABLE devices ADD COLUMN good TEXT")
        self._db.execute("ALTER TABLE nonexistent ADD COLUMN bad TEXT")

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    monkeypatch.setattr(Database, "_half_apply", half_apply, raising=False)
    monkeypatch.setattr(
        Database,
        "MIGRATIONS",
        ((SCHEMA_VERSION + 1, "add two columns, badly", "_half_apply"),),
    )

    with pytest.raises(MigrationFailed) as caught:
        Database(path)

    # The snapshot path survives to the caller, so an operator can recover.
    assert caught.value.snapshot == path.with_name(
        f"{path.name}.v{SCHEMA_VERSION}.bak"
    )
    assert caught.value.snapshot.exists()
    # Neither the first statement nor the version bump stuck.
    assert _user_version(path) == SCHEMA_VERSION
    connection = sqlite3.connect(path)
    try:
        names = {row[1] for row in connection.execute("PRAGMA table_info(devices)")}
        assert "good" not in names
    finally:
        connection.close()


def test_a_fresh_database_is_stamped_rather_than_migrated(tmp_path):
    """SCHEMA already built the current shape, so no step should replay on it.

    The regression this guards: v2 adds ``messages.raw_pdu`` both in SCHEMA and
    as an ADD COLUMN migration.  Replaying that against a table SCHEMA had just
    created fails on a duplicate column — on every first start.
    """
    path = tmp_path / "hub.db"
    database = Database(path)
    try:
        assert _user_version(path) == SCHEMA_VERSION
        columns = {row["name"] for row in database.query("PRAGMA table_info(messages)")}
        assert {"raw_pdu", "dcs", "is_binary"} <= columns
        delivery_columns = {
            row["name"]
            for row in database.query("PRAGMA table_info(sms_delivery_segments)")
        }
        assert {
            "message_id", "modem_reference", "status_code", "service_center_ts",
            "discharge_ts", "raw_pdu",
        } <= delivery_columns
        sim_columns = {
            row["name"] for row in database.query("PRAGMA table_info(sims)")
        }
        assert {
            "billing_type",
            "plan_name",
            "balance",
            "low_balance_threshold",
            "currency",
            "balance_updated_at",
            "expires_at",
            "activity_due_at",
        } <= sim_columns
        message_indexes = {
            row["name"] for row in database.query("PRAGMA index_list(messages)")
        }
        assert "idx_messages_conversation" in message_indexes
    finally:
        database.close()


def test_the_v2_migration_is_idempotent_against_a_half_built_database(tmp_path):
    """A table SCHEMA created fresh must not then be ALTERed for the same columns.

    Not hypothetical, and this is the case that caught it: the pre-versioning
    fixture writes today's SCHEMA minus one column, so its ``messages`` table
    already carries ``raw_pdu``.  The database still reads as version 0 and
    walks 0 -> 1 -> 2, and the v2 step has to notice the columns are present
    instead of failing on a duplicate.
    """
    path = tmp_path / "hub.db"
    _write_pre_radio_schema(path)
    assert _user_version(path) == 0

    database = Database(path)
    try:
        assert _user_version(path) == SCHEMA_VERSION
        columns = {row["name"] for row in database.query("PRAGMA table_info(messages)")}
        assert {"raw_pdu", "dcs", "is_binary"} <= columns
        # The 0 -> 1 step still did its own job on the way through.
        device_columns = {
            row["name"] for row in database.query("PRAGMA table_info(devices)")
        }
        assert "radio_enabled" in device_columns
    finally:
        database.close()


def test_an_upgrade_adds_the_diagnostic_columns_and_keeps_the_messages(tmp_path):
    """The v1 -> v2 path on a database that already holds messages."""
    path = tmp_path / "hub.db"
    database = Database(path)
    try:
        database.insert_message(
            agent_id="agent-a", device="a", direction="in", peer="10086",
            body="验证码 1234", ts=utcnow(),
        )
    finally:
        database.close()

    # Rewind to v1 and drop the v2 columns, i.e. exactly a pre-upgrade file.
    connection = sqlite3.connect(path)
    try:
        # A real v1 file predates the v7 index, so remove it before rebuilding
        # the old table shape.
        connection.execute("DROP INDEX idx_messages_conversation")
        for column in ("raw_pdu", "dcs", "is_binary"):
            connection.execute(f"ALTER TABLE messages DROP COLUMN {column}")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    try:
        assert _user_version(path) == SCHEMA_VERSION
        columns = {row["name"] for row in database.query("PRAGMA table_info(messages)")}
        assert {"raw_pdu", "dcs", "is_binary"} <= columns
        row = database.one("SELECT body, raw_pdu, dcs, is_binary FROM messages")
        assert row["body"] == "验证码 1234"
        # Existing rows get no PDU — it was never stored — but must read as text.
        assert row["raw_pdu"] is None
        assert row["is_binary"] == 0
    finally:
        database.close()

    snapshot = path.with_name(f"{path.name}.v1.bak")
    assert snapshot.exists(), "an upgrade of a populated database must snapshot it"


def test_v2_upgrade_adds_agent_protocol_and_delivery_segments(tmp_path):
    path = tmp_path / "hub.db"
    Database(path).close()

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO agents (id, version) VALUES ('agent-a', '0.1.0')"
        )
        connection.execute("ALTER TABLE agents DROP COLUMN protocol_version")
        connection.execute("DROP TABLE sms_delivery_segments")
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    try:
        assert _user_version(path) == SCHEMA_VERSION
        agent = database.one(
            "SELECT id, version, protocol_version FROM agents WHERE id = 'agent-a'"
        )
        assert agent == {
            "id": "agent-a", "version": "0.1.0", "protocol_version": 0,
        }
        assert database.one(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'sms_delivery_segments'"
        ) == {"name": "sms_delivery_segments"}
    finally:
        database.close()

    assert path.with_name(f"{path.name}.v2.bak").exists()
    snapshot = sqlite3.connect(path.with_name(f"{path.name}.v2.bak"))
    try:
        agent_columns = {
            row[1] for row in snapshot.execute("PRAGMA table_info(agents)")
        }
        assert "protocol_version" not in agent_columns
        assert snapshot.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'sms_delivery_segments'"
        ).fetchone() is None
    finally:
        snapshot.close()


def test_v4_upgrade_reclassifies_stored_data_pdus(tmp_path):
    path = tmp_path / "hub.db"
    database = Database(path)
    # UDHL declares one three-octet concatenation element but only carries one
    # payload octet. This is the compact form of the real giffgaff failure.
    malformed = bytes([
        0x00, 0x40, 0x01, 0x81, 0xF1, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x05, 0x03, 0x00, 0x03, 0x01,
    ]).hex()
    bad_id = database.insert_message(
        agent_id="agent-a", device="a", direction="in", peer="giffgaff",
        body="decoded noise", ts=utcnow(), raw_pdu=malformed, dcs=0,
    )
    operator_control = bytes([
        0x00, 0x00, 0x01, 0x81, 0xF1, 0xDD, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    ]).hex()
    control_id = database.insert_message(
        agent_id="agent-a", device="a", direction="in", peer="giffgaff",
        body="", ts=utcnow(), raw_pdu=operator_control, dcs=0,
    )
    good_id = database.insert_message(
        agent_id="agent-a", device="a", direction="in", peer="10086",
        body="plain text", ts=utcnow(), raw_pdu="0000", dcs=0,
    )
    database.close()
    _set_user_version(path, 4)

    database = Database(path)
    try:
        rows = database.query(
            "SELECT id, is_binary FROM messages ORDER BY id"
        )
        assert rows == [
            {"id": bad_id, "is_binary": 1},
            {"id": control_id, "is_binary": 1},
            {"id": good_id, "is_binary": 0},
        ]
    finally:
        database.close()

    snapshot = sqlite3.connect(path.with_name(f"{path.name}.v4.bak"))
    try:
        assert snapshot.execute(
            "SELECT is_binary FROM messages WHERE id = ?", (bad_id,)
        ).fetchone() == (0,)
    finally:
        snapshot.close()


def test_v5_upgrade_adds_sim_billing_fields_and_keeps_sims(tmp_path):
    path = tmp_path / "hub.db"
    database = Database(path)
    sim_id = database.upsert_sim("8986000000000000001", operator="中国移动")
    database.execute("UPDATE sims SET label = '主卡' WHERE id = ?", (sim_id,))
    database.close()

    connection = sqlite3.connect(path)
    try:
        for column in (
            "activity_due_at",
            "expires_at",
            "balance_updated_at",
            "currency",
            "low_balance_threshold",
            "balance",
            "plan_name",
            "billing_type",
        ):
            connection.execute(f"ALTER TABLE sims DROP COLUMN {column}")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    try:
        assert _user_version(path) == SCHEMA_VERSION
        row = database.one(
            "SELECT iccid, label, billing_type, plan_name, balance, "
            "low_balance_threshold, currency, balance_updated_at, expires_at, "
            "activity_due_at FROM sims WHERE id = ?",
            (sim_id,),
        )
        assert row == {
            "iccid": "8986000000000000001",
            "label": "主卡",
            "billing_type": "unknown",
            "plan_name": "",
            "balance": None,
            "low_balance_threshold": None,
            "currency": "",
            "balance_updated_at": None,
            "expires_at": None,
            "activity_due_at": None,
        }
    finally:
        database.close()

    snapshot = sqlite3.connect(path.with_name(f"{path.name}.v5.bak"))
    try:
        columns = {row[1] for row in snapshot.execute("PRAGMA table_info(sims)")}
        assert not {
            "billing_type",
            "plan_name",
            "balance",
            "low_balance_threshold",
            "currency",
            "balance_updated_at",
            "expires_at",
            "activity_due_at",
        } & columns
    finally:
        snapshot.close()


def test_v6_upgrade_adds_the_conversation_index(tmp_path):
    path = tmp_path / "hub.db"
    database = Database(path)
    message_id = database.insert_message(
        agent_id="agent-a", device="a", direction="in", peer="10086",
        body="保留的短信", ts=utcnow(), iccid="8986000000000000001",
    )
    database.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX idx_messages_conversation")
        connection.execute("PRAGMA user_version = 6")
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    try:
        assert _user_version(path) == SCHEMA_VERSION
        indexes = {
            row["name"] for row in database.query("PRAGMA index_list(messages)")
        }
        assert "idx_messages_conversation" in indexes
        assert database.one(
            "SELECT body FROM messages WHERE id = ?", (message_id,)
        ) == {"body": "保留的短信"}
    finally:
        database.close()

    snapshot = sqlite3.connect(path.with_name(f"{path.name}.v6.bak"))
    try:
        snapshot_indexes = {
            row[1]
            for row in snapshot.execute("PRAGMA index_list(messages)")
        }
        assert "idx_messages_conversation" not in snapshot_indexes
    finally:
        snapshot.close()


def test_v7_upgrade_adds_modem_diagnostics_and_keeps_devices(tmp_path):
    path = tmp_path / "hub.db"
    database = Database(path)
    database.upsert_device(
        "agent-a",
        {
            "name": "a",
            "model": "AirM2M_780EPV",
            "hardware_model": "Air780EPV",
            "firmware": "V1011",
            "registered": True,
        },
    )
    database.close()

    connection = sqlite3.connect(path)
    try:
        for column in (
            "eps_registered",
            "cs_registered",
            "ims_registered",
            "hardware_model",
            "firmware",
        ):
            connection.execute(f"ALTER TABLE devices DROP COLUMN {column}")
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    try:
        assert _user_version(path) == SCHEMA_VERSION
        columns = {row["name"] for row in database.query("PRAGMA table_info(devices)")}
        assert {
            "eps_registered",
            "cs_registered",
            "ims_registered",
            "hardware_model",
            "firmware",
        } <= columns
        assert database.one("SELECT name, model FROM devices") == {
            "name": "a",
            "model": "AirM2M_780EPV",
        }
    finally:
        database.close()

    snapshot = sqlite3.connect(path.with_name(f"{path.name}.v7.bak"))
    try:
        columns = {row[1] for row in snapshot.execute("PRAGMA table_info(devices)")}
        assert "ims_registered" not in columns
        assert "firmware" not in columns
    finally:
        snapshot.close()


def test_message_read_paths_use_the_new_indexes(tmp_path):
    database = Database(tmp_path / "hub.db")
    try:
        for index in range(4):
            database.insert_message(
                agent_id="agent-a", device="a", direction="in", peer="10086",
                body=f"短信 {index}", ts=f"2026-08-16T00:00:0{index}+00:00",
                iccid="8986000000000000001",
            )
        thread_plan = database.query(
            "EXPLAIN QUERY PLAN SELECT id FROM messages "
            "WHERE sim_id = ? AND peer = ? "
            "ORDER BY ts DESC, id DESC LIMIT 50",
            (1, "10086"),
        )
        thread_details = " ".join(row["detail"] for row in thread_plan)
        assert "idx_messages_conversation" in thread_details
        assert "USE TEMP B-TREE" not in thread_details

        trend_plan = database.query(
            "EXPLAIN QUERY PLAN SELECT date(ts), sim_id, COUNT(*) FROM messages "
            "WHERE ts >= ? GROUP BY date(ts), sim_id",
            ("2026-08-15T00:00:00+00:00",),
        )
        trend_details = " ".join(row["detail"] for row in trend_plan)
        assert "idx_messages_ts" in trend_details
        assert "ts>?" in trend_details
    finally:
        database.close()


def test_sim_lifecycle_incidents_warn_escalate_and_resolve_independently(tmp_path):
    database = Database(tmp_path / "hub.db")
    today = date(2026, 8, 16)
    try:
        warning_id = database.upsert_sim("8986000000000000001")
        critical_id = database.upsert_sim("8986000000000000002")
        overdue_id = database.upsert_sim("8986000000000000003")
        database.execute(
            "UPDATE sims SET label = ?, plan_name = ?, expires_at = ?, "
            "activity_due_at = ? WHERE id = ?",
            ("主卡", "30GB 月包", "2026-09-15", "2026-08-21", warning_id),
        )
        database.execute(
            "UPDATE sims SET phone_number = ?, expires_at = ? WHERE id = ?",
            ("13800138000", "2026-08-23", critical_id),
        )
        database.execute(
            "UPDATE sims SET expires_at = ? WHERE id = ?",
            ("2026-08-15", overdue_id),
        )

        database.reconcile_sim_incidents(today)
        incidents = {
            row["fingerprint"]: row
            for row in database.query("SELECT * FROM incidents ORDER BY id")
        }
        warning = incidents[f"sim-expiry:{warning_id}"]
        assert warning["kind"] == "sim_expiring"
        assert warning["severity"] == "warning"
        assert warning["source"] == "SIM 主卡"
        assert "30GB 月包" in warning["detail"]
        assert "还有 30 天" in warning["detail"]

        activity = incidents[f"sim-activity:{warning_id}"]
        assert activity["kind"] == "sim_activity_due"
        assert activity["severity"] == "critical"
        assert activity["source"] == "SIM 主卡"
        assert "保号截止日 2026-08-21" in activity["detail"]
        assert "还有 5 天" in activity["detail"]

        critical = incidents[f"sim-expiry:{critical_id}"]
        assert critical["severity"] == "critical"
        assert "还有 7 天" in critical["detail"]

        overdue = incidents[f"sim-expiry:{overdue_id}"]
        assert overdue["severity"] == "critical"
        assert "已过期 1 天" in overdue["detail"]

        database.execute(
            "UPDATE sims SET expires_at = ? WHERE id = ?",
            ("2026-09-16", warning_id),
        )
        database.execute(
            "UPDATE sims SET expires_at = NULL WHERE id = ?", (critical_id,)
        )
        database.reconcile_sim_incidents(today)

        statuses = {
            row["fingerprint"]: row["status"]
            for row in database.query("SELECT fingerprint, status FROM incidents")
        }
        assert statuses[f"sim-expiry:{warning_id}"] == "resolved"
        assert statuses[f"sim-activity:{warning_id}"] == "active"
        assert statuses[f"sim-expiry:{critical_id}"] == "resolved"
        assert statuses[f"sim-expiry:{overdue_id}"] == "active"

        database.execute(
            "UPDATE sims SET activity_due_at = NULL WHERE id = ?", (warning_id,)
        )
        database.reconcile_sim_incidents(today)
        assert database.one(
            "SELECT status FROM incidents WHERE fingerprint = ?",
            (f"sim-activity:{warning_id}",),
        )["status"] == "resolved"
    finally:
        database.close()


def test_sim_low_balance_incident_warns_escalates_and_resolves(tmp_path):
    database = Database(tmp_path / "hub.db")
    today = date(2026, 8, 16)
    try:
        sim_id = database.upsert_sim("8944100000000000001")
        database.execute(
            "UPDATE sims SET label = ?, balance = ?, low_balance_threshold = ?, "
            "currency = ? WHERE id = ?",
            ("英国 PAYG", "5.50", "10.00", "GBP", sim_id),
        )
        database.reconcile_sim_incidents(today)

        incident = database.one(
            "SELECT * FROM incidents WHERE fingerprint = ?",
            (f"sim-balance:{sim_id}",),
        )
        assert incident["kind"] == "sim_low_balance"
        assert incident["severity"] == "warning"
        assert incident["source"] == "SIM 英国 PAYG"
        assert "当前余额 GBP 5.50" in incident["detail"]
        assert "低余额阈值 GBP 10.00" in incident["detail"]

        database.execute(
            "UPDATE sims SET balance = ? WHERE id = ?", ("-0.01", sim_id)
        )
        database.reconcile_sim_incidents(today)
        incident = database.one(
            "SELECT * FROM incidents WHERE fingerprint = ?",
            (f"sim-balance:{sim_id}",),
        )
        assert incident["severity"] == "critical"
        assert incident["title"] == "SIM 英国 PAYG 余额为负"

        database.execute(
            "UPDATE sims SET balance = ? WHERE id = ?", ("25.00", sim_id)
        )
        database.reconcile_sim_incidents(today)
        assert database.one(
            "SELECT status FROM incidents WHERE fingerprint = ?",
            (f"sim-balance:{sim_id}",),
        )["status"] == "resolved"

        database.execute(
            "UPDATE sims SET balance = ?, low_balance_threshold = ? WHERE id = ?",
            ("5.00", "10.00", sim_id),
        )
        database.reconcile_sim_incidents(today)
        database.execute(
            "UPDATE sims SET low_balance_threshold = NULL WHERE id = ?", (sim_id,)
        )
        database.reconcile_sim_incidents(today)
        assert database.one(
            "SELECT status FROM incidents WHERE fingerprint = ?",
            (f"sim-balance:{sim_id}",),
        )["status"] == "resolved"
    finally:
        database.close()


def test_the_cli_reports_a_too_new_database_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """A downgrade is a deliberate act, so it gets a message and exit 1.

    ``auth`` is the command an operator reaches for when they are already
    locked out; a traceback there hides which problem they actually have.
    """
    from hub_server import cli

    path = tmp_path / "hub.db"
    Database(path).close()
    _set_user_version(path, SCHEMA_VERSION + 1)
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HUB_AGENT_TOKEN", "token-for-the-test")

    assert cli.main(["auth", "status"]) == 1
    errors = capsys.readouterr().err
    assert "newer than this server" in errors
    assert "Traceback" not in errors


def test_restoring_a_backup_from_a_newer_server_is_rejected(tmp_path):
    backup = tmp_path / "newer.db"
    Database(backup).close()
    _set_user_version(backup, SCHEMA_VERSION + 1)

    with pytest.raises(ValueError, match="更新版本"):
        Database.validate_backup(backup)


def test_restoring_a_pre_versioning_backup_brings_it_to_the_current_version(tmp_path):
    backup = tmp_path / "legacy.db"
    _write_pre_radio_schema(backup)
    assert _user_version(backup) == 0

    live = tmp_path / "live.db"
    database = Database(live)
    try:
        database.restore_from(backup)
        assert _user_version(live) == SCHEMA_VERSION
        columns = {row["name"] for row in database.query("PRAGMA table_info(devices)")}
        assert "radio_enabled" in columns
    finally:
        database.close()
