"""Database migrations that do not need the HTTP application."""

from __future__ import annotations

import sqlite3

import pytest

from hub_server import db as db_module
from hub_server.db import (
    SCHEMA,
    SCHEMA_VERSION,
    Database,
    MigrationFailed,
    SchemaTooNew,
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


def test_a_database_from_a_newer_server_is_refused(tmp_path):
    path = tmp_path / "hub.db"
    Database(path).close()
    _set_user_version(path, SCHEMA_VERSION + 1)

    with pytest.raises(SchemaTooNew):
        Database(path)


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
    assert _user_version(path) == 1

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        Database,
        "MIGRATIONS",
        ((2, "add a probe column", ("ALTER TABLE devices ADD COLUMN probe TEXT",)),),
    )

    database = Database(path)
    try:
        assert _user_version(path) == 2
        columns = {row["name"] for row in database.query("PRAGMA table_info(devices)")}
        assert "probe" in columns
    finally:
        database.close()


def test_a_partly_failing_ordered_migration_rolls_back_completely(tmp_path, monkeypatch):
    """A step that fails midway must leave nothing behind.

    The regression this guards: listing the statements as one ``executescript``
    script instead of individually.  ``executescript`` COMMITs before it runs,
    which closes the transaction the step opened — the first statement would
    then stick, the version bump would commit on its own, and the rollback would
    raise inside the error handler and discard the snapshot path.
    """
    path = tmp_path / "hub.db"
    Database(path).close()

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        Database,
        "MIGRATIONS",
        (
            (
                2,
                "add two columns, badly",
                (
                    "ALTER TABLE devices ADD COLUMN good TEXT",
                    "ALTER TABLE nonexistent ADD COLUMN bad TEXT",
                ),
            ),
        ),
    )

    with pytest.raises(MigrationFailed) as caught:
        Database(path)

    # The snapshot path survives to the caller, so an operator can recover.
    assert caught.value.snapshot == path.with_name(f"{path.name}.v1.bak")
    assert caught.value.snapshot.exists()
    # Neither the first statement nor the version bump stuck.
    assert _user_version(path) == 1
    connection = sqlite3.connect(path)
    try:
        names = {row[1] for row in connection.execute("PRAGMA table_info(devices)")}
        assert "good" not in names
    finally:
        connection.close()


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
