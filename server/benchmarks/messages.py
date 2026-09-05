"""Repeatable 100k-message benchmark for the Server's read paths."""

from __future__ import annotations

import argparse
import gc
import json
import platform
import random
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hub_server.csv_export import iter_message_csv
from hub_server.db import Database, MessageScope

DEFAULT_ROWS = 100_000
DEFAULT_REPEAT = 5
DEFAULT_SEED = 780
SIM_COUNT = 12
PEERS_PER_SIM = 500
ANCHOR = datetime(2030, 1, 1, 12, tzinfo=UTC)

# These are regression tripwires, not latency promises for every host. They are
# intentionally several times slower than the reference machine in
# docs/performance.md and are enforced only when --enforce is requested.
LATENCY_BUDGETS_MS = {
    "message_list": 75.0,
    "message_search": 300.0,
    "conversation_thread": 75.0,
    "conversation_list": 300.0,
    "trend_30_days": 150.0,
    "trend_365_days": 750.0,
}
CSV_MIN_ROWS_PER_SECOND = 15_000.0
CSV_MAX_PYTHON_PEAK_MIB = 32.0


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_fixture(path: Path, *, rows: int, seed: int) -> tuple[int, str]:
    """Create a deterministic mix of cards, threads, text and data messages."""
    Database(path).close()
    connection = sqlite3.connect(path, isolation_level=None)
    rng = random.Random(seed)
    anchor = ANCHOR.isoformat(timespec="seconds")
    try:
        # Durability is irrelevant while constructing a disposable fixture.
        # The benchmark reopens it with the production NORMAL setting.
        connection.execute("PRAGMA synchronous = OFF")
        sim_ids = []
        for index in range(SIM_COUNT):
            cursor = connection.execute(
                "INSERT INTO sims (iccid, label, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    f"898600000000000000{index:02d}",
                    f"Benchmark SIM {index + 1}",
                    anchor,
                    anchor,
                ),
            )
            sim_ids.append(int(cursor.lastrowid))

        insert_sql = (
            "INSERT INTO messages "
            "(agent_id, device, sim_id, direction, peer, body, ts, status, "
            " segments, read_at, raw_pdu, dcs, is_binary, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        connection.execute("BEGIN IMMEDIATE")
        batch: list[tuple[Any, ...]] = []
        for index in range(rows):
            # Five percent form one long service-number thread. The remainder
            # spread over 6,000 possible card/peer pairs.
            if index % 20 == 0:
                sim_index = 0
                peer_index = 0
            else:
                sim_index = rng.randrange(SIM_COUNT)
                peer_index = rng.randrange(1, PEERS_PER_SIM)
            direction = "in" if rng.random() < 0.68 else "out"
            is_binary = index % 31 == 0
            timestamp = (ANCHOR - timedelta(seconds=index * 300)).isoformat(
                timespec="seconds"
            )
            body = f"benchmark message {index:06d}, code {index:06d}"
            if index % 97 == 0:
                body += "\nsecond line exercises CSV quoting"
            unread = direction == "in" and rng.random() < 0.25
            batch.append(
                (
                    "benchmark-agent",
                    f"modem-{sim_index + 1}",
                    sim_ids[sim_index],
                    direction,
                    f"+447700{peer_index:06d}",
                    body,
                    timestamp,
                    "received" if direction == "in" else "delivered",
                    1,
                    None if unread else timestamp,
                    "001122334455" if is_binary else None,
                    4 if is_binary else 0,
                    int(is_binary),
                    timestamp,
                )
            )
            if len(batch) == 5_000:
                connection.executemany(insert_sql, batch)
                batch.clear()
        if batch:
            connection.executemany(insert_sql, batch)
        connection.commit()
    finally:
        connection.close()
    return sim_ids[0], "+447700000000"


def measure(call: Callable[[], Any], *, repeat: int) -> tuple[dict[str, float], Any]:
    result = call()
    samples = []
    for _ in range(repeat):
        gc.collect()
        started = time.perf_counter()
        result = call()
        samples.append((time.perf_counter() - started) * 1_000)
    return (
        {
            "min_ms": round(min(samples), 3),
            "median_ms": round(statistics.median(samples), 3),
            "max_ms": round(max(samples), 3),
        },
        result,
    )


def consume_csv(db: Database) -> tuple[int, int]:
    chunks = 0
    encoded_bytes = 0
    for chunk in iter_message_csv(db):
        chunks += 1
        encoded_bytes += len(chunk.encode("utf-8"))
    return chunks - 2, encoded_bytes  # BOM and header are not message rows.


def explain(db: Database, sql: str, params: tuple[Any, ...] = ()) -> list[str]:
    return [
        row["detail"]
        for row in db.query(f"EXPLAIN QUERY PLAN {sql}", params)
    ]


def run_benchmark(*, rows: int, repeat: int, seed: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="air780e-message-benchmark-") as root:
        path = Path(root) / "hub.db"
        build_started = time.perf_counter()
        target_sim, target_peer = build_fixture(path, rows=rows, seed=seed)
        build_seconds = time.perf_counter() - build_started
        db = Database(path)
        try:
            since_30 = (ANCHOR - timedelta(days=29)).replace(
                hour=0, minute=0, second=0
            ).isoformat(timespec="seconds")
            since_365 = (ANCHOR - timedelta(days=364)).replace(
                hour=0, minute=0, second=0
            ).isoformat(timespec="seconds")
            search = MessageScope(search="code 042424")
            thread = MessageScope(sim=target_sim, peer=target_peer)
            calls: dict[str, Callable[[], Any]] = {
                "message_list": lambda: (
                    db.messages(limit=50),
                    db.count_messages(),
                ),
                "message_search": lambda: (
                    db.messages(search, limit=50),
                    db.count_messages(search),
                ),
                "conversation_thread": lambda: (
                    db.messages(thread, limit=50),
                    db.count_messages(thread),
                ),
                "conversation_list": lambda: db.conversations(limit=200),
                "trend_30_days": lambda: db.message_trend(since=since_30),
                "trend_365_days": lambda: db.message_trend(since=since_365),
            }
            metrics: dict[str, dict[str, Any]] = {}
            results: dict[str, Any] = {}
            for name, call in calls.items():
                timing, results[name] = measure(call, repeat=repeat)
                budget = LATENCY_BUDGETS_MS[name]
                metrics[name] = {
                    **timing,
                    "budget_ms": budget,
                    "passed": timing["median_ms"] <= budget,
                }

            if results["message_list"][1] != rows:
                raise RuntimeError("message list count does not match the fixture")
            if results["message_search"][1] != 1:
                raise RuntimeError("message search did not find its unique fixture row")
            if not results["conversation_thread"][0]:
                raise RuntimeError("long conversation fixture is empty")
            if len(results["conversation_list"]) > 200:
                raise RuntimeError("conversation result exceeded its limit")

            csv_timing, csv_result = measure(lambda: consume_csv(db), repeat=repeat)
            csv_rows, csv_bytes = csv_result
            if csv_rows != rows:
                raise RuntimeError("CSV export row count does not match the fixture")
            rows_per_second = rows / (csv_timing["median_ms"] / 1_000)

            gc.collect()
            tracemalloc.start()
            consume_csv(db)
            _, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            peak_mib = peak_bytes / (1024 * 1024)
            csv_passed = (
                rows_per_second >= CSV_MIN_ROWS_PER_SECOND
                and peak_mib <= CSV_MAX_PYTHON_PEAK_MIB
            )
            metrics["csv_export"] = {
                **csv_timing,
                "rows_per_second": round(rows_per_second, 1),
                "encoded_bytes": csv_bytes,
                "python_peak_mib": round(peak_mib, 3),
                "min_rows_per_second": CSV_MIN_ROWS_PER_SECOND,
                "max_python_peak_mib": CSV_MAX_PYTHON_PEAK_MIB,
                "passed": csv_passed,
            }

            plans = {
                "conversation_thread": explain(
                    db,
                    "SELECT id FROM messages WHERE sim_id = ? AND peer = ? "
                    "ORDER BY ts DESC, id DESC LIMIT 50",
                    (target_sim, target_peer),
                ),
                "conversation_summary": explain(
                    db,
                    "SELECT sim_id, peer, MAX(ts), COUNT(*) FROM messages "
                    "GROUP BY sim_id, peer ORDER BY MAX(ts) DESC LIMIT 200",
                ),
                "trend_30_days": explain(
                    db,
                    "SELECT date(ts), sim_id, COUNT(*) FROM messages "
                    "WHERE ts >= ? GROUP BY date(ts), sim_id",
                    (since_30,),
                ),
            }
            database_bytes = path.stat().st_size
        finally:
            db.close()

    return {
        "fixture": {
            "rows": rows,
            "sims": SIM_COUNT,
            "possible_conversations": SIM_COUNT * PEERS_PER_SIM,
            "seed": seed,
            "anchor": ANCHOR.isoformat(timespec="seconds"),
            "database_bytes": database_bytes,
            "build_seconds": round(build_seconds, 3),
        },
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "repeat": repeat,
        "metrics": metrics,
        "query_plans": plans,
        "passed": all(metric["passed"] for metric in metrics.values()),
    }


def print_human(result: dict[str, Any]) -> None:
    fixture = result["fixture"]
    environment = result["environment"]
    print(
        f"Fixture: {fixture['rows']:,} messages, {fixture['sims']} SIMs, "
        f"{fixture['database_bytes'] / (1024 * 1024):.1f} MiB, "
        f"built in {fixture['build_seconds']:.2f}s"
    )
    print(
        f"Runtime: Python {environment['python']}, SQLite {environment['sqlite']}, "
        f"{environment['machine']}"
    )
    for name, metric in result["metrics"].items():
        if name == "csv_export":
            print(
                f"{name:24} {metric['median_ms']:8.2f} ms  "
                f"{metric['rows_per_second']:9,.0f} rows/s  "
                f"peak {metric['python_peak_mib']:.2f} MiB  "
                f"{'PASS' if metric['passed'] else 'FAIL'}"
            )
        else:
            print(
                f"{name:24} {metric['median_ms']:8.2f} ms  "
                f"budget {metric['budget_ms']:6.0f} ms  "
                f"{'PASS' if metric['passed'] else 'FAIL'}"
            )
    print(f"Result: {'PASS' if result['passed'] else 'FAIL'}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--rows", type=positive_int, default=DEFAULT_ROWS)
    parser.add_argument("--repeat", type=positive_int, default=DEFAULT_REPEAT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="exit non-zero when the 100k regression budgets are missed",
    )
    args = parser.parse_args()
    if args.enforce and args.rows != DEFAULT_ROWS:
        parser.error(f"--enforce requires --rows={DEFAULT_ROWS}")

    result = run_benchmark(rows=args.rows, repeat=args.repeat, seed=args.seed)
    if args.json:
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print_human(result)
    if args.enforce and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
