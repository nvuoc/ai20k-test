"""Offline Groq wire, complete validated fallback, deadline and restart tests."""
from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.adapters.extraction_router import ExtractionRouter, ProviderAttempt
from app.adapters.extractor import (
    CallBudget,
    ExtractorError,
    ExtractorRuntime,
    ModelReply,
    llm_extractor_func,
)
from app.adapters.groq_rate_limit import GroqRateLimiter
from app.adapters.nlu_groq import DEFAULT_GROQ_MODEL, GroqExtractorClient, _reset_seconds
from app.adapters.rate_limit import RateLimitError
from app.contracts.nlu import empty_booking_state, provider_output_schema


def payload():
    return {"utterance": {"text": "Đón ở 36 Hoàng Cầu", "asr_confidence": None},
        "conversation_context": {"last_bot_message": None, "last_bot_action": None, "current_focus": None},
        "booking_state": empty_booking_state().model_dump(), "candidates": [], "booking_status": "collecting_info"}


def valid_reply(value="36 Hoàng Cầu", target="pickup", intent="provide_info"):
    return ModelReply(text=json.dumps({"speech_status": "clear", "dialogue_acts": [
        {"intent": intent, "target": target, "value": value}]}))


def response_body(reply=None, finish="stop"):
    return {"choices": [{"finish_reason": finish,
        "message": {"role": "assistant", "content": (reply or valid_reply()).text,
        "reasoning": "must stay private"}}], "usage": {"total_tokens": 800}}


class Fake:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        if callable(self.result):
            return await self.result()
        return self.result


def router(primary, fallback, **kwargs):
    return ExtractionRouter([ProviderAttempt("groq", primary, DEFAULT_GROQ_MODEL, .1),
        ProviderAttempt("gemini", fallback, "gemini-3.5-flash-lite", .2)], **kwargs)


def test_wire_uses_strict_schema_low_reasoning_and_only_one_http_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response_body())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = GroqExtractorClient(api_key="offline-key", client=http,
                limiter=GroqRateLimiter(bucket="wire"))
            result = await llm_extractor_func(payload(), runtime=ExtractorRuntime(client=provider,
                model=DEFAULT_GROQ_MODEL, max_attempts=1))
            assert result.dialogue_acts[0].value == "36 Hoàng Cầu"
            await provider.close()
            assert not http.is_closed

    asyncio.run(run())
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/openai/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer offline-key"
    assert "offline-key" not in str(request.url)
    sent = json.loads(request.content)
    assert sent["model"] == DEFAULT_GROQ_MODEL
    assert sent["reasoning_effort"] == "low"
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["response_format"]["json_schema"]["schema"] == provider_output_schema()
    assert json.loads(sent["messages"][1]["content"]) == payload()
    assert sent["stream"] is False
    assert "tools" not in sent


@pytest.mark.parametrize("status,code", [(401, "PROVIDER_AUTH_ERROR"), (403, "PROVIDER_AUTH_ERROR"),
    (400, "PROVIDER_CONFIG_ERROR"), (404, "PROVIDER_MODEL_UNAVAILABLE"), (503, "PROVIDER_UNAVAILABLE")])
def test_http_failure_does_not_expose_key_or_provider_body(status, code):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, text="sensitive key phone"))) as http:
            provider = GroqExtractorClient(api_key="sensitive", client=http,
                limiter=GroqRateLimiter(bucket="error"))
            with pytest.raises(ExtractorError) as error:
                await provider.generate(system_prompt="safe", input_json="{}", response_schema={},
                    model=DEFAULT_GROQ_MODEL, timeout_seconds=1)
            assert error.value.code == code
            assert "sensitive" not in str(error.value)
    asyncio.run(run())


@pytest.mark.parametrize("finish,refusal,incomplete", [("length", False, True), ("content_filter", True, False)])
def test_refusal_and_truncation_discard_complete_looking_content(finish, refusal, incomplete):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response_body(finish=finish)))) as http:
            provider = GroqExtractorClient(api_key="offline", client=http, limiter=GroqRateLimiter(bucket="finish"))
            reply = await provider.generate(system_prompt="safe", input_json="{}", response_schema={}, model=DEFAULT_GROQ_MODEL, timeout_seconds=1)
            assert bool(reply.refusal) is refusal
            assert reply.incomplete is incomplete
            assert reply.text is None
    asyncio.run(run())


@pytest.mark.parametrize("first", [ModelReply(text="not json"), valid_reply("disabled", "vehicle_type"),
    valid_reply("unknown-candidate", "pickup", "select_candidate"), ModelReply(incomplete=True),
    ExtractorError("PROVIDER_UNAVAILABLE", "safe", retryable=True)])
