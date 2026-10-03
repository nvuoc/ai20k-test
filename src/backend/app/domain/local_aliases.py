"""Versioned aliases resolve names within a known area, never coordinates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.maps import search_key

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "fixtures/local_aliases.json"


class LocalAlias(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    alias: str = Field(min_length=1)
    area: str = Field(min_length=1)
    canonical_query: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    kind: Literal["fixture", "reviewed"]


class AliasDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: str = Field(min_length=1)
    notice: str
    entries: list[LocalAlias]


class LocalAliasRegistry:
    def __init__(self, path: Path | None = None, *, allow_fixture=False):
        self.dataset = AliasDataset.model_validate(
            json.loads((path or DEFAULT_PATH).read_text(encoding="utf-8"))
        )
        self.entries = [e for e in self.dataset.entries if e.kind == "reviewed" or allow_fixture]

    def lookup(self, query: str, area: str | None = None) -> tuple[str, LocalAlias | None]:
        key = search_key(query)
        matches = [
            e
            for e in self.entries
            if key in {search_key(e.alias), search_key(e.alias + " " + e.area)}
        ]
        if not matches:
            return "none", None
        scoped = [
            e
            for e in matches
            if (area and search_key(area) == search_key(e.area)) or search_key(e.area) in key
        ]
        identities = {(e.area, e.entity_id, e.canonical_query) for e in scoped}
        if len(identities) != 1:
            return "ambiguous", None
        return "matched", scoped[0]


async def resolve_with_alias(adapter, registry, query, target, context):
    status, alias = registry.lookup(query, (context or {}).get("area"))
    if status == "ambiguous":
        from app.contracts.maps import resolution

        return resolution(
            "ambiguous",
            query,
            target,
            reason="ALIAS_AREA_REQUIRED",
            clarification="Tên địa phương này cần xác định địa bàn. Bạn cho mình phường/quận hoặc tỉnh/thành nhé.",
            context=context,
        )
    result = await adapter._resolve(alias.canonical_query if alias else query, target, context)
    if alias:
        evidence = {
            "alias": query,
            "area": alias.area,
            "entity_id": alias.entity_id,
            "source_ref": alias.source_ref,
            "source_version": alias.source_version,
            "dataset_version": registry.dataset.version,
        }
        for place in [
            result.get("place"),
            *result.get("candidates", []),
            *result.get("anchors", []),
        ]:
            if place:
                place["metadata"]["local_alias"] = evidence.copy()
        result["query"] = query
    return result
