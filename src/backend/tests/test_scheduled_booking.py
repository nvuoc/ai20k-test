"""Scheduled bookings retain the customer's time through consent and recovery."""

import asyncio
from datetime import datetime, timedelta

import pytest

from app.adapters.booking_sandbox import SandboxBookingProvider
from app.adapters.extractor import CallBudget, ExtractorRuntime, llm_extractor_func
from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.nlu_fixture import FixtureExtractorClient
from app.adapters.quote_fixture import QuoteAdapter
from app.config import Settings
from app.domain.conversation import LegacyConversationEngine as ConversationEngine
from app.domain.pickup_time import ZONE, parse_pickup_time, time_expression
from app.graph.builder import DurableGraph
from app.text import main

NOW = datetime(2026, 10, 3, 7, 0, tzinfo=ZONE).timestamp()


@pytest.mark.parametrize("raw,expected", [
    ("ngày mai 8 giờ sáng", "2026-10-04T08:00:00+07:00"),
    ("bảy giờ sáng ngày mai", "2026-10-04T07:00:00+07:00"),
    ("18:30", "2026-10-03T18:30:00+07:00"),
    ("18:30 ngày 05/10/2026", "2026-10-05T18:30:00+07:00"),
    ("8h30 ngày mai", "2026-10-04T08:30:00+07:00"),
    ("ngày kia 08:15", "2026-10-05T08:15:00+07:00"),
    ("tám giờ rưỡi sáng hôm nay", "2026-10-03T08:30:00+07:00"),
    ("sau 2 tiếng", "2026-10-03T09:00:00+07:00"),
    ("30 phút nữa", "2026-10-03T07:30:00+07:00"),
    ("ba mươi phút nữa", "2026-10-03T07:30:00+07:00"),
    ("2 giờ 30 phút nữa", "2026-10-03T09:30:00+07:00"),
    ("nửa tiếng nữa", "2026-10-03T07:30:00+07:00"),
    ("2026-10-04T01:00:00Z", "2026-10-04T08:00:00+07:00"),
    ("ngày 5 tháng 10 năm 2026 18h30", "2026-10-05T18:30:00+07:00"),
    ("thứ hai tuần sau 8 giờ sáng", "2026-10-05T08:00:00+07:00"),
])
def test_requested_time_resolves_in_vietnam_timezone(raw, expected):
    result = parse_pickup_time(raw, NOW)
    assert result["status"] == "valid", result
    assert result["pickup_at"] == expected
    assert result["mode"] == "scheduled"
    assert result["timezone"] == "Asia/Ho_Chi_Minh"


@pytest.mark.parametrize("raw,reason", [
    ("8 giờ", "MISSING_PERIOD"), ("bảy giờ", "MISSING_PERIOD"), ("ngày mai", "MISSING_TIME"),
    ("chiều mai", "MISSING_TIME"), ("hôm qua 18:00", "PAST_TIME"),
    ("ngày 31/02/2027 08:00", "INVALID_DATE"), ("24:00", "INVALID_TIME"),
    ("18:90", "INVALID_TIME"), ("0 phút nữa", "INVALID_DURATION"),
    ("3 hoặc 4 giờ chiều", "AMBIGUOUS_TIME"),
    ("tuần sau 18:30", "MISSING_DATE"),
    ("ngày 5 lúc 18:30", "MISSING_DATE"),
    ("ngày mai 30 phút nữa", "AMBIGUOUS_TIME"),
])
def test_ambiguous_invalid_or_past_time_needs_a_customer_answer(raw, reason):
    result = parse_pickup_time(raw, NOW)
    assert result["status"] == "needs_clarification"
    assert result["reason"] == reason
    assert result["pickup_at"] is None


@pytest.mark.parametrize("text", [
    "Tôi đứng cổng chính áo xanh", "Cái thứ hai", "đến Bạch Mai",
    "Nhà 3 đường 3/2", "123/45/6 Nguyễn Đình Chiểu",
    "ngay bây giờ",
])
def test_fixture_time_extraction_does_not_confuse_places_or_candidate_ordinals(text):
    assert time_expression(text) is None


def test_fixture_retains_spoken_seven_hours_instead_of_treating_it_as_now():
    assert time_expression("Đón lúc bảy giờ sáng ngày mai") == "bảy giờ sáng ngày mai"


def test_adjustment_uses_the_existing_pickup_time():
    result = parse_pickup_time("muộn hơn 30 phút", NOW, previous_pickup_at="2026-10-04T08:00:00+07:00")
    assert result["pickup_at"] == "2026-10-04T08:30:00+07:00"
    result = parse_pickup_time("sớm hơn nửa tiếng", NOW, previous_pickup_at=result["pickup_at"])
    assert result["pickup_at"] == "2026-10-04T08:00:00+07:00"


