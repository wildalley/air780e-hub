"""Bounded process-local timings, shared by the loop and database threads."""

from __future__ import annotations

import asyncio
import math
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

SAMPLE_LIMIT = 256
HTTP_SERIES_LIMIT = 256
GATEWAY_CLOSE_SERIES_LIMIT = 32
TIMINGS = (
    "loop_lag", "db_queue_wait", "db_worker", "db_lock_wait", "db_lock_hold",
    "db_read_session", "event_transaction", "gateway_ack", "notify_send",
)
COUNTERS = (
    "db_busy", "events_committed", "events_duplicate", "events_failed",
    "ack_send_failed", "notify_attempts", "notify_succeeded", "notify_failed",
    "notify_cancelled", "notify_retry_scheduled",
)
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
GATEWAY_CLOSE_CATEGORIES = frozenset({
    "maintenance", "auth_failed", "self_check", "protocol", "agent_conflict",
    "internal_error", "peer_disconnect", "other",
})


@dataclass
class _Timing:
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=SAMPLE_LIMIT))

    def observe(self, seconds: float) -> None:
        milliseconds = max(0.0, seconds * 1000)
        self.count += 1
        self.total_ms += milliseconds
        self.max_ms = max(self.max_ms, milliseconds)
        self.samples.append(milliseconds)

    def snapshot(self) -> dict[str, Any]:
        ordered = sorted(self.samples)
        result = {
            "count": self.count,
            "sample_count": len(ordered),
            "total_ms": round(self.total_ms, 3),
            "max_ms": round(self.max_ms, 3) if ordered else None,
        }
        for percentile in (50, 95, 99):
            result[f"p{percentile}_ms"] = (
                round(ordered[math.ceil(len(ordered) * percentile / 100) - 1], 3)
                if ordered else None
            )
        return result


class RuntimeMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._timings = {name: _Timing() for name in TIMINGS}
        self._counters = dict.fromkeys(COUNTERS, 0)
        self._http: dict[tuple[str, str, int, str], _Timing] = {}
        self._http_overflow = _Timing()
        self._gateway_closes: dict[tuple[str, int | None], int] = {}
        self._gateway_close_overflow = 0

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            self._timings[name].observe(seconds)

    def increment(self, name: str) -> None:
        with self._lock:
            self._counters[name] += 1

    def note_gateway_close(self, category: str, code: int | None = None) -> None:
        """Count a bounded, reason-level Agent WebSocket close series.

        Close reasons are deliberately supplied by the gateway rather than
        taking a peer-provided reason string.  A malformed or extension close
        code is kept as ``null`` so it cannot create unbounded labels.
        """
        category = category if category in GATEWAY_CLOSE_CATEGORIES else "other"
        if not isinstance(code, int) or not 1000 <= code <= 4999:
            code = None
        key = category, code
        with self._lock:
            if key not in self._gateway_closes:
                if len(self._gateway_closes) >= GATEWAY_CLOSE_SERIES_LIMIT:
                    self._gateway_close_overflow += 1
                    return
                self._gateway_closes[key] = 0
            self._gateway_closes[key] += 1

    @contextmanager
    def measure(self, name: str, *, failure_counter: str | None = None) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        except BaseException:
            if failure_counter is not None:
                self.increment(failure_counter)
            raise
        finally:
            self.observe(name, time.perf_counter() - started)

    def note_sqlite_error(self, error: BaseException | None) -> None:
        code = getattr(error, "sqlite_errorcode", 0) or 0
        if isinstance(error, sqlite3.Error) and code & 0xFF in (
            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
        ):
            self.increment("db_busy")

    def observe_http(
        self, scope: dict[str, Any], status: int, outcome: str, seconds: float,
    ) -> None:
        # Only framework route templates may enter labels. A missing route
        # includes early maintenance rejection and unmatched/SPA requests.
        route = getattr(scope.get("route"), "path", "")
        if not route.startswith("/api/") and route not in ("/healthz", "/readyz"):
            route = "<unmatched>"
        method = scope["method"] if scope["method"] in METHODS else "OTHER"
        key = method, route, status, outcome
        with self._lock:
            timing = self._http.get(key)
            if timing is None:
                if len(self._http) >= HTTP_SERIES_LIMIT:
                    self._http_overflow.observe(seconds)
                    return
                timing = self._http[key] = _Timing()
            timing.observe(seconds)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "scope": "process",
                "sample_limit": SAMPLE_LIMIT,
                "percentiles": "latest_samples_nearest_rank",
                "http_series_limit": HTTP_SERIES_LIMIT,
                "counters": dict(self._counters),
                "timings": {name: value.snapshot() for name, value in self._timings.items()},
                "http": [
                    {"method": method, "route": route, "status": status,
                     "outcome": outcome, **value.snapshot()}
                    for (method, route, status, outcome), value in sorted(self._http.items())
                ],
                "http_overflow": self._http_overflow.snapshot(),
                "gateway_closes": [
                    {"category": category, "code": code, "count": count}
                    for (category, code), count in sorted(
                        self._gateway_closes.items(),
                        key=lambda item: (item[0][0], item[0][1] or -1),
                    )
                ],
                "gateway_close_overflow": self._gateway_close_overflow,
            }


class MeasuredRLock:
    """Measure only outer ownership, preserving transaction reentrancy."""

    def __init__(self, metrics: RuntimeMetrics) -> None:
        self._lock = threading.RLock()
        self._local = threading.local()
        self._metrics = metrics

    def __enter__(self) -> MeasuredRLock:
        started = time.perf_counter()
        self._lock.acquire()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            self._local.started = time.perf_counter()
            self._local.wait = self._local.started - started
        self._local.depth = depth + 1
        return self

    def __exit__(self, _kind, error, _traceback) -> None:
        self._local.depth -= 1
        outer = self._local.depth == 0
        held = time.perf_counter() - self._local.started
        self._lock.release()
        if outer:
            self._metrics.observe("db_lock_wait", self._local.wait)
            self._metrics.observe("db_lock_hold", held)
            self._metrics.note_sqlite_error(error)


async def monitor_loop(metrics: RuntimeMetrics, interval: float = 1.0) -> None:
    while True:
        due = time.perf_counter() + interval
        await asyncio.sleep(interval)
        metrics.observe("loop_lag", time.perf_counter() - due)


class MetricsMiddleware:
    """Time through the final ASGI response body without buffering streams."""

    def __init__(self, app, *, metrics: RuntimeMetrics) -> None:
        self.app, self.metrics = app, metrics

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or (
            not path.startswith("/api/") and path not in ("/healthz", "/readyz")
        ):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status, finished, outcome = 500, False, "incomplete"

        async def capture(message) -> None:
            nonlocal status, finished
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                self.metrics.observe_http(scope, status, "complete", time.perf_counter() - started)
                finished = True

        try:
            await self.app(scope, receive, capture)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            if not finished:
                self.metrics.observe_http(scope, status, outcome, time.perf_counter() - started)
