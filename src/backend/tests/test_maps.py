from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.map_vietmap import VietMapAdapter
from app.adapters.quote_fixture import FixtureQuoteAdapter
from app.contracts.maps import MapProviderError


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.mark.parametrize("query,place_id", [
    ("Nhà hát Lớn Hà Nội", "hn_opera"),
    ("GA HA NOI", "hn_station"),
    ("Số 15 ngõ 20 Nguyễn Trãi Hà Nội", "hn_house_15"),
    ("12A Trần Phú HN", "hn_house_12a"),
    ("123 xuyệt 45 xuyệt 6 Nguyễn Đình Chiểu", "hcm_house_slash"),
    ("Nhà 3 đường Ba Tháng Hai Hồ Chí Minh", "hcm_house_32"),
    ("Chợ Bến Thành", "hcm_benthanh"),
])
def test_seeded_address_retains_raw(query, place_id):
    result = run(FixtureMapAdapter().resolve(query, context={"revision":4,"fingerprint":"abc"}))
    assert result["status"] == "resolved"
    assert result["place"]["id"] == place_id
    assert result["query"] == query
    assert result["binding"] == {"revision":4,"fingerprint":"abc"}
    assert result["place"]["source"] == "fixture:locations-sandbox-1"
    assert "confirmed" not in result


@pytest.mark.parametrize("query,reason", [
    ("Gần chợ Bến Thành", "RELATION_UNRESOLVED"),
    ("Đối diện trường Sao Mai", "RELATION_UNRESOLVED"),
    ("Nhà 12 hay 14 Trần Phú", "UNCERTAIN_COMPONENT"),
    ("Đón ở đây", "UNRESOLVED_REFERENCE"),
    ("Chỗ hôm qua", "UNRESOLVED_REFERENCE"),
    ("Nếu xe không vào được thì đón đầu ngõ", "PREMISE_DETAIL_MISSING"),
])
def test_unsupported_details_cannot_be_selected_as_actual_pickup(query, reason):
    result = run(FixtureMapAdapter().resolve(query))
    assert result["status"] == "ambiguous"
    assert result["reason_codes"] == [reason]
    assert result["place"] is None
    assert result["candidates"] == []


def test_airport_requires_specific_terminal_and_keeps_metadata():
    maps = FixtureMapAdapter()
    vague = run(maps.resolve("Nội Bài"))
    assert vague["status"] == "ambiguous"
    assert len(vague["candidates"]) == 2
    resolved = run(maps.resolve("Sân bay Nội Bài T1 cửa 3"))
    assert resolved["place"]["airport"] is True
    assert resolved["place"]["meeting_point"] == "Ga T1 cửa 3"


def test_duplicate_branches_and_distinct_gates():
    maps = FixtureMapAdapter()
    result = run(maps.resolve("Trường Sao Mai"))
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2
    front = run(maps.resolve("Bạch Mai cổng chính"))["place"]
    back = run(maps.resolve("Bạch Mai cổng sau"))["place"]
    assert front["id"] != back["id"]
    assert (front["lat"], front["lon"]) != (back["lat"], back["lon"])


def test_explicit_gate_note_updates_only_known_seeded_facility_entrance():
    maps = FixtureMapAdapter()
    raw = "Bạch Mai cổng chính, đón cổng sau"
    result = run(maps.resolve(raw, context={"pickup_note":"đón cổng sau"}))
    assert result["status"] == "resolved"
    assert result["query"] == raw
    assert result["place"]["id"] == "hn_bachmai_back"
    conditional = run(maps.resolve("Bạch Mai cổng chính, nếu được thì đón cổng sau", context={"pickup_note":"nếu được thì đón cổng sau"}))
    assert conditional["status"] == "ambiguous"
    unknown = run(maps.resolve("Ga Hà Nội, đón cổng sau", context={"pickup_note":"đón cổng sau"}))
    assert unknown["place"] is None