@pytest.fixture(params=[False, True], ids=["v2", "v3"])
def runtime(tmp_path, request):
    clock = [NOW]
    departures = []
    client = FixtureExtractorClient()

    async def extractor(data):
        return await llm_extractor_func(
            data, runtime=ExtractorRuntime(client=client, call_budget=CallBudget(1)),
        )

    class Maps(FixtureMapAdapter):
        async def route(self, *args, **kwargs):
            departures.append(kwargs.get("departure_time"))
            route = await super().route(*args, **kwargs)
            route["fetched_at"] = datetime.fromtimestamp(clock[0], ZONE).isoformat()
            route["valid_until"] = datetime.fromtimestamp(clock[0] + 3600, ZONE).isoformat()
            return route

    class Quotes(QuoteAdapter):
        async def quote(self, *args, **kwargs):
            result = await super().quote(*args, **kwargs)
            result["created_at"] = datetime.fromtimestamp(clock[0], ZONE).isoformat()
            result["expires_at"] = datetime.fromtimestamp(clock[0] + 3600, ZONE).isoformat()
            return result

    provider = SandboxBookingProvider(tmp_path / "provider.sqlite")
    engine = ConversationEngine(
        extractor, Maps(), provider, Quotes(), clock=lambda: clock[0],
        location_confirmation=request.param,
    )
    yield engine, provider, clock, departures
    provider.close()


async def say(engine, state, text):
    response = state["last_response"]
    return await engine.process(
        state, text, event_id=f"event-{state['control']['generation'] + 1}",
        delivered_response_ids=[response["response_id"]],
        reply_to_response_id=response["response_id"],
    )


async def complete(engine, when):
    state = await say(engine, engine.new_state("scheduled"),
                      f"Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, {when}, 2 người, xe 4 chỗ, số 0901234567.")
    return await confirm_locations(engine, state)


async def confirm_locations(engine, state):
    for _ in range(3):
        if state["last_response"]["action"] != "confirm_location":
            break
        state = await say(engine, state, "đúng")
    return state


def test_scheduled_summary_and_provider_payload_contain_the_confirmed_time(runtime):
    engine, provider, _, departures = runtime

    async def run():
        state = await complete(engine, "ngày mai 08:00")
        assert state["last_response"]["action"] == "confirm_booking"
        assert state["last_response"]["summary"]["pickup_time"] == "08:00 ngày 04/10/2026 (giờ Việt Nam)"
        assert "Đi ngay" not in state["last_response"]["text"]
        assert provider.booking_count() == 0
        assert departures[-1] == datetime(2026, 10, 4, 8, 0, tzinfo=ZONE)
        state = await say(engine, state, "Đồng ý đặt xe")
        assert state["booking_status"] == "booked"
        booked = await provider.get(state["transaction"]["booking_result"]["booking_id"])
        assert booked["payload"]["pickup_schedule"] == {
            "mode": "scheduled", "pickup_at": "2026-10-04T08:00:00+07:00",
            "timezone": "Asia/Ho_Chi_Minh",
        }
        assert "08:00 ngày 04/10/2026" in state["last_response"]["text"]
        assert provider.booking_count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("when", ["ngày mai", "ngày mai 8 giờ", "hôm qua 18:00", "ngày 31/02/2027 08:00"])
def test_generic_yes_does_not_resolve_missing_or_invalid_pickup_time(runtime, when):
    engine, provider, _, _ = runtime

    async def run():
        state = await complete(engine, when)
        assert state["last_response"]["focus"] == "pickup_time"
        state = await say(engine, state, "Đồng ý đặt xe")
        assert state["last_response"]["focus"] == "pickup_time"
        assert state["issues"]["validation_pickup_time"]
        assert provider.booking_count() == 0

    asyncio.run(run())


@pytest.mark.parametrize("when,answer", [("ngày mai", "8 giờ sáng"), ("ngày mai 8 giờ", "sáng")])
def test_time_clarification_keeps_the_original_day_across_midnight(runtime, when, answer):
    engine, provider, clock, _ = runtime

    async def run():
        state = await complete(engine, when)
        clock[0] = datetime(2026, 10, 4, 0, 10, tzinfo=ZONE).timestamp()
        state = await say(engine, state, answer)
        state = await confirm_locations(engine, state)
        assert state["last_response"]["summary"]["pickup_time"] == "08:00 ngày 04/10/2026 (giờ Việt Nam)"
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_relative_time_is_pinned_until_consent(runtime):
    engine, provider, clock, _ = runtime

    async def run():
        state = await complete(engine, "30 phút nữa")
        original = state["resolution"]["pickup_time"]["pickup_at"]
        clock[0] += 120
        state = await say(engine, state, "Đồng ý đặt xe")
        assert state["booking_status"] == "booked"
        assert state["transaction"]["committed_snapshot"]["pickup_schedule"]["pickup_at"] == original
        assert original == "2026-10-03T07:30:00+07:00"
        assert provider.booking_count() == 1

    asyncio.run(run())


