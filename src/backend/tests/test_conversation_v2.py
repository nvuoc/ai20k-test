"""V2 acceptance checks assert booking facts and provider effects, not just text."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.adapters.booking_sandbox import SandboxBookingProvider
from app.adapters.extractor import CallBudget, ExtractorRuntime, llm_extractor_func
from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.nlu_fixture import FixtureExtractorClient
from app.adapters.quote_fixture import QuoteAdapter
from app.adapters.weather_fixture import FixtureWeatherAdapter
from app.config import Settings
from app.contracts.weather import WeatherFact, WeatherSample
from app.domain.conversation import LegacyConversationEngine as ConversationEngine
from app.graph.builder import DurableGraph
from app.text import TextBot, main

COMPLETE = "Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, đi ngay, 2 người, xe 4 chỗ, số 0901234567."
INQUIRY = "Từ Nhà hát Lớn Hà Nội đến Bạch Mai cổng sau bao nhiêu tiền, bao nhiêu km và mất bao lâu?"


@pytest.fixture
def runtime(tmp_path):
    client = FixtureExtractorClient()
    calls = {"nlu": 0, "route": 0}

    async def extractor(data):
        calls["nlu"] += 1
        return await llm_extractor_func(
            data,
            runtime=ExtractorRuntime(
                client=client,
                call_budget=CallBudget(1),
                vehicle_codes=frozenset({"oto_4_cho", "oto_7_cho", "xe_may_dien", "xe_may"}),
            ),
        )

    class Maps(FixtureMapAdapter):
        async def route(self, *args, **kwargs):
            calls["route"] += 1
            return await super().route(*args, **kwargs)

    provider = SandboxBookingProvider(tmp_path / "provider.sqlite")
    engine = ConversationEngine(
        extractor, Maps(), provider, QuoteAdapter(), weather=FixtureWeatherAdapter()
    )
    yield engine, provider, calls
    provider.close()


async def say(engine, state, text="", action=None, delivered=True):
    response = state["last_response"]
    return await engine.process(
        state,
        text,
        action=action,
        event_id=f"event-{state['control']['generation'] + 1}",
        delivered_response_ids=[response["response_id"]] if delivered else [],
        reply_to_response_id=response["response_id"],
    )


@pytest.mark.parametrize("code", ["PROVIDER_AUTH_ERROR", "PROVIDER_MODEL_UNAVAILABLE", "PROVIDER_CONFIG_ERROR"])
def test_provider_configuration_error_does_not_lock_chat(runtime, tmp_path, code):
    from app.adapters.extractor import ExtractorError
    from app.api_store import ApiStore
    from app.workers.coordinator import Coordinator

    engine, provider, _ = runtime
    original = engine.extractor

    async def unavailable(data):
        raise ExtractorError(code, "Provider configuration failed")

    async def run():
        store = ApiStore(tmp_path / "api.sqlite")
        async with DurableGraph(engine, tmp_path / "chat.sqlite") as graph:
            state = engine.new_state("session")
            await graph.initialize("session", state)
            store.create_session("session", "owner", "client", state)
            coordinator = Coordinator(store, graph)
            engine.extractor = unavailable
            store.enqueue("session", "message", "first", {"text": COMPLETE})
            await coordinator.run_session("session")
            snapshot = store.snapshot("session")
            assert not snapshot["needs_support"]
            assert snapshot["pending_count"] == 0
            assert "lỗi cấu hình" in snapshot["state"]["last_response"]["text"]
            assert provider.booking_count() == 0
            engine.extractor = original
            store.enqueue("session", "message", "second", {"text": COMPLETE})
            await coordinator.run_session("session")
            snapshot = store.snapshot("session")
            assert not snapshot["needs_support"]
            assert snapshot["pending_count"] == 0
            assert snapshot["state"]["last_response"]["reason"] != code

    asyncio.run(run())


def test_inquiry_preserves_booking_and_groups_route_reads(runtime):
    engine, provider, calls = runtime

    async def run():
        state = await say(engine, engine.new_state("session"), COMPLETE)
        booking = deepcopy(state["booking_state"])
        revision = state["control"]["booking_revision"]
        quote = deepcopy(state["resolution"]["quote"])
        routes_before = calls["route"]
        state = await say(engine, state, INQUIRY)
        assert state["booking_state"] == booking
        assert state["control"]["booking_revision"] == revision
        assert state["resolution"]["quote"] == quote
        assert calls["route"] == routes_before + 1
        assert "Giá thử nghiệm" in state["last_response"]["text"]
        assert " km" in state["last_response"]["text"]
        assert "phút" in state["last_response"]["text"]
        assert "số điện thoại" not in state["last_response"]["text"]
        assert state["last_response"]["inquiry"]["can_use_route"]
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_confirm_with_vehicle_change_requires_new_summary(runtime):
    engine, provider, _ = runtime

    async def run():
        state = await say(engine, engine.new_state("change-vehicle"), COMPLETE)
        state = await say(engine, state, "Đồng ý nhưng đổi sang xe 7 chỗ nhé")
        assert state["booking_state"]["vehicle_type"]["value"] == "oto_7_cho"
        assert state["last_response"]["summary"]["vehicle_label"] == "Ô tô 7 chỗ", state[
            "last_response"
        ]
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_expiry_between_prepare_and_dispatch_requires_new_consent(runtime):
    engine, provider, _ = runtime

    async def run():
        state = await say(engine, engine.new_state("expiry"), COMPLETE)
        event = {
            "text": "Đồng ý đặt xe",
            "event_id": "expiry-confirm",
            "delivered_response_ids": [state["last_response"]["response_id"]],
            "reply_to_response_id": state["last_response"]["response_id"],
        }
        interpreted = await engine.interpret(state, event)
        prepared = await engine.prepare(state, event, interpreted)
        assert prepared["turn"]["ready_to_dispatch"]
        prepared["resolution"]["quote"]["expires_at"] = engine.clock() - 1
        state = await engine.finalize(prepared, event)
        assert state["last_response"]["reason"] == "QUOTE_EXPIRED_BEFORE_DISPATCH"
        assert state["last_response"]["action"] == "confirm_booking"
        assert state["confirmation"]["accepted_snapshot"] is None
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_wrong_scope_map_result_cannot_supply_inquiry_points(runtime):
    engine, provider, _ = runtime
    resolve = engine.maps.resolve

    async def mismatched(*args, **kwargs):
        result = await resolve(*args, **kwargs)
        result["binding"]["scope_id"] = "another-session"
        return result

    engine.maps.resolve = mismatched

    async def run():
        state = await say(engine, engine.new_state("binding"), INQUIRY)
        assert not state["last_response"]["inquiry"]["can_use_route"]
        assert not state["read_facts"]
        assert "không đúng yêu cầu" in state["last_response"]["text"]
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_promote_requires_new_summary_and_separate_consent(runtime):
    engine, provider, _ = runtime

    async def run():
        state = await say(engine, engine.new_state("session"), COMPLETE)
        state = await say(engine, state, INQUIRY)
        state = await say(engine, state, "Dùng tuyến vừa hỏi để đặt")
        assert "cổng sau" in state["booking_state"]["destination"]["value"]
        assert state["booking_state"]["contact_phone"]["value"] == "0901234567"
        assert state["last_response"]["action"] == "confirm_booking"
        assert provider.booking_count() == 0
        state = await say(engine, state, "Đồng ý đặt")
        assert state["booking_status"] == "booked"
        assert provider.booking_count() == 1

    asyncio.run(run())


def test_typed_promote_is_scoped_and_does_not_call_model(runtime):
    engine, provider, calls = runtime

    async def run():
        state = await say(engine, engine.new_state("session"), INQUIRY)
        item = state["last_response"]["inquiry"]
        count = calls["nlu"]
        action = {
            "type": "use_inquiry_route",
            "inquiry_id": item["inquiry_id"],
            "inquiry_revision": item["revision"],
            "booking_revision": item["booking_revision"],
            "route_fingerprint": item["route_fingerprint"],
        }
        rejected = await say(engine, state, action={**action, "inquiry_revision": 999})
        assert rejected["booking_state"] == state["booking_state"]
        state = await say(engine, state, action=action)
        assert calls["nlu"] == count
        assert state["booking_state"]["destination"]["value"]
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_inquiry_candidate_and_followup_only_change_inquiry(runtime):
    engine, _, _ = runtime

    async def run():
        state = await say(engine, engine.new_state("session"), COMPLETE)
        before = deepcopy(state["booking_state"])
        state = await say(
            engine, state, "Từ Nhà hát Lớn Hà Nội đến Bệnh viện Bạch Mai giá bao nhiêu?"
        )
        assert state["last_response"]["candidates"][0]["scope_kind"] == "inquiry"
        state = await say(engine, state, "cái thứ hai")
        assert state["booking_state"] == before
        assert "cổng sau" in state["last_response"]["text"]
        assert state["last_response"]["inquiry"]["can_use_route"]

    asyncio.run(run())


@pytest.mark.parametrize("text", ["Đồng ý nhưng giá có giảm không?", "Chỉ đặt nếu không mưa", "ừ"])
def test_questions_and_ambiguous_consent_never_create(runtime, text):
    engine, provider, _ = runtime

    async def run():
        state = await say(engine, engine.new_state("session"), COMPLETE)
        state = await say(engine, state, INQUIRY)
        await say(engine, state, text)
        assert provider.booking_count() == 0

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["booked", "cancelled", "booking_unknown", "cancel_unknown"])
def test_identity_after_terminal_or_unknown_status(runtime, kind):
    engine, _, calls = runtime

    async def run():
        state = engine.new_state("session")
        state["booking_status"] = kind
        before = deepcopy(state["booking_state"])
        state = await say(engine, state, "Bạn là ai?")
        assert "ParrotGo" in state["last_response"]["text"]
        assert state["booking_status"] == kind
        assert state["booking_state"] == before
        assert calls["nlu"] == 0

    asyncio.run(run())


def test_weather_single_location_and_time_does_not_require_booking(runtime):
    engine, provider, _ = runtime

    async def run():
        state = await say(
            engine,
            engine.new_state("session"),
            "Thời tiết ở Nhà hát Lớn Hà Nội bây giờ có mưa không?",
        )
        assert "dữ liệu thời tiết mẫu" in state["last_response"]["text"]
        assert "28°C" in state["last_response"]["text"]
        assert all(slot["value"] is None for slot in state["booking_state"].values())
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_relation_looks_up_anchor_without_committing_location(runtime):
    engine, _, _ = runtime

    async def run():
        result = await engine.maps.resolve("Đối diện Nhà hát Lớn Hà Nội")
        assert result["status"] == "ambiguous"
        assert result["place"] is None
        assert result["anchors"][0]["id"] == "hn_opera"
        assert "địa chỉ hoặc tên cổng" in result["clarification"]

    asyncio.run(run())


def test_graph_checkpoints_interpretation_before_reads(runtime, tmp_path):
    engine, _, calls = runtime

    async def run():
        original = engine.prepare
        attempts = 0

        async def interrupted(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("simulated read interruption")
            return await original(*args)

        engine.prepare = interrupted
        async with DurableGraph(engine, tmp_path / "graph.sqlite") as graph:
            await graph.initialize("session", engine.new_state("session"))
            with pytest.raises(RuntimeError):
                await graph.process("session", "event", INQUIRY)
            assert calls["nlu"] == 1
            state = await graph.process("session", "event", INQUIRY)
            assert calls["nlu"] == 1
            assert state["last_response"]["inquiry"]
            assert (await graph.process("session", "event", INQUIRY)) == state

    asyncio.run(run())


def test_main_text_function_persists_and_deduplicates(tmp_path):
    settings = Settings(
        profile="test",
        secret="text-test",
        database_path=tmp_path / "app.sqlite",
        checkpoint_path=tmp_path / "graph.sqlite",
    )
    assert "ParrotGo" in main("Bạn là ai?", settings=settings, session_id="caller", customer_phone="0901234567", customer_name="An")
    summary = main(COMPLETE, settings=settings, session_id="caller", message_id="collect")
    assert "Có phải đón bạn" in summary
    assert main(COMPLETE, settings=settings, session_id="caller", message_id="collect") == summary
    for _ in range(3):
        summary = main("đúng", settings=settings, session_id="caller")
    assert "đồng/km" in summary
    result = main("Đồng ý đặt", settings=settings, session_id="caller", message_id="confirm")
    assert "SBX-" in result
    assert (
        main("Đồng ý đặt", settings=settings, session_id="caller", message_id="confirm") == result
    )
    assert "ParrotGo" in main("Bạn là ai?", settings=settings, session_id="caller")
    other = replace(
        settings,
        database_path=tmp_path / "other.sqlite",
        checkpoint_path=tmp_path / "other-graph.sqlite",
    )
    assert "ParrotGo" in main("Bạn là ai?", settings=other, customer_phone="0901234567", customer_name="An")


def test_text_bot_multiple_sessions_keep_separate_state(tmp_path):
    settings = Settings(
        profile="test",
        secret="text-test",
        database_path=tmp_path / "app.sqlite",
        checkpoint_path=tmp_path / "graph.sqlite",
    )

    async def run():
        async with TextBot(settings) as bot:
            a = await bot.ask(COMPLETE, session_id="a", customer_phone="0901234567", customer_name="An")
            b = await bot.ask("Bạn là ai?", session_id="b", customer_phone="0911234567", customer_name="Bình")
            assert "Có phải đón bạn" in a
            assert "ParrotGo" in b and "0901234567" not in b

    asyncio.run(run())


def test_mixed_booking_update_and_hypothetical_route(runtime):
    engine, provider, _ = runtime

    async def run():
        state = await say(engine, engine.new_state("session"), COMPLETE)
        revision = state["control"]["booking_revision"]
        state = await say(
            engine,
            state,
            "Đổi điểm đón sang 15 ngõ 20 Nguyễn Trãi; nếu từ Nhà hát Lớn Hà Nội đến Bạch Mai cổng sau thì đi bao lâu?",
        )
        assert state["booking_state"]["pickup"]["value"] == "15 ngõ 20 Nguyễn Trãi"
        assert state["booking_state"]["destination"]["value"] == "Ga Hà Nội"
        assert state["control"]["booking_revision"] == revision + 1
        assert "phút" in state["last_response"]["text"]
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_inquiry_future_time_does_not_add_scheduled_issue(runtime):
    engine, _, _ = runtime

    async def run():
        state = await say(engine, engine.new_state("session"), COMPLETE)
        before = deepcopy(state["booking_state"])
        state = await say(
            engine,
            state,
            "Nếu từ Nhà hát Lớn Hà Nội đến Ga Hà Nội bằng xe 7 chỗ ngày mai 08:00 thì giá bao nhiêu?",
        )
        assert state["booking_state"] == before
        assert not state["issues"]
        assert "scheduled" not in str(state["issues"])
        assert "Ô tô 7 chỗ" in state["last_response"]["text"]

    asyncio.run(run())


def test_reverse_route_uses_directional_source(runtime):
    engine, _, _ = runtime

    async def run():
        state = await say(
            engine, engine.new_state("session"), "Từ Nhà hát Lớn Hà Nội đến Ga Hà Nội bao nhiêu km?"
        )
        assert "3.1 km" in state["last_response"]["text"]
        state = await say(engine, state, "Đảo chiều tuyến vừa hỏi")
        assert "3.4 km" in state["last_response"]["text"]
        assert state["booking_state"]["pickup"]["value"] is None

    asyncio.run(run())


def test_question_after_committed_booking_uses_committed_snapshot(runtime):
    engine, provider, _ = runtime

    async def run():
        state = await say(engine, engine.new_state("session"), COMPLETE)
        state = await say(engine, state, "Đồng ý đặt")
        committed = deepcopy(state["transaction"]["committed_snapshot"])
        state = await say(engine, state, "Chuyến này đi mất bao lâu?")
        assert "phút" in state["last_response"]["text"]
        assert state["transaction"]["committed_snapshot"] == committed
        assert state["booking_status"] == "booked" and provider.booking_count() == 1

    asyncio.run(run())


def test_expired_inquiry_promotion_cannot_change_booking(runtime):
    engine, _, _ = runtime

    async def run():
        state = await say(engine, engine.new_state("session"), INQUIRY)
        item = engine.inquiry_service.active(state)
        item["expires_at"] = engine.clock() - 1
        state = await say(engine, state, "Dùng tuyến vừa hỏi để đặt")
        assert state["last_response"]["reason"] == "INQUIRY_EXPIRED"
        assert state["booking_state"]["pickup"]["value"] is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "question,expected_calls",
    [
        ("lúc lên xe và tới nơi bây giờ có mưa không?", 2),
        ("khi tới nơi nếu xuất phát 23:55 có mưa không?", 1),
    ],
)
def test_weather_arrival_crossing_midnight(runtime, question, expected_calls):
    engine, _, _ = runtime
    fixed = datetime(2026, 10, 2, 16, 55, tzinfo=UTC).timestamp()
    engine.clock = lambda: fixed
    calls = []

    class Weather:
        async def forecast(self, req):
            calls.append(req)
            return WeatherFact(
                status="available",
                request_id=req.request_id,
                scope_kind=req.scope_kind,
                scope_id=req.scope_id,
                dependency_fingerprint=req.dependency_fingerprint,
                provider="replay",
                source_ref="replay:weather:1",
                attribution="weather replay",
                fetched_at=datetime.fromtimestamp(fixed, UTC),
                valid_until=datetime.fromtimestamp(fixed + 900, UTC),
                timezone=req.timezone,
                coverage_start=datetime.fromtimestamp(fixed - 3600, UTC),
                coverage_end=datetime.fromtimestamp(fixed + 3600, UTC),
                sample=WeatherSample(
                    timestamp=req.target_time, temperature_c=25, precipitation_probability=50
                ),
            )

    engine.inquiry_service.weather = Weather()

    async def run():
        state = await say(
            engine,
            engine.new_state("session"),
            "Từ Nhà hát Lớn Hà Nội đến Ga Hà Nội, " + question,
        )
        assert len(calls) == expected_calls
        assert calls[-1].location_ref == "hn_station"
        if expected_calls == 2:
            assert calls[0].location_ref == "hn_opera"
        assert (
            calls[-1].target_time.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).date().isoformat()
            == "2026-10-03"
        )
        assert "03/10" in state["last_response"]["text"]
        assert all(slot["value"] is None for slot in state["booking_state"].values())

    asyncio.run(run())
