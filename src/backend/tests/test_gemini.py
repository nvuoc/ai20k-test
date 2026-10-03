"""Real HTTP wire contracts and atomic quota tests; no real keys or network."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.adapters.extractor import CallBudget, ExtractorError, ExtractorRuntime, llm_extractor_func
from app.adapters.nlu_gemini import (
    DEFAULT_GEMINI_MODEL,
    GeminiExtractorClient,
    gemini_output_schema,
)
from app.adapters.rate_limit import RateLimitError, RollingWindowRateLimiter, shared_gemini_limiter
from app.contracts.nlu import empty_booking_state, provider_output_schema


def payload() -> dict:
    return {
        "utterance": {"text": "Đón ở 36 Hoàng Cầu", "asr_confidence": None},
        "conversation_context": {
            "last_bot_message": None,
            "last_bot_action": None,
            "current_focus": None,
        },
        "booking_state": empty_booking_state().model_dump(),
        "candidates": [],
        "booking_status": "collecting_info",
    }


def answer(text: str | None = None, finish: str = "STOP") -> dict:
    if text is None:
        text = json.dumps(
            {
                "speech_status": "clear",
                "dialogue_acts": [
                    {"intent": "provide_info", "target": "pickup", "value": "36 Hoàng Cầu"}
                ],
            }
        )
    return {
        "candidates": [
            {
                "finishReason": finish,
                "content": {
                    "parts": [{"thought": True, "text": "private reasoning"}, {"text": text}]
                },
            }
        ]
    }


def limiter(*, limit: int = 15) -> RollingWindowRateLimiter:
    return RollingWindowRateLimiter(bucket="offline", limit=limit)


def test_native_wire_schema_auth_header_and_no_hidden_requests() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=answer())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = GeminiExtractorClient(
                api_key="offline-test-key", client=http, limiter=limiter()
            )
            result = await llm_extractor_func(
                payload(),
                runtime=ExtractorRuntime(
                    client=provider, model=DEFAULT_GEMINI_MODEL, call_budget=CallBudget(limit=1)
                ),
            )
            assert result.dialogue_acts[0].value == "36 Hoàng Cầu"
            await provider.close()  # Injected client remains owned by caller.
            assert not http.is_closed

    asyncio.run(scenario())
    assert len(requests) == 1
    request = requests[0]
    assert request.headers["x-goog-api-key"] == "offline-test-key"
    assert "offline-test-key" not in str(request.url)
    assert request.url.path.endswith("/models/gemini-3.5-flash-lite:generateContent")
    sent = json.loads(request.content)
    assert sent["generationConfig"]["responseFormat"]["text"]["schema"] == gemini_output_schema(
        provider_output_schema()
    )
    assert sent["generationConfig"]["responseFormat"]["text"]["mimeType"] == "APPLICATION_JSON"
    assert "tools" not in sent
    assert json.loads(sent["contents"][0]["parts"][0]["text"]) == payload()
    assert "Only" not in sent["systemInstruction"]["parts"][0]["text"]


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "PROVIDER_AUTH_ERROR"),
        (403, "PROVIDER_AUTH_ERROR"),
        (404, "PROVIDER_MODEL_UNAVAILABLE"),
        (400, "PROVIDER_ERROR"),
        (503, "PROVIDER_UNAVAILABLE"),
    ],
)
def test_http_errors_safe_without_provider_bodies(status, code):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, text="secret phone and key")
            )
        ) as http:
            provider = GeminiExtractorClient(
                api_key="offline-secret", client=http, limiter=limiter()
            )
            with pytest.raises(ExtractorError) as error:
                await provider.generate(
                    system_prompt="safe",
                    input_json="{}",
                    response_schema={},
                    model=DEFAULT_GEMINI_MODEL,
                    timeout_seconds=1,
                )
            assert error.value.code == code
            assert "secret" not in str(error.value)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body,refusal,incomplete",
    [
        (answer(finish="MAX_TOKENS"), False, True),
        (answer(finish="SAFETY"), True, False),
        ({"promptFeedback": {"blockReason": "SAFETY"}}, True, False),
    ],
)
def test_incomplete_and_safety_do_not_apply_complete_looking_json(body, refusal, incomplete):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
        ) as http:
            provider = GeminiExtractorClient(api_key="offline", client=http, limiter=limiter())
            result = await provider.generate(
                system_prompt="safe",
                input_json="{}",
                response_schema={},
                model=DEFAULT_GEMINI_MODEL,
                timeout_seconds=1,
            )
            assert bool(result.refusal) is refusal
            assert result.incomplete is incomplete
            assert result.text is None

    asyncio.run(scenario())


def test_fifteen_parallel_reservations_then_exact_rolling_expiry():
    clock = [100.0]
    quota = RollingWindowRateLimiter(bucket="clocked", clock=lambda: clock[0])

    def reserve(_):
        try:
            quota.reserve()
            return True
        except RateLimitError:
            return False

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(reserve, range(40)))
    assert sum(results) == 15
    clock[0] = 159.99
    with pytest.raises(RateLimitError) as error:
        quota.reserve()
    assert error.value.retry_after_seconds == 1
    clock[0] = 160.0
    quota.reserve()


def test_shared_quota_not_reset_by_new_client():
    a = shared_gemini_limiter("offline-shared-quota", rpm=1)
    b = shared_gemini_limiter("offline-shared-quota", rpm=15)
    assert a is b
    a.reserve()
    with pytest.raises(RateLimitError):
        b.reserve()
    assert a.limit == 1


def test_project_bucket_combines_different_keys():
    a = shared_gemini_limiter("offline-key-a", project_bucket="test-google-project")
    b = shared_gemini_limiter("offline-key-b", project_bucket="test-google-project")
    assert a is b


def test_sqlite_quota_atomic_across_instances_and_survives_restart(tmp_path):
    database = tmp_path / "quota.sqlite3"
    now = [100.0]
    a = RollingWindowRateLimiter(bucket="project", sqlite_path=database, clock=lambda: now[0])
    b = RollingWindowRateLimiter(bucket="project", sqlite_path=database, clock=lambda: now[0])

    def reserve(index):
        try:
            (a if index % 2 else b).reserve()
            return True
        except RateLimitError:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(reserve, range(30))) == 15
    c = RollingWindowRateLimiter(bucket="project", sqlite_path=database, clock=lambda: now[0])
    with pytest.raises(RateLimitError):
        c.reserve()
    now[0] += 60
    c.reserve()


def test_http_429_shared_cooldown_without_automatic_retry():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            429, headers={"Retry-After": "30"}, json={"error": {"message": "secret"}}
        )

    async def scenario():
        quota = limiter()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = GeminiExtractorClient(api_key="offline-key", client=http, limiter=quota)
            for _ in range(2):
                with pytest.raises(RateLimitError) as error:
                    await llm_extractor_func(
                        payload(),
                        runtime=ExtractorRuntime(client=provider, model=DEFAULT_GEMINI_MODEL),
                    )
                assert 1 <= error.value.retry_after_seconds <= 30

    asyncio.run(scenario())
    assert len(requests) == 1


def test_repair_counts_at_same_quota_and_fails_before_extra_http():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=answer(text="malformed json"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = GeminiExtractorClient(
                api_key="offline-key", client=http, limiter=limiter(limit=1)
            )
            with pytest.raises(RateLimitError):
                await llm_extractor_func(
                    payload(),
                    runtime=ExtractorRuntime(
                        client=provider, model=DEFAULT_GEMINI_MODEL, call_budget=CallBudget(limit=2)
                    ),
                )

    asyncio.run(scenario())
    assert len(requests) == 1


def test_failed_connection_still_consumes_quota():
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ConnectError("secret", request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = GeminiExtractorClient(
                api_key="offline", client=http, limiter=limiter(limit=1)
            )
            for _ in range(2):
                with pytest.raises(ExtractorError):
                    await llm_extractor_func(
                        payload(),
                        runtime=ExtractorRuntime(
                            client=provider,
                            model=DEFAULT_GEMINI_MODEL,
                            call_budget=CallBudget(limit=1),
                        ),
                    )

    asyncio.run(scenario())
    assert len(requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"candidates": []},
        {"candidates": [{"finishReason": "OTHER"}]},
        [],
        {"promptFeedback": []},
    ],
)
def test_malformed_provider_response_is_typed(body):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
        ) as http:
            provider = GeminiExtractorClient(api_key="offline", client=http, limiter=limiter())
            with pytest.raises(ExtractorError) as error:
                await provider.generate(
                    system_prompt="safe",
                    input_json="{}",
                    response_schema={},
                    model=DEFAULT_GEMINI_MODEL,
                    timeout_seconds=1,
                )
            assert error.value.code == "PROVIDER_ERROR"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid",
    [
        {
            "speech_status": "clear",
            "dialogue_acts": [{"intent": "provide_info", "target": "passengers", "value": True}],
        },
        {
            "speech_status": "clear",
            "dialogue_acts": [
                {
                    "intent": "provide_info",
                    "target": "luggage",
                    "value": {"count": 0, "size": "large"},
                }
            ],
        },
        {
            "speech_status": "clear",
            "dialogue_acts": [
                {"intent": "confirm", "target": None, "value": None, "confirmed": True}
            ],
        },
        {
            "speech_status": "clear",
            "dialogue_acts": [{"intent": "confirm", "target": None, "value": None}] * 25,
        },
    ],
)
def test_portable_gemini_schema_never_weakens_local_validation(invalid):
    original = payload()
    before = json.dumps(original, sort_keys=True)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=answer(text=json.dumps(invalid)))
            )
        ) as http:
            provider = GeminiExtractorClient(api_key="offline", client=http, limiter=limiter())
            with pytest.raises(ExtractorError) as error:
                await llm_extractor_func(
                    original,
                    runtime=ExtractorRuntime(
                        client=provider, model=DEFAULT_GEMINI_MODEL, call_budget=CallBudget(limit=1)
                    ),
                )
            assert error.value.code == "OUTPUT_INVALID"

    asyncio.run(scenario())
    assert json.dumps(original, sort_keys=True) == before


def test_schema_subset_retains_union_types_and_does_not_mutate_authoritative_schema():
    original = provider_output_schema()
    before = json.dumps(original, sort_keys=True)
    subset = gemini_output_schema(original)
    assert json.dumps(original, sort_keys=True) == before
    union = subset["properties"]["dialogue_acts"]["items"]["properties"]["value"]["anyOf"]
    assert {item["type"] for item in union} == {"string", "integer", "object", "array", "null"}
    assert original["additionalProperties"] is False
    assert "additionalProperties" not in subset
