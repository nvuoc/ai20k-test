"""Provider failures keep durable inbox/state, release workers, and retry finitely."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.adapters.extraction_router import ExtractionRouter, ProviderAttempt
from app.adapters.extractor import ExtractorError
from app.adapters.rate_limit import RateLimitError
from app.api_store import ApiStore
from app.domain.engine import new_state
from app.workers.coordinator import Coordinator


class Graph:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def process(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        if callable(self.result):
            return await self.result()
        return deepcopy(self.result)


def pending(tmp_path):
    store = ApiStore(tmp_path/"api.sqlite")
    state = new_state("provider-session")
    store.create_session("provider-session", "owner", "client", state)
    receipt = store.enqueue("provider-session", "message", "pending", {"text": "Xin chào"})
    return store, state, receipt


def due(store):
    with store.connection(write=True) as db:
        db.execute("UPDATE api_turn_retries SET next_at=0")


@pytest.mark.parametrize("code", ["EXTRACTION_UNAVAILABLE", "DEADLINE_EXCEEDED", "PROVIDER_TIMEOUT"])
def test_transient_provider_failure_survives_restart_then_completes_same_event(tmp_path, code):
    store, initial, receipt = pending(tmp_path)
    graph = Graph(ExtractorError(code, "safe", retryable=True))
    coordinator = Coordinator(store, graph, concurrency=1)
    asyncio.run(coordinator.run_session("provider-session"))
    snapshot = store.snapshot("provider-session")
    assert snapshot["state"] == initial
    assert snapshot["pending_count"] == 1
    assert not snapshot["needs_support"]
    assert coordinator.concurrency._value == 1
    assert snapshot["events"][-1]["payload"]["retryable"]
    store = ApiStore(tmp_path/"api.sqlite")
    due(store)
    graph = Graph(initial)
    asyncio.run(Coordinator(store, graph).run_session("provider-session"))
    assert graph.calls[0]["event_id"] == receipt["event_id"]
    assert store.snapshot("provider-session")["pending_count"] == 0


def test_repeated_provider_failures_stop_after_three_without_mutating_state(tmp_path):
    store, initial, _ = pending(tmp_path)
    graph = Graph(ExtractorError("EXTRACTION_UNAVAILABLE", "safe", retryable=True))
    for expected in (1, 2, 3):
        asyncio.run(Coordinator(store, graph).run_session("provider-session"))
        with store.connection() as db:
            assert db.execute("SELECT attempt FROM api_turn_retries").fetchone()[0] == expected
        store = ApiStore(tmp_path/"api.sqlite")
        due(store)
    snapshot = store.snapshot("provider-session")
    assert snapshot["needs_support"] and snapshot["blocked_count"] == 1
    assert snapshot["state"] == initial
    assert store.next_pending("provider-session") is None
    assert len(graph.calls) == 3


def test_nonretryable_provider_config_failure_stops_immediately_and_retains_inbox(tmp_path):
    store, initial, _ = pending(tmp_path)
    error = ExtractorError("PROVIDER_ERROR", "safe", retryable=False)
    error.configuration_error = True
    graph = Graph(error)
    asyncio.run(Coordinator(store, graph).run_session("provider-session"))
    due(store)
    asyncio.run(Coordinator(store, graph).run_session("provider-session"))
    snapshot = store.snapshot("provider-session")
    assert snapshot["needs_support"]
    assert snapshot["state"] == initial
    assert not snapshot["events"][-1]["payload"]["retryable"]
    assert len(graph.calls) == 1


def test_local_quota_wait_has_no_http_retry_cost_and_does_not_hold_semaphore(tmp_path):
    store, initial, _ = pending(tmp_path)
    graph = Graph(RateLimitError(12, provider="LLM", request_sent=False))
    for _ in range(5):
        coordinator = Coordinator(store, graph, concurrency=1)
        asyncio.run(coordinator.run_session("provider-session"))
        assert coordinator.concurrency._value == 1
        with store.connection() as db:
            assert db.execute("SELECT http_attempt FROM api_quota_retries").fetchone()[0] == 0
            assert db.execute("SELECT attempt FROM api_turn_retries").fetchone()[0] == 0
        snapshot = store.snapshot("provider-session")
        assert snapshot["waiting_for_quota"] and not snapshot["needs_support"]
        assert snapshot["state"] == initial
        store = ApiStore(tmp_path/"api.sqlite")
        due(store)


def test_remote_quota_processing_attempts_are_bounded_and_survive_restart(tmp_path):
    store, initial, _ = pending(tmp_path)
    graph = Graph(RateLimitError(1, provider="LLM", request_sent=True))
    for count in (1, 2, 3):
        asyncio.run(Coordinator(store, graph).run_session("provider-session"))
        with store.connection() as db:
            assert db.execute("SELECT http_attempt FROM api_quota_retries").fetchone()[0] == count
            assert db.execute("SELECT attempt FROM api_turn_retries").fetchone()[0] == 0
        store = ApiStore(tmp_path/"api.sqlite")
        due(store)
    snapshot = store.snapshot("provider-session")
    assert snapshot["needs_support"] and not snapshot["waiting_for_quota"]
    assert snapshot["state"] == initial
    assert store.next_pending("provider-session") is None
    assert len(graph.calls) == 3


def test_completed_retry_cleans_both_persisted_counters(tmp_path):
    store, initial, _ = pending(tmp_path)
    asyncio.run(Coordinator(store, Graph(RateLimitError(1, request_sent=True))).run_session("provider-session"))
    due(store)
    asyncio.run(Coordinator(store, Graph(initial)).run_session("provider-session"))
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM api_turn_retries").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM api_quota_retries").fetchone()[0] == 0


def test_router_tells_worker_whether_any_quota_http_was_sent(tmp_path):
    from test_groq_router import Fake, payload
    for sent in (False, True):
        providers = [ProviderAttempt(name, Fake(RateLimitError(1, provider=name, request_sent=sent)), "model", 1)
            for name in ("groq", "gemini")]
        with pytest.raises(RateLimitError) as error:
            asyncio.run(ExtractionRouter(providers).extract(payload()))
        assert error.value.request_sent is sent
