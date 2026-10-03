"""Durable sandbox booking provider and immutable operation ledger.

The sandbox commits the booking and its operation result in one SQLite
transaction. A lost response can therefore always be recovered by its key.
No driver or live transport service is contacted by this provider.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class SandboxBookingProvider:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sandbox_bookings (
                booking_id TEXT PRIMARY KEY,
                draft_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sandbox_operations (
                idempotency_key TEXT PRIMARY KEY,
                operation_type TEXT NOT NULL,
                booking_id TEXT,
                payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at REAL NOT NULL
            );
        """)
        self._db.commit()
        # Opt-in deterministic faults used only by tests/local demonstrations.
        self.faults: dict[str, int] = {}

    def close(self) -> None:
        self._db.close()

    def _prior(self, key: str, kind: str, payload: dict) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM sandbox_operations WHERE idempotency_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        if row["operation_type"] != kind or row["payload_hash"] != digest:
            raise ValueError("IDEMPOTENCY_PAYLOAD_CONFLICT")
        return json.loads(row["result"])

    def _record(self, key: str, kind: str, payload: dict, result: dict) -> None:
        encoded = canonical(payload)
        self._db.execute(
            "INSERT INTO sandbox_operations VALUES (?,?,?,?,?,?,?,?)",
            (key, kind, result.get("booking_id"), encoded,
             hashlib.sha256(encoded.encode()).hexdigest(), result["status"],
             canonical(result), time.time()),
        )

    def _maybe_lose_response(self, kind: str) -> None:
        fault = f"{kind}_lost_response"
        if self.faults.get(fault, 0) > 0:
            self.faults[fault] -= 1
            raise TimeoutError("SANDBOX_RESPONSE_LOST")

    async def create(self, payload: dict, idempotency_key: str) -> dict:
        with self._db:
            prior = self._prior(idempotency_key, "create", payload)
            if prior:
                return prior
            # The draft has only one logical create even across distinct confirmations.
            existing = self._db.execute(
                "SELECT * FROM sandbox_bookings WHERE draft_id=?", (payload["draft_id"],)
            ).fetchone()
            if existing:
                result = self._projection(existing)
                result["status"] = "succeeded"
                self._record(idempotency_key, "create", payload, result)
                return result
            booking_id = "SBX-" + uuid.uuid4().hex[:10].upper()
            now = time.time()
            self._db.execute(
                "INSERT INTO sandbox_bookings VALUES (?,?,?,?,?,?,?)",
                (booking_id, payload["draft_id"], payload["session_id"], canonical(payload),
                 "booked", now, now),
            )
            result = {"status": "succeeded", "booking_id": booking_id,
                      "provider": "sandbox", "provider_status": "booked",
                      "driver_name": None, "license_plate": None, "eta_minutes": None}
            self._record(idempotency_key, "create", payload, result)
        self._maybe_lose_response("create")
        return result

    async def cancel(self, booking_id: str, idempotency_key: str) -> dict:
        payload = {"booking_id": booking_id}
        with self._db:
            prior = self._prior(idempotency_key, "cancel", payload)
            if prior:
                return prior
            row = self._db.execute(
                "SELECT * FROM sandbox_bookings WHERE booking_id=?", (booking_id,)
            ).fetchone()
            if row is None:
                result = {"status": "rejected", "reason": "BOOKING_NOT_FOUND",
                          "booking_id": booking_id, "provider": "sandbox"}
            else:
                self._db.execute(
                    "UPDATE sandbox_bookings SET status='cancelled', updated_at=? WHERE booking_id=?",
                    (time.time(), booking_id),
                )
                result = {"status": "succeeded", "booking_id": booking_id,
                          "provider": "sandbox", "provider_status": "cancelled"}
            self._record(idempotency_key, "cancel", payload, result)
        self._maybe_lose_response("cancel")
        return result

    async def lookup(self, idempotency_key: str) -> dict | None:
        row = self._db.execute(
            "SELECT result FROM sandbox_operations WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        return json.loads(row["result"]) if row else None

    async def get(self, booking_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM sandbox_bookings WHERE booking_id=?", (booking_id,)
        ).fetchone()
        return self._projection(row) if row else None

    async def find_by_draft(self, draft_id: str) -> dict | None:
        """Recover a committed create whose checkpoint never integrated its result."""
        row = self._db.execute(
            "SELECT * FROM sandbox_bookings WHERE draft_id=?", (draft_id,)
        ).fetchone()
        return self._projection(row) if row else None

    @staticmethod
    def _projection(row: sqlite3.Row) -> dict:
        return {"booking_id": row["booking_id"], "provider": "sandbox",
                "provider_status": row["status"], "payload": json.loads(row["payload"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def operation_count(self, kind: str = "create") -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM sandbox_operations WHERE operation_type=?", (kind,)
        ).fetchone()[0]

    def booking_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM sandbox_bookings").fetchone()[0]
