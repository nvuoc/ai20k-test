"""Groq request/token reservations and provider cooldown, independent of Gemini.

Only hashed buckets and numeric usage are persisted. Token reservations estimate
input and expected output; actual usage reconciles them after a response. Remote
rate-limit headers remain authoritative when the account has tighter limits.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Callable

from .extractor import ExtractorError
from .rate_limit import RateLimitError


class GroqRateLimiter:
    def __init__(self, *, bucket: str, rpm: int = 30, tpm: int = 8000,
                 sqlite_path: str | Path | None = None, clock: Callable[[], float] | None = None):
        if type(rpm) is not int or rpm < 1 or type(tpm) is not int or tpm < 1:
            raise ValueError("Groq request/token limits must be positive integers")
        self.bucket, self.rpm, self.tpm = bucket, rpm, tpm
        self.path = Path(sqlite_path).resolve() if sqlite_path else None
        self._clock = clock or (time.time if self.path else time.monotonic)
        self._lock = threading.Lock()
        self._usage: dict[str, tuple[float, int]] = {}
        self._blocked_until = 0.0
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS groq_reservations "
                    "(id TEXT PRIMARY KEY, bucket TEXT NOT NULL, reserved_at REAL NOT NULL, tokens INTEGER NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS groq_bucket_time ON groq_reservations(bucket,reserved_at)")
                db.execute("CREATE TABLE IF NOT EXISTS groq_cooldowns (bucket TEXT PRIMARY KEY, blocked_until REAL NOT NULL)")

    def _connect(self):
        return sqlite3.connect(str(self.path), timeout=2)

    def reserve(self, tokens: int) -> str:
        if type(tokens) is not int or tokens < 0:
            raise ValueError("estimated tokens must be a nonnegative integer")
        # A single oversized request can still be accepted remotely; do not create
        # an infinite local wait based on an estimate. Reserve the full window.
        tokens = min(tokens, self.tpm)
        with self._lock:
            now, reservation = self._clock(), uuid.uuid4().hex
            try:
                if self.path:
                    with closing(self._connect()) as db, db:
                        db.execute("BEGIN IMMEDIATE")
                        db.execute("DELETE FROM groq_reservations WHERE bucket=? AND reserved_at<=?", (self.bucket, now - 60))
                        rows = db.execute("SELECT reserved_at,tokens FROM groq_reservations WHERE bucket=? ORDER BY reserved_at", (self.bucket,)).fetchall()
                        cooldown = db.execute("SELECT blocked_until FROM groq_cooldowns WHERE bucket=?", (self.bucket,)).fetchone()
                        self._check(rows, tokens, now, cooldown[0] if cooldown else 0)
                        db.execute("INSERT INTO groq_reservations VALUES (?,?,?,?)", (reservation, self.bucket, now, tokens))
                else:
                    self._usage = {key: entry for key, entry in self._usage.items() if entry[0] > now - 60}
                    self._check(sorted(self._usage.values()), tokens, now, self._blocked_until)
                    self._usage[reservation] = (now, tokens)
            except sqlite3.Error:
                raise ExtractorError("PROVIDER_UNAVAILABLE", "Groq quota coordination is unavailable.", retryable=True) from None
            return reservation

    def _check(self, rows, tokens, now, cooldown):
        wait = max(0, cooldown - now)
        if len(rows) >= self.rpm:
            wait = max(wait, rows[len(rows) - self.rpm][0] + 60 - now)
        used = sum(item[1] for item in rows)
        for timestamp, count in rows:
            if used + tokens <= self.tpm:
                break
            used -= count
            wait = max(wait, timestamp + 60 - now)
        if wait > 0:
            raise RateLimitError(wait, provider="Groq", request_sent=False)

    def reconcile(self, reservation: str, actual_tokens: int) -> None:
        if type(actual_tokens) is not int or actual_tokens < 0:
            return
        with self._lock:
            try:
                if self.path:
                    with closing(self._connect()) as db, db:
                        db.execute("UPDATE groq_reservations SET tokens=? WHERE id=? AND bucket=?", (actual_tokens, reservation, self.bucket))
                elif reservation in self._usage:
                    self._usage[reservation] = (self._usage[reservation][0], actual_tokens)
            except sqlite3.Error:
                raise ExtractorError("PROVIDER_UNAVAILABLE", "Groq quota coordination is unavailable.", retryable=True) from None

    def cooldown(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds <= 0:
            seconds = 60
        with self._lock:
            until = self._clock() + min(seconds, 86400)
            try:
                if self.path:
                    with closing(self._connect()) as db, db:
                        db.execute("INSERT INTO groq_cooldowns VALUES (?,?) ON CONFLICT(bucket) DO UPDATE SET "
                            "blocked_until=MAX(blocked_until,excluded.blocked_until)", (self.bucket, until))
                self._blocked_until = max(self._blocked_until, until)
            except sqlite3.Error:
                raise ExtractorError("PROVIDER_UNAVAILABLE", "Groq quota coordination is unavailable.", retryable=True) from None


_LOCK = threading.Lock()
_LIMITERS: dict[tuple[str, str | None], GroqRateLimiter] = {}


def shared_groq_limiter(api_key: str, *, rpm: int = 30, tpm: int = 8000,
                        sqlite_path: str | Path | None = None, model: str = "openai/gpt-oss-120b"):
    bucket = hashlib.sha256((api_key + "\0" + model).encode()).hexdigest()
    path = str(Path(sqlite_path).resolve()) if sqlite_path else None
    with _LOCK:
        existing = _LIMITERS.get((bucket, path))
        if existing:
            existing.rpm, existing.tpm = min(existing.rpm, rpm), min(existing.tpm, tpm)
            return existing
        limiter = GroqRateLimiter(bucket=bucket, rpm=rpm, tpm=tpm, sqlite_path=path)
        _LIMITERS[(bucket, path)] = limiter
        return limiter
