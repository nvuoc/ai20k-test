"""Acceptance scenarios for architecture_fixed.md, independent of live services."""

import asyncio
import json
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.booking_sandbox import SandboxBookingProvider
from app.adapters.crm_sqlite import SQLiteCRM
from app.adapters.extractor import ExtractorRuntime, llm_extractor_func
from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.nlu_fixture import FixtureExtractorClient
from app.adapters.turn_fixture import extract_turn_fixture
from app.contracts.booking import BotState, Customer, ready_to_book
from app.contracts.nlu import NluResult
from app.domain.booking_engine import ConversationEngine
from app.domain.conversation import new_conversation_state
from app.graph.builder import DurableGraph


def act(intent, target=None, value=None):
    return {"intent": intent, "target": target, "value": value}


class Scripted:
    def __init__(self):
        self.acts = [act("chit_chat")]
        self.status = "clear"

    async def __call__(self, projection):
        return NluResult(speech_status=self.status, dialogue_acts=self.acts)


@pytest.fixture
def runtime():
    provider = SandboxBookingProvider(":memory:")
    crm = SQLiteCRM(":memory:")
    model = Scripted()
    engine = ConversationEngine(model, FixtureMapAdapter(), provider, crm=crm)
    yield engine, model, provider, crm
    provider.close()
    crm.close()


def fresh(engine):
    return engine.new_state(str(uuid.uuid4()), customer_phone="0901234567", customer_name="An")


async def say(runtime, state, acts, text="thông tin", **kwargs):
    engine, model, *_ = runtime
    model.acts = acts
    response = state["last_response"]
    return await engine.process(state, text, delivered_response_ids=[response["response_id"]],
                                reply_to_response_id=response["response_id"], **kwargs)


async def complete(runtime, **overrides):
    values = {"pickup": "Nhà hát Lớn Hà Nội", "destination": "Ga Hà Nội",
              "pickup_time": "đi ngay", "vehicle_type": "oto_4_cho"} | overrides
    state = await say(runtime, fresh(runtime[0]), [act("provide_info", k, v) for k, v in values.items()])
    for _ in range(10):
        if state["last_bot_action"]["action_type"] == "confirm_booking":
            return state
        state = await say(runtime, state, [act("confirm")], "đúng")
    raise AssertionError(state["final_response_text"])


def test_session_requires_identity_and_generates_independent_uuid(runtime):
    engine, *_ = runtime
    a, b = fresh(engine), fresh(engine)
    assert a["session_id"] != b["session_id"]
    assert a["customer_phone"] == b["customer_phone"]
    uuid.UUID(a["session_id"])
    BotState.model_validate({k: a[k] for k in BotState.model_fields})
    with pytest.raises(ValueError):
        Customer(customer_phone="wrong", customer_name="An")
    with pytest.raises(ValueError):
        Customer(customer_phone="0901234567", customer_name=" ")


def test_optional_passengers_and_meter_tariff_require_final_consent(runtime):
    engine, _, provider, _ = runtime

    async def run():
        state = await complete(runtime)
        assert ready_to_book(state)
        assert provider.booking_count() == 0
        assert state["booking_slots"]["passengers"]["value"] is None
        summary = state["last_response"]["summary"]
        assert summary["tariff"]["per_km"] > 0
        assert "fare" not in summary and '"amount":' not in json.dumps(summary)
        state = await say(runtime, state, [act("confirm")], "đồng ý")
        assert state["booking_status"] == "booked"
        receipt = await provider.get(state["transaction"]["booking_result"]["booking_id"])
        assert receipt["payload"]["customer_name"] == "An"
        assert receipt["payload"]["booking_slots"]["distance_km"] > 0
        assert "quote" not in receipt["payload"]
        booked_slots = deepcopy(state["booking_slots"])
        state = await say(runtime, state, [act("deny", "pickup")], "điểm đón sai")
        assert state["booking_slots"] == booked_slots
        assert provider.booking_count() == 1

    asyncio.run(run())


