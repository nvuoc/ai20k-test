"""Atomic rolling-window reservations shared by all Gemini clients for a key.

Reservations are made before HTTP and are never refunded: network failures,
repair attempts, and rejected requests can still count at the provider. SQLite
adds coordination across workers and restarts when configured on a local disk.
Only SHA-256 bucket identifiers and timestamps are persisted, never API keys.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Callable

from .extractor import ExtractorError


class RateLimitError(ExtractorError):
    def __init__(self, retry_after_seconds: float, *, provider: str = "Gemini", request_sent: bool | None = None) -> None:
        super().__init__(
            "RATE_LIMITED", f"{provider} request quota is temporarily exhausted.", retryable=False
        )
        self.retry_after_seconds = max(1, math.ceil(retry_after_seconds))
        self.request_sent = request_sent


class RollingWindowRateLimiter:
    """Fail fast at the quota; never hold a chat request for a full minute."""

    def __init__(
        self,
        *,
        bucket: str,
        limit: int = 15,
        window_seconds: float = 60.0,
        sqlite_path: str | Path | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if type(limit) is not int or not 1 <= limit <= 15:
            raise ValueError("Gemini RPM must be an integer from 1 to 15")
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("quota window must be positive")
        self.bucket = bucket
        self.limit = limit
        self.window_seconds = window_seconds
        self.sqlite_path = Path(sqlite_path).resolve() if sqlite_path is not None else None
        self._clock = clock or (time.time if self.sqlite_path else time.monotonic)
        self._timestamps: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = threading.Lock()
        if self.sqlite_path:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS gemini_reservations "
                    "(bucket TEXT NOT NULL, reserved_at REAL NOT NULL)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS gemini_reservations_bucket_time "
                    "ON gemini_reservations(bucket, reserved_at)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS gemini_cooldowns "
                    "(bucket TEXT PRIMARY KEY, blocked_until REAL NOT NULL)"
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.sqlite_path), timeout=2.0)

    def reserve(self) -> None:
        with self._lock:
            now = self._clock()
            if self.sqlite_path:
                self._reserve_sqlite(now)
                return
            while self._timestamps and self._timestamps[0] <= now - self.window_seconds:
                self._timestamps.popleft()
            wait = max(0.0, self._blocked_until - now)
            if len(self._timestamps) >= self.limit:
                wait = max(wait, self._timestamps[0] + self.window_seconds - now)
            if wait > 0:
                raise RateLimitError(wait, request_sent=False)
            self._timestamps.append(now)

    def _reserve_sqlite(self, now: float) -> None:
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM gemini_reservations WHERE bucket=? AND reserved_at<=?",
                    (self.bucket, now - self.window_seconds),
                )
                count, oldest = connection.execute(
                    "SELECT COUNT(*), MIN(reserved_at) FROM gemini_reservations WHERE bucket=?",
                    (self.bucket,),
                ).fetchone()
                cooldown = connection.execute(
                    "SELECT blocked_until FROM gemini_cooldowns WHERE bucket=?", (self.bucket,)
                ).fetchone()
                wait = max(0.0, cooldown[0] - now) if cooldown else 0.0
                if count >= self.limit:
                    wait = max(wait, oldest + self.window_seconds - now)
                if wait > 0:
                    raise RateLimitError(wait, request_sent=False)
                connection.execute(
                    "INSERT INTO gemini_reservations(bucket, reserved_at) VALUES (?, ?)",
                    (self.bucket, now),
                )
        except sqlite3.Error:
            # Failing open here would violate the quota when coordination is lost.
            raise ExtractorError(
                "PROVIDER_UNAVAILABLE", "Gemini quota coordination is unavailable."
            ) from None

    def cooldown(self, seconds: float) -> None:
        with self._lock:
            blocked_until = self._clock() + max(1.0, seconds)
            if self.sqlite_path:
                try:
                    with closing(self._connect()) as connection, connection:
                        connection.execute(
                            "INSERT INTO gemini_cooldowns(bucket, blocked_until) VALUES (?, ?) "
                            "ON CONFLICT(bucket) DO UPDATE SET "
                            "blocked_until=MAX(blocked_until, excluded.blocked_until)",
                            (self.bucket, blocked_until),
                        )
                except sqlite3.Error:
                    raise ExtractorError(
                        "PROVIDER_UNAVAILABLE", "Gemini quota coordination is unavailable."
                    ) from None
            self._blocked_until = max(self._blocked_until, blocked_until)


_GLOBAL_LOCK = threading.Lock()
_GLOBAL_LIMITERS: dict[str, RollingWindowRateLimiter] = {}


def shared_gemini_limiter(
    api_key: str,
    *,
    rpm: int = 15,
    sqlite_path: str | Path | None = None,
    project_bucket: str | None = None,
) -> RollingWindowRateLimiter:
    """Use project_bucket to share quota across keys in the same Google project.

    Configure the same SQLite path in every worker. The first client establishes
    a bucket's persistence mode; conflicting modes are rejected, never isolated.
    """
    if type(rpm) is not int or not 1 <= rpm <= 15:
        raise ValueError("Gemini RPM must be an integer from 1 to 15")
    bucket = hashlib.sha256((project_bucket or api_key).encode("utf-8")).hexdigest()
    path = Path(sqlite_path).resolve() if sqlite_path is not None else None
    with _GLOBAL_LOCK:
        existing = _GLOBAL_LIMITERS.get(bucket)
        if existing:
            if existing.sqlite_path != path:
                raise ValueError("conflicting persistence configuration for Gemini quota bucket")
            existing.limit = min(existing.limit, rpm)
            return existing
        limiter = RollingWindowRateLimiter(bucket=bucket, limit=rpm, sqlite_path=path)
        _GLOBAL_LIMITERS[bucket] = limiter
        return limiter