def test_repeating_a_relative_time_as_a_change_anchors_to_the_new_turn(runtime):
    engine, provider, clock, departures = runtime

    async def run():
        state = await complete(engine, "30 phút nữa")
        clock[0] += 300
        state = await say(engine, state, "Đồng ý nhưng đổi giờ đón sang 30 phút nữa")
        state = await confirm_locations(engine, state)
        assert state["booking_state"]["pickup"]["value"] == "Nhà hát Lớn Hà Nội"
        assert state["resolution"]["pickup_time"]["pickup_at"] == "2026-10-03T07:35:00+07:00"
        assert departures[-1] == datetime(2026, 10, 3, 7, 35, tzinfo=ZONE)
        assert state["last_response"]["action"] == "confirm_booking"
        assert provider.booking_count() == 0
        state = await say(engine, state, "Đồng ý đặt xe")
        assert provider.booking_count() == 1

    asyncio.run(run())


def test_switching_back_to_asap_clears_the_scheduled_departure(runtime):
    engine, provider, _, departures = runtime

    async def run():
        state = await complete(engine, "ngày mai 08:00")
        state = await say(engine, state, "Đổi sang đi ngay")
        state = await confirm_locations(engine, state)
        assert state["resolution"]["pickup_time"]["mode"] == "asap"
        assert state["last_response"]["summary"]["pickup_time"] == "Ngay bây giờ"
        assert departures[-1] is None
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_pickup_time_is_checked_again_between_prepare_and_dispatch(runtime):
    engine, provider, clock, _ = runtime

    async def run():
        state = await complete(engine, "1 phút nữa")
        response = state["last_response"]["response_id"]
        event = {"text": "Đồng ý đặt xe", "event_id": "consent", "occurred_at": clock[0],
                 "delivered_response_ids": [response], "reply_to_response_id": response}
        prepared = await engine.prepare(state, event, await engine.interpret(state, event))
        assert prepared["turn"]["ready_to_dispatch"]
        clock[0] += 61
        state = await engine.finalize(prepared, event)
        assert state["last_response"]["reason"] == "PICKUP_TIME_ELAPSED"
        assert state["last_response"]["focus"] == "pickup_time"
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_checkpoint_replay_uses_the_original_message_time(runtime, tmp_path):
    engine, provider, clock, _ = runtime

    async def run():
        original = engine.prepare
        interrupted = True

        async def fail_once(*args):
            nonlocal interrupted
            if interrupted:
                interrupted = False
                raise RuntimeError("interrupted before resolving time")
            return await original(*args)

        engine.prepare = fail_once
        async with DurableGraph(engine, tmp_path / "checkpoint.sqlite") as graph:
            await graph.initialize("replay", engine.new_state("replay"))
            with pytest.raises(RuntimeError, match="interrupted"):
                await graph.process("replay", "input", "Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, 30 phút nữa, 2 người, xe 4 chỗ, số 0901234567.", occurred_at=NOW)
            clock[0] += 600
            state = await graph.process("replay", "input", "retry")
            assert state["resolution"]["pickup_time"]["pickup_at"] == "2026-10-03T07:30:00+07:00"
            assert await graph.process("replay", "input", "retry") == state
        assert provider.booking_count() == 0

    asyncio.run(run())


def test_lost_create_response_recovers_even_after_the_pickup_time(runtime):
    engine, provider, clock, _ = runtime

    async def run():
        state = await complete(engine, "30 phút nữa")
        provider.faults["create_lost_response"] = 1
        state = await say(engine, state, "Đồng ý đặt xe")
        assert state["booking_status"] == "booking_unknown"
        clock[0] += 3600
        state = await say(engine, state, "Đồng ý đặt xe")
        assert state["booking_status"] == "booked"
        assert provider.booking_count() == 1
        assert provider.operation_count() == 1

    asyncio.run(run())


def test_text_bot_scheduled_booking_survives_restart(tmp_path):
    settings = Settings(profile="test", secret="scheduled-test",
                        database_path=tmp_path / "app.sqlite", checkpoint_path=tmp_path / "graph.sqlite")
    target = datetime.now(ZONE) + timedelta(days=2)
    when = target.strftime("%H:%M ngày %d/%m/%Y")
    message = f"Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, {when}, 2 người, xe 4 chỗ, số 0901234567."
    reply = main(message, settings=settings, session_id="scheduled", message_id="request", customer_phone="0901234567", customer_name="An")
    first_reply = reply
    for _ in range(3):
        reply = main("đúng", settings=settings, session_id="scheduled")
    assert when in reply
    assert main(message, settings=settings, session_id="scheduled", message_id="request") == first_reply
    reply = main("Đồng ý đặt xe", settings=settings, session_id="scheduled")
    assert "SBX-" in reply
    assert when in reply