def test_fallback_includes_business_validation_and_original_input(first):
    primary, fallback = Fake(first), Fake(valid_reply())
    budget = CallBudget()
    result = asyncio.run(router(primary, fallback).extract(payload(), runtime=ExtractorRuntime(client=primary, call_budget=budget)))
    assert result.dialogue_acts[0].value == "36 Hoàng Cầu"
    assert len(primary.calls) == len(fallback.calls) == 1
    assert budget.used == 2
    assert primary.calls[0]["input_json"] == fallback.calls[0]["input_json"]
    assert primary.calls[0]["system_prompt"] == fallback.calls[0]["system_prompt"]
    assert primary.calls[0]["response_schema"] == fallback.calls[0]["response_schema"]
    assert "REPAIR" not in fallback.calls[0]["system_prompt"]


def test_valid_unclear_never_falls_back_to_guess():
    primary, fallback = Fake(valid_reply(None, None, "no_understanding")), Fake(valid_reply())
    result = asyncio.run(router(primary, fallback).extract(payload()))
    assert result.dialogue_acts[0].intent == "no_understanding"
    assert not fallback.calls


@pytest.mark.parametrize("first,code", [(ModelReply(refusal="safe"), "MODEL_REFUSAL"),
    (ExtractorError("PROVIDER_AUTH_ERROR", "safe"), "PROVIDER_AUTH_ERROR"),
    (ExtractorError("PROVIDER_CONFIG_ERROR", "safe"), "PROVIDER_CONFIG_ERROR")])
def test_refusal_and_config_never_fallback_without_explicit_degraded_mode(first, code):
    primary, fallback = Fake(first), Fake(valid_reply())
    with pytest.raises(ExtractorError) as error:
        asyncio.run(router(primary, fallback).extract(payload()))
    assert error.value.code == code
    assert len(primary.calls) == 1
    assert not fallback.calls


def test_degraded_config_fallback_is_explicit():
    primary, fallback = Fake(ExtractorError("PROVIDER_AUTH_ERROR", "safe")), Fake(valid_reply())
    asyncio.run(router(primary, fallback, allow_degraded=True).extract(payload()))
    assert len(fallback.calls) == 1


def test_legacy_gemini_config_marker_stops_routing_without_degraded_mode():
    error = ExtractorError("PROVIDER_ERROR", "safe")
    error.configuration_error = True
    primary, fallback = Fake(error), Fake(valid_reply())
    with pytest.raises(ExtractorError) as failure:
        asyncio.run(router(primary, fallback).extract(payload()))
    assert failure.value.configuration_error
    assert failure.value.failures[0].configuration_error
    assert not fallback.calls
    asyncio.run(router(primary, fallback, allow_degraded=True).extract(payload()))
    assert len(fallback.calls) == 1


def test_turn_contract_downgrade_is_rejected_before_applying_and_fallback_matches_version():
    from app.contracts.turn import TurnInput
    projection = TurnInput.model_validate({**payload(), "contract_version": "parrotgo-turn-3",
        "occurred_at": "2026-10-03T10:00:00+07:00"})
    def reply(version):
        return ModelReply(text=json.dumps({"contract_version": version, "speech_status": "clear",
            "booking_acts": [], "questions": [], "inquiry_actions": [],
            "conversational_acts": ["greeting"], "travel_party": None, "location_decisions": []}))
    primary, fallback = Fake(reply("parrotgo-turn-2")), Fake(reply("parrotgo-turn-3"))
    result = asyncio.run(router(primary, fallback).extract(projection))
    assert result.contract_version == "parrotgo-turn-3"
    assert len(primary.calls) == len(fallback.calls) == 1


def test_input_invalid_before_http_never_falls_back_or_consumes_budget():
    primary, fallback = Fake(valid_reply()), Fake(valid_reply())
    budget = CallBudget()
    with pytest.raises(ExtractorError) as error:
        asyncio.run(router(primary, fallback).extract({"utterance": "wrong"}, runtime=ExtractorRuntime(client=primary, call_budget=budget)))
    assert error.value.code == "INPUT_INVALID"
    assert budget.used == 0
    assert not primary.calls and not fallback.calls


def test_both_invalid_keep_failure_causes_without_third_attempt():
    primary, fallback = Fake(ModelReply(text="invalid")), Fake(ModelReply(text="invalid"))
    with pytest.raises(ExtractorError) as error:
        asyncio.run(router(primary, fallback).extract(payload()))
    assert error.value.code == "EXTRACTION_UNAVAILABLE"
    assert [f.code for f in error.value.failures] == ["OUTPUT_INVALID", "OUTPUT_INVALID"]
    assert len(primary.calls) == len(fallback.calls) == 1


