"""Streaming CSV exports shared by the API and performance benchmark."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from typing import Any

from .db import Database

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
    *,
    limit: int | None = None,
    sim_id: int | None = None,
    peer: str | None = None,
    search: str | None = None,
    content: str | None = None,
) -> Iterator[str]:
    """Yield a UTF-8 Excel-compatible CSV without retaining the full export."""
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
    for message in db.iter_messages(
        limit=limit,
        sim_id=sim_id,
        peer=peer,
        search=search,
        content=content,
    ):
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
