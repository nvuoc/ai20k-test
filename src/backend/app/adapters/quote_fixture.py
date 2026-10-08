"""Archived V1–V3 sandbox quote adapter for compatibility tests.

Current runtime uses KnowledgeBase per-kilometre rates and never a trip total.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from app.adapters.map_fixture import FIXTURE_DIR
from app.contracts.maps import MapProviderError, RouteResult


class FixtureQuoteAdapter:
    def __init__(self, fixture_dir: Path = FIXTURE_DIR, ttl_seconds: int | None = None,
                 catalog_path: Path | None = None, pricing_path: Path | None = None) -> None:
        self.pricing = json.loads((pricing_path or fixture_dir / "pricing.json").read_text(encoding="utf-8"))
        self.catalog = json.loads((catalog_path or fixture_dir / "vehicle_catalog.json").read_text(encoding="utf-8"))
        self.vehicle_catalog = self.catalog["vehicles"]
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else self.pricing["ttl_seconds"]
        self.version = self.pricing["version"]
        for code, vehicle in self.vehicle_catalog.items():
            available = code in self.pricing["tariffs"] and vehicle.get("route_profile", "car") in {"car", "motorcycle"}
            if vehicle.get("bookable", True) and not available:
                raise ValueError(f"Bookable vehicle {code} requires a tariff and supported profile")
            if code == "xe_may_dien" and vehicle.get("bookable") and "allow_adult_with_child" not in vehicle:
                raise ValueError("Electric motorbike requires an explicit travel-party policy")

    def get_vehicle(self, vehicle_type: str) -> dict[str, Any] | None:
        return self.vehicle_catalog.get(vehicle_type)

    async def quote(self, route: dict[str, Any], vehicle_type: str) -> dict[str, Any]:
        route = RouteResult.model_validate(route).model_dump(mode="json")
        tariff = self.pricing["tariffs"].get(vehicle_type)
        vehicle = self.get_vehicle(vehicle_type)
        if tariff is None or not vehicle or not vehicle.get("quote_available", True):
            raise MapProviderError("UNSUPPORTED_VEHICLE")
        if route["vehicle_profile"] != vehicle.get("route_profile", "car"):
            raise MapProviderError("VEHICLE_PROFILE_MISMATCH")
        amount = Decimal(str(tariff["base_fee"])) + (
            Decimal(str(route["distance_m"])) / 1000 * Decimal(str(tariff["per_km"]))
        )
        rounding = Decimal(str(self.pricing["rounding"]))
        amount = int((amount / rounding).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * rounding)
        dependencies = {"route":route,"vehicle_type":vehicle_type,"pricing_version":self.version,
                        "catalog_version":self.catalog["version"]}
        fingerprint = hashlib.sha256(json.dumps(dependencies, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        now = datetime.now(UTC)
        return {
            "id":"quote_" + fingerprint[:20], "kind":"estimate", "amount":amount,
            "currency":self.pricing["currency"], "label":"Giá thử nghiệm", "sandbox":True,
            "created_at":now.isoformat(), "expires_at":(now + timedelta(seconds=self.ttl_seconds)).isoformat(),
            "fingerprint":fingerprint, "pricing_version":self.version,
            "catalog_version":self.catalog["version"], "vehicle_type":vehicle_type,
            "distance_m":route["distance_m"], "duration_s":route["duration_s"], "route_source":route["source"],
            "breakdown": {"base_fee":tariff["base_fee"], "per_km":tariff["per_km"], "rounding":self.pricing["rounding"]},
        }


QuoteFixtureAdapter = FixtureQuoteAdapter
QuoteAdapter = FixtureQuoteAdapter
