"""Database migrations that do not need the HTTP application."""

from __future__ import annotations

import sqlite3

from hub_server.db import SCHEMA, Database


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