def test_number_mismatch_and_unknown_address_do_not_invent_coordinates():
    maps = FixtureMapAdapter()
    for query in ("Nhà 12", "12 Trần Phú Hà Nội", "15 ngõ 21 Nguyễn Trãi", "Nhà 998 phố chưa biết"):
        result = run(maps.resolve(query))
        assert result["status"] == "not_found"
        assert result["candidates"] == []
        assert result["place"] is None


def test_directed_route_matrix_and_missing_route():
    maps = FixtureMapAdapter()
    opera = run(maps.resolve("Nhà hát Lớn"))["place"]
    station = run(maps.resolve("Ga Hà Nội"))["place"]
    airport = run(maps.resolve("Nội Bài T1 cửa 3"))["place"]
    outward = run(maps.route(opera, station))
    reverse = run(maps.route(station, opera))
    assert outward["distance_m"] == 3100
    assert reverse["distance_m"] == 3400
    with pytest.raises(MapProviderError, match="ROUTE_NOT_SEEDED"):
        run(maps.route(airport, airport))


def test_quote_is_deterministic_but_route_and_catalog_changes_invalidate_fingerprint():
    maps, quotes = FixtureMapAdapter(), FixtureQuoteAdapter()
    pickup = run(maps.resolve("Nhà hát Lớn"))["place"]
    destination = run(maps.resolve("Ga Hà Nội"))["place"]
    route = run(maps.route(pickup, destination))
    quote = run(quotes.quote(route, "oto_4_cho"))
    repeat = run(quotes.quote(route, "oto_4_cho"))
    larger = run(quotes.quote(route, "oto_7_cho"))
    assert quote["amount"] == 46000
    assert quote["currency"] == "VND"
    assert quote["kind"] == "estimate"
    assert quote["sandbox"] is True
    assert quote["fingerprint"] == repeat["fingerprint"]
    assert quote["fingerprint"] != larger["fingerprint"]
    assert quotes.get_vehicle("oto_7_cho")["max_passengers"] == 6


