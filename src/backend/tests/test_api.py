"""HTTP, durable inbox, graph and sandbox provider integration with no network."""

from __future__ import annotations

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

COMPLETE_TEXT = (
    "Đón ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, ngay bây giờ, "
    "2 người, xe 4 chỗ, số điện thoại 0901234567"
)


@pytest.fixture
def settings(tmp_path):
    return Settings(profile="test", secret="stable-test-secret",
                    database_path=tmp_path / "app.sqlite", checkpoint_path=tmp_path / "checkpoints.sqlite")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as connection:
        bootstrap = connection.get("/api/bootstrap")
        assert bootstrap.status_code == 200
        yield connection


def create_session(client, key="trip-one"):
    response = client.post("/api/sessions", json={"client_session_key":key})
    assert response.status_code == 200, response.text
    return response.json()


def poll(client, session_id, *, event_id=None, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/sessions/{session_id}/updates")
        assert response.status_code == 200, response.text
        snapshot = response.json()
        if event_id:
            failed = [event for event in snapshot["events"] if event["event_id"] == event_id + ":failed"]
            assert not failed, failed
            completed = any(event["event_id"] == event_id + ":response" for event in snapshot["events"])
        else:
            completed = True
        if completed and snapshot["pending_count"] == 0:
            return snapshot
        time.sleep(0.01)
    pytest.fail(f"Turn did not complete for {session_id}")


def send_text(client, session_id, text, *, key=None, rendered=None, reply=None):
    body = {"client_message_id":key or str(uuid.uuid4()), "text":text,
            "rendered_response_ids":rendered or [], "reply_to_response_id":reply}
    response = client.post(f"/api/sessions/{session_id}/messages", json=body)
    assert response.status_code == 202, response.text
    receipt = response.json()
    return poll(client, session_id, event_id=receipt["event_id"]), receipt


def send_action(client, session_id, action, *, key=None, rendered=None, reply=None):
    body = {"client_action_id":key or str(uuid.uuid4()), "action":action,
            "rendered_response_ids":rendered or [], "reply_to_response_id":reply}
    response = client.post(f"/api/sessions/{session_id}/actions", json=body)
    assert response.status_code == 202, response.text
    receipt = response.json()
    return poll(client, session_id, event_id=receipt["event_id"]), receipt


def acknowledge(client, session_id, response, *, key=None):
    return client.post(f"/api/sessions/{session_id}/delivery-acks", json={
        "ack_id":key or str(uuid.uuid4()), "response_id":response["response_id"],
        "generation":response["generation"], "delivery_type":"rendered",
    })


def confirm_action(response):
    summary = response["summary"]
    return {"type":"confirm_booking", "prompt_id":summary["prompt_id"],
            "booking_revision":summary["booking_revision"],
            "snapshot_fingerprint":summary["snapshot_fingerprint"]}


def complete(client, session_id):
    snapshot, _ = send_text(client, session_id, COMPLETE_TEXT)
    assert snapshot["booking_status"] == "awaiting_confirmation"
    assert snapshot["booking"] is None
    assert snapshot["active_response"]["action"] == "confirm_booking"
    return snapshot


@pytest.mark.parametrize("typed", [True, False])
def test_complete_confirm_cancel_use_durable_sandbox(client, typed):
    session_id = create_session(client)["session_id"]
    snapshot = complete(client, session_id)
    shown = snapshot["active_response"]
    assert shown["summary"]["fare"] == 46000
    assert client.app.state.engine.booking.booking_count() == 0
    assert acknowledge(client, session_id, shown).status_code == 200
    if typed:
        booked, _ = send_action(client, session_id, confirm_action(shown), reply=shown["response_id"])
    else:
        booked, _ = send_text(client, session_id, "Đồng ý đặt", reply=shown["response_id"])
    assert booked["booking_status"] == "booked"
    booking_id = booked["booking"]["booking_id"]
    assert booking_id.startswith("SBX-")
    assert "Đã tạo đơn thử nghiệm" in booked["active_response"]["text"]
    assert client.app.state.engine.booking.booking_count() == 1
    assert client.get(f"/api/sessions/{session_id}/booking").json()["booking"]["booking_id"] == booking_id
    if typed:
        cancelled, _ = send_action(client, session_id, {"type":"cancel_booking","booking_id":booking_id})
    else:
        cancelled, _ = send_text(client, session_id, "Hủy chuyến")
    assert cancelled["booking_status"] == "cancelled"
    assert cancelled["booking"]["provider_status"] == "cancelled"
    assert "Đã hủy đơn thử nghiệm" in cancelled["active_response"]["text"]
    assert client.app.state.engine.booking.operation_count("create") == 1
    assert client.app.state.engine.booking.operation_count("cancel") == 1


def test_typed_actions_and_delivery_ack_never_call_llm(client):
    session_id = create_session(client)["session_id"]
    shown = complete(client, session_id)["active_response"]
    async def forbidden(projection):
        raise AssertionError("A typed action must not consume Gemini quota")
    client.app.state.engine.extractor = forbidden
    assert acknowledge(client, session_id, shown).status_code == 200
    booked, _ = send_action(client, session_id, confirm_action(shown), reply=shown["response_id"])
    assert booked["booking_status"] == "booked"
    cancelled, _ = send_action(client, session_id, {"type":"cancel_booking","booking_id":booked["booking"]["booking_id"]})
    assert cancelled["booking_status"] == "cancelled"


def test_render_evidence_bundled_with_input_is_persisted_atomically(client):
    session_id = create_session(client)["session_id"]
    shown = complete(client, session_id)["active_response"]
    booked, _ = send_action(client, session_id, confirm_action(shown), rendered=[shown["response_id"]], reply=shown["response_id"])
    assert booked["booking_status"] == "booked"
    assert shown["response_id"] in client.app.state.store.delivered(session_id)


def test_unshown_summary_or_stale_typed_fingerprint_cannot_book(client):
    session_id = create_session(client)["session_id"]
    shown = complete(client, session_id)["active_response"]
    unshown, _ = send_action(client, session_id, confirm_action(shown), reply=shown["response_id"])
    assert unshown["booking_status"] != "booked"
    current = complete(client, session_id)["active_response"]
    assert acknowledge(client, session_id, current).status_code == 200
    forged = confirm_action(current) | {"snapshot_fingerprint":"forged"}
    stale, _ = send_action(client, session_id, forged, reply=current["response_id"])
    assert stale["booking_status"] != "booked"
    assert client.app.state.engine.booking.booking_count() == 0


def test_typed_candidate_selection_derives_target_from_scoped_set(client):
    session_id = create_session(client)["session_id"]
    snapshot, _ = send_text(client, session_id, "Đón ở Nhà hát Lớn Hà Nội, đến Bệnh viện Bạch Mai, ngay bây giờ, 2 người, xe 4 chỗ, số điện thoại 0901234567")
    shown = snapshot["active_response"]
    assert shown["action"] == "offer_candidates"
    candidate = shown["candidates"][0]
    async def forbidden(projection):
        raise AssertionError("Selecting a candidate must not call Gemini")
    client.app.state.engine.extractor = forbidden
    selected, _ = send_action(client, session_id, {"type":"select_candidate", "candidate_set_id":candidate["candidate_set_id"], "candidate_id":candidate["candidate_id"]}, rendered=[shown["response_id"]], reply=shown["response_id"])
    assert selected["active_response"]["action"] == "confirm_booking"
    assert "cổng chính" in selected["active_response"]["summary"]["destination"]


def test_realistic_provider_opaque_candidate_ref_longer_than_128(client):
    maps = client.app.state.engine.maps
    long_id = "geocode:" + "x" * 140

    class OpaqueMaps:
        async def resolve(self, query, target="pickup", context=None):
            result = await maps.resolve(query, target=target, context=context)
            for place in result["candidates"]:
                if place["id"] == "hn_bachmai_front":
                    place["id"] = long_id
            return result

        async def route(self, pickup, destination, vehicle_type=None):
            if destination["id"] == long_id:
                destination = destination | {"id":"hn_bachmai_front"}
            return await maps.route(pickup, destination, vehicle_type=vehicle_type)

    client.app.state.engine.maps = OpaqueMaps()
    session_id = create_session(client)["session_id"]
    snapshot, _ = send_text(client, session_id, "Đón ở Nhà hát Lớn Hà Nội, đến Bệnh viện Bạch Mai, ngay bây giờ, 2 người, xe 4 chỗ, số điện thoại 0901234567")
    shown = snapshot["active_response"]
    candidate = shown["candidates"][0]
    assert candidate["candidate_id"] == long_id
    selected, _ = send_action(client, session_id, {"type":"select_candidate", "candidate_set_id":candidate["candidate_set_id"], "candidate_id":candidate["candidate_id"]}, rendered=[shown["response_id"]], reply=shown["response_id"])
    assert selected["active_response"]["action"] == "confirm_booking"


def test_typed_cancel_draft_and_wrong_refs(client):
    session_id = create_session(client)["session_id"]
    snapshot = complete(client, session_id)
    wrong, _ = send_action(client, session_id, {"type":"cancel_draft","draft_id":"another-draft"})
    assert wrong["booking_status"] != "cancelled"
    cancelled, _ = send_action(client, session_id, {"type":"cancel_draft","draft_id":snapshot["draft_id"]})
    assert cancelled["booking_status"] == "cancelled"
    assert client.app.state.engine.booking.booking_count() == 0


def test_wrong_booking_reference_does_not_cancel_current_order(client):
    session_id = create_session(client)["session_id"]
    shown = complete(client, session_id)["active_response"]
    booked, _ = send_action(client, session_id, confirm_action(shown), rendered=[shown["response_id"]], reply=shown["response_id"])
    wrong, _ = send_action(client, session_id, {"type":"cancel_booking","booking_id":"SBX-ANOTHER"})
    assert wrong["booking_status"] == "booked"
    assert client.app.state.engine.booking.operation_count() == 1
    assert wrong["booking"]["booking_id"] == booked["booking"]["booking_id"]


def test_message_dedup_conflict_and_action_namespace(client):
    session_id = create_session(client)["session_id"]
    snapshot, first = send_text(client, session_id, COMPLETE_TEXT, key="shared-id")
    duplicate = client.post(f"/api/sessions/{session_id}/messages", json={"client_message_id":"shared-id","text":COMPLETE_TEXT})
    assert duplicate.status_code == 202
    assert duplicate.json()["event_id"] == first["event_id"]
    assert duplicate.json()["status"] == "completed"
    assert duplicate.json()["ingress_seq"] == first["ingress_seq"]
    conflict = client.post(f"/api/sessions/{session_id}/messages", json={"client_message_id":"shared-id","text":"Nội dung khác"})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert "Nội dung khác" not in conflict.text
    shown = snapshot["active_response"]
    booked, action_receipt = send_action(client, session_id, confirm_action(shown), key="shared-id", rendered=[shown["response_id"]], reply=shown["response_id"])
    assert action_receipt["event_id"] != first["event_id"]
    assert booked["booking_status"] == "booked"
    assert client.app.state.engine.booking.booking_count() == 1


def test_ack_dedup_conflict_stale_generation_and_cross_session_refs(client):
    session_id = create_session(client)["session_id"]
    shown = complete(client, session_id)["active_response"]
    assert acknowledge(client, session_id, shown, key="ack-one").status_code == 200
    assert acknowledge(client, session_id, shown, key="ack-one").status_code == 200
    wrong_generation = shown | {"generation":shown["generation"] + 1}
    assert acknowledge(client, session_id, wrong_generation).status_code == 409
    assert acknowledge(client, session_id, wrong_generation, key="ack-one").status_code == 409
    newer, _ = send_text(client, session_id, "Đổi sang xe 7 chỗ")
    assert newer["active_response"]["response_id"] != shown["response_id"]
    assert acknowledge(client, session_id, shown).status_code == 409
    other_id = create_session(client, "other-trip")["session_id"]
    assert acknowledge(client, other_id, shown).status_code == 409
    forged = client.post(f"/api/sessions/{other_id}/messages", json={"client_message_id":"forged","text":"Đồng ý", "rendered_response_ids":[shown["response_id"]]})
    assert forged.status_code == 409
    assert client.app.state.store.snapshot(other_id)["pending_count"] == 0


def test_owner_scope_and_tampered_cookie(client, settings):
    session_id = create_session(client)["session_id"]
    original_cookie = client.cookies.get("chat_owner")
    with TestClient(create_app(settings)) as other:
        assert other.get(f"/api/sessions/{session_id}").status_code == 401
        assert other.get("/api/bootstrap").status_code == 200
        assert other.get(f"/api/sessions/{session_id}").status_code == 404
        assert other.post(f"/api/sessions/{session_id}/messages", json={"client_message_id":"foreign","text":"Hủy"}).status_code == 404
        second = create_session(other)
        assert second["session_id"] != session_id
        other.cookies.set("chat_owner", original_cookie + "tampered")
        assert other.get(f"/api/sessions/{session_id}").status_code == 401


def test_signed_cookie_and_idempotent_session_survive_restart(settings):
    with TestClient(create_app(settings)) as first:
        bootstrap = first.get("/api/bootstrap")
        cookie = first.cookies.get("chat_owner")
        assert "HttpOnly" in bootstrap.headers["set-cookie"]
        assert "SameSite=strict" in bootstrap.headers["set-cookie"]
        session = create_session(first)
        session_id = session["session_id"]
        repeat = create_session(first)
        assert repeat["session_id"] == session_id
        snapshot = complete(first, session_id)
    with TestClient(create_app(settings)) as second:
        second.cookies.set("chat_owner", cookie)
        assert "set-cookie" not in second.get("/api/bootstrap").headers
        restored = second.get(f"/api/sessions/{session_id}").json()
        assert restored["active_response"] == snapshot["active_response"]
        shown = restored["active_response"]
        booked, _ = send_action(second, session_id, confirm_action(shown), rendered=[shown["response_id"]], reply=shown["response_id"])
        assert booked["booking_status"] == "booked"
        assert second.app.state.engine.booking.booking_count() == 1


def test_cursor_pagination_does_not_skip_events_or_other_sessions(client):
    session_id = create_session(client)["session_id"]
    other_id = create_session(client, "interleaved")["session_id"]
    for number in range(3):
        send_text(client, session_id, "Xin chào", key=f"msg-{number}")
        send_text(client, other_id, "Xin chào", key=f"other-{number}")
    full = client.get(f"/api/sessions/{session_id}?limit=200").json()
    events, after = [], 0
    for _ in range(20):
        page = client.get(f"/api/sessions/{session_id}/updates", params={"after_cursor":after,"limit":2}).json()
        events.extend(page["events"])
        if page["events"]:
            assert page["next_cursor"] == page["events"][-1]["cursor"]
            assert all(event["cursor"] > after for event in page["events"])
        assert page["current_cursor"] == full["current_cursor"]
        after = page["next_cursor"]
        if not page["has_more"]:
            break
    else:
        pytest.fail("Pagination did not finish")
    assert events == full["events"]
    assert len({event["cursor"] for event in events}) == len(events)
    empty = client.get(f"/api/sessions/{session_id}/updates", params={"after_cursor":after}).json()
    assert empty["events"] == []
    assert empty["next_cursor"] == after
    assert empty["has_more"] is False


def test_public_projection_errors_and_headers_hide_runtime_secrets(client):
    for endpoint in ("/api/bootstrap", "/api/ready", "/api/health"):
        response = client.get(endpoint)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "stable-test-secret" not in response.text
        assert "api_key" not in response.text.casefold()
    session_id = create_session(client)["session_id"]
    snapshot = complete(client, session_id)
    assert not {"state","booking_state","resolution","confirmation","transaction","issues"}.intersection(snapshot)
    private = client.post(f"/api/sessions/{session_id}/messages", json={"client_message_id":"bad","text":"secret contact text","graph_state":{"ready_to_book":True}})
    assert private.status_code == 422
    assert "secret contact text" not in private.text
    assert "ready_to_book" not in private.text
    assert private.json()["code"] == "INPUT_INVALID"
    forbidden = client.post(f"/api/sessions/{session_id}/messages", headers={"Origin":"https://untrusted.example"}, json={"client_message_id":"foreign-origin","text":"Hủy"})
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "ORIGIN_NOT_ALLOWED"
    assert "stable-test-secret" not in json.dumps(snapshot)


def test_openapi_declares_public_response_contracts_and_forbids_runtime_fields(client):
    schema = client.get("/openapi.json").json()
    components = schema["components"]["schemas"]
    receipt = schema["paths"]["/api/sessions/{session_id}/messages"]["post"]["responses"]["202"]
    assert receipt["content"]["application/json"]["schema"]["$ref"].endswith("/Receipt")
    invalid = schema["paths"]["/api/sessions/{session_id}/messages"]["post"]["responses"]["422"]
    assert invalid["content"]["application/json"]["schema"]["$ref"].endswith("/PublicError")
    snapshot = schema["paths"]["/api/sessions/{session_id}"]["get"]["responses"]["200"]
    assert snapshot["content"]["application/json"]["schema"]["$ref"].endswith("/ChatSnapshot")
    for name in ("ChatSnapshot", "AssistantResponse", "PublicBooking", "PublicPresentation", "TripSummary"):
        assert components[name]["additionalProperties"] is False
        assert not {"GraphState","booking_state","resolution","confirmation","transaction","payload","api_key"}.intersection(components[name]["properties"])
    assert components["SelectCandidate"]["properties"]["candidate_id"]["maxLength"] == 256


def test_provider_recovery_payload_never_leaks_through_public_booking_or_events(client):
    provider = client.app.state.engine.booking
    original_create = provider.create
    async def noisy_create(payload, key):
        result = await original_create(payload, key)
        return result | {"payload":{"api_key":"sentinel-provider-secret","raw_graph":"private"}}
    provider.create = noisy_create
    session_id = create_session(client)["session_id"]
    shown = complete(client, session_id)["active_response"]
    booked, _ = send_action(client, session_id, confirm_action(shown), rendered=[shown["response_id"]], reply=shown["response_id"])
    assert booked["booking_status"] == "booked"
    encoded = json.dumps(booked)
    assert "sentinel-provider-secret" not in encoded
    assert "raw_graph" not in encoded
    assert set(booked["booking"]) == {"booking_id","status","provider_status","provider"}
    booking = client.get(f"/api/sessions/{session_id}/booking")
    assert "sentinel-provider-secret" not in booking.text


def test_generated_frontend_types_match_openapi_contract():
    from scripts.generate_api_types import OUTPUT, generated_types
    assert OUTPUT.read_text(encoding="utf-8") == generated_types()