def test_scoped_confirmation_change_and_vague_denial_preserve_other_slots(runtime):
    async def run():
        state = await say(runtime, fresh(runtime[0]), [act("provide_info", "pickup", "Nhà hát Lớn Hà Nội"),
                                                       act("provide_info", "destination", "Ga Hà Nội")])
        assert state["last_bot_action"]["target_slots"] == ["pickup"]
        state = await say(runtime, state, [act("confirm")], "đúng")
        assert state["booking_slots"]["pickup"]["status"] == "confirmed"
        assert state["booking_slots"]["destination"]["status"] == "extracted"
        state = await complete(runtime)
        before = json.dumps(state["booking_slots"], sort_keys=True)
        state = await say(runtime, state, [act("deny")], "không")
        assert json.dumps(state["booking_slots"], sort_keys=True) == before
        state = await say(runtime, state, [act("change_info", "vehicle_type", "oto_7_cho"), act("confirm")], "đổi xe 7 chỗ, đúng")
        assert state["booking_slots"]["vehicle_type"]["status"] == "extracted"
        assert state["booking_slots"]["pickup"]["status"] == "confirmed"
        assert runtime[2].booking_count() == 0

    asyncio.run(run())


def test_cancel_requires_separate_confirmation_and_denial_resumes(runtime):
    async def run():
        state = await complete(runtime)
        state = await say(runtime, state, [act("cancel")], "hủy")
        assert state["booking_status"] == "cancel_pending"
        state = await say(runtime, state, [act("deny")], "không")
        assert state["booking_status"] != "canceled"
        state = await say(runtime, state, [act("cancel")], "hủy")
        state = await say(runtime, state, [act("confirm")], "đúng")
        assert state["booking_status"] == "canceled"
        assert runtime[2].booking_count() == 0

    asyncio.run(run())


def test_multi_intent_cancel_preserves_updates_and_answers_question(runtime):
    async def run():
        state = await say(runtime, fresh(runtime[0]), [act("cancel"), act("provide_info", "pickup", "Nhà hát Lớn Hà Nội"),
                                                       act("ask_question", value="giá một km bao nhiêu?")], "hủy; đón ở Nhà hát Lớn; giá một km?")
        assert state["booking_slots"]["pickup"]["coords"]
        assert state["booking_status"] == "cancel_pending"
        assert "đồng/km" in state["final_response_text"]
        assert state["last_bot_action"]["action_type"] == "confirm_cancel"
        assert set(state["intents"]) >= {"cancel", "provide_info", "ask_question"}

    asyncio.run(run())


def test_stopovers_are_geocoded_and_route_includes_every_leg(runtime):
    async def run():
        state = await complete(runtime, stops=["Bạch Mai cổng chính"])
        stop = state["booking_slots"]["stopovers"][0]
        assert stop["order"] == 1 and stop["address"]["coords"]
        assert stop["address"]["status"] == "confirmed"
        state = await say(runtime, state, [act("confirm")], "đồng ý")
        assert state["booking_status"] == "booked"
        route = state["resolution"]["route"]
        assert route["waypoints_count"] == 3 and len(route["legs"]) == 2
        assert route["distance_km"] == pytest.approx(9)

    asyncio.run(run())


def test_mega_pickup_asks_once_then_falls_back_and_destination_does_not_require_gate(runtime):
    async def run():
        state = await say(runtime, fresh(runtime[0]), [act("provide_info", "pickup", "Sân bay Nội Bài"),
                                                       act("provide_info", "destination", "Bệnh viện Bạch Mai")])
        assert state["booking_slots"]["pickup"]["metadata"]["clarification_count"] == 1
        assert state["booking_slots"]["destination"]["default_point_used"]
        state = await say(runtime, state, [act("unclear")], "không biết, đừng hỏi nữa")
        pickup = state["booking_slots"]["pickup"]
        assert pickup["default_point_used"] and pickup["coords"]
        assert pickup["note"] and pickup["is_mega_poi"]
        assert state["last_bot_action"]["action_type"] == "confirm_slots"

    asyncio.run(run())


def test_unclear_still_applies_valid_entities(runtime):
    async def run():
        runtime[1].status = "low_confidence"
        state = await say(runtime, fresh(runtime[0]), [act("provide_info", "vehicle_type", "oto_7_cho"), act("unclear")])
        assert state["booking_slots"]["vehicle_type"]["value"] == "oto_7_cho"
        assert "unclear" in state["intents"]

    asyncio.run(run())


def test_unknown_moving_pickup_hands_off_without_repeated_questions(runtime):
    async def run():
        state = await say(runtime, fresh(runtime[0]), [act("provide_info", "pickup", "đang đi bộ dọc đường, không rõ ở đâu")])
        assert state["booking_status"] == "operator_required"
        assert state["last_bot_action"]["action_type"] == "human_handoff"
        assert runtime[2].booking_count() == 0

    asyncio.run(run())


