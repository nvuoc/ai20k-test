"""V3 acceptance: textual consent, area fees, migration and durable replay."""

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.adapters.booking_sandbox import SandboxBookingProvider
from app.adapters.extractor import CallBudget, ExtractorError, ExtractorRuntime, llm_extractor_func
from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.nlu_fixture import FixtureExtractorClient
from app.adapters.quote_fixture import QuoteAdapter
from app.adapters.turn_fixture import extract_turn_fixture
from app.config import Settings
from app.domain.conversation import ConversationEngine, new_conversation_state
from app.domain.location_confirmation import fee_reply_matches, upgrade_state
from app.graph.builder import DurableGraph
from app.main import create_app

COMPLETE = "Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, đi ngay, 2 người, xe 4 chỗ, số 0901234567."
AREA = "Đón tôi ở Nhà hát Lớn Hà Nội, đến Ocean Park 1, đi ngay, 2 người, xe 4 chỗ, số 0901234567."


@pytest.fixture
def runtime(tmp_path):
    client = FixtureExtractorClient()

    async def extractor(data):
        return await llm_extractor_func(
            data,
            runtime=ExtractorRuntime(
                client=client,
                call_budget=CallBudget(1),
                vehicle_codes=frozenset({"oto_4_cho", "oto_7_cho", "xe_may_dien", "xe_may"}),
            ),
        )

    provider = SandboxBookingProvider(tmp_path / "provider.sqlite")
    engine = ConversationEngine(
        extractor,
        FixtureMapAdapter(),
        provider,
        QuoteAdapter(),
        location_confirmation=True,
        area_assistance_enabled=True,
    )
    yield engine, provider
    provider.close()


async def say(engine, state, text, *, delivered=True, reply=None, event_id=None):
    response = state["last_response"]
    return await engine.process(
        state,
        text,
        event_id=event_id or f"event-{state['control']['generation'] + 1}",
        delivered_response_ids=[response["response_id"]] if delivered else [],
        reply_to_response_id=reply or response["response_id"],
    )


async def confirm_locations(engine, state):
    for _ in range(3):
        if state["last_response"]["action"] != "confirm_location":
            return state
        state = await say(engine, state, "đúng")
    return state


def test_unique_locations_are_confirmed_once_before_booking(runtime):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state("unique"), COMPLETE)
        assert state["control"]["schema_version"] == 6
        assert state["last_response"]["action"] == "confirm_location"
        assert state["last_response"]["focus"] == "destination"
        assert not state["last_response"]["candidates"]
        state = await say(engine, state, "đồng ý")
        assert state["booking_state"]["destination"]["confirmed"]
        assert state["last_response"]["action"] == "confirm_location"
        assert state["last_response"]["focus"] == "pickup"
        assert not state["confirmation"]["accepted_snapshot"]
        assert provider.booking_count() == 0
        state = await say(engine, state, "đúng")
        assert state["last_response"]["action"] == "confirm_booking"
        state = await say(engine, state, "đồng ý đặt xe")
        assert state["booking_status"] == "booked"
        assert provider.booking_count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("case", ["unrendered", "old_reply", "expired", "revision"])
