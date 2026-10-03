"""V2 public actions, read-only weather and durable quota waiting."""

# ruff: noqa: F811 -- pytest fixtures are intentionally imported for discovery.
import asyncio
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from test_api import (  # noqa: F401
    acknowledge,
    client,
    complete,
    create_session,
    send_action,
    send_text,
    settings,
)
from test_conversation_v2 import INQUIRY, runtime  # noqa: F401

from app.adapters.rate_limit import RateLimitError
from app.api_store import ApiStore
from app.graph.builder import DurableGraph
from app.workers.coordinator import Coordinator


def test_weather_endpoint_reads_sourced_location_without_mutation(client):
    sid = create_session(client)["session_id"]
    complete(client, sid)
    before = deepcopy(client.app.state.store.snapshot(sid)["state"])
    response = client.get(
        f"/api/sessions/{sid}/weather",
        params={"at": datetime.now(UTC).isoformat(), "target": "pickup"},
    )
    assert response.status_code == 200, response.text
    fact = response.json()
    assert fact["status"] == "available" and fact["provider"] == "fixture"
    assert "model" not in fact and "api_key" not in response.text
    assert client.app.state.store.snapshot(sid)["state"] == before
    naive = client.get(f"/api/sessions/{sid}/weather", params={"at": "2026-10-03T08:00:00"})
    assert naive.status_code == 422


def test_scoped_inquiry_action_and_reload(client):
    sid = create_session(client)["session_id"]
    complete(client, sid)
    snapshot, _ = send_text(client, sid, INQUIRY)
    shown = snapshot["active_response"]
    item = shown["inquiry"]
    assert client.get(f"/api/sessions/{sid}").json()["active_response"]["inquiry"] == item
    acknowledge(client, sid, shown)
    action = {
        "type": "use_inquiry_route",
        "inquiry_id": item["inquiry_id"],
        "inquiry_revision": item["revision"],
        "route_fingerprint": item["route_fingerprint"],
        "booking_revision": item["booking_revision"],
    }
    updated, _ = send_action(client, sid, action, reply=shown["response_id"])
    assert "cổng sau" in updated["active_response"]["summary"]["destination"]
    assert client.app.state.engine.booking.booking_count() == 0


def test_quota_wait_is_persistent_and_not_a_fault_retry(runtime, tmp_path):
    engine, _, calls = runtime
    original = engine.extractor
    attempts = 0

    async def limited(data):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RateLimitError(20)
        return await original(data)

    engine.extractor = limited

    async def run():
        store = ApiStore(tmp_path / "api.sqlite")
        async with DurableGraph(engine, tmp_path / "graph.sqlite") as graph:
            state = engine.new_state("session")
            await graph.initialize("session", state)
            store.create_session("session", "owner", "key", state)
            receipt = store.enqueue("session", "message", "msg", {"text": INQUIRY})
            worker = Coordinator(store, graph)
            await worker.run_session("session")
            snapshot = store.snapshot("session")
            assert snapshot["waiting_for_quota"] and snapshot["pending_count"] == 1
            assert not snapshot["needs_support"]
            with store.connection() as db:
                row = db.execute(
                    "SELECT * FROM api_turn_retries WHERE event_id=?", (receipt["event_id"],)
                ).fetchone()
                assert row["attempt"] == 0 and row["status"] == "waiting_for_quota"
            reopened = ApiStore(tmp_path / "api.sqlite")
            assert reopened.snapshot("session")["waiting_for_quota"]
            with reopened.connection(write=True) as db:
                db.execute("UPDATE api_turn_retries SET next_at=0")
            await Coordinator(reopened, graph).run_session("session")
            result = reopened.snapshot("session")
            assert not result["waiting_for_quota"] and result["pending_count"] == 0
            assert result["state"]["last_response"]["inquiry"]
            assert calls["nlu"] == 1

    asyncio.run(run())


def test_v2_crash_after_provider_commit_recovers_before_quote_expiry(runtime, tmp_path):
    engine, provider, calls = runtime
    original = provider.create
    failed = False

    async def commit_then_crash(*args):
        nonlocal failed
        result = await original(*args)
        if not failed:
            failed = True
            raise RuntimeError("crash after commit before node checkpoint")
        return result

    provider.create = commit_then_crash

    async def run():
        from test_conversation_v2 import COMPLETE

        async with DurableGraph(engine, tmp_path / "graph.sqlite") as graph:
            await graph.initialize("session", engine.new_state("session"))
            state = await graph.process("session", "collect", COMPLETE)
            shown = state["last_response"]["response_id"]
            with pytest.raises(RuntimeError):
                await graph.process(
                    "session",
                    "confirm",
                    "Đồng ý đặt",
                    delivered_response_ids=[shown],
                    reply_to_response_id=shown,
                )
            assert provider.booking_count() == 1
            count = calls["nlu"]
            clock = engine.clock()
            engine.clock = lambda: clock + 1000
            state = await graph.process(
                "session",
                "confirm",
                "Đồng ý đặt",
                delivered_response_ids=[shown],
                reply_to_response_id=shown,
            )
            assert state["booking_status"] == "booked"
            assert state["last_response"]["action"] == "inform_success"
            assert provider.booking_count() == 1 and calls["nlu"] == count

    asyncio.run(run())
