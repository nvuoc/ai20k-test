"""Real SQLite checkpoints and provider restart preserve booking outcomes."""

from __future__ import annotations

import asyncio

import pytest

from app.adapters.booking_sandbox import SandboxBookingProvider
from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.quote_fixture import FixtureQuoteAdapter
from app.domain.engine import ChatEngine, new_state
from app.graph.builder import DurableGraph
from tests.test_domain import CORE_VALUES, Extractor, act


def test_sandbox_key_payload_conflict_and_restart_lookup(tmp_path):
    async def scenario():
        path = tmp_path / "provider.sqlite"
        provider = SandboxBookingProvider(path)
        payload = {"draft_id": "draft", "session_id": "session", "slots": {"passengers": 2}}
        result = await provider.create(payload, "key")
        assert await provider.create(payload, "key") == result
        with pytest.raises(ValueError, match="IDEMPOTENCY_PAYLOAD_CONFLICT"):
            await provider.create(payload | {"slots": {"passengers": 3}}, "key")
        provider.close()
        recovered = SandboxBookingProvider(path)
        assert await recovered.lookup("key") == result
        assert recovered.booking_count() == 1
        await recovered.cancel(result["booking_id"], "cancel-key")
        assert (await recovered.get(result["booking_id"]))["provider_status"] == "cancelled"
        recovered.close()
    asyncio.run(scenario())


def test_langgraph_restart_completed_event_dedup_and_rendered_confirmation(tmp_path):
    async def scenario():
        provider = SandboxBookingProvider(tmp_path / "provider.sqlite")
        extractor = Extractor()
        engine = ChatEngine(extractor, FixtureMapAdapter(), provider, FixtureQuoteAdapter())
        graph_path = tmp_path / "checkpoint.sqlite"
        async with DurableGraph(engine, graph_path) as graph:
            await graph.initialize("session", new_state("session"))
            extractor.acts = [act("provide_info", slot, value) for slot, value in CORE_VALUES.items()]
            state = await graph.process("session", "complete", "Đặt chuyến")
            response_id = state["last_response"]["response_id"]
        async with DurableGraph(engine, graph_path) as graph:
            reloaded = await graph.get_state("session")
            assert reloaded == state
            calls = extractor.calls
            assert await graph.process("session", "complete", "Đặt chuyến") == state
            assert extractor.calls == calls
            extractor.acts = [act("confirm")]
            state = await graph.process("session", "confirm", "Đồng ý", delivered_response_ids=[response_id])
            assert state["booking_status"] == "booked"
        async with DurableGraph(engine, graph_path) as graph:
            assert (await graph.get_state("session"))["booking_status"] == "booked"
            calls = extractor.calls
            await graph.process("session", "confirm", "Đồng ý", delivered_response_ids=[response_id])
            assert extractor.calls == calls
            assert provider.booking_count() == 1
        provider.close()
    asyncio.run(scenario())


def test_commit_before_checkpoint_recovers_even_after_quote_expires(tmp_path):
    async def scenario():
        provider = SandboxBookingProvider(tmp_path / "provider.sqlite")
        extractor = Extractor()
        engine = ChatEngine(extractor, FixtureMapAdapter(), provider, FixtureQuoteAdapter())
        extractor.acts = [act("provide_info", slot, value) for slot, value in CORE_VALUES.items()]
        state = await engine.process(new_state("lost-checkpoint"), "Đặt chuyến", event_id="collect")
        payload = engine._snapshot_payload(state)
        # This is the provider commit boundary; checkpoint still has a summary.
        await provider.create(payload, "old-operation")
        provider.close()
        recovered_provider = SandboxBookingProvider(tmp_path / "provider.sqlite")
        recovered_engine = ChatEngine(extractor, FixtureMapAdapter(), recovered_provider, FixtureQuoteAdapter())
        state["resolution"]["quote"]["expires_at"] = 0
        extractor.acts = [act("confirm")]
        state = await recovered_engine.process(state, "Đặt lại", event_id="recover")
        assert state["booking_status"] == "booked"
        assert recovered_provider.booking_count() == 1
        assert state["transaction"]["committed_snapshot"] == payload
        recovered_provider.close()
    asyncio.run(scenario())