def test_stale_location_yes_cannot_confirm(runtime, case):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state(case), COMPLETE)
        if case == "expired":
            state["location_workflow"]["proposals"]["destination"]["expires_at"] = 0
        if case == "revision":
            state["control"]["booking_revision"] += 1
        state = await say(
            engine,
            state,
            "đúng",
            delivered=case != "unrendered",
            reply="old-response" if case == "old_reply" else None,
        )
        assert not state["booking_state"]["destination"]["confirmed"]
        assert state["last_response"]["reason"] == "STALE_LOCATION_PROPOSAL"
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_yes_with_correction_applies_change_without_location_consent(runtime):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state("correction"), COMPLETE)
        state = await say(engine, state, "Đồng ý nhưng đổi điểm đến sang Bạch Mai cổng sau")
        assert "Bạch Mai" in state["booking_state"]["destination"]["value"]
        assert not state["booking_state"]["destination"]["confirmed"]
        assert not state["confirmation"]["accepted_snapshot"]
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_rejected_unique_place_requests_another_address(runtime):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state("reject"), COMPLETE)
        state = await say(engine, state, "không")
        assert "địa chỉ" in state["last_response"]["text"]
        assert not state["booking_state"]["destination"]["confirmed"]
        state = await say(engine, state, "Đổi điểm đến sang Bạch Mai cổng sau")
        assert state["last_response"]["action"] == "confirm_location"
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_area_assistance_separates_preview_fee_and_order_consent(runtime):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state("area"), AREA)
        assert state["last_response"]["action"] == "ask_area_detail"
        state = await say(engine, state, "không biết địa chỉ")
        assert state["last_response"]["action"] == "choose_area_service"
        state = await say(engine, state, "hỗ trợ")
        assert state["last_response"]["action"] == "confirm_assistance"
        assert "chưa cộng phụ phí" in state["last_response"]["text"]
        assert not state["location_workflow"]["assistance"]
        assert provider.booking_count() == 0
        state = await say(engine, state, "đồng ý phí")
        assert state["location_workflow"]["assistance"]["consent"]
        assert state["resolution"]["quote"]["assistance_fee"] == 20000
        state = await confirm_locations(engine, state)
        summary = state["last_response"]["summary"]
        assert summary["provisional"]
        assert summary["fare"] == summary["base_fare"] + summary["assistance_fee"]
        assert provider.booking_count() == 0
        state = await say(engine, state, "đồng ý đặt xe")
        assert state["booking_status"] == "booked"
        assert provider.booking_count() == 1
        assert state["transaction"]["committed_snapshot"]["area_service"]["fee_amount"] == 20000

    asyncio.run(run())


@pytest.mark.parametrize("decline_at", ["choose", "fee"])
def test_declining_assistance_requires_fixed_point_confirmation(runtime, decline_at):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state(decline_at), AREA)
        state = await say(engine, state, "không biết địa chỉ")
        if decline_at == "fee":
            state = await say(engine, state, "hỗ trợ")
        state = await say(engine, state, "không hỗ trợ")
        assert state["last_response"]["action"] == "confirm_location"
        assert not state["booking_state"]["destination"]["confirmed"]
        state = await confirm_locations(engine, state)
        assert state["last_response"]["action"] == "confirm_booking"
        assert not state["location_workflow"]["assistance"]
        assert not state["last_response"]["summary"].get("provisional", False)
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_disabled_assistance_and_pickup_area_need_fixed_place(runtime):
    engine, provider = runtime
    engine.area_assistance_enabled = False

    async def run():
        state = await say(engine, engine.new_state("disabled"), AREA)
        state = await say(engine, state, "không biết địa chỉ")
        assert state["last_response"]["action"] == "confirm_location"
        assert "chưa có dịch vụ hỗ trợ" in state["last_response"]["text"]
        state = await say(
            engine,
            engine.new_state("pickup-area"),
            COMPLETE.replace("Nhà hát Lớn Hà Nội", "thôn Lai Xá"),
        )
        state = await say(engine, state, "đúng")
        assert state["last_response"]["action"] == "ask_clarification"
        assert "nơi bạn đang đứng" in state["last_response"]["text"]
        assert provider.booking_count() == 0

    asyncio.run(run())


@pytest.mark.parametrize("change", ["vehicle", "policy", "expiry"])
def test_assistance_consent_invalidated_by_changes(runtime, change):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state(change), AREA)
        for text in ("không biết địa chỉ", "hỗ trợ", "đồng ý phí"):
            state = await say(engine, state, text)
        state = await confirm_locations(engine, state)
        if change == "vehicle":
            state = await say(engine, state, "đổi sang xe 7 chỗ")
        else:
            if change == "policy":
                engine.assistance_policy.version = "sandbox-area-assistance-2"
            else:
                state["location_workflow"]["assistance"]["consent_expires_at"] = 0
            state = await say(engine, state, "đồng ý đặt xe")
        assert state["last_response"]["action"] == "confirm_assistance"
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_area_replaced_by_exact_address_clears_fee(runtime):
    engine, _ = runtime

    async def run():
        state = await say(engine, engine.new_state("exact"), AREA)
        state = await say(engine, state, "Đổi điểm đến sang Ga Hà Nội")
        assert not state["location_workflow"]["areas"]
        assert not state["location_workflow"]["assistance"]
        assert state["last_response"]["action"] == "confirm_location"

    asyncio.run(run())


