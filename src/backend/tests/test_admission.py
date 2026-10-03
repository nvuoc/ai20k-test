"""Bound incoming work atomically without changing retry receipts or booking state."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api_store import AdmissionError, ApiStore
from app.config import Settings
from app.domain.engine import new_state
from app.main import create_app
from app.workers.coordinator import Coordinator


def store(tmp_path, **limits):
    return ApiStore(tmp_path/"api.sqlite", limits=replace(Settings(), **limits))


def add_session(storage, sid="session", owner="owner", key="client"):
    return storage.create_session(sid, owner, key, new_state(sid))


def test_owner_and_global_session_caps_are_persisted_and_idempotent(tmp_path):
    storage = store(tmp_path, max_sessions_per_owner=1, max_sessions_total=2)
    assert add_session(storage) == "session"
    assert add_session(storage, sid="ignored") == "session"
    with pytest.raises(AdmissionError, match="giới hạn phiên") as error:
        add_session(storage, sid="other", key="other")
    assert error.value.code == "OWNER_SESSION_LIMIT"
    add_session(storage, sid="second", owner="second", key="second")
    storage = ApiStore(storage.path, limits=storage.limits)
    with pytest.raises(AdmissionError) as error:
        add_session(storage, sid="third", owner="third", key="third")
    assert error.value.code == "SESSION_CAPACITY"
    assert add_session(storage, sid="ignored", owner="second", key="second") == "second"


def test_pending_cap_accepts_exact_retry_but_rejects_new_work_atomically(tmp_path):
    storage = store(tmp_path, max_pending_per_session=1)
    add_session(storage)
    payload = {"text": "Xin chào"}
    first = storage.enqueue("session", "message", "one", payload)
    retry = storage.enqueue("session", "message", "one", payload)
    assert retry["event_id"] == first["event_id"]
    with pytest.raises(AdmissionError) as error:
        storage.enqueue("session", "message", "two", payload)
    assert error.value.code == "SESSION_QUEUE_FULL"
    with storage.connection() as db:
        assert db.execute("SELECT latest_seq FROM api_sessions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM api_inbox").fetchone()[0] == 1
    event = storage.next_pending("session")
    storage.complete(event, new_state("session"))
    storage.enqueue("session", "message", "two", payload)


def test_global_pending_cap_is_shared_across_owners_and_restart(tmp_path):
    storage = store(tmp_path, max_pending_total=1)
    add_session(storage)
    add_session(storage, sid="second", owner="second", key="second")
    storage.enqueue("session", "message", "one", {"text": "Xin chào"})
    storage = ApiStore(storage.path, limits=storage.limits)
    with pytest.raises(AdmissionError) as error:
        storage.enqueue("second", "message", "two", {"text": "Xin chào"})
    assert error.value.code == "INBOX_CAPACITY"


def test_admission_rate_is_durable_rolling_window_and_duplicates_cost_no_work(tmp_path):
    clock = [100.]
    limits = replace(Settings(), inbound_owner_rpm=2)
    storage = ApiStore(tmp_path/"api.sqlite", limits=limits, clock=lambda: clock[0])
    add_session(storage)  # One new admitted operation.
    first = storage.enqueue("session", "message", "one", {"text": "Xin chào"})
    storage = ApiStore(storage.path, limits=limits, clock=lambda: clock[0])
    assert storage.enqueue("session", "message", "one", {"text": "Xin chào"})["event_id"] == first["event_id"]
    with pytest.raises(AdmissionError) as error:
        storage.enqueue("session", "message", "two", {"text": "Xin chào"})
    assert error.value.retry_after_seconds == 60
    clock[0] = 160
    storage.enqueue("session", "message", "two", {"text": "Xin chào"})


def test_global_rate_reservations_are_shared_across_owners(tmp_path):
    storage = store(tmp_path, inbound_global_rpm=2)
    add_session(storage)
    add_session(storage, sid="second", owner="second", key="second")
    with pytest.raises(AdmissionError) as error:
        storage.enqueue("session", "message", "one", {"text": "Xin chào"})
    assert error.value.code == "INBOUND_RATE_LIMITED"
    assert add_session(storage, sid="ignored") == "session"


def test_parallel_enqueue_has_exact_capacity_not_check_then_insert_race(tmp_path):
    storage = store(tmp_path, max_pending_per_session=3)
    add_session(storage)
    def enqueue(number):
        try:
            storage.enqueue("session", "message", str(number), {"text": "Xin chào"})
            return True
        except AdmissionError:
            return False
    with ThreadPoolExecutor(max_workers=12) as workers:
        assert sum(workers.map(enqueue, range(20))) == 3
    assert storage.snapshot("session")["pending_count"] == 3


def test_parallel_session_creation_has_exact_owner_capacity(tmp_path):
    storage = store(tmp_path, max_sessions_per_owner=2)
    def create(number):
        try:
            add_session(storage, sid=str(number), key=str(number))
            return True
        except AdmissionError:
            return False
    with ThreadPoolExecutor(max_workers=10) as workers:
        assert sum(workers.map(create, range(15))) == 2


def api_settings(tmp_path, **limits):
    return replace(Settings(profile="test", secret="offline-test",
        database_path=tmp_path/"api.sqlite", checkpoint_path=tmp_path/"graph.sqlite"), **limits)


def test_api_session_cap_429_preserves_existing_session_and_bounded_creation_lock(tmp_path):
    with TestClient(create_app(api_settings(tmp_path, max_sessions_per_owner=1))) as client:
        client.get("/api/bootstrap")
        first = client.post("/api/sessions", json={"client_session_key": "one"})
        assert first.status_code == 200
        denied = client.post("/api/sessions", json={"client_session_key": "two"})
        assert denied.status_code == 429 and denied.json()["code"] == "OWNER_SESSION_LIMIT"
        assert client.post("/api/sessions", json={"client_session_key": "one"}).json()["session_id"] == first.json()["session_id"]
        assert isinstance(client.app.state.creation_lock, asyncio.Lock)
        assert not hasattr(client.app.state, "creation_locks")


def test_api_rate_429_has_retry_after_and_duplicate_receipt_still_works(tmp_path):
    with TestClient(create_app(api_settings(tmp_path, inbound_owner_rpm=2))) as client:
        client.get("/api/bootstrap")
        session = client.post("/api/sessions", json={"client_session_key": "one"}).json()["session_id"]
        path = f"/api/sessions/{session}/messages"
        payload = {"client_message_id": "one", "text": "Xin chào"}
        first = client.post(path, json=payload)
        assert first.status_code == 202
        denied = client.post(path, json={"client_message_id": "two", "text": "Xin chào"})
        assert denied.status_code == 429 and denied.json()["code"] == "INBOUND_RATE_LIMITED"
        assert 1 <= int(denied.headers["Retry-After"]) <= 60
        assert client.post(path, json=payload).json()["event_id"] == first.json()["event_id"]


@pytest.mark.parametrize("claimed", [None, "1", "100000"])
def test_streamed_body_limit_413_ignores_missing_or_false_content_length(tmp_path, claimed):
    with TestClient(create_app(api_settings(tmp_path, max_request_body_bytes=128))) as client:
        client.get("/api/bootstrap")
        chunks = [b'{"client_session_key":"', b"x"*100, b"x"*100, b'"}']
        headers = {"Content-Type": "application/json"}
        if claimed:
            headers["Content-Length"] = claimed
        denied = client.post("/api/sessions", content=iter(chunks), headers=headers)
        assert denied.status_code == 413
        assert denied.json()["code"] == "REQUEST_BODY_TOO_LARGE"
        with client.app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM api_sessions").fetchone()[0] == 0


def test_coordinator_schedules_no_more_than_configured_tasks():
    class FakeStore:
        def pending_sessions(self):
            return [str(number) for number in range(50)]
        def due_reconciliations(self):
            return []
    async def run():
        worker = Coordinator(FakeStore(), None, concurrency=2)
        gate = asyncio.Event()
        async def blocked(session_id):
            await gate.wait()
        worker.run_session = blocked
        runner = asyncio.create_task(worker.run())
        await asyncio.sleep(.03)
        assert len(worker.tasks) == 2
        await worker.close()
        await runner
    asyncio.run(run())


def test_api_session_retries_repair_graph_initialization_without_extra_admission(tmp_path):
    with TestClient(create_app(api_settings(tmp_path, inbound_owner_rpm=1)), raise_server_exceptions=False) as client:
        client.get("/api/bootstrap")
        graph = client.app.state.graph
        original = graph.initialize
        failed = [False]
        async def fault(session_id, state):
            if not failed[0]:
                failed[0] = True
                raise RuntimeError("temporary checkpoint failure")
            return await original(session_id, state)
        graph.initialize = fault
        assert client.post("/api/sessions", json={"client_session_key": "one"}).status_code == 500
        repaired = client.post("/api/sessions", json={"client_session_key": "one"})
        assert repaired.status_code == 200
        with client.app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM api_sessions").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM api_admission_requests").fetchone()[0] == 2


def test_ready_rejects_stopped_background_worker_without_external_requests(tmp_path):
    with TestClient(create_app(api_settings(tmp_path))) as client:
        assert client.get("/api/ready").status_code == 200
        async def stop():
            worker = client.app.state.coordinator
            worker.running = False
            worker.wakeup.set()
            await client.app.state.runner
        client.portal.call(stop)
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert response.json()["code"] == "WORKER_UNAVAILABLE"


def test_ready_rejects_uninitialized_store_safely(tmp_path):
    with TestClient(create_app(api_settings(tmp_path))) as client:
        assert client.get("/api/ready").status_code == 200
        with client.app.state.store.connection(write=True) as db:
            db.execute("DELETE FROM api_meta")
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert response.json()["code"] == "STORE_UNAVAILABLE"
        assert "offline-test" not in response.text


def test_coordinator_alternates_chat_and_reconciliation_priority():
    class FakeStore:
        def pending_sessions(self):
            return ["chat"]
        def due_reconciliations(self):
            return ["reconcile"]
    async def run():
        worker = Coordinator(FakeStore(), None, concurrency=1)
        calls = []
        async def work(session):
            calls.append(session)
            if len(calls) == 2:
                worker.running = False
                worker.wakeup.set()
        worker.run_session = worker.run_reconciliation = work
        await worker.run()
        assert calls == ["reconcile", "chat"]
        await worker.close()
    asyncio.run(run())
