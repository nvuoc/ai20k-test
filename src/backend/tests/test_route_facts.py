"""Traffic remains an optional, sourced read and never a fare/consent mutation."""

from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.map_vietmap import VietMapAdapter
from app.adapters.turn_fixture import extract_turn_fixture
from app.contracts.maps import RouteResult, TrafficFacts
from app.contracts.turn import QuestionIntent
from app.domain.conversation import new_conversation_state
from app.domain.inquiries import InquiryService, duration_explanation
from app.domain.location_policy import AreaRegistry, representative_point


def run(awaitable):
    return asyncio.run(awaitable)


def points():
    maps = FixtureMapAdapter()
    return run(maps.resolve("Nhà hát Lớn"))["place"], run(maps.resolve("Ga Hà Nội"))["place"]


def provider(handler, **kwargs):
    return VietMapAdapter("test-secret", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs)


def question(kind="travel_duration", scope="explicit_pair", origin="Nhà hát Lớn", destination="Ga Hà Nội", vehicle=None, raw_text=None):
    text = raw_text or ("Sao đi lâu thế?" if kind == "travel_duration_explanation" else "Đi mất bao lâu?")
    return QuestionIntent(question_id="question", type=kind, raw_text=text,
                          evidence_span={"start": 0, "end": len(text), "text": text}, route_scope=scope,
                          origin=origin, destination=destination, vehicle_ref=vehicle,
                          departure_time_ref=None, weather_target=None, relation_to_booking="read_only")


def service():
    engine = SimpleNamespace(
        maps=FixtureMapAdapter(), clock=time.time, brand_name="ParrotGo", location_confirmation=False,
        vehicle_catalog={"oto_4_cho": {"label": "Ô tô 4 chỗ", "route_profile": "car", "quote_available": True}},
    )
    return InquiryService(engine)


def test_annotations_are_opt_in_and_do_not_prove_eta_basis():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"paths": [{"distance": 3100, "time": 720000,
            "annotations": {"congestion": ["low", "moderate", "heavy", "severe", "unknown"],
                            "congestion_distance": [80, 40]}}]})

    adapter = provider(handler)
    origin, destination = points()
    plain = run(adapter.route(origin, destination))
    annotated = run(adapter.route(origin, destination, include_traffic=True))
    assert "annotations" not in calls[0].url.params
    assert calls[1].url.params["annotations"] == "congestion,congestion_distance"
    assert plain["traffic"]["status"] == "unknown"
    assert annotated["traffic"]["status"] == "available"
    assert annotated["eta_basis"] == "provider_estimate"
    assert annotated["traffic"]["congestion_distance"] == [80, 40]
    assert datetime.fromisoformat(annotated["valid_until"]) > datetime.fromisoformat(annotated["fetched_at"])
    run(adapter.route(origin, destination, include_traffic=True))
    assert len(calls) == 2


@pytest.mark.parametrize("annotations", [None, {"congestion": "heavy"}, {"congestion": ["bogus"]},
                                         {"congestion": ["heavy"], "congestion_distance": [-1]},
                                         {"congestion": ["heavy"], "congestion_distance": [True]},
                                         {"congestion": ["heavy"], "congestion_distance": [5000]}])
def test_invalid_or_missing_traffic_preserves_distance_and_time(annotations):
    adapter = provider(lambda _: httpx.Response(200, json={"paths": [{"distance": 3100, "time": 720000,
                                                                       "annotations": annotations}]}))
    route = run(adapter.route(*points(), include_traffic=True))
    assert route["distance_m"] == 3100 and route["duration_s"] == 720
    assert route["traffic"]["status"] == "unavailable"


def test_annotations_request_failure_falls_back_to_plain_route():
    calls = []

    def handler(request):
        calls.append(request)
        if "annotations" in request.url.params:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"paths": [{"distance": 3100, "time": 720000}]})

    route = run(provider(handler).route(*points(), include_traffic=True))
    assert len(calls) == 2
    assert route["duration_s"] == 720 and route["traffic"]["status"] == "unavailable"


