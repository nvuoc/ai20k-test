"""Shared runtime for HTTP chat, direct text calls and command-line use."""

from __future__ import annotations

from contextlib import asynccontextmanager

from app.adapters.booking_sandbox import SandboxBookingProvider
from app.adapters.extraction_router import ExtractionRouter, ProviderAttempt
from app.adapters.extractor import CallBudget, ExtractorRuntime, llm_extractor_func
from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.nlu_fixture import FixtureExtractorClient
from app.adapters.quote_fixture import QuoteAdapter
from app.config import Settings
from app.domain.conversation import ConversationEngine
from app.graph.builder import DurableGraph


@asynccontextmanager
async def conversation_runtime(settings: Settings):
    offline = settings.profile in {"fixture_demo", "test"}
    clients = []
    sandbox = None
    router = None
    try:
        if offline:
            client = FixtureExtractorClient()
            maps = FixtureMapAdapter(alias_path=settings.local_alias_path, service_area_path=settings.service_area_path)
        else:
            from app.adapters.nlu_gemini import GeminiExtractorClient
            from app.adapters.nlu_groq import GroqExtractorClient

            providers = []
            if settings.llm_provider == "groq":
                if settings.groq_key:
                    client = GroqExtractorClient(api_key=settings.groq_key,
                        rpm=settings.groq_rpm, tpm=settings.groq_tpm,
                        limiter_path=settings.groq_rate_path, model=settings.model,
                        reasoning_effort=settings.groq_reasoning_effort)
                    clients.append(client)
                    providers.append(ProviderAttempt("groq", client, settings.model, settings.groq_timeout))
                elif not settings.llm_allow_degraded:
                    raise RuntimeError("LLM_PROVIDER=groq requires GROQ_API_KEY; explicit Gemini-only or LLM_ALLOW_DEGRADED=true is supported")
                if settings.llm_fallback_enabled:
                    if not settings.gemini_key:
                        raise RuntimeError("Groq fallback requires GEMINI_API_KEY or LLM_FALLBACK_ENABLED=false")
                    fallback = GeminiExtractorClient(api_key=settings.gemini_key,
                        rpm=settings.gemini_rpm, limiter_path=settings.gemini_rate_path)
                    clients.append(fallback)
                    providers.append(ProviderAttempt("gemini", fallback, settings.gemini_model, settings.gemini_timeout))
            elif settings.llm_provider == "gemini":
                if not settings.gemini_key:
                    raise RuntimeError("LLM_PROVIDER=gemini requires GEMINI_API_KEY")
                client = GeminiExtractorClient(api_key=settings.gemini_key,
                    rpm=settings.gemini_rpm, limiter_path=settings.gemini_rate_path)
                clients.append(client)
                model = settings.model if settings.model.startswith("gemini") else settings.gemini_model
                providers.append(ProviderAttempt("gemini", client, model, settings.gemini_timeout))
            else:
                raise RuntimeError("LLM_PROVIDER must be groq or gemini")
            if not providers:
                raise RuntimeError("No configured model provider is available")
            if len(providers) > settings.max_llm_calls:
                raise RuntimeError("Configured fallback requires MAX_LLM_CALLS_PER_TURN=2")
            router = ExtractionRouter(providers, deadline_seconds=settings.llm_timeout,
                allow_degraded=settings.llm_allow_degraded)
            client = providers[0].client
            if settings.maps_provider == "vietmap":
                if not settings.vietmap_key:
                    raise RuntimeError("MAPS_PROVIDER=vietmap requires VIETMAP_API_KEY")
                from app.adapters.map_vietmap import VietMapAdapter

                maps = VietMapAdapter(
                    api_key=settings.vietmap_key,
                    timeout_seconds=settings.read_timeout,
                    alias_path=settings.local_alias_path,
                    service_area_path=settings.service_area_path,
                    traffic_enabled=settings.traffic_enabled,
                    traffic_ttl_seconds=settings.traffic_ttl_seconds,
                )
            elif settings.maps_provider == "fixture":
                maps = FixtureMapAdapter(alias_path=settings.local_alias_path, service_area_path=settings.service_area_path)
            else:
                raise RuntimeError("MAPS_PROVIDER must be fixture or vietmap")
        if client not in clients:
            clients.append(client)
        clients.append(maps)
        weather = None
        if settings.weather_provider == "open_meteo" and not offline:
            from app.adapters.weather_open_meteo import OpenMeteoWeatherAdapter

            weather = OpenMeteoWeatherAdapter(
                api_key=settings.open_meteo_key,
                timeout_seconds=settings.read_timeout,
                cache_seconds=settings.weather_cache_seconds,
            )
            clients.append(weather)
        elif settings.weather_provider == "fixture" or (
            offline and settings.weather_provider != "disabled"
        ):
            from app.adapters.weather_fixture import FixtureWeatherAdapter

            weather = FixtureWeatherAdapter()

        async def extractor(projection):
            runtime = ExtractorRuntime(
                    client=client,
                    model=settings.model,
                    timeout_seconds=settings.llm_timeout,
                    call_budget=CallBudget(limit=settings.max_llm_calls),
                    vehicle_codes=frozenset({"oto_4_cho", "oto_7_cho", "xe_may", "xe_may_dien"}),
                )
            if router:
                return await router.extract(projection, runtime=runtime)
            return await llm_extractor_func(projection, runtime=runtime)

        sandbox = SandboxBookingProvider(settings.database_path)
        engine = ConversationEngine(
            extractor=extractor,
            maps=maps,
            booking=sandbox,
            quote=QuoteAdapter(
                ttl_seconds=settings.quote_ttl,
                catalog_path=settings.vehicle_catalog_path,
                pricing_path=settings.pricing_path,
            ),
            quote_ttl_seconds=settings.quote_ttl,
            weather=weather,
            brand_name=settings.brand_name,
            inquiry_ttl=settings.inquiry_ttl,
            read_deadline=settings.read_deadline,
            location_confirmation=settings.location_confirmation_enabled,
            assistance_policy_path=settings.assistance_policy_path,
            area_assistance_enabled=settings.area_assistance_enabled,
            service_area_path=settings.service_area_path,
        )
        async with DurableGraph(engine, settings.checkpoint_path) as graph:
            yield engine, graph
    finally:
        for client in clients:
            close = getattr(client, "aclose", None) or getattr(client, "close", None)
            if close:
                await close()
        if sandbox is not None:
            sandbox.close()
