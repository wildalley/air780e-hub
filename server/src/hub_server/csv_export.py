"""Streaming CSV exports shared by the API and performance benchmark."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from typing import Any

from .db import Database, MessageScope

MESSAGE_CSV_HEADER = (
    "id",
    "ts",
    "direction",
    "sim_id",
    "sim_label",
    "peer",
    "body",
    "status",
    "is_binary",
    # A damaged frame's `body` is mojibake, so an export without these three
    # loses the only readable part of it.
    "truncated",
    "recovered_body",
    "recovered_code",
    "dcs",
    "raw_pdu",
)


def iter_message_csv(
    db: Database,
    scope: MessageScope | None = None,
    *,
    limit: int | None = None,
) -> Iterator[str]:
    """Yield a UTF-8 Excel-compatible CSV without retaining the full export.

    Takes the same scope object the list and the total are read with, so a
    download cannot quietly cover a different set of cards than the screen it
    was started from.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def line(values: tuple[Any, ...]) -> str:
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow(values)
        return buffer.getvalue()

    # Excel otherwise guesses a legacy encoding for Chinese message bodies.
    yield "\ufeff"
    yield line(MESSAGE_CSV_HEADER)
    for message in db.iter_messages(scope, limit=limit):
        yield line(
            (
                message["id"],
                message["ts"],
                message["direction"],
                message["sim_id"] or "",
                message.get("sim_label") or "",
                message["peer"],
                message["body"],
                message["status"],
                message.get("is_binary") or 0,
                message.get("truncated") or 0,
                message.get("recovered_body") or "",
                message.get("recovered_code") or "",
                "" if message.get("dcs") is None else message["dcs"],
                message.get("raw_pdu") or "",
            )
        )
