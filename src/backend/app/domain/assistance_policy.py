"""Explicit, bounded sandbox policy for help inside an area."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class AssistanceRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area_id: str
    vehicle_types: list[str]
    fee_amount: int = Field(ge=0, le=200000)
    max_extra_distance_m: int = Field(gt=0, le=20000)
    max_wait_minutes: int = Field(gt=0, le=120)


class AssistancePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1)
    sandbox: bool = True
    consent_ttl_seconds: int = Field(default=600, ge=30, le=3600)
    rules: list[AssistanceRule]

    @classmethod
    def load(cls, path: Path | None) -> AssistancePolicy:
        path = path or Path(__file__).resolve().parents[1] / "fixtures" / "assistance_policy.json"
        policy = cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if not policy.sandbox:
            raise ValueError("Area assistance requires a verified live dispatch integration")
        if len({rule.area_id for rule in policy.rules}) != len(policy.rules):
            raise ValueError("Duplicate assistance area")
        return policy

    def rule(self, area_id: str, vehicle: str | None) -> AssistanceRule | None:
        return next(
            (
                rule
                for rule in self.rules
                if rule.area_id == area_id and vehicle in rule.vehicle_types
            ),
            None,
        )