def test_local_quota_does_not_consume_http_budget_but_remote_quota_does():
    primary, fallback = Fake(RateLimitError(30, provider="Groq", request_sent=False)), Fake(valid_reply())
    budget = CallBudget(limit=1)
    asyncio.run(router(primary, fallback).extract(payload(), runtime=ExtractorRuntime(client=primary, call_budget=budget)))
    assert budget.used == 1
    assert len(fallback.calls) == 1
    primary = Fake(RateLimitError(30, provider="Groq", request_sent=True))
    budget = CallBudget()
    asyncio.run(router(primary, fallback).extract(payload(), runtime=ExtractorRuntime(client=primary, call_budget=budget)))
    assert budget.used == 2


def test_both_quota_defer_until_first_available_provider_and_mixed_failure_is_preserved():
    primary, fallback = Fake(RateLimitError(40)), Fake(RateLimitError(12))
    with pytest.raises(RateLimitError) as error:
        asyncio.run(router(primary, fallback).extract(payload()))
    assert error.value.retry_after_seconds == 12
    assert len(error.value.failures) == 2
    fallback = Fake(ExtractorError("PROVIDER_UNAVAILABLE", "safe", retryable=True))
    with pytest.raises(ExtractorError) as error:
        asyncio.run(router(primary, fallback).extract(payload()))
    assert error.value.code == "EXTRACTION_UNAVAILABLE" and error.value.retryable
    assert [f.code for f in error.value.failures] == ["RATE_LIMITED", "PROVIDER_UNAVAILABLE"]


def test_provider_timeout_leaves_shared_deadline_for_fallback():
    async def slow():
        await asyncio.sleep(2)
    primary, fallback = Fake(slow), Fake(valid_reply())
    result = asyncio.run(router(primary, fallback, deadline_seconds=.5).extract(payload()))
    assert result.dialogue_acts[0].value == "36 Hoàng Cầu"
    assert len(primary.calls) == len(fallback.calls) == 1
    assert fallback.calls[0]["timeout_seconds"] <= .2


def test_expired_deadline_and_cancellation_do_not_start_fallback():
    primary, fallback = Fake(valid_reply()), Fake(valid_reply())
    with pytest.raises(ExtractorError) as error:
        asyncio.run(router(primary, fallback).extract(payload(), runtime=ExtractorRuntime(client=primary, deadline=time.monotonic()-1)))
    assert error.value.code == "DEADLINE_EXCEEDED"
    assert not primary.calls and not fallback.calls
    primary = Fake(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(router(primary, fallback).extract(payload()))
    assert not fallback.calls


def test_quota_request_and_token_reservations_are_atomic_independent_of_gemini():
    limiter = GroqRateLimiter(bucket="parallel", rpm=40, tpm=1000)
    def reserve(_):
        try:
            limiter.reserve(100)
            return True
        except RateLimitError:
            return False
    with ThreadPoolExecutor(max_workers=30) as pool:
        assert sum(pool.map(reserve, range(50))) == 10
    # More than 15 requests is supported when token budget allows it.
    limiter = GroqRateLimiter(bucket="requests", rpm=30, tpm=100000)
    for _ in range(30):
        limiter.reserve(1)
    with pytest.raises(RateLimitError):
        limiter.reserve(1)


def test_sqlite_quota_and_cooldown_survive_restart_without_credentials(tmp_path):
    db, now = tmp_path / "groq.sqlite", [100.]
    limiter = GroqRateLimiter(bucket="hashed-bucket", rpm=2, tpm=1000, sqlite_path=db, clock=lambda: now[0])
    reservation = limiter.reserve(100)
    limiter.reconcile(reservation, 900)
    restarted = GroqRateLimiter(bucket="hashed-bucket", rpm=2, tpm=1000, sqlite_path=db, clock=lambda: now[0])
    with pytest.raises(RateLimitError):
        restarted.reserve(101)
    now[0] = 160
    restarted.reserve(10)
    restarted.cooldown(5)
    with pytest.raises(RateLimitError) as error:
        limiter.reserve(1)
    assert error.value.retry_after_seconds == 5
    now[0] = 165
    limiter.reserve(1)
    assert b"offline-key" not in db.read_bytes()


def test_remote_429_cooldown_prevents_second_http_request(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "9"}, text="private body")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = GroqExtractorClient(api_key="offline-key", client=http,
                limiter=GroqRateLimiter(bucket="429", sqlite_path=tmp_path/"groq.sqlite"))
            for request_sent in [True, False]:
                with pytest.raises(RateLimitError) as error:
                    await provider.generate(system_prompt="safe", input_json="{}", response_schema={}, model=DEFAULT_GROQ_MODEL, timeout_seconds=1)
                assert error.value.request_sent is request_sent
                assert error.value.retry_after_seconds == 9
    asyncio.run(run())
    assert len(requests) == 1


@pytest.mark.parametrize("value,seconds", [("2m59.56s", 179.56), ("7.66s", 7.66), ("1h2m", 3720), ("0", 60), ("NaN", 60), ("invalid", 60)])
def test_groq_reset_headers_are_bounded_and_parsed(value, seconds):
    assert _reset_seconds(value) == seconds
