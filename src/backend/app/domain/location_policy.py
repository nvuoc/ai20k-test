"""Pure sourced area classification and stable billing anchors.

Provider ranking does not establish identity or popularity. A registry entry is
either reviewed operational data or a clearly marked sandbox fixture; no helper
in this module fabricates coordinates from an area name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.maps import AreaFact, MapResolution, Place, resolution, search_key

DEFAULT_AREA_PATH = Path(__file__).resolve().parents[1] / "fixtures/service_areas.json"
BROAD_LAYERS = frozenset({"CITY", "DIST", "WARD", "VILLAGE", "STREET"})
AREA_CATEGORIES = frozenset(
    {"village", "thon", "lang", "residential area", "residential_area", "khu do thi",
     "urban area", "ward", "district", "city", "street"}
)


class AreaEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aliases: list[str] = Field(min_length=1)
    kind: Literal["fixture", "reviewed"]
    area_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    source: str = Field(min_length=1)
    locality: str | None = None
    place_types: list[str] = Field(default_factory=list)
    representative_points: list[Place] = Field(default_factory=list)
    provider_ref_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def data_provenance(self):
        if any(not alias.strip() for alias in self.aliases):
            raise ValueError("Area aliases must not be blank")
        ids = [point.id for point in self.representative_points]
        if len(set(ids)) != len(ids):
            raise ValueError("Representative point identities must be unique")
        if self.kind == "fixture":
            if not self.source.startswith("fixture:") or any(
                not point.source.startswith("fixture:") or point.metadata.get("sandbox") is not True
                for point in self.representative_points
            ):
                raise ValueError("Fixture area data must be marked sandbox")
        elif self.source.startswith("fixture:") or any(
            point.source.startswith("fixture:") or point.metadata.get("sandbox") is True
            or point.metadata.get("access") != "verified"
            for point in self.representative_points
        ):
            raise ValueError("Reviewed anchors need a real source and verified vehicle access")
        return self

    def fact(self) -> AreaFact:
        return AreaFact.model_validate({
            key: getattr(self, key)
            for key in ("area_id", "label", "source", "locality", "place_types", "representative_points")
        })


class AreaDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1)
    notice: str
    entries: list[AreaEntry]


class AreaRegistry:
    def __init__(self, path: Path | None = None, *, allow_fixture: bool = False):
        self.dataset = AreaDataset.model_validate(
            json.loads((path or DEFAULT_AREA_PATH).read_text(encoding="utf-8"))
        )
        self.entries = [entry for entry in self.dataset.entries if allow_fixture or entry.kind == "reviewed"]

    def lookup(
        self, query: str, locality: str | None = None, *, provider_ref_id: str | None = None
    ) -> tuple[Literal["none", "ambiguous", "matched"], AreaFact | None]:
        key = search_key(query)
        matches = [entry for entry in self.entries if
                   provider_ref_id and provider_ref_id in entry.provider_ref_ids or
                   key in {search_key(alias) for alias in entry.aliases} or
                   key in {search_key(alias + " " + (entry.locality or "")) for alias in entry.aliases}]
        if locality:
            matches = [entry for entry in matches if not entry.locality or
                       search_key(entry.locality) == search_key(locality)]
        identities = {entry.area_id for entry in matches}
        if not identities:
            return "none", None
        if len(identities) != 1:
            return "ambiguous", None
        return "matched", matches[0].fact()


def category_keys(row: dict[str, Any]) -> set[str]:
    values = row.get("categories") or []
    if not isinstance(values, list):
        return set()
    return {search_key(str(value.get("name", value.get("label", ""))))
            if isinstance(value, dict) else search_key(str(value)) for value in values}


def provider_area_kind(row: dict[str, Any]) -> str | None:
    """Classify only explicit source layers/categories, never a name substring."""
    layer = str(row.get("layer", row.get("type", ""))).upper()
    ref = str(row.get("ref_id", "")).upper()
    if layer in BROAD_LAYERS:
        return layer.casefold()
    for broad in sorted(BROAD_LAYERS):
        if f":{broad}:" in ref:
            return broad.casefold()
    source_categories = category_keys(row)
    matched = source_categories & {search_key(value) for value in AREA_CATEGORIES}
    return sorted(matched)[0] if matched else None


def representative_point(area: AreaFact | dict, vehicle_profile: str = "car") -> dict | None:
    """Choose a stable, accessible sourced point; never an arbitrary centroid."""
    fact = AreaFact.model_validate(area)
    usable = [point for point in fact.representative_points if
              point.metadata.get("access") in {"verified", "provider_entry_point"}
              and vehicle_profile in point.metadata.get("vehicle_profiles", ["car", "motorcycle"])]
    if not usable:
        return None
    usable.sort(key=lambda point: (not bool(point.metadata.get("default_representative")),
                                   search_key(point.label), point.id))
    return usable[0].model_dump(mode="json")


def area_resolution(query, target, area, *, context=None):
    result = resolution(
        "ambiguous", query, target, reason="AREA_EXACT_POINT_REQUIRED",
        clarification="Bạn có biết địa chỉ, tòa nhà, cổng hoặc điểm hẹn cụ thể bên trong địa danh này không?",
        context=context,
    )
    return MapResolution.model_validate({
        **result, "entity_kind": "area", "area": AreaFact.model_validate(area).model_dump(mode="json")
    }).model_dump(mode="json")
