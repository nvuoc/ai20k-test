"""Customer address history keyed by phone, independent of conversation IDs."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from app.contracts.booking import AddressSlot, normalize_phone
from app.contracts.maps import Place


class SQLiteCRM:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS crm_customers (
              phone TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS crm_addresses (
              phone TEXT NOT NULL, label TEXT NOT NULL, address TEXT NOT NULL,
              updated_at REAL NOT NULL, uses INTEGER NOT NULL DEFAULT 1,
              PRIMARY KEY(phone,label,address));
        """)
        self.db.commit()

    def customer(self, phone: str, name: str):
        with self.db:
            self.db.execute("INSERT INTO crm_customers VALUES(?,?) ON CONFLICT(phone) "
                            "DO UPDATE SET name=excluded.name", (normalize_phone(phone), name))

    def remember(self, phone: str, label: str, address: dict):
        AddressSlot.model_validate(address)
        if address.get("status") != "confirmed" or not address.get("coords"):
            raise ValueError("CRM history requires a confirmed address")
        Place.model_validate(address.get("metadata", {}).get("place"))
        encoded = json.dumps(address, ensure_ascii=False, sort_keys=True)
        with self.db:
            self.db.execute("INSERT INTO crm_addresses VALUES(?,?,?,?,1) ON CONFLICT(phone,label,address) "
                            "DO UPDATE SET updated_at=excluded.updated_at,uses=uses+1",
                            (normalize_phone(phone), label, encoded, time.time()))

    def suggestions(self, phone: str, label: str | None = None, limit: int = 2):
        rows = self.db.execute("SELECT address FROM crm_addresses WHERE phone=? "
                               "AND (? IS NULL OR label=?) ORDER BY updated_at DESC,uses DESC LIMIT ?",
                               (normalize_phone(phone), label, label, min(2, max(1, limit)))).fetchall()
        return [json.loads(row["address"]) for row in rows]

    def close(self):
        self.db.close()
