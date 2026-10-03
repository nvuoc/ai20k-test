"""Open-Meteo hourly forecasts. API reference: https://open-meteo.com/en/docs ."""

from __future__ import annotations

import asyncio
import copy
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from app.contracts.weather import WeatherFact, WeatherRequest, WeatherSample

FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
CUSTOMER_FORECAST_ENDPOINT = "https://customer-api.open-meteo.com/v1/forecast"
FIELDS = (
    "temperature_2m",
    "weather_code",
    "precipitation_probability",
    "precipitation",
    "wind_speed_10m",
)
UNITS = {
    "temperature_2m": "°C",
    "weather_code": "wmo code",
    "precipitation_probability": "%",
    "precipitation": "mm",
    "wind_speed_10m": "km/h",
}


class OpenMeteoWeatherAdapter:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        api_key: str = "",
        timeout_seconds: float = 5,
        cache_seconds: int = 900,
        clock=time.time,
    ):
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self.clock = clock
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._semaphore = asyncio.Semaphore(2)
        if api_key:
            import logging

            from app.adapters.map_vietmap import _SecretFilter

            for name in ("httpx", "httpcore"):
                logging.getLogger(name).addFilter(_SecretFilter(api_key))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def unavailable(self, request: WeatherRequest, reason: str) -> WeatherFact:
        now = datetime.fromtimestamp(self.clock(), UTC)
        return WeatherFact(
            status="unavailable",
            request_id=request.request_id,
            scope_kind=request.scope_kind,
            scope_id=request.scope_id,
            dependency_fingerprint=request.dependency_fingerprint,
            provider="Open-Meteo",
            source_ref="https://open-meteo.com/en/docs",
            attribution="Weather data by Open-Meteo.com",
            fetched_at=now,
            valid_until=now + timedelta(seconds=30),
            timezone=request.timezone,
            unavailable_reason=reason,
        )

    async def forecast(self, request: WeatherRequest) -> WeatherFact:
        request = WeatherRequest.model_validate(request)
        now = datetime.fromtimestamp(self.clock(), UTC)
        target = request.target_time.astimezone(UTC)
        if target < now - timedelta(minutes=5):
            return self.unavailable(request, "TIME_IN_PAST")
        if target > now + timedelta(days=16):
            return self.unavailable(request, "OUTSIDE_COVERAGE")
        # Unix timestamps avoid DST ambiguity. Precipitation is for the preceding
        # hour, so select the ceiling sample whose interval contains target.
        key = (request.latitude, request.longitude, FIELDS, "UTC", target.date().isoformat())
        cached = self._cache.get(key)
        if cached and cached[0] > self.clock():
            payload = copy.deepcopy(cached[1])
        else:
            params: dict[str, Any] = {
                "latitude": request.latitude,
                "longitude": request.longitude,
                "hourly": ",".join(FIELDS),
                "timezone": "UTC",
                "timeformat": "unixtime",
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh",
                "precipitation_unit": "mm",
                "forecast_days": 16,
            }
            if self._api_key:
                params["apikey"] = self._api_key
            endpoint = CUSTOMER_FORECAST_ENDPOINT if self._api_key else FORECAST_ENDPOINT
            try:
                async with self._semaphore:
                    response = await self._client.get(
                        endpoint, params=params, timeout=self.timeout_seconds
                    )
                if response.status_code != 200:
                    reason = {429: "RATE_LIMITED", 401: "AUTH_ERROR", 403: "AUTH_ERROR"}.get(
                        response.status_code, "PROVIDER_UNAVAILABLE"
                    )
                    return self.unavailable(request, reason)
                payload = response.json()
            except (httpx.TimeoutException, asyncio.TimeoutError):
                return self.unavailable(request, "PROVIDER_TIMEOUT")
            except (httpx.HTTPError, ValueError):
                return self.unavailable(request, "PROVIDER_UNAVAILABLE")
        try:
            hourly, units = payload["hourly"], payload["hourly_units"]
            times = hourly["time"]
            if not times or any(type(t) not in {int, float} or not math.isfinite(t) for t in times):
                raise ValueError("invalid timestamps")
            if any(b <= a for a, b in zip(times, times[1:])):
                raise ValueError("unordered timestamps")
            sample_time = math.ceil(target.timestamp() / 3600) * 3600
            if sample_time not in times:
                return self.unavailable(request, "OUTSIDE_COVERAGE")
            index = times.index(sample_time)
            values = {}
            missing = []
            names = (
                "temperature_c",
                "weather_code",
                "precipitation_probability",
                "precipitation_mm",
                "wind_speed_kmh",
            )
            for field, name in zip(FIELDS, names):
                column = hourly.get(field)
                if column is None:
                    missing.append(field)
                    values[name] = None
                    continue
                if units.get(field) != UNITS[field] or len(column) != len(times):
                    raise ValueError("invalid units or column length")
                value = column[index]
                if value is None:
                    missing.append(field)
                elif type(value) not in {int, float} or not math.isfinite(value):
                    raise ValueError("invalid sample")
                values[name] = value
            if all(value is None for value in values.values()):
                return self.unavailable(request, "NO_SAMPLE")
            sample = WeatherSample(timestamp=datetime.fromtimestamp(sample_time, UTC), **values)
            if not cached or cached[0] <= self.clock():
                self._cache[key] = (self.clock() + self.cache_seconds, copy.deepcopy(payload))
            while len(self._cache) > 128:
                del self._cache[next(iter(self._cache))]
            fetched_at = (
                now
                if not cached or cached[0] <= self.clock()
                else datetime.fromtimestamp(cached[0] - self.cache_seconds, UTC)
            )
            return WeatherFact(
                status="available",
                request_id=request.request_id,
                scope_kind=request.scope_kind,
                scope_id=request.scope_id,
                dependency_fingerprint=request.dependency_fingerprint,
                provider="Open-Meteo",
                source_ref="https://open-meteo.com/en/docs",
                attribution="Weather data by Open-Meteo.com",
                fetched_at=fetched_at,
                valid_until=fetched_at + timedelta(seconds=self.cache_seconds),
                timezone=request.timezone,
                coverage_start=datetime.fromtimestamp(times[0], UTC),
                coverage_end=datetime.fromtimestamp(times[-1], UTC),
                sample=sample,
                missing_fields=missing,
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            return self.unavailable(request, "INVALID_RESPONSE")