def test_traffic_disabled_never_requests_annotations():
    calls = []
    adapter = provider(lambda request: calls.append(request) or httpx.Response(200, json={"paths": [{"distance": 3100, "time": 720000}]}),
                       traffic_enabled=False)
    route = run(adapter.route(*points(), include_traffic=True))
    assert "annotations" not in calls[0].url.params and route["traffic"]["status"] == "unknown"


def test_expired_traffic_cache_is_refreshed_without_quote_write():
    calls = []
    adapter = provider(lambda request: calls.append(request) or httpx.Response(200, json={"paths": [{"distance": 3100, "time": 720000,
        "annotations": {"congestion": ["low"], "congestion_distance": []}}]}))
    origin, destination = points()
    run(adapter.route(origin, destination, include_traffic=True))
    key = next(iter(adapter._route_cache))
    adapter._route_cache[key] = (time.monotonic() - 1, adapter._route_cache[key][1])
    run(adapter.route(origin, destination, include_traffic=True))
    assert len(calls) == 2


def test_departure_time_is_transmitted_and_part_of_cache_identity():
    calls = []
    adapter = provider(lambda request: calls.append(request) or httpx.Response(200, json={"paths": [{"distance": 3100, "time": 720000}]}))
    origin, destination = points()
    now = datetime.now(UTC)
    run(adapter.route(origin, destination, departure_time=now))
    run(adapter.route(origin, destination, departure_time=now + timedelta(hours=1)))
    assert len(calls) == 2 and calls[0].url.params["time"] != calls[1].url.params["time"]


def test_duration_explanation_is_sourced_and_does_not_change_booking():
    inquiry = service()
    state = new_conversation_state("explanation")
    before = deepcopy(state["booking_state"])
    revision = state["control"]["booking_revision"]
    run(inquiry.answer(state, [question()]))
    route_id = state["dialogue"]["last_discussed_route_ref"]
    reply = run(inquiry.answer(state, [question("travel_duration_explanation", "active_inquiry", None, None)]))
    assert "3.1 km" in reply and "12 phút" in reply and "dữ liệu tuyến thử nghiệm" in reply
    assert "giao thông mẫu" in reply and "chưa đủ để khẳng định" in reply
    assert state["dialogue"]["last_discussed_route_ref"] == route_id
    assert state["booking_state"] == before and state["control"]["booking_revision"] == revision
    assert state["confirmation"]["accepted_snapshot"] is None


def test_latest_discussed_route_wins_over_active_or_booking_route():
    inquiry = service()
    state = new_conversation_state("route-context")
    run(inquiry.answer(state, [question()]))
    first = state["active_inquiry_id"]
    run(inquiry.answer(state, [question(origin="Ga Hà Nội", destination="Nhà hát Lớn")]))
    second = state["active_inquiry_id"]
    state["active_inquiry_id"] = first
    reply = run(inquiry.answer(state, [question("travel_duration_explanation", "active_inquiry", None, None)]))
    assert "3.4 km" in reply and "13 phút" in reply
    assert state["dialogue"]["last_discussed_route_ref"] == second


def test_changed_vehicle_or_route_invalidates_explanation_reference():
    inquiry = service()
    state = new_conversation_state("route-changed")
    run(inquiry.answer(state, [question()]))
    item = state["inquiries"][state["active_inquiry_id"]]
    inquiry.update(item, "destination", "Bạch Mai cổng chính")
    reply = run(inquiry.answer(state, [question("travel_duration_explanation", "active_inquiry", None, None)]))
    assert "tuyến nào" in reply
    assert item["routes"] == {}


