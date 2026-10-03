"""Open-Meteo replay tests: units, UTC windows, nulls, binding, cache and failure."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from app.adapters.weather_open_meteo import FIELDS, UNITS, OpenMeteoWeatherAdapter
from app.contracts.weather import WeatherRequest
from app.domain.inquiries import parse_forecast_time

NOW = datetime(2026, 10, 2, 16, 40, tzinfo=UTC)


def request(**values):
    return WeatherRequest(
        request_id="weather-1",
        scope_kind="inquiry",
        scope_id="inquiry-1",
        location_ref="hn_opera",
        latitude=21.0245,
        longitude=105.8575,
        target_time=NOW + timedelta(minutes=25),
        dependency_fingerprint="fp",
        **values,
    )


def payload():
    start = NOW.replace(minute=0)
    return {
        "latitude": 21.02,
        "longitude": 105.85,
        "timezone": "GMT",
        "utc_offset_seconds": 0,
        "hourly_units": {"time": "unixtime", **UNITS},
        "hourly": {
            "time": [(start + timedelta(hours=i)).timestamp() for i in range(5)],
            "temperature_2m": [26, 27, 28, 29, 30],
            "weather_code": [2, 2, 61, 3, 0],
            "precipitation_probability": [10, 20, 70, 40, 0],
            "precipitation": [0, 0.2, 1.2, 0.4, 0],
            "wind_speed_10m": [3, 4, 5, 6, 7],
        },
    }


def test_forecast_uses_ceiling_hour_units_and_rebinds_cached_facts():
    clock = [NOW.timestamp()]
    calls = []

    def handler(req):
        calls.append(req)
        assert req.url.path == "/v1/forecast"
        assert req.url.host == "api.open-meteo.com"
        assert req.url.params["hourly"] == ",".join(FIELDS)
        assert req.url.params["timeformat"] == "unixtime"
        assert req.url.params["timezone"] == "UTC"
        return httpx.Response(200, json=payload())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenMeteoWeatherAdapter(client=client, clock=lambda: clock[0])
            fact = await adapter.forecast(request())
            assert fact.status == "available"
            assert fact.sample.timestamp == datetime(2026, 10, 2, 18, tzinfo=UTC)
            assert fact.sample.precipitation_probability == 70
            assert fact.sample.temperature_c == 28
            assert fact.source_ref == "https://open-meteo.com/en/docs"
            clock[0] += 100
            different = request().model_copy(
                update={
                    "request_id": "weather-2",
                    "scope_id": "inquiry-2",
                    "dependency_fingerprint": "other",
                }
            )
            cached = await adapter.forecast(different)
            assert cached.request_id == "weather-2" and cached.scope_id == "inquiry-2"
            assert cached.dependency_fingerprint == "other"
            assert cached.fetched_at == fact.fetched_at
            assert cached.valid_until == fact.valid_until
            assert len(calls) == 1
            clock[0] += 901
            await adapter.forecast(different)
            assert len(calls) == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,reason", [(429, "RATE_LIMITED"), (401, "AUTH_ERROR"), (503, "PROVIDER_UNAVAILABLE")]
)
def test_weather_failure_never_becomes_no_rain(status, reason):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(status, json={"error": True}))
        ) as client:
            fact = await OpenMeteoWeatherAdapter(client=client, clock=NOW.timestamp).forecast(
                request()
            )
            assert fact.status == "unavailable"
            assert fact.sample is None and fact.unavailable_reason == reason

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["wrong_unit", "short_column", "nan", "bad_times", "bad_code"])
def test_provider_schema_errors_are_unavailable(damage):
    data = payload()
    if damage == "wrong_unit":
        data["hourly_units"]["temperature_2m"] = "°F"
    elif damage == "short_column":
        data["hourly"]["precipitation_probability"] = [10]
    elif damage == "nan":
        data["hourly"]["temperature_2m"][2] = "NaN"
    elif damage == "bad_code":
        data["hourly"]["weather_code"][2] = 999
    else:
        data["hourly"]["time"].reverse()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data))
        ) as client:
            fact = await OpenMeteoWeatherAdapter(client=client, clock=NOW.timestamp).forecast(
                request()
            )
            assert fact.status == "unavailable" and fact.unavailable_reason == "INVALID_RESPONSE"

    asyncio.run(run())


def test_missing_variables_remain_missing_not_zero():
    data = payload()
    data["hourly"]["precipitation_probability"][2] = None
    del data["hourly"]["precipitation"]

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data))
        ) as client:
            fact = await OpenMeteoWeatherAdapter(client=client, clock=NOW.timestamp).forecast(
                request()
            )
            assert fact.status == "available"
            assert fact.sample.precipitation_probability is None
            assert set(fact.missing_fields) == {"precipitation_probability", "precipitation"}

    asyncio.run(run())


@pytest.mark.parametrize(
    "offset,reason",
    [(timedelta(days=-1), "TIME_IN_PAST"), (timedelta(days=17), "OUTSIDE_COVERAGE")],
)
def test_outside_time_coverage_does_not_fetch(offset, reason):
    def forbidden(req):
        raise AssertionError("do not fetch")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            fact = await OpenMeteoWeatherAdapter(client=client, clock=NOW.timestamp).forecast(
                request().model_copy(update={"target_time": NOW + offset})
            )
            assert fact.unavailable_reason == reason

    asyncio.run(run())


def test_customer_key_uses_paid_endpoint_without_exposing_credentials():
    def handler(req):
        assert req.url.host == "customer-api.open-meteo.com"
        assert req.url.params["apikey"] == "test-private-key"
        return httpx.Response(200, json=payload())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fact = await OpenMeteoWeatherAdapter(
                client=client, api_key="test-private-key", clock=NOW.timestamp
            ).forecast(request())
            assert "test-private-key" not in fact.model_dump_json()

    asyncio.run(run())


def test_weather_request_rejects_naive_or_invalid_coordinates():
    with pytest.raises(ValidationError):
        WeatherRequest.model_validate(
            request().model_dump() | {"target_time": datetime(2026, 10, 2)}
        )
    with pytest.raises(ValidationError):
        WeatherRequest.model_validate(request().model_dump() | {"latitude": float("nan")})


def test_relative_dates_are_pinned_and_ambiguous_hours_need_clarification():
    target, basis = parse_forecast_time("mai 08:00", NOW.timestamp())
    assert target.isoformat() == "2026-10-03T08:00:00+07:00"
    assert basis == "thời điểm bạn yêu cầu"
    unclear, message = parse_forecast_time("mai 8 giờ", NOW.timestamp())
    assert unclear is None and "sáng hay tối" in message
    current, message = parse_forecast_time("bây giờ", NOW.timestamp())
    assert current.timestamp() == NOW.timestamp() and "chưa có giờ đón" in message
