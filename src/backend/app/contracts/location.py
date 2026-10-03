"""Parsed address evidence; a landmark relation never invents a meeting point."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ParsedLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_text: str
    normalized_query: str
    house_number: str | None = None
    alley_path: list[str] = Field(default_factory=list)
    anchor_name: str | None = None
    relation: Literal[
        "near", "opposite", "adjacent", "behind", "left_of", "right_of", "between", "none"
    ] = "none"
    qualifier: str | None = None
    uncertainties: list[str] = Field(default_factory=list)
    excluded_entities: list[str] = Field(default_factory=list)
    parser_version: str = "location-parser-2"
