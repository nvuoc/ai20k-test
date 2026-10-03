"""Bounded, published sandbox locations and directed route lookup."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.contracts.maps import (
    MapProviderError,
    Place,
    RouteResult,
    TrafficFacts,
    resolution,
    search_key,
    unresolved_language,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"


class FixtureMapAdapter:
    def __init__(self, fixture_dir: Path = FIXTURE_DIR, *, alias_path: Path | None = None,
                 service_area_path: Path | None = None, traffic_enabled: bool = True,
                 traffic_ttl_seconds: int = 60, route_ttl_seconds: int = 120) -> None:
        from app.domain.local_aliases import LocalAliasRegistry
        from app.domain.location_policy import AreaRegistry
        self.aliases = LocalAliasRegistry(alias_path, allow_fixture=True)
        self.areas = AreaRegistry(service_area_path, allow_fixture=True)
        self.traffic_enabled = traffic_enabled
        self.traffic_ttl_seconds = traffic_ttl_seconds
        self.route_ttl_seconds = route_ttl_seconds
        locations = json.loads((fixture_dir / "locations.json").read_text(encoding="utf-8"))
        routes = json.loads((fixture_dir / "route_matrix.json").read_text(encoding="utf-8"))
        self.version = locations["version"]
        self.route_version = routes["version"]
        self.places: list[dict[str, Any]] = []
        self._keys: dict[str, list[dict[str, Any]]] = {}
        for row in locations["places"]:
            place = Place(
                id=row["id"], label=row["label"], lat=row["lat"], lon=row["lon"],
                source=f"fixture:{self.version}", airport="airport" in row["place_types"],
                place_types=row["place_types"], meeting_point=row.get("meeting_point"),
                address_components=row.get("components", {}),
                metadata={"locality":row["locality"],"sandbox":True,"facility_id":row.get("facility_id")},
            ).model_dump(mode="json")
            self.places.append(place)
            self._keys[place["id"]] = [search_key(row["label"]), *map(search_key, row["aliases"])]
        for area in self.areas.entries:
            for point in area.representative_points:
                if point.id not in self._keys:
                    self.places.append(point.model_dump(mode="json"))
                    self._keys[point.id] = [search_key(point.label)]
        self._routes = {
            (row["pickup_id"], row["destination_id"], row.get("vehicle_profile", "car")): row for row in routes["routes"]
        }
        self.supported_profiles = frozenset(key[2] for key in self._routes)

    async def resolve(
        self, query: str, target: str = "pickup", context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        from app.domain.local_aliases import resolve_with_alias
        from app.domain.location_policy import area_resolution
        status, area = self.areas.lookup(query, (context or {}).get("area"))
        if status == "matched":
            return area_resolution(query, target, area, context=context)
        if status == "ambiguous":
            return resolution("ambiguous", query, target, reason="AREA_LOCALITY_REQUIRED",
                              clarification="Bạn cho mình tỉnh/thành hoặc quận/huyện để xác định đúng địa danh nhé.", context=context)
        return await resolve_with_alias(self, self.aliases, query, target, context)

    async def _resolve(
        self, query: str, target: str = "pickup", context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Map query must be a nonempty string")
        from app.domain.location_parser import parse_location, resolve_relation
        parsed = parse_location(query)
        if parsed.relation != "none" and not parsed.uncertainties:
            return await resolve_relation(self, parsed, target, context)
        query = parsed.normalized_query
        gate = unresolved_language(query)
        if gate:
            return resolution("ambiguous", query, target, reason=gate[0], clarification=gate[1], context=context)
        note = (context or {}).get("pickup_note") if target == "pickup" else None
        if isinstance(note, str) and query.endswith(", " + note):
            # A bounded update between explicitly related, catalogued entrances.
            # Preserve raw input and don't treat unverified gates as coordinates.
            updated_gate = re.search(r"\b(?:don|hen|doi(?: sang)?) (?:o |tai )?(cong (?:chinh|sau))\b", search_key(note))
            previous_key = search_key(query[:-(len(note) + 2)])
            previous = [place for place in self.places if previous_key in self._keys[place["id"]]]
            if updated_gate and len(previous) == 1:
                family = previous[0]["metadata"].get("facility_id")
                candidates = [place for place in self.places if family and
                              place["metadata"].get("facility_id") == family and
                              search_key(place.get("meeting_point") or "").startswith(updated_gate.group(1))]
                if len(candidates) == 1:
                    return resolution("resolved", query, target, candidates=candidates, context=context)
        key = search_key(query)
        exact = [place for place in self.places if key in self._keys[place["id"]]]
        if len(exact) == 1:
            return resolution("resolved", query, target, candidates=exact, context=context)
        # Substrings only suggest candidates. They never turn a partial address into a fix.
        partial = exact or [
            place for place in self.places
            if any(
                key in alias and all(
                    token in alias.split() for token in key.split() if re.search(r"\d", token)
                )
                for alias in self._keys[place["id"]]
            )
        ]
        if partial:
            return resolution("ambiguous", query, target, candidates=partial[:3],
                              reason="MULTIPLE_PLACES" if len(partial) > 1 else "PARTIAL_COMPONENT_MATCH",
                              clarification="Anh/chị chọn đúng địa điểm và cổng/ga trong danh sách nhé.", context=context)
        return resolution("not_found", query, target, reason="NO_MATCH",
                          clarification="Em chưa tìm thấy địa điểm này trong dữ liệu thử nghiệm. Anh/chị thử một địa điểm mẫu hoặc bổ sung địa chỉ nhé.", context=context, ttl=30)

    async def route(
        self, pickup: dict[str, Any], destination: dict[str, Any], vehicle_type: str | None = None,
        *, include_traffic: bool = False, departure_time: datetime | None = None,
    ) -> dict[str, Any]:
        if vehicle_type not in {None, "oto_4_cho", "oto_7_cho", "xe_may_dien"}:
            raise MapProviderError("UNSUPPORTED_VEHICLE_PROFILE")
        profile = "motorcycle" if vehicle_type == "xe_may_dien" else "car"
        pair = (pickup.get("id"), destination.get("id"), profile)
        row = self._routes.get(pair)
        if row is None:
            raise MapProviderError("ROUTE_NOT_SEEDED")
        if departure_time is not None and (not isinstance(departure_time, datetime) or not departure_time.tzinfo):
            raise MapProviderError("INVALID_DEPARTURE_TIME")
        now = datetime.now(UTC)
        data = {key: value for key, value in row.items() if key != "traffic"}
        traffic = TrafficFacts(status="unknown")
        if include_traffic and self.traffic_enabled:
            if row.get("traffic"):
                traffic = TrafficFacts(**row["traffic"], source=f"fixture:{self.route_version}:traffic",
                                       fetched_at=now, valid_until=now + timedelta(seconds=self.traffic_ttl_seconds))
            else:
                traffic = TrafficFacts(status="unavailable", source=f"fixture:{self.route_version}:traffic",
                                       fetched_at=now, valid_until=now + timedelta(seconds=self.traffic_ttl_seconds))
        return RouteResult(**data, source=f"fixture:{self.route_version}",
                           fetched_at=now, valid_until=now + timedelta(seconds=self.route_ttl_seconds),
                           departure_time=departure_time or now, eta_basis="provider_estimate", traffic=traffic).model_dump(mode="json")


MapFixtureAdapter = FixtureMapAdapter
