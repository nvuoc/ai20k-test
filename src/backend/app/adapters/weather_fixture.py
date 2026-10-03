"""Explicitly labelled offline forecast; never a fallback for live API failure."""

from datetime import UTC, datetime, timedelta

from app.contracts.weather import WeatherFact, WeatherRequest, WeatherSample


class FixtureWeatherAdapter:
    async def forecast(self, request: WeatherRequest) -> WeatherFact:
        now = datetime.now(UTC)
        unavailable = request.target_time < now - timedelta(
            minutes=5
        ) or request.target_time > now + timedelta(days=16)
        return WeatherFact(
            status="unavailable" if unavailable else "available",
            request_id=request.request_id,
            scope_kind=request.scope_kind,
            scope_id=request.scope_id,
            dependency_fingerprint=request.dependency_fingerprint,
            provider="fixture",
            source_ref="fixture:weather:demo-1",
            attribution="dữ liệu thời tiết mẫu thử nghiệm, không phải dự báo thực tế",
            fetched_at=now,
            valid_until=now + timedelta(minutes=15),
            timezone=request.timezone,
            coverage_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
            coverage_end=now + timedelta(days=16),
            sample=None
            if unavailable
            else WeatherSample(
                timestamp=request.target_time.astimezone(UTC),
                weather_code=2,
                temperature_c=28,
                precipitation_probability=20,
                precipitation_mm=0,
                wind_speed_kmh=8,
            ),
            unavailable_reason="OUTSIDE_COVERAGE" if unavailable else None,
        )
