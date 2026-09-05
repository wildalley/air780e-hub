"""Synthetic read/write/CSV/retention load; never opens an operator's database."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from messages import ANCHOR, DEFAULT_SEED, build_fixture, consume_csv, positive_int

from hub_server.config import Settings
from hub_server.db import Database, MessageScope
from hub_server.gateway import AgentConnection, Gateway


class TimedLock:
    """Measure outer shared-connection lock waits and holds, excluding recursion."""

    def __init__(self):
        self.lock = threading.RLock()
        self.local = threading.local()
        self.wait_ms = []
        self.hold_ms = []

    def __enter__(self):
        started = time.perf_counter()
        self.lock.acquire()
        depth = getattr(self.local, "depth", 0)
        if depth == 0:
            self.local.started = time.perf_counter()
            self.wait_ms.append((self.local.started - started) * 1000)
        self.local.depth = depth + 1
        return self

    def __exit__(self, *_args):
        self.local.depth -= 1
        if self.local.depth == 0:
            self.hold_ms.append((time.perf_counter() - self.local.started) * 1000)
        self.lock.release()


class SharedSynchronousDatabase(Database):
    """Reference scheduling: shared query lock, synchronous async callers.

    Keeps today's SQL and schema. This isolates the scheduling change; it is
    not a checkout of an older release or an HTTP benchmark.
    """

    def read_query(self, sql, params=()):
        return self.query(sql, params)

    async def run(self, operation, /, *args, **kwargs):
        return operation(*args, **kwargs)


def summary(samples):
    ordered = sorted(samples)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "p50_ms": round(ordered[math.ceil(len(ordered) * 0.50) - 1], 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
        "max_ms": round(ordered[-1], 3),
    }


class AckSocket:
    def __init__(self):
        self.count = 0

    async def send_text(self, raw):
        assert json.loads(raw)["type"] == "ack"
        self.count += 1


async def run_load(path, *, reference, readers, iterations, events, rows):
    db = (SharedSynchronousDatabase if reference else Database)(path)
    db.upsert_agent("load", "0.1.0", 1, connected=True)
    device_id = db.upsert_device("load", {"name": "a", "online": True})
    for _ in range(1200):
        db.record_status(device_id, {"ts": "2000-01-01"})
    db._lock = lock = TimedLock()
    socket = AckSocket()
    gateway = Gateway(db, Settings(data_dir=path.parent, agent_token="synthetic-only"))
    connection = AgentConnection("load", socket, stream_id="benchmark")
    ack_ms, loop_ms, query_ms, csv_ms, purge_ms = [], [], [], [], []
    stopped = asyncio.Event()

    async def monitor_loop():
        while not stopped.is_set():
            due = time.perf_counter() + 0.01
            await asyncio.sleep(0.01)
            loop_ms.append(max(0.0, (time.perf_counter() - due) * 1000))

    operations = [
        lambda: (db.messages(limit=50), db.count_messages()),
        lambda: db.count_messages(MessageScope(search="code 042424")),
        lambda: db.conversations(limit=200),
        lambda: db.message_trend(since="2029-01-01"),
    ]

    def query(operation):
        started = time.perf_counter()
        operation()
        query_ms.append((time.perf_counter() - started) * 1000)

    async def read(index):
        for iteration in range(iterations):
            await asyncio.to_thread(query, operations[(index + iteration) % len(operations)])

    async def ingest():
        for seq in range(1, events + 1):
            started = time.perf_counter()
            await gateway._ingest(connection, {
                "type": "status", "seq": seq, "device": "a", "online": True,
                "rssi": 20, "ts": ANCHOR.isoformat(),
            })
            ack_ms.append((time.perf_counter() - started) * 1000)
            await asyncio.sleep(0.005)

    async def csv():
        started = time.perf_counter()
        count, _ = await asyncio.to_thread(consume_csv, db)
        csv_ms.append((time.perf_counter() - started) * 1000)
        return count

    async def purge():
        started = time.perf_counter()
        if reference:
            count = db.execute(
                "DELETE FROM device_status WHERE ts < '2020-01-01'"
            ).rowcount
        else:
            count = (await db.purge_async(message_days=0, status_days=1))["status"]
        purge_ms.append((time.perf_counter() - started) * 1000)
        assert count == 1200

    started = time.perf_counter()
    monitor = asyncio.create_task(monitor_loop())
    try:
        async with asyncio.timeout(120):
            results = await asyncio.gather(
                csv(), ingest(), purge(), *(read(i) for i in range(readers))
            )
        assert socket.count == events
        assert results[0] == rows
        assert db.one("SELECT COUNT(*) AS n FROM ingested")["n"] == events
        return {
            "mode": "shared_sync_reference" if reference else "wal_readers_async_writer",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "ack": summary(ack_ms), "loop_lag": summary(loop_ms),
            "query": summary(query_ms), "shared_lock_wait": summary(lock.wait_ms),
            "shared_lock_hold": summary(lock.hold_ms),
            "csv": {**summary(csv_ms), "rows": results[0]},
            "purge": summary(purge_ms),
        }
    finally:
        stopped.set()
        await monitor
        db.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=positive_int, default=100_000)
    parser.add_argument("--readers", type=positive_int, default=4)
    parser.add_argument("--iterations", type=positive_int, default=8)
    parser.add_argument("--events", type=positive_int, default=200)
    parser.add_argument("--repeat", type=positive_int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    runs = []
    with tempfile.TemporaryDirectory(prefix="hub-concurrency-") as root:
        for repeat in range(args.repeat):
            # Alternate mode order to reduce systematic warm-cache bias.
            for reference in ([True, False] if repeat % 2 == 0 else [False, True]):
                path = Path(root) / f"{repeat}-{reference}.db"
                build_fixture(path, rows=args.rows, seed=DEFAULT_SEED)
                runs.append(await run_load(
                    path, reference=reference, readers=args.readers,
                    iterations=args.iterations, events=args.events, rows=args.rows,
                ))
    report = {
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "platform": platform.platform(), "cpu_count": os.cpu_count()},
        "workload": {key: value for key, value in vars(args).items() if key != "output"},
        "runs": runs,
    }
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    asyncio.run(main())