def test_crm_suggestion_is_scoped_and_denial_keeps_destination(runtime):
    async def run():
        previous = await complete(runtime)
        runtime[3].remember("0901234567", "home", previous["booking_slots"]["pickup"])
        state = await say(runtime, fresh(runtime[0]), [act("provide_info", "pickup", "nhà"),
                                                       act("provide_info", "destination", "Ga Hà Nội")])
        assert state["booking_slots"]["pickup"]["status"] == "extracted"
        state = await say(runtime, state, [act("deny", "pickup")], "không đúng nhà")
        assert state["booking_slots"]["pickup"]["status"] == "empty"
        assert state["booking_slots"]["destination"]["coords"]

    asyncio.run(run())


def test_real_fixture_interpretation_supports_text_only_booking(runtime):
    engine, _, provider, _ = runtime

    async def extract(data):
        return extract_turn_fixture(data)

    async def run():
        engine.extractor = extract
        state = fresh(engine)
        for text in ["Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, đi ngay, xe 4 chỗ", "đúng", "đúng", "đúng", "đồng ý"]:
            response = state["last_response"]
            state = await engine.process(state, text, delivered_response_ids=[response["response_id"]], reply_to_response_id=response["response_id"])
        assert state["booking_status"] == "booked", state["final_response_text"]
        assert provider.booking_count() == 1

    asyncio.run(run())


async def fixture_say(engine, state, text):
    async def extract(data):
        return await llm_extractor_func(data, runtime=ExtractorRuntime(
            client=FixtureExtractorClient(), vehicle_codes=frozenset({"xe_may", "oto_4_cho", "oto_7_cho"})))
    engine.extractor = extract
    response = state["last_response"]
    return await engine.process(state, text, delivered_response_ids=[response["response_id"]],
                                reply_to_response_id=response["response_id"])


@pytest.mark.parametrize("query", ["Không biết, đừng hỏi nữa", "không", "không nhớ"])
def test_mega_fallback_through_real_extractor_preserves_parent(runtime, query):
    async def run():
        engine = runtime[0]
        state = await fixture_say(engine, fresh(engine), "Đón ở Sân bay Nội Bài, đến Ga Hà Nội, đi ngay, xe 4 chỗ")
        state = await fixture_say(engine, state, query)
        assert state["booking_slots"]["pickup"]["default_point_used"]
        assert state["booking_slots"]["pickup"]["metadata"]["clarification_count"] == 1
        assert state["last_bot_action"]["action_type"] == "confirm_slots"
        assert "gọi khách" in state["final_response_text"]
    asyncio.run(run())


def test_real_extractor_number_selects_candidate_then_requires_confirmation(runtime):
    async def run():
        engine = runtime[0]
        state = await fixture_say(engine, fresh(engine), "Đón ở Nhà hát Lớn Hà Nội, đến Trường Sao Mai, đi ngay, xe 4 chỗ")
        state = await fixture_say(engine, state, "đúng")
        assert len(state["candidates"]) == 2
        state = await fixture_say(engine, state, "1")
        assert state["booking_slots"]["destination"]["status"] == "extracted"
        assert "Cầu Giấy" in state["booking_slots"]["destination"]["formatted"]
        assert runtime[2].booking_count() == 0
    asyncio.run(run())


@pytest.mark.parametrize("query,expected", [
    ("Cho xe 4 chỗ, à thôi xe 7 chỗ đi", "oto_7_cho"),
    ("Cho xe 7 chỗ, à thôi xe 4 chỗ đi", "oto_4_cho"),
    ("Cho xe máy", "xe_may"),
])
def test_last_vehicle_correction_and_motorcycle_supported(runtime, query, expected):
    async def run():
        state = await fixture_say(runtime[0], fresh(runtime[0]), query)
        assert state["booking_slots"]["vehicle_type"]["value"] == expected
        assert state["booking_slots"]["vehicle_type"]["status"] == "extracted"
    asyncio.run(run())


