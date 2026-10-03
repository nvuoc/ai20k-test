"""Provider-neutral map boundary. Coordinates always carry their data source."""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.contracts.location import ParsedLocation

MAP_CONTRACT_VERSION = "map-resolution-1"


class MapProviderError(Exception):
    """A safe reason code; never expose a provider URL or credentials."""

    def __init__(self, reason: str = "PROVIDER_UNAVAILABLE") -> None:
        self.reason = reason
        super().__init__(reason)


class Place(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    source: str = Field(min_length=1)
    airport: bool = False
    place_types: list[str] = Field(default_factory=list)
    meeting_point: str | None = None
    address_components: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def no_null_island(self) -> Place:
        if self.lat == 0 and self.lon == 0:
            raise ValueError("(0,0) is not a usable VIETMAP location")
        return self


class AreaFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    source: str = Field(min_length=1)
    locality: str | None = None
    place_types: list[str] = Field(default_factory=list)
    representative_points: list[Place] = Field(default_factory=list)
    requires_exact_point: bool = True


class MapResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["map-resolution-1"] = MAP_CONTRACT_VERSION
    status: Literal["resolved", "ambiguous", "not_found", "unavailable"]
    query: str
    target: str = "pickup"
    candidates: list[Place] = Field(default_factory=list)
    place: Place | None = None
    reason_codes: list[str] = Field(default_factory=list)
    clarification: str | None = None
    resolved_at: str
    expires_at: str
    binding: dict[str, Any] = Field(default_factory=dict)
    parsed_location: ParsedLocation | None = None
    anchors: list[Place] = Field(default_factory=list)
    entity_kind: Literal["point", "area", "unknown"] = "unknown"
    area: AreaFact | None = None

    @model_validator(mode="after")
    def resolved_has_place(self) -> MapResolution:
        if (self.status == "resolved") != (self.place is not None):
            raise ValueError("Only resolved results may contain a place")
        return self


class TrafficFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "unknown", "unavailable", "stale"] = "unknown"
    source: str | None = None
    fetched_at: datetime | None = None
    valid_until: datetime | None = None
    congestion: list[Literal["unknown", "low", "moderate", "heavy", "severe"]] = Field(default_factory=list)
    congestion_distance: list[float] = Field(default_factory=list)

    @field_validator("fetched_at", "valid_until")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Fact timestamps must include a timezone")
        return value

    @field_validator("congestion_distance")
    @classmethod
    def valid_distances(cls, values: list[float]) -> list[float]:
        if any(value < 0 or not math.isfinite(value) for value in values):
            raise ValueError("Traffic distances must be finite and nonnegative")
        return values

    @model_validator(mode="after")
    def sourced_available_facts(self) -> TrafficFacts:
        if self.fetched_at and self.valid_until and self.valid_until <= self.fetched_at:
            raise ValueError("Traffic expiry must follow fetch time")
        if self.status == "available" and (
            not self.source or not self.source.strip() or not self.fetched_at or not self.valid_until
            or not any(level != "unknown" for level in self.congestion)
        ):
            raise ValueError("Available traffic needs source, freshness and known segment levels")
        return self


class RouteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    distance_m: float = Field(gt=0, allow_inf_nan=False)
    duration_s: float = Field(gt=0, allow_inf_nan=False)
    source: str = Field(min_length=1)
    pickup_id: str
    destination_id: str
    vehicle_profile: str = "car"
    fetched_at: datetime | None = None
    valid_until: datetime | None = None
    departure_time: datetime | None = None
    eta_basis: Literal["provider_estimate", "traffic_adjusted", "unknown"] = "provider_estimate"
    traffic: TrafficFacts = Field(default_factory=TrafficFacts)

    @field_validator("fetched_at", "valid_until", "departure_time")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Fact timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def ordered_freshness(self) -> RouteResult:
        if self.fetched_at and self.valid_until and self.valid_until <= self.fetched_at:
            raise ValueError("Route expiry must follow fetch time")
        return self

    @field_validator("distance_m", "duration_s")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Route values must be finite")
        return value


