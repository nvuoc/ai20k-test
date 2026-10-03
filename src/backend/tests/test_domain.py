"""Safety checks inspect actual durable bookings, not only response wording."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.adapters.booking_sandbox import SandboxBookingProvider
from app.adapters.extractor import ExtractorError
from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.quote_fixture import FixtureQuoteAdapter
from app.contracts.nlu import NluResult
from app.domain.engine import ChatEngine, acknowledge, new_state


def act(intent, target=None, value=None):
    return {"intent": intent, "target": target, "value": value}


CORE_VALUES = {"pickup": "Nhà hát Lớn Hà Nội", "destination": "Ga Hà Nội",
    "pickup_time": "ngay bây giờ", "passengers": 2, "vehicle_type": "oto_4_cho",
    "contact_phone": "0901234567"}


class Extractor:
    def __init__(self):
        self.acts = []
        self.calls = 0
        self.status = "clear"
        self.error = None

    async def __call__(self, projection):
        self.calls += 1
        if self.error:
            raise self.error
        return NluResult.model_validate({"speech_status": self.status, "dialogue_acts": self.acts})


@pytest.fixture
def setup(tmp_path):
    extractor = Extractor()
    provider = SandboxBookingProvider(tmp_path / "provider.sqlite")
    engine = ChatEngine(extractor, FixtureMapAdapter(), provider, FixtureQuoteAdapter())
    yield engine, extractor, provider
    provider.close()


async def complete(engine, extractor, **overrides):
    values = CORE_VALUES | overrides
    extractor.acts = [act("provide_info", slot, value) for slot, value in values.items()]
    return await engine.process(new_state("session-1"), "Đặt chuyến", event_id="complete")


async def confirm(engine, extractor, state, **kwargs):
    extractor.acts = [act("confirm")]
    response_id = state["last_response"]["response_id"]
    return await engine.process(state, "Đồng ý đặt", event_id="confirm",
        delivered_response_ids=[response_id], reply_to_response_id=response_id, **kwargs)


def test_complete_summary_only_creates_after_delivered_consent(setup):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor)
        assert state["booking_status"] == "awaiting_confirmation"
        assert provider.booking_count() == 0
        assert len(state["confirmation"]["pending_prompt"]["scope"]) == 6
        state = await confirm(engine, extractor, state)
        assert state["booking_status"] == "booked"
        assert state["transaction"]["booking_result"]["booking_id"].startswith("SBX-")
        assert provider.booking_count() == 1
        assert state["transaction"]["committed_snapshot"]["slots"] == CORE_VALUES | {
            slot: None for slot in ("contact_name", "pickup_note", "luggage", "payment_method", "stops", "special_requests")}
        retry = await engine.process(state, "Đồng ý", event_id="confirm")
        assert retry == state
        assert provider.operation_count() == 1
    asyncio.run(scenario())


def test_missing_render_evidence_cannot_book(setup):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor)
        extractor.acts = [act("confirm")]
        state = await engine.process(state, "Đồng ý", event_id="unshown")
        assert provider.booking_count() == 0
        assert state["confirmation"]["accepted_snapshot"] is None
        assert state["last_response"]["action"] == "confirm_booking"
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("vehicle_type", "oto_7_cho"), ("contact_phone", "0912345678"), ("passengers", 3)])
def test_confirm_and_change_never_creates_old_or_new_payload(setup, field, value):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor)
        response = state["last_response"]["response_id"]
        extractor.acts = [act("confirm"), act("change_info", field, value)]
        state = await engine.process(state, "Đồng ý nhưng đổi thông tin", event_id="change",
            delivered_response_ids=[response])
        assert state["booking_state"][field] == {"value": value, "confirmed": False}
        assert state["booking_state"]["pickup"]["confirmed"] is True
        assert state["confirmation"]["accepted_snapshot"] is None
        assert provider.booking_count() == 0
        state = await confirm(engine, extractor, state)
        assert provider.booking_count() == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["Đặt nếu dưới 200 nghìn", "Đồng ý nhưng đổi xe nhé", "Chỉ đúng điểm đón thôi", "Địa chỉ đúng, giờ chưa chắc"])
def test_untrusted_confirm_does_not_erase_full_turn_conditions(setup, text):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor)
        response = state["last_response"]["response_id"]
        extractor.acts = [act("confirm")]
        state = await engine.process(state, text, event_id="conditional", delivered_response_ids=[response])
        assert provider.booking_count() == 0
        assert state["confirmation"]["accepted_snapshot"] is None
    asyncio.run(scenario())


def test_question_in_confirmation_answers_before_reconfirmation(setup):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor)
        response = state["last_response"]["response_id"]
        extractor.acts = [act("confirm"), act("ask_question", value="Giá bao nhiêu?")]
        state = await engine.process(state, "Đúng rồi mà giá bao nhiêu?", event_id="price",
            delivered_response_ids=[response])
        assert "Giá thử nghiệm" in state["last_response"]["text"]
        assert provider.booking_count() == 0
        state = await confirm(engine, extractor, state)
        assert provider.booking_count() == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("pickup_time", "ngày mai lúc 8 giờ"),
    ("stops", ["Bạch Mai"]), ("special_requests", ["pet"]), ("payment_method", "linked_card"),
    ("passengers", 5), ("luggage", {"count": 2, "size": "large"}),
    ("contact_phone", "123456")])
def test_unsupported_and_invalid_values_survive_generic_yes(setup, field, value):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor, **{field: value})
        assert state["issues"]
        assert state["booking_state"][field]["value"] == value
        extractor.acts = [act("confirm")]
        state = await engine.process(state, "Ừ", event_id="generic",
            delivered_response_ids=[state["last_response"]["response_id"]])
        assert state["issues"]
        assert state["booking_state"][field]["value"] == value
        assert provider.booking_count() == 0
    asyncio.run(scenario())


def test_optional_zero_and_empty_arrays_in_confirmed_payload(setup):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor, luggage={"count": 0, "size": "none"},
            stops=[], special_requests=[], payment_method="cash", pickup_note="Áo xanh")
        assert len(state["confirmation"]["pending_prompt"]["scope"]) == 11
        state = await confirm(engine, extractor, state)
        assert state["booking_status"] == "booked"
        payload = state["transaction"]["committed_snapshot"]["slots"]
        assert payload["stops"] == []
        assert payload["special_requests"] == []
        assert payload["luggage"] == {"count": 0, "size": "none"}
        assert payload["pickup_note"] == "Áo xanh"
        assert provider.booking_count() == 1
    asyncio.run(scenario())


def test_airport_and_booking_for_other_require_conditional_fields(setup):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor, destination="Nội Bài T1 cửa 3")
        assert state["last_response"]["focus"] == "luggage"
        extractor.acts = [act("provide_info", "luggage", {"count": 0, "size": "none"})]
        state = await engine.process(state, "Đặt hộ mẹ không có hành lý", event_id="other")
        assert state["last_response"]["focus"] == "contact_name"
        assert provider.booking_count() == 0
    asyncio.run(scenario())


def test_persistent_round_trip_requires_explicit_withdrawal(setup):
    engine, extractor, provider = setup
    async def scenario():
        extractor.acts = [act("provide_info", slot, value) for slot, value in CORE_VALUES.items()]
        state = await engine.process(new_state("round"), "Tôi cần khứ hồi", event_id="round")
        assert "unsupported_journey" in state["issues"]
        extractor.acts = [act("confirm")]
        state = await engine.process(state, "Đồng ý", event_id="generic")
        assert "unsupported_journey" in state["issues"]
        extractor.acts = [act("chit_chat")]
        state = await engine.process(state, "Đi một chiều thôi", event_id="withdraw")
        assert "unsupported_journey" not in state["issues"]
        assert state["last_response"]["action"] == "confirm_booking"
        assert provider.booking_count() == 0
    asyncio.run(scenario())


def test_low_confidence_and_extractor_failure_keep_values_and_block_cancel(setup):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor)
        original_slots = deepcopy(state["booking_state"])
        extractor.status = "low_confidence"
        extractor.acts = [act("cancel")]
        state = await engine.process(state, "Hủy", event_id="low")
        assert state["booking_state"] == original_slots
        assert state["booking_status"] != "cancelled"
        extractor.error = ExtractorError("RATE_LIMITED", "limited")
        state = await engine.process(state, "Gửi lại", event_id="rate")
        assert state["booking_state"] == original_slots
        assert state["last_response"]["reason"] == "RATE_LIMITED"
        assert provider.booking_count() == 0
    asyncio.run(scenario())


def test_quote_expiry_and_superseded_ingress_require_new_summary(setup):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor)
        state["resolution"]["quote"]["expires_at"] = 0
        state = await confirm(engine, extractor, state)
        assert provider.booking_count() == 0
        state = await confirm(engine, extractor, state, ingress_guard=lambda: False)
        # Duplicate event is rejected. A genuinely new turn must also respect guard.
        extractor.acts = [act("confirm")]
        state = await engine.process(state, "Đặt", event_id="dispatch", ingress_guard=lambda: False,
            delivered_response_ids=[state["last_response"]["response_id"]])
        assert provider.booking_count() == 0
        assert state["last_response"]["reason"] == "SUPERSEDED_BEFORE_DISPATCH"
    asyncio.run(scenario())


def test_stale_confirmation_button_and_ack_rejected(setup):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor)
        old = state["last_response"]["response_id"]
        extractor.acts = [act("change_info", "contact_phone", "0912345678")]
        state = await engine.process(state, "Đổi số", event_id="edit")
        acknowledge(state, [old])
        assert state["last_response"]["delivery_status"] == "planned"
        state = await engine.process(state, "", action={"type": "confirm_booking", "response_id": old},
            event_id="stale", delivered_response_ids=[old])
        assert provider.booking_count() == 0
    asyncio.run(scenario())


def test_candidate_requires_render_and_current_set(setup):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor, destination="Bạch Mai")
        assert state["last_response"]["action"] == "offer_candidates"
        candidate = state["candidates"][0]
        action = {"type": "select_candidate", **candidate}
        without_ack = await engine.process(state, "", action=action, event_id="noack")
        assert without_ack["booking_state"]["destination"]["value"] == "Bạch Mai"
        selected = await engine.process(state, "", action=action, event_id="selected",
            delivered_response_ids=[state["last_response"]["response_id"]])
        assert selected["booking_state"]["destination"]["value"] == candidate["label"]
        assert selected["booking_state"]["destination"]["confirmed"]
        assert provider.booking_count() == 0
    asyncio.run(scenario())


def test_timeout_reconciles_same_booking_and_cancels_exact_id(setup):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor)
        provider.faults["create_lost_response"] = 1
        state = await confirm(engine, extractor, state)
        assert state["booking_status"] == "booking_unknown"
        assert provider.booking_count() == 1
        extractor.acts = [act("cancel")]
        state = await engine.process(state, "Hủy", event_id="cancel")
        assert state["booking_status"] == "cancelled"
        assert provider.booking_count() == 1
        assert provider.operation_count("cancel") == 1
        booking = await provider.get(state["transaction"]["booking_result"]["booking_id"])
        assert booking["provider_status"] == "cancelled"
    asyncio.run(scenario())


def test_booked_changes_keep_committed_payload(setup):
    engine, extractor, provider = setup
    async def scenario():
        state = await confirm(engine, extractor, await complete(engine, extractor))
        committed = deepcopy(state["transaction"]["committed_snapshot"])
        extractor.acts = [act("change_info", "passengers", 3)]
        state = await engine.process(state, "Đổi ba người", event_id="amend")
        assert state["last_response"]["action"] == "handoff"
        assert state["transaction"]["committed_snapshot"] == committed
        assert state["booking_state"]["passengers"]["value"] == 2
        assert provider.booking_count() == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["Nếu hủy thì có phí không?", "Đừng hủy chuyến", "Không hủy nữa", "Hủy giúp tôi, à thôi giữ chuyến"])
def test_incorrect_extractor_cancel_cannot_override_negation_or_condition(setup, text):
    engine, extractor, provider = setup
    async def scenario():
        state = await confirm(engine, extractor, await complete(engine, extractor))
        extractor.acts = [act("cancel")]
        state = await engine.process(state, text, event_id="untrusted-cancel")
        assert state["booking_status"] == "booked"
        assert state["last_response"]["reason"] == "AMBIGUOUS_CANCEL_REQUEST"
        assert provider.operation_count("cancel") == 0
        booking = await provider.get(state["transaction"]["booking_result"]["booking_id"])
        assert booking["provider_status"] == "booked"
    asyncio.run(scenario())


@pytest.mark.parametrize("text,key", [("Tôi muốn đặt trước ngày mai", "raw_scheduled_request"),
    ("Tôi muốn ghé Bạch Mai", "raw_stop_request"),
    ("Thanh toán bằng momo", "raw_payment_request"),
    ("Tôi cần xe 16 chỗ", "raw_vehicle_request")])
def test_omitted_unsupported_request_remains_blocked_across_turns(setup, text, key):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor)
        extractor.acts = [act("chit_chat")]
        state = await engine.process(state, text, event_id="omitted")
        assert key in state["issues"]
        extractor.acts = [act("confirm")]
        state = await engine.process(state, "Đồng ý", event_id="generic",
            delivered_response_ids=[state["last_response"]["response_id"]])
        assert key in state["issues"]
        assert provider.booking_count() == 0
    asyncio.run(scenario())


def test_gate_note_change_updates_route_and_demands_new_consent(setup):
    engine, extractor, provider = setup
    async def scenario():
        state = await complete(engine, extractor, pickup="Bạch Mai cổng chính")
        old_quote = state["resolution"]["quote"]["quote_id"]
        response = state["last_response"]["response_id"]
        extractor.acts = [act("confirm"), act("change_info", "pickup_note", "đón cổng sau")]
        state = await engine.process(state, "Đồng ý nhưng đổi sang đón cổng sau", event_id="gate",
            delivered_response_ids=[response])
        assert state["resolution"]["locations"]["pickup"]["place"]["id"] == "hn_bachmai_back"
        assert "cổng sau" in state["booking_state"]["pickup"]["value"]
        assert not state["booking_state"]["pickup"]["confirmed"]
        assert state["resolution"]["route"]["pickup_id"] == "hn_bachmai_back"
        assert state["resolution"]["quote"]["quote_id"] != old_quote
        assert provider.booking_count() == 0
        state = await confirm(engine, extractor, state)
        assert state["booking_status"] == "booked"
        assert state["transaction"]["committed_snapshot"]["pickup"]["id"] == "hn_bachmai_back"
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["Cần ghế trẻ em", "Cần hai ghế trẻ em", "Can ghe tre em"])
def test_child_seat_is_not_a_stop_and_explicit_withdrawal_unblocks_summary(setup, text):
    engine, extractor, provider = setup

    async def scenario():
        state = await complete(engine, extractor)
        extractor.acts = [act("provide_info", "special_requests", ["child_seat"])]
        state = await engine.process(state, text, event_id="child-seat")
        assert "raw_stop_request" not in state["issues"]
        assert "validation_special_requests" in state["issues"]
        assert provider.booking_count() == 0
        extractor.acts = [act("change_info", "special_requests", [])]
        state = await engine.process(state, "Bỏ yêu cầu ghế trẻ em", event_id="withdraw-seat")
        assert not state["issues"]
        assert state["last_response"]["action"] == "confirm_booking"
        state = await confirm(engine, extractor, state)
        assert state["booking_status"] == "booked"
        assert state["transaction"]["committed_snapshot"]["slots"]["special_requests"] == []

    asyncio.run(scenario())


def test_long_provider_ref_is_bounded_for_nlu_and_selects_original_place(setup):
    engine, extractor, provider = setup

    async def scenario():
        state = await complete(engine, extractor, destination="Bạch Mai")
        batch = state["resolution"]["candidate_sets"]["destination"]
        provider_id = "provider:" + "x" * 600
        batch["places"][0]["id"] = provider_id
        engine._offer_candidates(state, batch, "")
        response_id = state["last_response"]["response_id"]
        candidate = state["candidates"][0]
        assert len(candidate["candidate_id"]) <= 256
        assert candidate["candidate_id"] != provider_id
        state = await engine.process(state, "", action={"type": "select_candidate",
            "candidate_id": candidate["candidate_id"], "candidate_set_id": batch["candidate_set_id"]},
            delivered_response_ids=[response_id], event_id="select-long-ref")
        assert state["resolution"]["locations"]["destination"]["place"]["id"] == provider_id
        assert provider.booking_count() == 0

    asyncio.run(scenario())