def test_read_inquiry_confirmation_never_confirms_or_dispatches_booking(runtime):
    async def run():
        engine = runtime[0]
        state = await complete(runtime)
        before = deepcopy(state["booking_slots"])
        state = await fixture_say(engine, state, "Từ Nhà hát Lớn Hà Nội đến Bạch Mai cổng sau bao nhiêu km và mất bao lâu?")
        for _ in range(5):
            if state["last_response"]["action"] != "confirm_slots":
                break
            assert state["last_bot_action"]["target_slots"] == []
            state = await fixture_say(engine, state, "đúng")
        assert state["last_response"]["inquiry"]["can_use_route"]
        assert state["booking_slots"] == before
        assert runtime[2].booking_count() == 0
        state = await fixture_say(engine, state, "Tuyến vừa hỏi nếu đi xe 7 chỗ giá bao nhiêu?")
        assert state["last_response"]["inquiry"]["vehicle"] == "oto_7_cho"
        assert state["booking_slots"] == before
        assert "đồng/km" in state["final_response_text"]
        state = await fixture_say(engine, state, "Dùng tuyến vừa hỏi để đặt")
        assert state["booking_slots"]["destination"]["status"] == "extracted"
        assert "cổng sau" in state["booking_slots"]["destination"]["formatted"]
        assert runtime[2].booking_count() == 0
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["scope", "target", "expired", "future"])
def test_unrelated_or_stale_map_data_never_reaches_booking(runtime, mode):
    engine, *_ = runtime
    maps = engine.maps
    class BrokenBinding:
        async def resolve(self, *args, **kwargs):
            result = await maps.resolve(*args, **kwargs)
            if mode == "scope":
                result["binding"]["scope_id"] = "other-session"
            elif mode == "target":
                result["target"] = "destination"
            elif mode == "expired":
                result["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            else:
                result["resolved_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            return result
    engine.maps = BrokenBinding()
    async def run():
        state = await say(runtime, fresh(engine), [act("provide_info", "pickup", "Nhà hát Lớn Hà Nội")])
        assert state["booking_slots"]["pickup"]["coords"] is None
        assert state["tool_status"] == "API_ERROR"
        assert not ready_to_book(state)
    asyncio.run(run())


@pytest.mark.parametrize("query", ["Hà Nội", "đường Nguyễn Trãi"])
def test_broad_pickup_cannot_be_confirmed_as_an_administrative_or_street_centroid(runtime, query):
    async def run():
        state = await say(runtime, fresh(runtime[0]), [act("provide_info", "pickup", query)])
        state = await say(runtime, state, [act("confirm")], "đúng")
        assert state["booking_slots"]["pickup"]["status"] != "confirmed"
        assert runtime[2].booking_count() == 0
    asyncio.run(run())


def test_landmark_preserves_relation_without_inventing_coordinates(runtime):
    async def run():
        state = await say(runtime, fresh(runtime[0]), [act("provide_info", "pickup", "đối diện Ga Hà Nội")])
        anchor = (await runtime[0].maps.resolve("Ga Hà Nội"))["place"]
        pickup = state["booking_slots"]["pickup"]
        assert pickup["coords"] == {"lat": anchor["lat"], "lng": anchor["lon"]}
        assert "đối diện" in pickup["note"] and "đối diện" in state["final_response_text"]
    asyncio.run(run())


def test_moving_customer_with_a_sourced_landmark_is_not_needlessly_handed_off(runtime):
    async def run():
        state = await say(runtime, fresh(runtime[0]), [act("provide_info", "pickup", "đang đi bộ, đối diện Ga Hà Nội")])
        assert state["booking_slots"]["pickup"]["coords"]
        assert state["booking_status"] != "operator_required"
        assert "đang đi bộ" in state["booking_slots"]["pickup"]["note"]
    asyncio.run(run())


def test_crm_does_not_leak_addresses_across_phones(runtime):
    async def run():
        state = await complete(runtime)
        runtime[3].remember("0911234567", "home", state["booking_slots"]["pickup"])
        state = await say(runtime, fresh(runtime[0]), [act("provide_info", "pickup", "nhà")])
        assert state["booking_slots"]["pickup"]["coords"] is None
        assert state["booking_slots"]["pickup"]["status"] == "needs_clarification"
    asyncio.run(run())


def test_precise_denial_does_not_repeat_the_rejected_address_as_resolved(runtime):
    async def run():
        state = await complete(runtime)
        destination = deepcopy(state["booking_slots"]["destination"])
        state = await say(runtime, state, [act("deny", "pickup")], "Điểm đón sai")
        assert state["booking_slots"]["pickup"]["status"] == "needs_clarification"
        assert state["booking_slots"]["pickup"]["coords"] is None
        assert state["booking_slots"]["destination"] == destination
    asyncio.run(run())


def test_static_faq_and_session_repeat_keep_valid_updates(runtime):
    async def run():
        state = await say(runtime, fresh(runtime[0]), [act("provide_info", "vehicle_type", "oto_7_cho"),
                                                      act("provide_info", "general_note", "cốp rộng"),
                                                      act("ask_question", value="Có được mang thú cưng không?")])
        assert state["booking_slots"]["vehicle_type"]["value"] == "oto_7_cho"
        assert "cốp rộng" in state["booking_slots"]["general_note"]
        assert "lồng" in state["final_response_text"]
        state = await say(runtime, state, [act("repeat_request", "vehicle_type")], "đọc lại loại xe")
        assert "Ô tô 7 chỗ" in state["final_response_text"]
        assert not runtime[0].kb.retrieve("cho tôi thông tin chuyến")
    asyncio.run(run())


def test_route_membership_reads_tools_without_mutating_booking(runtime):
    async def run():
        state = await complete(runtime)
        before = deepcopy(state["booking_slots"])
        state = await fixture_say(runtime[0], state, "Có đi qua Ga Hà Nội không?")
        assert "chính là một điểm" in state["final_response_text"]
        assert state["booking_slots"] == before
        assert runtime[2].booking_count() == 0
    asyncio.run(run())


def test_canonical_checkpoint_recovers_commit_without_a_second_create(runtime, tmp_path):
    engine, _, provider, _ = runtime
    original = provider.create
    attempts = []
    async def crash_after_commit(payload, key):
        await original(payload, key)
        attempts.append(key)
        raise RuntimeError("crash after provider commit")
    async def run():
        state = await complete(runtime)
        provider.create = crash_after_commit
        runtime[1].acts = [act("confirm")]
        response = state["last_response"]
        sid = state["session_id"]
        async with DurableGraph(engine, tmp_path / "canonical.sqlite") as graph:
            await graph.initialize(sid, state)
            kwargs = {"delivered_response_ids": [response["response_id"]], "reply_to_response_id": response["response_id"]}
            with pytest.raises(RuntimeError, match="crash after provider commit"):
                await graph.process(sid, "commit", "đồng ý", **kwargs)
            assert provider.booking_count() == 1
            restored = await graph.process(sid, "commit", "đồng ý", **kwargs)
            assert restored["booking_status"] == "booked"
            assert len(attempts) == 1 and provider.operation_count() == 1
            assert len(restored["messages"]) == len(state["messages"]) + 2
    asyncio.run(run())


def test_changing_and_clearing_a_note_replaces_only_that_note(runtime):
    async def run():
        state = await complete(runtime)
        pickup = deepcopy(state["booking_slots"]["pickup"])
        state = await say(runtime, state, [act("provide_info", "general_note", "cốp rộng")])
        state = await say(runtime, state, [act("change_info", "general_note", "tài xế không hút thuốc")])
        assert state["booking_slots"]["general_note"] == "tài xế không hút thuốc"
        state = await say(runtime, state, [act("deny", "general_note")])
        assert state["booking_slots"]["general_note"] is None
        assert state["booking_slots"]["pickup"] == pickup
        assert runtime[2].booking_count() == 0
    asyncio.run(run())


def test_legacy_unknown_commit_is_recovered_with_missing_profile_and_same_ids(runtime):
    async def run():
        engine, _, provider, _ = runtime
        old = new_conversation_state("legacy-session")
        maps = engine.maps
        payload = {"session_id": "legacy-session", "draft_id": old["control"]["draft_id"],
                   "pickup": (await maps.resolve("Nhà hát Lớn Hà Nội"))["place"],
                   "destination": (await maps.resolve("Ga Hà Nội", "destination"))["place"],
                   "slots": {"pickup": "Nhà hát Lớn Hà Nội", "destination": "Ga Hà Nội"}}
        receipt = await provider.create(payload, "old-create")
        old["transaction"]["active_operation"] = {"type": "create", "idempotency_key": "old-create", "payload": payload, "status": "unknown"}
        old["booking_status"] = "booking_unknown"
        restored = await engine.reconcile(old, "recover-legacy")
        assert restored["booking_status"] == "booked"
        assert restored["session_id"] == "legacy-session"
        assert restored["transaction"]["booking_result"]["booking_id"] == receipt["booking_id"]
        assert restored["transaction"]["committed_snapshot"] == payload
        assert restored["booking_slots"]["pickup"]["coords"]
        assert provider.operation_count() == provider.booking_count() == 1
    asyncio.run(run())
