"""Location questions use map facts without changing the trip or creating a route."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.booking_sandbox import SandboxBookingProvider
from app.adapters.extractor import CallBudget, ExtractorRuntime, llm_extractor_func
from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.nlu_fixture import FixtureExtractorClient
from app.adapters.quote_fixture import QuoteAdapter
from app.adapters.turn_fixture import extract_turn_fixture
from app.config import Settings
from app.contracts.nlu import empty_booking_state
from app.contracts.turn import TurnInput
from app.domain.conversation import LegacyConversationEngine as ConversationEngine
from app.text import main

COMPLETE = "Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, đi ngay, 2 người, xe 4 chỗ, số 0901234567."


@pytest.mark.parametrize(
    "text,query",
    [
        ("Nhà hát Lớn Hà Nội ở đâu?", "Nhà hát Lớn Hà Nội"),
        ("Chợ Bến Thành nằm ở đâu?", "Chợ Bến Thành"),
        ("Cho tôi hỏi Ocean Park 1 ở chỗ nào?", "Ocean Park 1"),
        ("Địa chỉ của Ga Hà Nội là gì?", "Ga Hà Nội"),
        ("Tôi muốn biết địa chỉ Ga Hà Nội.", "Ga Hà Nội"),
        ("Bạn có biết Bệnh viện Chợ Rẫy ở đâu không?", "Bệnh viện Chợ Rẫy"),
        ("Ga Hà Nội thuộc thành phố nào?", "Ga Hà Nội"),
        ("Ga Ha Noi o dau", "Ga Ha Noi"),
        ("VietMap, 3 Trần Nhân Tôn, Hồ Chí Minh ở đâu?", "VietMap, 3 Trần Nhân Tôn, Hồ Chí Minh"),
    ],
)
def test_location_question_preserves_the_named_place_and_literal_evidence(text, query):
    data = TurnInput(
        utterance={"text": text, "asr_confidence": None},
        conversation_context={
            "last_bot_message": None, "last_bot_action": None, "current_focus": None,
        },
        booking_state=empty_booking_state(),
        booking_status="collecting_info",
        candidates=[],
        occurred_at=datetime.now(UTC).isoformat(),
    )
    result = extract_turn_fixture(data)
    result.validate_evidence(data)
    assert not result.booking_acts
    assert len(result.questions) == 1
    question = result.questions[0]
    assert question.type == "place_location"
    assert question.origin == query
    assert question.destination is None
    assert question.weather_target is None
    assert question.route_scope == "explicit_pair"
    assert question.relation_to_booking == "read_only"
    assert question.raw_text == text


@pytest.fixture(params=[False, True], ids=["v2", "v3"])
def runtime(tmp_path, request):
    client = FixtureExtractorClient()
    calls = {"queries": [], "route": 0}

    async def extractor(data):
        return await llm_extractor_func(
            data, runtime=ExtractorRuntime(client=client, call_budget=CallBudget(1)),
        )

    class Maps(FixtureMapAdapter):
        async def resolve(self, query, *args, **kwargs):
            calls["queries"].append(query)
            return await super().resolve(query, *args, **kwargs)

        async def route(self, *args, **kwargs):
            calls["route"] += 1
            return await super().route(*args, **kwargs)

    provider = SandboxBookingProvider(tmp_path / "provider.sqlite")
    engine = ConversationEngine(
        extractor, Maps(), provider, QuoteAdapter(), location_confirmation=request.param,
    )
    yield engine, provider, calls
    provider.close()


async def say(engine, state, text, *, delivered=True):
    response = state["last_response"]
    return await engine.process(
        state, text,
        event_id=f"event-{state['control']['generation'] + 1}",
        delivered_response_ids=[response["response_id"]] if delivered else [],
        reply_to_response_id=response["response_id"],
    )


def test_unique_place_is_answered_immediately_without_booking_details(runtime):
    engine, provider, calls = runtime

    async def run():
        state = await say(engine, engine.new_state("single"), "Nhà hát Lớn Hà Nội ở đâu?")
        response = state["last_response"]
        assert "1 Tràng Tiền, Hà Nội" in response["text"]
        assert "dữ liệu địa điểm thử nghiệm" in response["text"]
        assert "fixture:locations-sandbox-1" in response["text"]
        assert response["action"] == "answer_question"
        assert not response["inquiry"]["can_use_route"]
        assert all(slot["value"] is None for slot in state["booking_state"].values())
        assert calls == {"queries": ["Nhà hát Lớn Hà Nội"], "route": 0}
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_place_lookup_and_selection_preserve_a_pending_booking(runtime):
    engine, provider, calls = runtime

    async def run():
        state = await say(engine, engine.new_state("booking"), COMPLETE)
        booking = deepcopy(state["booking_state"])
        resolution = deepcopy(state["resolution"])
        revision = state["control"]["booking_revision"]
        routes = calls["route"]
        state = await say(engine, state, "Trường Sao Mai ở đâu?")
        assert state["last_response"]["action"] == "offer_candidates"
        assert len(state["last_response"]["candidates"]) == 2
        assert all(c["scope_kind"] == "inquiry" for c in state["last_response"]["candidates"])
        state = await say(engine, state, "cái thứ hai")
        assert "cơ sở Hà Đông" in state["last_response"]["text"]
        assert state["last_response"]["action"] == "answer_question"
        assert state["booking_state"] == booking
        assert state["resolution"] == resolution
        assert state["control"]["booking_revision"] == revision
        assert calls["route"] == routes
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_area_location_is_answered_without_a_representative_point(runtime):
    engine, provider, calls = runtime

    async def run():
        state = await say(engine, engine.new_state("area"), "Ocean Park 1 nằm ở đâu?")
        response = state["last_response"]
        assert "Ocean Park 1, Gia Lâm, Hà Nội" in response["text"]
        assert "khu vực rộng" in response["text"]
        assert "điểm đại diện" not in response["text"]
        assert response["action"] == "answer_question"
        assert not response["inquiry"]["can_use_route"]
        assert not state.get("location_workflow", {}).get("assistance")
        assert calls["route"] == 0
        assert provider.booking_count() == 0

    asyncio.run(run())


@pytest.mark.parametrize("text", ["Ở đâu?", "Địa danh này ở đâu?", "Đảo Chưa Có Trong Dữ Liệu ở đâu?"])
def test_missing_or_unknown_place_can_be_clarified_without_filling_booking_slots(runtime, text):
    engine, provider, calls = runtime

    async def run():
        state = await say(engine, engine.new_state("clarify"), text)
        assert state["last_response"]["action"] == "answer_question"
        assert state["dialogue"]["pending_prompt"]["purpose"] == "place_location"
        assert "địa" in state["last_response"]["text"]
        state = await say(engine, state, "Ga Hà Nội")
        assert "120 Lê Duẩn, Hà Nội" in state["last_response"]["text"]
        assert all(slot["value"] is None for slot in state["booking_state"].values())
        assert calls["route"] == 0
        assert provider.booking_count() == 0

    asyncio.run(run())


@pytest.mark.parametrize("field,text,expected", [
    ("pickup", "Điểm đón chuyến này ở đâu?", "1 Tràng Tiền"),
    ("destination", "Điểm đến chuyến này ở đâu?", "120 Lê Duẩn"),
])
def test_current_booking_endpoint_is_read_without_changing_the_trip(runtime, field, text, expected):
    engine, provider, calls = runtime

    async def run():
        state = await say(engine, engine.new_state(field), COMPLETE)
        before = deepcopy(state["booking_state"])
        routes = calls["route"]
        state = await say(engine, state, text)
        assert expected in state["last_response"]["text"]
        assert state["last_response"]["action"] == "answer_question"
        assert state["booking_state"] == before
        assert calls["route"] == routes
        assert provider.booking_count() == 0

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["scope", "expired", "future", "unavailable"])
def test_unusable_map_facts_never_supply_a_location(runtime, monkeypatch, failure):
    engine, provider, calls = runtime
    resolve = engine.maps.resolve

    async def faulty(*args, **kwargs):
        if failure == "unavailable":
            raise RuntimeError("provider unavailable")
        result = await resolve(*args, **kwargs)
        if failure == "scope":
            result["binding"]["scope_id"] = "another-session"
        elif failure == "expired":
            result["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        else:
            result["resolved_at"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        return result

    monkeypatch.setattr(engine.maps, "resolve", faulty)

    async def run():
        state = await say(engine, engine.new_state(failure), "Nhà hát Lớn Hà Nội ở đâu?")
        assert "1 Tràng Tiền" not in state["last_response"]["text"]
        assert not state["read_facts"]
        assert not state["last_response"]["candidates"]
        assert calls["route"] == 0
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_location_followup_survives_text_bot_restart(tmp_path):
    settings = Settings(
        profile="test", secret="place-test", database_path=tmp_path / "app.sqlite",
        checkpoint_path=tmp_path / "graph.sqlite",
    )
    reply = main("Ga Hà Nội ở đâu?", settings=settings, session_id="visitor", customer_phone="0901234567", customer_name="An")
    assert "120 Lê Duẩn" in reply
    assert "120 Lê Duẩn" in main("Địa danh đó ở đâu?", settings=settings, session_id="visitor")