def test_inquiry_confirmation_is_scoped_and_area_preview_cannot_promote(runtime):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state("inquiry"), COMPLETE)
        booking = deepcopy(state["booking_state"])
        state = await say(engine, state, "Từ Nhà hát Lớn Hà Nội đến Ocean Park 1 giá bao nhiêu?")
        assert state["dialogue"]["pending_prompt"]["scope_kind"] == "inquiry"
        state = await confirm_locations(engine, state)
        assert "điểm đại diện" in state["last_response"]["text"]
        assert not state["last_response"]["inquiry"]["can_use_route"]
        assert state["booking_state"] == booking
        state = await say(engine, state, "Dùng tuyến vừa hỏi để đặt")
        assert provider.booking_count() == 0
        assert state["booking_state"] == booking

    asyncio.run(run())


@pytest.mark.parametrize(
    "status", ["collecting_info", "booking_unknown", "booked", "cancel_unknown"]
)
def test_v2_migration_preserves_transaction_boundaries(status):
    state = new_conversation_state("legacy")
    state["booking_status"] = status
    state["confirmation"]["accepted_snapshot"] = {"old": True}
    migrated = upgrade_state(deepcopy(state), True)
    assert migrated["control"]["schema_version"] == (6 if status == "collecting_info" else 5)
    assert migrated["confirmation"]["accepted_snapshot"] == (
        None if status == "collecting_info" else {"old": True}
    )


def test_location_proposal_survives_checkpoint_restart_and_replay(runtime, tmp_path):
    engine, provider = runtime

    async def run():
        path = tmp_path / "graph.sqlite"
        async with DurableGraph(engine, path) as graph:
            await graph.initialize("durable", engine.new_state("durable"))
            state = await graph.process("durable", "details", COMPLETE)
            proposal = deepcopy(state["location_workflow"]["proposals"])
        async with DurableGraph(engine, path) as graph:
            loaded = await graph.get_state("durable")
            assert loaded["location_workflow"]["proposals"] == proposal
            response = loaded["last_response"]
            state = await graph.process(
                "durable",
                "yes-destination",
                "đúng",
                delivered_response_ids=[response["response_id"]],
                reply_to_response_id=response["response_id"],
            )
            replay = await graph.process("durable", "yes-destination", "đúng")
            assert replay == state
            assert replay["last_response"]["focus"] == "pickup"
            assert provider.booking_count() == 0

    asyncio.run(run())


def test_v3_http_transcript_uses_text_for_location_and_booking(tmp_path):
    settings = replace(
        Settings(
            profile="test",
            secret="test-secret",
            database_path=tmp_path / "http.sqlite",
            checkpoint_path=tmp_path / "cp.sqlite",
        ),
        location_confirmation_enabled=True,
    )
    from tests.test_api import create_session, send_text

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/bootstrap").json()["capabilities"]["text_only_chat"]
        state = create_session(client, "v3")
        session = state["session_id"]
        for text in (COMPLETE, "đúng", "đúng", "đồng ý đặt xe"):
            response = state["active_response"]
            state, _ = send_text(
                client,
                session,
                text,
                rendered=[response["response_id"]],
                reply=response["response_id"],
            )
        assert state["api_version"] == "chat-api-3"
        assert state["booking_status"] == "booked"
        assert state["active_response"]["presentation"]["contract_version"] == "chat-presentation-3"


