"""Versioned local KB retrieval for static policies and per-kilometre tariffs."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from app.contracts.maps import search_key

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
METER_NOTE = "Tiền cuối cùng tính theo đồng hồ và quãng đường thực tế tài xế đi."


class KnowledgeBase:
    def __init__(self, path: Path | None = None):
        self.data = json.loads((path or ROOT / "knowledge_base.json").read_text(encoding="utf-8"))
        self.version = self.data["version"]
        self.vehicle_catalog = self.data["vehicles"]
        if not self.version.strip() or set(self.vehicle_catalog) != {"xe_may", "oto_4_cho", "oto_7_cho"}:
            raise ValueError("Knowledge Base needs a source version and the three supported vehicles")
        for code, row in self.vehicle_catalog.items():
            rate = row.get("per_km")
            if (type(rate) not in {int, float} or not math.isfinite(rate) or rate < 0
                    or type(row.get("max_passengers")) is not int or row["max_passengers"] < 1
                    or row.get("route_profile") != ("motorcycle" if code == "xe_may" else "car")):
                raise ValueError("Invalid Knowledge Base tariff or vehicle profile")
        self.catalog = {"version": self.version}
        self.pricing = {"tariffs": {code: {"per_km": row["per_km"]}
                                   for code, row in self.vehicle_catalog.items()}}

    def retrieve(self, text: str, vehicle: str | None = None):
        key = search_key(text)
        def matches(word):
            if word == "chó":
                return bool(re.search(r"\bchó\b", text.casefold()) or re.search(r"\bmang cho\b", key))
            return bool(re.search(r"\b" + re.escape(search_key(word)) + r"\b", key))
        hits = [row for row in self.data["policies"]
                if any(matches(word) for word in row["keywords"])]
        return [{"id": row["id"], "text": row["answer"], "source": self.version} for row in hits]

    def tariff(self, vehicle: str):
        row = self.vehicle_catalog[vehicle]
        return {"per_km": row["per_km"], "currency": "VND", "vehicle_type": vehicle,
                "source": self.version, "final_amount_basis": "meter"}

    def price_text(self, vehicle: str | None = None):
        codes = [vehicle] if vehicle in self.vehicle_catalog else list(self.vehicle_catalog)
        return "; ".join(f"{self.vehicle_catalog[code]['label']}: "
                         f"{self.vehicle_catalog[code]['per_km']:,.0f} đồng/km" for code in codes) + ". " + METER_NOTE

    async def quote(self, route, vehicle_type):
        # Compatibility for route inquiries: this is a tariff, never a trip estimate.
        return self.tariff(vehicle_type)
