"""Restart the real graph and worker across commits/publication boundaries."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from app.adapters.booking_sandbox import SandboxBookingProvider
from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.quote_fixture import FixtureQuoteAdapter
from app.api_store import ApiStore, Conflict
from app.domain.engine import ChatEngine, new_state
from app.graph.builder import DurableGraph
from app.workers.coordinator import Coordinator
from tests.test_domain import CORE_VALUES, Extractor, act


def force_due(store):
    with store.connection(write=True) as db:
        db.execute("UPDATE api_reconciliation SET next_at=0")


def test_worker_reconciles_lost_response_without_another_user_message(tmp_path):
    async def scenario():
        provider = SandboxBookingProvider(tmp_path / "provider.sqlite")
        extractor = Extractor()
        engine = ChatEngine(extractor, FixtureMapAdapter(), provider, FixtureQuoteAdapter())
        store = ApiStore(tmp_path / "api.sqlite")
        async with DurableGraph(engine, tmp_path / "graph.sqlite") as graph:
            initial = new_state("session")
            await graph.initialize("session", initial)
            store.create_session("session", "owner", "client", initial)
            extractor.acts = [act("provide_info", slot, value) for slot, value in CORE_VALUES.items()]
            receipt = store.enqueue("session", "message", "collect", {"text": "Đặt chuyến"})
            coordinator = Coordinator(store, graph)
            await coordinator.run_session("session")
            state = await graph.get_state("session")
            extractor.acts = [act("confirm")]
            provider.faults["create_lost_response"] = 1
            store.enqueue("session", "message", "confirm", {"text": "Đồng ý",
                "rendered_response_ids": [state["last_response"]["response_id"]]})
            await coordinator.run_session("session")
            assert store.snapshot("session")["state"]["booking_status"] == "booking_unknown"
            assert store.unknown_sessions() == ["session"]
            force_due(store)
        # A new process-equivalent graph/worker picks up the persisted schedule.
        async with DurableGraph(engine, tmp_path / "graph.sqlite") as graph:
            coordinator = Coordinator(store, graph)
            task = asyncio.create_task(coordinator.run())
            for _ in range(100):
                if store.snapshot("session")["state"]["booking_status"] == "booked":
                    break
                await asyncio.sleep(0.01)
            assert store.snapshot("session")["state"]["booking_status"] == "booked"
            assert provider.booking_count() == 1
            assert store.unknown_sessions() == []
            await coordinator.close()
            await task
        provider.close()
        assert receipt["status"] == "received"
    asyncio.run(scenario())


def test_reconcile_attempts_are_bounded_and_schedule_survives_restart(tmp_path):
    store = ApiStore(tmp_path / "api.sqlite")
    state = new_state("unknown")
    state["booking_status"] = "booking_unknown"
    store.create_session("unknown", "owner", "client", state)
    store.unknown_sessions()
    for attempt in range(1, 6):
        force_due(store)
        event = store.claim_reconciliation("unknown")
        assert event == f"reconcile:unknown:{attempt}"
        store.publish_reconciliation("unknown", event, state)
        store = ApiStore(tmp_path / "api.sqlite")
    force_due(store)
    assert store.due_reconciliations() == []
    assert store.claim_reconciliation("unknown") is None
    snapshot = store.snapshot("unknown")
    assert snapshot["needs_support"]
    assert snapshot["blocked_count"] == 1
    assert snapshot["state"]["booking_status"] == "booking_unknown"


def test_transient_pending_graph_resumes_same_event_after_retry(tmp_path):
    async def scenario():
        provider = SandboxBookingProvider(tmp_path / "provider.sqlite")
        extractor = Extractor()
        extractor.acts = [act("provide_info", slot, value) for slot, value in CORE_VALUES.items()]
        engine = ChatEngine(extractor, FixtureMapAdapter(), provider, FixtureQuoteAdapter())
        original = engine.process
        calls = 0
        async def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary storage interruption")
            return await original(*args, **kwargs)
        engine.process = interrupted
        store = ApiStore(tmp_path / "api.sqlite")
        async with DurableGraph(engine, tmp_path / "graph.sqlite") as graph:
            initial = new_state("retry-session")
            await graph.initialize("retry-session", initial)
            store.create_session("retry-session", "owner", "client", initial)
            receipt = store.enqueue("retry-session", "message", "collect", {"text": "Đặt chuyến"})
            coordinator = Coordinator(store, graph)
            await coordinator.run_session("retry-session")
            assert store.snapshot("retry-session")["pending_count"] == 1
            assert store.pending_sessions() == []
            assert (await graph.graph.aget_state(graph._config("retry-session"))).next
        with store.connection(write=True) as db:
            db.execute("UPDATE api_turn_retries SET next_at=0")
        async with DurableGraph(engine, tmp_path / "graph.sqlite") as graph:
            coordinator = Coordinator(store, graph)
            await coordinator.run_session("retry-session")
            result = store.snapshot("retry-session")
            assert result["pending_count"] == 0
            assert result["state"]["control"]["last_event_id"] == receipt["event_id"]
            assert result["state"]["booking_status"] == "awaiting_confirmation"
            assert calls == 2
            assert provider.booking_count() == 0
        provider.close()
    asyncio.run(scenario())


def test_exhausted_pending_event_stops_session_without_dropping_checkpoint(tmp_path):
    store = ApiStore(tmp_path / "api.sqlite")
    initial = new_state("broken")
    store.create_session("broken", "owner", "client", initial)
    store.enqueue("broken", "message", "collect", {"text": "Đặt chuyến"})
    event = store.next_pending("broken")
    for _ in range(3):
        store.defer(event, "TURN_PROCESSING_ERROR")
        with store.connection(write=True) as db:
            db.execute("UPDATE api_turn_retries SET next_at=0")
    result = store.snapshot("broken")
    assert result["needs_support"]
    assert result["blocked_count"] == 1
    assert result["pending_count"] == 0
    assert store.pending_sessions() == []
    assert store.next_pending("broken") is None
    with pytest.raises(Conflict, match="SESSION_NEEDS_SUPPORT"):
        store.enqueue("broken", "message", "new", {"text": "Đặt tiếp"})


CHILD = """
import asyncio, os, sys
from pathlib import Path
from app.adapters.booking_sandbox import SandboxBookingProvider
from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.quote_fixture import FixtureQuoteAdapter
from app.api_store import ApiStore
from app.domain.engine import ChatEngine, new_state
from app.graph.builder import DurableGraph
from tests.test_domain import CORE_VALUES, Extractor, act