@pytest.mark.parametrize(
    "text", ["đúng nếu xe đến ngay", "đúng điểm đón thôi", "đúng nhưng đổi địa chỉ"]
)
def test_provider_location_yes_cannot_ignore_conditions_or_wrong_target(runtime, text):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state("guard"), COMPLETE)
        response = state["last_response"]
        interpretation = {
            "kind": "turn",
            "occurred_at": engine.clock(),
            "result": {
                "contract_version": "parrotgo-turn-3",
                "speech_status": "clear",
                "booking_acts": [],
                "questions": [],
                "inquiry_actions": [],
                "conversational_acts": [],
                "travel_party": None,
                "location_decisions": [
                    {"decision": "confirm", "evidence_span": {"start": 0, "end": 4, "text": "đúng"}}
                ],
            },
        }
        state = await engine.prepare(
            state,
            {
                "event_id": "condition",
                "text": text,
                "delivered_response_ids": [response["response_id"]],
                "reply_to_response_id": response["response_id"],
            },
            interpretation,
        )
        assert state["last_response"]["reason"] == "LOCATION_CONSENT_SCOPE_UNCLEAR"
        assert not state["booking_state"]["destination"]["confirmed"]
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_v2_checkpointed_interpretation_resumes_without_v3_migration(runtime):
    engine, provider = runtime

    async def run():
        state = new_conversation_state("legacy-checkpoint")
        engine.location_confirmation = False
        data = engine.projection(state, COMPLETE, engine.clock())
        interpreted = extract_turn_fixture(data)
        engine.location_confirmation = True
        event = {
            "event_id": "old-input",
            "text": COMPLETE,
            "delivered_response_ids": [],
            "occurred_at": engine.clock(),
        }
        state = await engine.prepare(
            state,
            event,
            {
                "kind": "turn",
                "occurred_at": engine.clock(),
                "result": interpreted.model_dump(mode="json"),
            },
        )
        assert state["control"]["schema_version"] == 5
        assert state["last_response"]["action"] == "confirm_booking"
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_area_commit_lost_response_recovers_same_fee_and_booking(runtime):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state("lost-area"), AREA)
        for text in ("không", "hỗ trợ", "đồng ý phí"):
            state = await say(engine, state, text)
        state = await confirm_locations(engine, state)
        fee = deepcopy(state["location_workflow"]["assistance"])
        provider.faults["create_lost_response"] = 1
        state = await say(engine, state, "đồng ý đặt xe")
        assert state["booking_status"] == "booking_unknown"
        assert provider.booking_count() == 1
        state = await engine.reconcile(state, "recover-area")
        assert state["booking_status"] == "booked"
        assert state["transaction"]["committed_snapshot"]["area_service"] == fee
        assert provider.booking_count() == 1
        state = await say(engine, state, "đồng ý đặt xe")
        assert provider.booking_count() == 1

    asyncio.run(run())


