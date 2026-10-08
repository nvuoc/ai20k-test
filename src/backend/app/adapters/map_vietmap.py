"""Server-side VIETMAP search/place and real road routing.

Official protocols:
https://maps.vietmap.vn/docs/map-api/geocodev3/
https://maps.vietmap.vn/docs/map-api/autocomplete-version/autocomplete-v4/
https://maps.vietmap.vn/docs/map-api/place-v4/
https://maps.vietmap.vn/docs/map-api/route-version/route-v4/
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from app.contracts.maps import (
    AreaFact,
    MapProviderError,
    Place,
    RouteResult,
    TrafficFacts,
    resolution,
    search_key,
    unresolved_language,
)


class _SecretFilter(logging.Filter):
    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        rendered = rendered.replace(self.secret, "[REDACTED]")
        rendered = re.sub(r"([?&]apikey=)[^&\s\"]+", r"\1[REDACTED]", rendered)
        record.msg, record.args = rendered, ()
        return True


class VietMapAdapter:
    """Never substitutes fixture data when a live lookup fails."""
    supported_profiles = frozenset({"car", "motorcycle"})

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_version: str = "v4",
        timeout_seconds: float = 8.0,
        cache_seconds: int = 300,
        alias_path: Path | None = None,
        service_area_path: Path | None = None,
        traffic_enabled: bool = True,
        traffic_ttl_seconds: int = 60,
        route_ttl_seconds: int = 120,
        voice_top_two: bool = False,
    ) -> None:
        from app.domain.local_aliases import LocalAliasRegistry
        self.aliases = LocalAliasRegistry(alias_path)
        from app.domain.location_policy import AreaRegistry
        self.areas = AreaRegistry(service_area_path)
        if not api_key.strip():
            raise ValueError("VIETMAP_API_KEY is required for live maps")
        if api_version not in {"v3", "v4"}:
            raise ValueError("VIETMAP_API_VERSION must be v3 or v4")
        self._api_key = api_key
        self.api_version = api_version
        self.voice_top_two = voice_top_two
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
        self._owns_client = client is None
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        if min(traffic_ttl_seconds, route_ttl_seconds) <= 0:
            raise ValueError("Route and traffic TTL must be positive")
        self.traffic_enabled = traffic_enabled
        self.traffic_ttl_seconds = traffic_ttl_seconds
        self.route_ttl_seconds = route_ttl_seconds
        self._semaphore = asyncio.Semaphore(2)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._route_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # httpx emits request URLs at INFO, and VIETMAP authenticates in a query.
        # Redact those records even if an embedding application enables INFO logs.
        for logger_name in ("httpx", "httpcore"):
            logging.getLogger(logger_name).addFilter(_SecretFilter(api_key))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, params: list[tuple[str, str]]) -> Any:
        params = [("apikey", self._api_key), *params]
        try:
            async with self._semaphore:
                response = await self._client.get(
                    "https://maps.vietmap.vn/api/" + path,
                    params=params,
                    timeout=self.timeout_seconds,
                )
            if response.status_code in {401, 403}:
                raise MapProviderError("PROVIDER_AUTH_ERROR")
            if response.status_code == 429:
                raise MapProviderError("PROVIDER_RATE_LIMIT")
            if response.status_code >= 500:
                raise MapProviderError("PROVIDER_UNAVAILABLE")
            if response.status_code != 200:
                raise MapProviderError("PROVIDER_INVALID_RESPONSE")
            payload = response.json()
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise MapProviderError("PROVIDER_TIMEOUT") from None
        except httpx.NetworkError:
            raise MapProviderError("PROVIDER_UNAVAILABLE") from None
        except (httpx.HTTPError, ValueError):
            raise MapProviderError("PROVIDER_INVALID_RESPONSE") from None
        if isinstance(payload, dict) and payload.get("code") not in {None, "OK"}:
            reasons = {
                "OVER_DAILY_LIMIT": "PROVIDER_RATE_LIMIT",
                "INVALID_API_KEY": "PROVIDER_AUTH_ERROR",
                "ZERO_RESULTS": "NO_MATCH",
                "ERROR_UNKNOWN": "PROVIDER_UNAVAILABLE",
            }
            raise MapProviderError(reasons.get(str(payload["code"]), "PROVIDER_INVALID_RESPONSE"))
        return payload

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list) and all(isinstance(row, dict) for row in payload):
            return payload
        if isinstance(payload, dict):
            for field in ("data", "results"):
                if field in payload:
                    return VietMapAdapter._rows(payload[field])
            if payload.get("ref_id"):
                return [payload]
        raise MapProviderError("PROVIDER_INVALID_RESPONSE")

    @staticmethod
    def _matches(query: str, suggestion: dict[str, Any], detail: dict[str, Any]) -> bool:
        label = search_key(" ".join(str(row.get(field, "")) for row in (suggestion, detail) for field in ("display", "name", "address", "street", "ward", "district", "city")))
        # Include the old-format record supplied by v4 rather than inventing an admin mapping.
        old = suggestion.get("data_old")
        if isinstance(old, dict):
            label += " " + search_key(str(old.get("display", "")))
        key = search_key(query)
        house = re.match(r"^(?:(?:nha|so)\s+)?(\d+[a-z]?(?:/\d+[a-z]?)*)(?:\s|$)", key)
        if house and str(detail.get("hs_num", "")).casefold() != house.group(1):
            return False
        detail_city = search_key(str(detail.get("city", "")))
        for city in ("ha noi", "ho chi minh"):
            if city in key and detail_city and city not in detail_city:
                return False
        ignored = {"o", "tai", "dia", "chi", "duong", "nha", "so", "tp", "thanh", "pho"}
        tokens = [token for token in key.split() if token not in ignored]
        label_tokens = set(label.split())
        return bool(tokens) and all(token in label_tokens for token in tokens)

    @staticmethod
    def _suggestion_relevant(query, suggestion):
        """Shortlist by complete source text; detail lookup still proves identity."""
        labels = [str(suggestion.get(field, "")) for field in ("name", "display", "address")]
        for variant in ("data_old", "data_new"):
            if isinstance(suggestion.get(variant), dict):
                labels.extend(str(suggestion[variant].get(field, "")) for field in ("name", "display", "address"))
        tokens = set(search_key(" ".join(labels)).split())
        ignored = {"o", "tai", "dia", "chi", "duong", "nha", "so", "tp", "thanh", "pho"}
        required = [token for token in search_key(query).split() if token not in ignored]
        return bool(required) and all(token in tokens for token in required)

    @staticmethod
    def _place(suggestion: dict[str, Any], detail: dict[str, Any], version: str) -> dict[str, Any]:
        ref_id = suggestion.get("ref_id")
        label = suggestion.get("display") or detail.get("display") or suggestion.get("name")
        if not isinstance(ref_id, str) or not isinstance(label, str) or not label.strip():
            raise MapProviderError("PROVIDER_INVALID_RESPONSE")
        categories = suggestion.get("categories") or detail.get("categories") or []
        # Classification is sourced metadata, never the word "airport" in a label.
        category_names = {
            search_key(str(item.get("name", item.get("label", ""))))
            if isinstance(item, dict) else search_key(str(item))
            for item in categories
        }
        airport = bool(category_names & {"airport", "san bay"})
        try:
            return Place(
                id=ref_id, label=label, lat=detail["lat"], lon=detail["lng"],
                source=f"vietmap:place:{version}", airport=airport,
                place_types=["airport"] if airport else ["provider_place"],
                meeting_point=suggestion.get("meeting_point"),
                address_components={field:detail.get(field) for field in ("hs_num", "street", "ward", "district", "city")},
                metadata={"categories":categories,"sandbox":False,
                          "access":"provider_entry_point" if suggestion.get("meeting_point") else "unknown",
                          "facility_id":suggestion.get("facility_id")},
            ).model_dump(mode="json")
        except (KeyError, TypeError, ValidationError):
            raise MapProviderError("PROVIDER_INVALID_RESPONSE") from None

    async def _provider_area(self, row, *, detail=None):
        """Entrances carry provider coordinates; an area's center never does."""
        from app.domain.location_policy import provider_area_kind
        entrances = row.get("entry_points") or (detail or {}).get("entry_points") or []
        representatives = []
        if isinstance(entrances, list):
            sourced = [entry for entry in entrances if isinstance(entry, dict)
                       and isinstance(entry.get("ref_id"), str) and entry["ref_id"]
                       and isinstance(entry.get("name"), str) and entry["name"].strip()]
            # Stable source identities, independent of search ranking or reload.
            sourced.sort(key=lambda entry: (search_key(entry["name"]), entry["ref_id"]))
            for entrance in sourced[:3]:
                suggestion = {**row, "ref_id": entrance["ref_id"], "facility_id": row["ref_id"],
                              "display": str(row.get("display") or row.get("name")) + ", " + entrance["name"],
                              "meeting_point": entrance["name"]}
                try:
                    point_detail = await self._get(f"place/{self.api_version}", [("refid", entrance["ref_id"])])
                    if isinstance(point_detail, dict):
                        representatives.append(self._place(suggestion, point_detail, self.api_version))
                except MapProviderError:
                    # Area identity is still usable for clarification, even
                    # when its optional representative cannot be validated.
                    continue
        return AreaFact(area_id=row["ref_id"], label=row.get("display") or row.get("name"),
                        source=f"vietmap:{'place' if detail else 'search'}:{self.api_version}",
                        locality=(detail or {}).get("city") or row.get("address"),
                        place_types=[provider_area_kind(detail or row)],
                        representative_points=representatives)

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
        if len(query) > 500:
            return resolution("ambiguous", query, target, reason="PREMISE_DETAIL_MISSING", clarification="Anh/chị nhập một địa điểm hoặc địa chỉ ngắn gọn cho mỗi điểm nhé.", context=context)
        cache_key = f"{self.api_version}:{target}:{search_key(query)}"
        cached = self._cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            result = copy.deepcopy(cached[1])
            result["query"] = query
            result["binding"] = resolution("ambiguous", query, target, context=context)["binding"]
            return result
        try:
            params = [("text", query)]
            if self.api_version == "v4":
                params.append(("display_type", "5"))
            rows = self._rows(await self._get(f"search/{self.api_version}", params))
            if not rows:
                rows = self._rows(await self._get(f"autocomplete/{self.api_version}", params))
            if not rows:
                return resolution("not_found", query, target, reason="NO_MATCH", context=context, ttl=30)
            # Preserve VietMap order; never substitute result 3+ in voice mode.
            if self.voice_top_two:
                rows = rows[:2]
            from app.domain.location_policy import area_resolution, provider_area_kind
            broad_rows = [row for row in rows if provider_area_kind(row)]
            area_matches = [row for row in broad_rows if self._suggestion_relevant(query, row)]
            rows = [row for row in rows if not provider_area_kind(row)]
            if area_matches or not rows:
                matched = area_matches
                matched = list({row.get("ref_id"):row for row in matched}.values())
                if len(matched) == 1:
                    row = matched[0]
                    _, reviewed = self.areas.lookup(query, provider_ref_id=row.get("ref_id"))
                    area = reviewed or await self._provider_area(row)
                    return area_resolution(query, target, area, context=context)
                return resolution("ambiguous", query, target, reason="AREA_LOCALITY_REQUIRED",
                                  clarification="Bạn cho mình tỉnh/thành hoặc quận/huyện để xác định đúng địa danh nhé.", context=context)
            # An entry-point ref identifies an entrance, unlike a POI centroid.
            # Offer sourced entrances before routing to a large site's center.
            entrance_selection_required = False
            expanded = []
            for row in rows:
                if self.voice_top_two:
                    expanded.append(row)
                    continue
                entrances = row.get("entry_points") or []
                if entrances:
                    if not isinstance(entrances, list):
                        raise MapProviderError("PROVIDER_INVALID_RESPONSE")
                    for entrance in entrances:
                        if not isinstance(entrance, dict) or not entrance.get("ref_id") or not entrance.get("name"):
                            raise MapProviderError("PROVIDER_INVALID_RESPONSE")
                        entry_name = str(entrance["name"])
                        specific = set(search_key(entry_name).split()) <= set(search_key(query).split())
                        entrance_selection_required = entrance_selection_required or not specific
                        expanded.append({**row, "facility_id":row["ref_id"], "ref_id":entrance["ref_id"],
                                         "display":str(row.get("display", row.get("name", ""))) + ", " + entry_name,
                                         "name":str(row.get("name", "")) + " " + entry_name,
                                         "meeting_point":entry_name,"entry_points":[],
                                         "_needs_entrance_selection":not specific})
                else:
                    expanded.append(row)
            rows = expanded[:2] if self.voice_top_two else expanded
            # Deduplicate exact identities, never nearby coordinates.
            rows = list({row.get("ref_id"):row for row in rows}.values())
            relevant_rows = [row for row in rows if self._suggestion_relevant(query, row)]
            # Missing display components require detail lookup, not a forced first choice.
            house_query = bool(re.match(r"^(?:(?:nha|so)\s+)?\d", search_key(query)))
            # Nearby number/suffix suggestions remain competitors until their
            # full address detail has been inspected; do not discard them from
            # display text alone. Retain the legacy bounded address budget.
            rows = rows if house_query or self.voice_top_two else relevant_rows or rows
            detail_budget = 2 if self.voice_top_two else (3 if house_query else 6)
            entrance_selection_required = any(row.get("_needs_entrance_selection") for row in rows)
            candidates: list[dict[str, Any]] = []
            areas = []
            for row in rows[:detail_budget]:
                ref_id = row.get("ref_id")
                if not isinstance(ref_id, str) or not ref_id:
                    raise MapProviderError("PROVIDER_INVALID_RESPONSE")
                detail = await self._get(f"place/{self.api_version}", [("refid", ref_id)])
                if not isinstance(detail, dict):
                    raise MapProviderError("PROVIDER_INVALID_RESPONSE")
                if self.voice_top_two or self._matches(query, row, detail):
                    kind = provider_area_kind(detail)
                    if kind:
                        areas.append(await self._provider_area(row, detail=detail))
                    else:
                        candidates.append(self._place(row, detail, self.api_version))
            if not candidates and len(areas) == 1 and len(rows) <= detail_budget:
                return area_resolution(query, target, areas[0], context=context)
            if not candidates:
                return resolution("ambiguous", query, target, reason="PARTIAL_COMPONENT_MATCH", clarification="Kết quả bản đồ chưa khớp đủ chi tiết anh/chị cung cấp. Anh/chị kiểm tra số nhà, tên đường hoặc cổng giúp em nhé.", context=context)
            if len(candidates) == 1 and len(rows) <= detail_budget and not areas and not entrance_selection_required:
                result = resolution("resolved", query, target, candidates=candidates, context=context, ttl=self.cache_seconds, entity_kind="point")
            else:
                result = resolution("ambiguous", query, target, candidates=candidates, reason="MULTIPLE_PLACES", context=context, ttl=self.cache_seconds)
            # Bounded cache for public place data, with no user context or consent.
            if len(self._cache) >= 256:
                self._cache.pop(next(iter(self._cache)))
            saved = copy.deepcopy(result)
            saved["binding"] = {}
            self._cache[cache_key] = (time.monotonic() + self.cache_seconds, saved)
            return result
        except (KeyError, TypeError, ValidationError):
            return resolution("unavailable", query, target, reason="PROVIDER_INVALID_RESPONSE", context=context, ttl=0,
                              clarification="Dịch vụ bản đồ trả dữ liệu địa điểm chưa hợp lệ. Bạn thử lại sau nhé.")
        except MapProviderError as error:
            if error.reason == "NO_MATCH":
                return resolution("not_found", query, target, reason="NO_MATCH", context=context, ttl=30)
            return resolution("unavailable", query, target, reason=error.reason, context=context, ttl=0,
                              clarification="Dịch vụ bản đồ đang không khả dụng. Anh/chị thử lại sau ít phút nhé.")

    async def route(
        self, pickup: dict[str, Any], destination: dict[str, Any], vehicle_type: str | None = None,
        *, include_traffic: bool = False, departure_time: datetime | None = None,
    ) -> dict[str, Any]:
        try:
            pickup_place = Place.model_validate(pickup)
            destination_place = Place.model_validate(destination)
        except ValidationError:
            raise MapProviderError("INVALID_ROUTE_POINTS") from None
        if vehicle_type not in {None, "xe_may", "oto_4_cho", "oto_7_cho", "xe_may_dien"}:
            raise MapProviderError("UNSUPPORTED_VEHICLE_PROFILE")
        profile = "motorcycle" if vehicle_type in {"xe_may", "xe_may_dien"} else "car"
        if departure_time is not None and (not isinstance(departure_time, datetime) or not departure_time.tzinfo):
            raise MapProviderError("INVALID_DEPARTURE_TIME")
        requested_traffic = include_traffic and self.traffic_enabled
        departure = departure_time.astimezone(UTC) if departure_time else None
        now = datetime.now(UTC)
        cache_key = repr((pickup_place.id, pickup_place.lat, pickup_place.lon,
                          destination_place.id, destination_place.lat, destination_place.lon,
                          profile, requested_traffic, departure.isoformat() if departure else None))
        cached = self._route_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return copy.deepcopy(cached[1])
        params = [
            ("point", f"{pickup_place.lat},{pickup_place.lon}"),
            ("point", f"{destination_place.lat},{destination_place.lon}"),
            ("vehicle", profile), ("points_encoded", "true"),
        ]
        if departure:
            params.append(("time", departure.isoformat().replace("+00:00", "Z")))
        if requested_traffic:
            params.append(("annotations", "congestion,congestion_distance"))
        traffic_failed = False
        try:
            payload = await self._get("route/v4", params)
        except MapProviderError:
            if not requested_traffic:
                raise
            # Optional annotations must not prevent a normal route/quote.
            traffic_failed = True
            payload = await self._get("route/v4", [(key, value) for key, value in params if key != "annotations"])
        try:
            path = payload["paths"][0]
            now = datetime.now(UTC)
            route = RouteResult(
                pickup_id=pickup_place.id, destination_id=destination_place.id,
                distance_m=path["distance"], duration_s=float(path["time"]) / 1000,
                source="vietmap:route:v4", vehicle_profile=profile,
                fetched_at=now, valid_until=now + timedelta(seconds=self.route_ttl_seconds),
                departure_time=departure or now, eta_basis="provider_estimate",
                traffic=self._traffic(path, now, requested_traffic, traffic_failed),
            ).model_dump(mode="json")
            ttl = min(self.route_ttl_seconds, self.traffic_ttl_seconds) if requested_traffic else self.route_ttl_seconds
            if len(self._route_cache) >= 256:
                self._route_cache.pop(next(iter(self._route_cache)))
            self._route_cache[cache_key] = (time.monotonic() + ttl, copy.deepcopy(route))
            return route
        except (KeyError, IndexError, TypeError, ValueError, ValidationError):
            raise MapProviderError("PROVIDER_INVALID_RESPONSE") from None

    def _traffic(self, path, now, requested, failed=False):
        if not requested:
            return TrafficFacts(status="unknown")
        facts = {"status": "unavailable", "source": "vietmap:route:v4:annotations",
                 "fetched_at": now, "valid_until": now + timedelta(seconds=self.traffic_ttl_seconds)}
        if failed:
            return TrafficFacts(**facts)
        annotations = path.get("annotations")
        if isinstance(annotations, list) and all(isinstance(row, dict) for row in annotations):
            merged = {}
            for annotation in annotations:
                for key in ("congestion", "congestion_distance"):
                    if key in annotation:
                        value = annotation[key]
                        if not isinstance(value, list):
                            return TrafficFacts(**facts)
                        merged.setdefault(key, []).extend(value)
            annotations = merged
        if not isinstance(annotations, dict):
            return TrafficFacts(**facts)
        congestion = annotations.get("congestion", [])
        distances = annotations.get("congestion_distance", [])
        levels = {"unknown", "low", "moderate", "heavy", "severe"}
        if not isinstance(congestion, list) or any(not isinstance(value, str) or value not in levels for value in congestion):
            return TrafficFacts(**facts)
        if not isinstance(distances, list) or any(isinstance(value, bool) or not isinstance(value, (int, float))
                                                or not math.isfinite(value) or value < 0 for value in distances):
            return TrafficFacts(**facts)
        if sum(distances) > float(path.get("distance", 0)) + 1:
            return TrafficFacts(**facts)
        facts.update(status="available" if any(value != "unknown" for value in congestion) else "unknown",
                     congestion=congestion, congestion_distance=distances)
        return TrafficFacts(**facts)


VietmapAdapter = VietMapAdapter