class MapAdapter(Protocol):
    async def resolve(
        self, query: str, target: str = "pickup", context: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def route(
        self, pickup: dict[str, Any], destination: dict[str, Any], vehicle_type: str | None = None,
        *, include_traffic: bool = False, departure_time: datetime | None = None,
    ) -> dict[str, Any]: ...


def search_key(text: str) -> str:
    """Secondary search key; raw text and sourced labels remain untouched."""
    text = unicodedata.normalize("NFKD", text.casefold().replace("đ", "d"))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"(?<=\d)\s*(?:xuyet|sec|suoc|gach cheo)\s*(?=\d)", "/", text)
    text = re.sub(r"\b(?:tp\.?\s*hcm|tphcm|hcm|sai gon)\b", "ho chi minh", text)
    text = re.sub(r"\b(?:hn)\b", "ha noi", text)
    text = re.sub(r"\b(?:thanh pho|tp)\.?\s+", "", text)
    text = re.sub(r"[^a-z0-9/\-\s]", " ", text)
    return " ".join(text.split())


def unresolved_language(query: str) -> tuple[str, str] | None:
    """Conservative gates for details a provider cannot silently discard."""
    key = search_key(query)
    if re.search(r"\b(hinh nhu|co le|khong nho|khong chac|hay|hoac)\b", key):
        return "UNCERTAIN_COMPONENT", "Anh/chị xác định lại địa điểm hoặc số nhà giúp em nhé."
    if re.search(r"\b(gan|doi dien|canh|dang sau|phia sau|ben trai|ben phai|ben kia|giua)\b", key):
        return "RELATION_UNRESOLVED", "Anh/chị cho em một điểm hẹn cụ thể, ví dụ tên cổng hoặc địa chỉ nơi mình đứng nhé."
    if re.search(r"\b(o day|cho cu|hom qua|nha toi|nha rieng|nha em|cong ty toi)\b", key):
        return "UNRESOLVED_REFERENCE", "Anh/chị cho em địa chỉ cụ thể; phiên thử nghiệm chưa có vị trí hoặc địa chỉ lưu sẵn."
    if re.search(r"\b(neu|khong phai|chua qua|re trai|re phai|cot dien|cong xanh)\b", key):
        return "PREMISE_DETAIL_MISSING", "Anh/chị nêu rõ điểm hẹn cuối cùng hoặc địa chỉ cụ thể giúp em nhé."
    if re.search(r"https?://|\b(?:toa do|gps|pin)\b", query.casefold()):
        return "UNSUPPORTED_LOCATION_INPUT", "Anh/chị nhập tên địa điểm hoặc địa chỉ bằng văn bản; phiên này chưa hỗ trợ pin hay link bản đồ."
    return None


def resolution(
    status: str,
    query: str,
    target: str,
    *,
    candidates: list[dict[str, Any]] | None = None,
    reason: str | None = None,
    clarification: str | None = None,
    context: dict[str, Any] | None = None,
    ttl: int = 300,
    parsed_location: dict | None = None,
    anchors: list[dict] | None = None,
    entity_kind: str = "unknown",
    area: dict | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    candidates = candidates or []
    binding = {
        key: value
        for key, value in (context or {}).items()
        if key in {"request_id", "revision", "source_slot_revision", "fingerprint", "dependency_fingerprint", "request_mode", "stop_ref", "scope_kind", "scope_id"}
    }
    return MapResolution(
        status=status,
        query=query,
        target=target,
        candidates=candidates,
        place=candidates[0] if status == "resolved" else None,
        reason_codes=[reason] if reason else [],
        clarification=clarification,
        resolved_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl)).isoformat(),
        binding=binding,
        parsed_location=parsed_location,
        anchors=anchors or [],
        entity_kind=entity_kind,
        area=area,
    ).model_dump(mode="json")