def test_missing_route_context_and_stale_traffic_never_claim_current_traffic():
    inquiry = service()
    state = new_conversation_state("no-route")
    reply = run(inquiry.answer(state, [question("travel_duration_explanation", "unresolved", None, None)]))
    assert "thời gian xe đến đón" in reply and not state["inquiries"]
    route = run(FixtureMapAdapter().route(*points(), include_traffic=True))
    route["source"] = "vietmap:route:v4"
    route["traffic"]["valid_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    text = duration_explanation(route, "A", "B", "Ô tô 4 chỗ", time.time())
    assert "giao thông đã cũ" in text and "có đoạn" not in text


@pytest.mark.parametrize("text", ["sao nhanh thế", "sao đi lâu vậy"])
def test_exact_customer_phrases_are_interpreted_as_duration_explanation(text):
    from app.domain.conversation import LegacyConversationEngine as ConversationEngine

    inquiry = service()
    state = new_conversation_state("customer-phrase")
    run(inquiry.answer(state, [question()]))
    # Projection is the same bounded context used by live model extraction.
    projected_engine = ConversationEngine.__new__(ConversationEngine)
    projected_engine.location_confirmation = False
    projected_engine.inquiry_service = inquiry
    projected_engine.vehicle_catalog = inquiry.engine.vehicle_catalog
    turn = extract_turn_fixture(projected_engine.projection(state, text, time.time()))
    assert turn.questions[0].type == "travel_duration_explanation"
    reply = run(inquiry.answer(state, list(turn.questions)))
    assert "12 phút" in reply and "ước tính" in reply


@pytest.mark.parametrize("prior_topic,reason", [("pickup_availability", None), ("travel_duration", "RATE_LIMITED")])
def test_bare_long_wait_after_pickup_eta_or_provider_wait_asks_correct_subject(prior_topic, reason):
    inquiry = service()
    state = new_conversation_state("wait-topic")
    run(inquiry.answer(state, [question()]))
    state["dialogue"]["last_discussed_topic"] = prior_topic
    state["last_response"]["reason"] = reason
    reply = run(inquiry.answer(state, [question("travel_duration_explanation", "unresolved", None, None, raw_text="lâu thế")]))
    assert "thời gian xe đến đón" in reply and "thời gian chờ phản hồi" in reply
    assert "12 phút" not in reply


@pytest.mark.parametrize("model,field", [(TrafficFacts, "fetched_at"), (TrafficFacts, "valid_until"),
                                          (RouteResult, "fetched_at"), (RouteResult, "valid_until"),
                                          (RouteResult, "departure_time")])
def test_fact_timestamps_require_timezone(model, field):
    data = {} if model is TrafficFacts else {"distance_m": 3100, "duration_s": 720,
                                           "source": "fixture:route", "pickup_id": "a", "destination_id": "b"}
    data[field] = datetime(2026, 10, 3)
    with pytest.raises(ValueError, match="timezone"):
        model.model_validate(data)


@pytest.mark.parametrize("model", [TrafficFacts, RouteResult])
def test_fact_expiry_must_follow_fetch(model):
    now = datetime.now(UTC)
    data = {"fetched_at": now, "valid_until": now - timedelta(seconds=1)}
    if model is RouteResult:
        data.update(distance_m=3100, duration_s=720, source="fixture:route", pickup_id="a", destination_id="b")
    with pytest.raises(ValueError, match="expiry"):
        model.model_validate(data)


@pytest.mark.parametrize("data", [{"status": "available", "congestion": ["heavy"]},
                                    {"status": "available", "source": "vietmap:traffic", "congestion": ["unknown"]}])
def test_available_traffic_requires_provenance_and_known_segments(data):
    now = datetime.now(UTC)
    data.update(fetched_at=now, valid_until=now + timedelta(seconds=60))
    with pytest.raises(ValueError, match="Available traffic"):
        TrafficFacts.model_validate(data)


def test_area_fixture_is_not_an_operational_location_and_anchor_is_stable():
    maps = FixtureMapAdapter()
    result = run(maps.resolve("Ocean Park 1", context={"revision": 3}))
    assert result["status"] == "ambiguous" and result["entity_kind"] == "area"
    assert result["place"] is None and result["candidates"] == []
    first = representative_point(result["area"])
    second = representative_point(run(FixtureMapAdapter().resolve("Ocean Park 1"))["area"])
    assert first == second and first["metadata"]["sandbox"] is True
    assert AreaRegistry().lookup("Ocean Park 1")[0] == "none"


def test_reviewed_anchor_choice_is_stable_across_order_and_vehicle_access():
    origin, destination = points()
    for point in (origin, destination):
        point["metadata"].update(access="verified", vehicle_profiles=["car"])
    destination["metadata"]["default_representative"] = True
    area = {"area_id": "reviewed", "label": "Reviewed area", "source": "reviewed:dataset-1",
            "representative_points": [origin, destination]}
    selected = representative_point(area)
    area["representative_points"].reverse()
    assert representative_point(area) == selected
    assert selected["id"] == destination["id"]
    assert representative_point(area, "motorcycle") is None


def test_area_layer_wins_over_interior_point_for_broad_customer_query():
    adapter = provider(lambda _: httpx.Response(200, json=[
        {"ref_id": ":VILLAGE:42", "name": "Thôn Lai Xá", "display": "Thôn Lai Xá, Hà Nội"},
        {"ref_id": "poi:interior", "name": "Nhà văn hóa Thôn Lai Xá", "display": "Nhà văn hóa Thôn Lai Xá, Hà Nội"},
    ]))
    result = run(adapter.resolve("Thôn Lai Xá"))
    assert result["entity_kind"] == "area" and result["place"] is None


def test_live_area_entrance_can_supply_stable_source_coordinate_without_using_centroid():
    calls = []

    def handler(request):
        calls.append(request)
        if "/search/" in request.url.path:
            return httpx.Response(200, json=[{"ref_id": "area:example", "name": "Khu đô thị Mẫu",
                "display": "Khu đô thị Mẫu, Hà Nội", "categories": ["residential area"],
                "lat": 21.9, "lng": 105.9,
                "entry_points": [{"ref_id": "entry:b", "name": "Cổng B"}, {"ref_id": "entry:a", "name": "Cổng A"}]}])
        gate = request.url.params["refid"]
        assert gate in {"entry:a", "entry:b"}
        return httpx.Response(200, json={"lat": 21.01 if gate == "entry:a" else 21.02,
                                       "lng": 105.81, "city": "Hà Nội"})

    result = run(provider(handler).resolve("Khu đô thị Mẫu"))
    assert result["entity_kind"] == "area" and result["place"] is None
    anchor = representative_point(result["area"])
    assert anchor["id"] == "entry:a" and anchor["lat"] == 21.01
    assert anchor["source"] == "vietmap:place:v4" and anchor["metadata"]["access"] == "provider_entry_point"
    assert len(calls) == 3


def test_broad_vietmap_entity_is_kept_as_area_even_with_centroid_coordinates():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[{"ref_id": ":VILLAGE:42", "name": "Thôn Lai Xá",
            "display": "Thôn Lai Xá, Hà Nội", "address": "Hà Nội", "lat": 21.0, "lng": 105.8}])

    result = run(provider(handler).resolve("Thôn Lai Xá"))
    assert result["entity_kind"] == "area" and result["place"] is None
    assert result["area"]["representative_points"] == [] and len(calls) == 1