def test_low_confidence_location_and_fee_decisions_do_not_grant_consent(runtime):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state("low"), COMPLETE)
        response = state["last_response"]
        result = {
            "contract_version": "parrotgo-turn-3",
            "speech_status": "low_confidence",
            "booking_acts": [],
            "questions": [],
            "inquiry_actions": [],
            "conversational_acts": [],
            "travel_party": None,
            "location_decisions": [
                {"decision": "confirm", "evidence_span": {"start": 0, "end": 4}}
            ],
        }
        state = await engine.prepare(
            state,
            {
                "event_id": "low-yes",
                "text": "đúng",
                "delivered_response_ids": [response["response_id"]],
            },
            {"kind": "turn", "occurred_at": engine.clock(), "result": result},
        )
        assert not state["booking_state"]["destination"]["confirmed"]
        assert state["last_response"]["action"] == "ask_clarification"
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_cancel_mixed_with_location_yes_is_processed(runtime):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state("cancel-mixed"), COMPLETE)
        response = state["last_response"]
        result = {
            "contract_version": "parrotgo-turn-3",
            "speech_status": "clear",
            "booking_acts": [
                {
                    "intent": "cancel",
                    "target": None,
                    "value": None,
                    "evidence_span": {"start": 6, "end": 15},
                }
            ],
            "questions": [],
            "inquiry_actions": [],
            "conversational_acts": [],
            "travel_party": None,
            "location_decisions": [
                {"decision": "confirm", "evidence_span": {"start": 0, "end": 4}}
            ],
        }
        event = {
            "event_id": "mixed-cancel",
            "text": "đúng, hủy chuyến",
            "delivered_response_ids": [response["response_id"]],
        }
        state = await engine.prepare(
            state, event, {"kind": "turn", "occurred_at": engine.clock(), "result": result}
        )
        state = await engine.finalize(state, event)
        assert not state["booking_state"]["destination"]["confirmed"]
        assert state["booking_status"] == "cancelled"
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_inquiry_rejection_requests_replacement_without_retry_failure(runtime):
    engine, provider = runtime

    async def run():
        state = await say(
            engine,
            engine.new_state("reject-inquiry"),
            "Từ Nhà hát Lớn Hà Nội đến Ga Hà Nội giá bao nhiêu?",
        )
        state = await say(engine, state, "không")
        assert state["last_response"]["text"] == "Bạn muốn tính từ đâu?"
        assert state["dialogue"]["pending_prompt"]["field"] == "origin"
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_disabling_assistance_offers_actionable_fixed_point_and_clears_fee(runtime):
    engine, provider = runtime

    async def run():
        state = await say(engine, engine.new_state("flag-off"), AREA)
        for text in ("không biết địa chỉ", "hỗ trợ", "đồng ý phí"):
            state = await say(engine, state, text)
        state = await confirm_locations(engine, state)
        engine.area_assistance_enabled = False
        state = await say(engine, state, "tiếp tục chuyến đang đặt")
        assert state["last_response"]["action"] == "confirm_location"
        assert not state["location_workflow"]["assistance"]
        state = await confirm_locations(engine, state)
        assert state["last_response"]["action"] == "confirm_booking"
        assert not state["resolution"]["quote"].get("assistance_fee")
        assert provider.booking_count() == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "code", ["PROVIDER_AUTH_ERROR", "PROVIDER_MODEL_UNAVAILABLE", "PROVIDER_CONFIG_ERROR"]
)
def test_provider_configuration_failure_reaches_worker_not_rewrite_prompt(runtime, code):
    engine, _ = runtime

    async def broken(data):
        raise ExtractorError(code, "provider configuration is invalid")

    async def run():
        engine.extractor = broken
        with pytest.raises(ExtractorError, match="provider configuration"):
            await engine.interpret(
                engine.new_state("config"),
                {
                    "event_id": "config-input",
                    "text": "Đặt xe giúp tôi",
                    "occurred_at": engine.clock(),
                },
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        ("đồng ý phí", True),
        ("đồng ý phí 20.000 đ", True),
        ("đồng ý phí 20 nghìn", True),
        ("đồng ý phí 20k", True),
        ("đồng ý phí 20,000 đồng...", True),
        ("đồng ý phí tối đa 10.000 thôi", False),
        ("đồng ý phí 10 nghìn", False),
        ("đồng ý phí 10000", False),
        ("đồng ý phí dưới 20.000 đ", False),
        ("đồng ý phí mười nghìn", False),
        ("đồng ý phí 20.00.0 đ", False),
    ],
)
def test_fee_reply_cannot_accept_a_different_price(text, accepted):
    assert fee_reply_matches(text, 20000) is accepted


def test_interrupted_inquiry_proposal_is_not_attached_to_unnamed_reply(runtime):
    engine, provider = runtime

    async def run():
        state = await say(
            engine,
            engine.new_state("interrupted"),
            "Từ Nhà hát Lớn Hà Nội đến Ga Hà Nội giá bao nhiêu?",
        )
        state = await say(engine, state, "cảm ơn")
        assert not state["dialogue"]["pending_questions"]
        state = await say(engine, state, "đúng")
        assert state["last_response"]["action"] != "confirm_location"
        assert not state["dialogue"]["pending_prompt"]
        assert provider.booking_count() == 0

    asyncio.run(run())
