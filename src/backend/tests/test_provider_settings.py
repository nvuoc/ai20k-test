"""Startup/configuration coverage without loading local keys or external calls."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import config
from app.adapters.extractor import ExtractorError, ModelReply
from app.config import Settings
from app.runtime import conversation_runtime


def isolate_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)
    env = {"APP_PROFILE": "fixture_demo", "APP_SECRET": "test-secret",
        "BUSINESS_DB_PATH": str(tmp_path/"business.sqlite"),
        "CHECKPOINT_DB_PATH": str(tmp_path/"checkpoints.sqlite"),
        "GEMINI_RATE_DB_PATH": str(tmp_path/"gemini.sqlite"),
        "GROQ_RATE_DB_PATH": str(tmp_path/"groq.sqlite"),
        "GROQ_API_KEY": "offline-groq", "GEMINI_API_KEY": "offline-gemini",
        "GROQ_MODEL": "openai/gpt-oss-120b", "GEMINI_MODEL": "gemini-3.5-flash-lite",
        "LLM_FALLBACK_ENABLED": "true", "LLM_ALLOW_DEGRADED": "false",
        "LLM_TIMEOUT_SECONDS": "25", "GROQ_TIMEOUT_SECONDS": "8", "GEMINI_TIMEOUT_SECONDS": "12",
        "MAX_LLM_CALLS_PER_TURN": "2", "GROQ_RPM": "30", "GROQ_TPM": "8000",
        "GROQ_REASONING_EFFORT": "low", "GEMINI_RPM": "15", "TRAFFIC_TTL_SECONDS": "60",
        "LOCATION_CONFIRMATION_ENABLED": "true", "AREA_ASSISTANCE_ENABLED": "false",
        "ASSISTANCE_POLICY_PATH": "", "SERVICE_AREA_PATH": "", "LOCAL_ALIAS_PATH": "",
        "PRICING_PATH": "", "VEHICLE_CATALOG_PATH": ""}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)


def test_loaded_defaults_select_groq_with_gemini_fallback_without_exposing_secrets(monkeypatch, tmp_path):
    isolate_env(monkeypatch, tmp_path)
    settings = Settings.load()
    assert settings.llm_provider == "groq"
    assert settings.model == "openai/gpt-oss-120b"
    assert settings.gemini_model == "gemini-3.5-flash-lite"
    assert settings.max_llm_calls == 2
    assert settings.location_confirmation_enabled
    assert not settings.area_assistance_enabled
    public = json.dumps(settings.public())
    assert "offline-groq" not in public and "offline-gemini" not in public
    assert not Settings().location_confirmation_enabled


def test_existing_explicit_gemini_config_keeps_single_provider_budget(monkeypatch, tmp_path):
    isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("MAX_LLM_CALLS_PER_TURN", "1")
    settings = Settings.load()
    assert settings.llm_provider == "gemini"
    assert settings.model == "gemini-3.5-flash-lite"
    assert settings.max_llm_calls == 1


@pytest.mark.parametrize("key,value", [("GROQ_TPM", "0"), ("GROQ_RPM", "-1"),
    ("GROQ_TIMEOUT_SECONDS", "nan"), ("GEMINI_TIMEOUT_SECONDS", "0"),
    ("LLM_TIMEOUT_SECONDS", "inf"), ("GROQ_REASONING_EFFORT", "unsupported"),
    ("MAX_LLM_CALLS_PER_TURN", "1")])
def test_invalid_provider_configuration_rejected_before_network(monkeypatch, tmp_path, key, value):
    isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        Settings.load()


def fake_runtime(monkeypatch, primary_result):
    from app import runtime
    from app.adapters import nlu_gemini, nlu_groq
    created = []

    class Provider:
        def __init__(self, **kwargs):
            self.kwargs, self.calls, self.closed = kwargs, [], False
            created.append(self)

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            if self.kwargs["api_key"] == "offline-groq" and isinstance(primary_result, Exception):
                raise primary_result
            return ModelReply(text=json.dumps({"speech_status": "clear", "dialogue_acts": [
                {"intent": "provide_info", "target": "pickup", "value": "36 Hoàng Cầu"}]}))

        async def close(self):
            self.closed = True

    class Engine:
        def __init__(self, **kwargs):
            self.extractor, self.kwargs = kwargs["extractor"], kwargs

    class Graph:
        def __init__(self, engine, path):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Booking:
        def __init__(self, path):
            pass

        def close(self):
            pass

    monkeypatch.setattr(nlu_groq, "GroqExtractorClient", Provider)
    monkeypatch.setattr(nlu_gemini, "GeminiExtractorClient", Provider)
    monkeypatch.setattr(runtime, "ConversationEngine", Engine)
    monkeypatch.setattr(runtime, "DurableGraph", Graph)
    monkeypatch.setattr(runtime, "SandboxBookingProvider", Booking)
    return created


def test_runtime_uses_groq_then_gemini_and_closes_both(monkeypatch, tmp_path):
    created = fake_runtime(monkeypatch, ExtractorError("PROVIDER_UNAVAILABLE", "safe", retryable=True))
    settings = Settings(profile="chat_sandbox", maps_provider="fixture", weather_provider="disabled",
        groq_key="offline-groq", gemini_key="offline-gemini", database_path=tmp_path/"business.sqlite")
    from test_groq_router import payload

    async def run():
        async with conversation_runtime(settings) as (engine, _):
            result = await engine.extractor(payload())
            assert result.dialogue_acts[0].target == "pickup"
            assert "area_assistance_enabled" not in engine.kwargs
            assert engine.kwargs["kb"].tariff("oto_4_cho")["final_amount_basis"] == "meter"
            assert engine.kwargs["crm"] is not None and engine.kwargs["mega_pois"] is not None
    asyncio.run(run())
    assert len(created) == 2
    assert created[0].calls[0]["model"] == "openai/gpt-oss-120b"
    assert created[1].calls[0]["model"] == "gemini-3.5-flash-lite"
    assert all(client.closed for client in created)


def test_runtime_missing_primary_key_blocks_until_explicit_degraded_mode(monkeypatch, tmp_path):
    created = fake_runtime(monkeypatch, None)
    settings = Settings(profile="chat_sandbox", maps_provider="fixture", weather_provider="disabled",
        groq_key="", gemini_key="offline-gemini", database_path=tmp_path/"business.sqlite")
    async def run():
        async with conversation_runtime(settings):
            pytest.fail("missing Groq key must block startup")
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        asyncio.run(run())
    assert not created
    from dataclasses import replace
    async def degraded():
        async with conversation_runtime(replace(settings, llm_allow_degraded=True)):
            pass
    asyncio.run(degraded())
    assert len(created) == 1 and created[0].kwargs["api_key"] == "offline-gemini"
    assert created[0].closed


def test_runtime_explicit_gemini_uses_gemini_model_with_direct_settings(monkeypatch, tmp_path):
    created = fake_runtime(monkeypatch, None)
    settings = Settings(profile="chat_sandbox", llm_provider="gemini", gemini_key="offline-gemini",
        maps_provider="fixture", weather_provider="disabled", database_path=tmp_path/"business.sqlite")
    from test_groq_router import payload
    async def run():
        async with conversation_runtime(settings) as (engine, _):
            await engine.extractor(payload())
    asyncio.run(run())
    assert len(created) == 1
    assert created[0].calls[0]["model"] == "gemini-3.5-flash-lite"