def test_irrelevant_provider_first_result_cannot_hide_exact_match():
    calls = []

    def handler(request):
        calls.append(request)
        if "/search/" in request.url.path:
            return httpx.Response(200, json=[{"ref_id": "wrong", "display": "Trường Sao Mai Hồ Chí Minh"},
                {"ref_id": "exact", "display": "Ga Hà Nội"},
                {"ref_id": "other", "display": "Bệnh viện Bạch Mai Hà Nội"}])
        assert request.url.params["refid"] == "exact"
        return httpx.Response(200, json={"lat": 21.0253, "lng": 105.8413, "city": "Hà Nội"})

    result = run(provider(handler).resolve("Ga Hà Nội"))
    assert result["status"] == "resolved" and result["place"]["id"] == "exact"
    assert len(calls) == 2


def test_reviewed_registry_rejects_fixture_or_unknown_access(tmp_path):
    origin, _ = points()
    dataset = {"version": "reviewed-1", "notice": "Reviewed data", "entries": [{
        "aliases": ["Area"], "kind": "reviewed", "area_id": "area", "label": "Area", "source": "reviewed:source",
        "representative_points": [origin]}]}
    path = tmp_path / "areas.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")
    with pytest.raises(ValueError, match="real source"):
        AreaRegistry(path)