async def run():
    root = Path(sys.argv[1]); mode = sys.argv[2]
    provider = SandboxBookingProvider(root / 'provider.sqlite')
    extractor = Extractor()
    engine = ChatEngine(extractor, FixtureMapAdapter(), provider, FixtureQuoteAdapter())
    store = ApiStore(root / 'api.sqlite')
    async with DurableGraph(engine, root / 'graph.sqlite') as graph:
        initial = new_state('crash-session')
        await graph.initialize('crash-session', initial)
        store.create_session('crash-session', 'owner', 'client', initial)
        extractor.acts = [act('provide_info', s, v) for s,v in CORE_VALUES.items()]
        store.enqueue('crash-session','message','collect',{'text':'collect'})
        event = store.next_pending('crash-session')
        state = await graph.process('crash-session',event['id'],'collect')
        store.complete(event,state)
        extractor.acts = [act('confirm')]
        response = state['last_response']['response_id']
        store.enqueue('crash-session','message','confirm',{'text':'confirm','rendered_response_ids':[response]})
        event = store.next_pending('crash-session')
        if mode == 'after-provider':
            original = provider.create
            async def die_after_create(payload,key):
                await original(payload,key)
                os._exit(72)
            provider.create = die_after_create
        await graph.process('crash-session',event['id'],'confirm',delivered_response_ids=[response])
        os._exit(73)
asyncio.run(run())
"""


@pytest.mark.parametrize("boundary,code", [("after-provider", 72), ("after-checkpoint", 73)])
def test_actual_process_crash_recovers_one_booking_and_publishes_once(tmp_path, boundary, code):
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(tmp_path), boundary],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
    )
    assert child.returncode == code, child.stderr
    async def scenario():
        provider = SandboxBookingProvider(tmp_path / "provider.sqlite")
        assert provider.booking_count() == 1
        extractor = Extractor()
        extractor.acts = [act("confirm")]
        engine = ChatEngine(extractor, FixtureMapAdapter(), provider, FixtureQuoteAdapter())
        store = ApiStore(tmp_path / "api.sqlite")
        async with DurableGraph(engine, tmp_path / "graph.sqlite") as graph:
            coordinator = Coordinator(store, graph)
            await coordinator.run_session("crash-session")
            published = store.snapshot("crash-session")
            assert published["state"]["booking_status"] == "booked"
            assert published["pending_count"] == 0
            assert provider.booking_count() == 1
            assert provider.operation_count("create") == 1
            responses = [e for e in published["events"] if e["type"] == "assistant_response"]
            assert len(responses) == 3  # Welcome, summary, committed outcome.
            await coordinator.run_session("crash-session")
            assert store.snapshot("crash-session")["events"] == published["events"]
        provider.close()
    asyncio.run(scenario())