def provider_adapter(handler, **kwargs):
    return VietMapAdapter("secret-test-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs)


def test_vietmap_v3_place_lookup_and_v4_road_route_units():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.params["apikey"] == "secret-test-key"
        if request.url.path == "/api/search/v3":
            return httpx.Response(200, json=[{"ref_id":":POI:123","display":"Nhà hát Lớn Hà Nội","name":"Nhà hát Lớn"}])
        if request.url.path == "/api/place/v3":
            assert request.url.params["refid"] == ":POI:123"
            return httpx.Response(200, json={"lat":21.02,"lng":105.85,"city":"Hà Nội"})
        if request.url.path == "/api/route/v4":
            assert request.url.params.get_list("point") == ["21.02,105.85", "21.02,105.85"]
            return httpx.Response(200, json={"code":"OK","paths":[{"distance":5678,"time":321000}]})
        raise AssertionError(request.url.path)
    adapter = provider_adapter(handler, api_version="v3")
    result = run(adapter.resolve("Nhà hát Lớn Hà Nội"))
    assert result["status"] == "resolved"
    assert result["place"]["source"] == "vietmap:place:v3"
    route = run(adapter.route(result["place"], result["place"]))
    assert route["distance_m"] == 5678
    assert route["duration_s"] == 321
    assert len(calls) == 3


def test_vietmap_autocomplete_fallback_and_provider_cache():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/search/v4":
            assert request.url.params["display_type"] == "5"
            return httpx.Response(200, json=[])
        if request.url.path == "/api/autocomplete/v4":
            return httpx.Response(200, json=[{"ref_id":"auto:test","display":"12A Trần Phú Hà Nội"}])
        return httpx.Response(200, json={"lat":21.02,"lng":105.85,"hs_num":"12A","street":"Trần Phú","city":"Hà Nội"})
    adapter = provider_adapter(handler)
    first = run(adapter.resolve("12A Trần Phú Hà Nội", context={"revision":1}))
    second = run(adapter.resolve("12A Trần Phú Hà Nội", context={"revision":2}))
    assert first["status"] == second["status"] == "resolved"
    assert second["binding"] == {"revision":2}
    assert len(calls) == 3


@pytest.mark.parametrize("status,reason", [(401,"PROVIDER_AUTH_ERROR"),(429,"PROVIDER_RATE_LIMIT"),(503,"PROVIDER_UNAVAILABLE")])
def test_provider_errors_are_unavailable_without_secret(status, reason):
    adapter = provider_adapter(lambda request: httpx.Response(status, json={"error":"failure"}))
    result = run(adapter.resolve("Ga Hà Nội"))
    assert result["status"] == "unavailable"
    assert result["reason_codes"] == [reason]
    assert "secret-test-key" not in json.dumps(result)


def test_provider_timeout_not_not_found():
    def handler(request):
        raise httpx.ReadTimeout("https://maps.vietmap.vn?apikey=secret-test-key", request=request)
    result = run(provider_adapter(handler).resolve("Ga Hà Nội"))
    assert result["status"] == "unavailable"
    assert result["reason_codes"] == ["PROVIDER_TIMEOUT"]


@pytest.mark.parametrize("detail", [
    {"lat":21,"lng":105,"hs_num":"12","city":"Hà Nội","street":"Trần Phú"},
    {"lat":21,"lng":105,"hs_num":"12A","city":"Hồ Chí Minh","street":"Trần Phú"},
])
def test_provider_cannot_drop_suffix_or_explicit_city(detail):
    def handler(request):
        if "/search/" in request.url.path:
            return httpx.Response(200, json=[{"ref_id":"geocode:test","display":detail["hs_num"] + " Trần Phú " + detail["city"]}])
        return httpx.Response(200, json=detail)
    result = run(provider_adapter(handler).resolve("12A Trần Phú Hà Nội"))
    assert result["status"] == "ambiguous"
    assert result["candidates"] == []


def test_provider_invalid_coordinates_are_unavailable():
    def handler(request):
        if "/search/" in request.url.path:
            return httpx.Response(200, json=[{"ref_id":"test","display":"Ga Hà Nội"}])
        return httpx.Response(200, json={"lat":105,"lng":21,"city":"Hà Nội"})
    result = run(provider_adapter(handler).resolve("Ga Hà Nội"))
    assert result["status"] == "unavailable"


def test_provider_centroid_is_replaced_by_selectable_sourced_entrances():
    requests = []
    def handler(request):
        requests.append(request)
        if "/search/" in request.url.path:
            return httpx.Response(200, json=[{
                "ref_id":"airport:parent", "display":"Sân bay Nội Bài Hà Nội",
                "categories":[{"name":"airport"}],
                "entry_points":[{"ref_id":"entry:t1","name":"Ga T1 cửa 3"},{"ref_id":"entry:t2","name":"Ga T2 cửa 2"}],
            }])
        assert request.url.params["refid"] != "airport:parent"
        return httpx.Response(200, json={"lat":21.21,"lng":105.81,"city":"Hà Nội"})
    result = run(provider_adapter(handler).resolve("Sân bay Nội Bài Hà Nội"))
    assert result["status"] == "ambiguous"
    assert result["place"] is None
    assert len(result["candidates"]) == 2
    assert all(candidate["airport"] for candidate in result["candidates"])
    assert result["candidates"][0]["meeting_point"] == "Ga T1 cửa 3"
    assert len(requests) == 3


def test_httpx_info_logs_redact_api_key(caplog):
    adapter = provider_adapter(lambda request: httpx.Response(401, json={}))
    with caplog.at_level(logging.INFO, logger="httpx"):
        run(adapter.resolve("Ga Hà Nội"))
    assert "secret-test-key" not in caplog.text
    assert "[REDACTED]" in caplog.text
