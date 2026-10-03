"""Weather facts with source, UTC timestamps, units and explicit coverage."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WeatherRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    request_id: str = Field(min_length=1, max_length=128)
    scope_kind: Literal["booking", "inquiry"]
    scope_id: str = Field(min_length=1, max_length=128)
    location_ref: str = Field(min_length=1)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    target_time: datetime
    timezone: str = "Asia/Ho_Chi_Minh"
    dependency_fingerprint: str

    @field_validator("target_time")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("target_time must include timezone")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except KeyError:
            raise ValueError("unknown timezone") from None
        return value


class WeatherSample(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    timestamp: datetime
    weather_code: int | None = Field(default=None, ge=0, le=99)
    temperature_c: float | None = None
    precipitation_probability: float | None = Field(default=None, ge=0, le=100)
    precipitation_mm: float | None = Field(default=None, ge=0)
    wind_speed_kmh: float | None = Field(default=None, ge=0)

    @field_validator("timestamp")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        return WeatherRequest.aware_time(value)


class WeatherFact(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    status: Literal["available", "unavailable"]
    request_id: str
    scope_kind: Literal["booking", "inquiry"]
    scope_id: str
    dependency_fingerprint: str
    provider: str
    source_ref: str
    attribution: str
    fetched_at: datetime
    valid_until: datetime
    timezone: str
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    sample: WeatherSample | None = None
    unavailable_reason: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    time_basis: str = "hourly forecast; precipitation describes the preceding hour"

    @field_validator("fetched_at", "valid_until", "coverage_start", "coverage_end")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        return WeatherRequest.aware_time(value) if value is not None else None

    @model_validator(mode="after")
    def consistent_fact(self):
        if self.valid_until <= self.fetched_at:
            raise ValueError("invalid freshness window")
        if self.status == "available":
            if (
                not self.sample
                or not self.coverage_start
                or not self.coverage_end
                or self.unavailable_reason
            ):
                raise ValueError("available fact needs sample and coverage")
            if not self.coverage_start <= self.sample.timestamp <= self.coverage_end:
                raise ValueError("sample outside coverage")
        elif self.sample is not None or not self.unavailable_reason:
            raise ValueError("unavailable forecast needs reason and no sample")
        return self


class WeatherAdapter(Protocol):
    async def forecast(self, request: WeatherRequest) -> WeatherFact: ...
