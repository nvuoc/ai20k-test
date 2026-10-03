"""Server-owned configuration; secrets never enter public projections."""
from __future__ import annotations

import math
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    profile: str = "fixture_demo"
    secret: str = ""
    database_path: Path = ROOT / "data/app.sqlite"
    checkpoint_path: Path = ROOT / "data/checkpoints.sqlite"
    gemini_rate_path: Path = ROOT / "data/gemini_rate.sqlite"
    llm_provider: str = "groq"
    gemini_key: str = ""
    model: str = "openai/gpt-oss-120b"
    gemini_model: str = "gemini-3.5-flash-lite"
    groq_key: str = ""
    groq_rpm: int = 30
    groq_tpm: int = 8000
    groq_rate_path: Path = ROOT / "data/groq_rate.sqlite"
    groq_timeout: float = 8
    gemini_timeout: float = 12
    groq_reasoning_effort: str = "low"
    llm_fallback_enabled: bool = True
    llm_allow_degraded: bool = False
    gemini_rpm: int = 15
    llm_timeout: float = 25
    max_llm_calls: int = 2
    maps_provider: str = "fixture"
    vietmap_key: str = ""
    quote_ttl: int = 120
    brand_name: str = "ParrotGo"
    weather_provider: str = "open_meteo"
    open_meteo_key: str = ""
    weather_cache_seconds: int = 900
    inquiry_ttl: int = 600
    read_timeout: float = 5
    read_deadline: float = 15
    vehicle_catalog_path: Path | None = None
    pricing_path: Path | None = None
    local_alias_path: Path | None = None
    location_confirmation_enabled: bool = False
    area_assistance_enabled: bool = False
    assistance_policy_path: Path | None = None
    service_area_path: Path | None = None
    traffic_enabled: bool = True
    traffic_ttl_seconds: int = 60
    cookie_secure: bool = False
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:8000", "http://localhost:8000",
        "http://127.0.0.1:5173", "http://localhost:5173",
    )

    @classmethod
    def load(cls) -> Settings:
        # Tests explicitly pass Settings and never read local credentials.
        load_dotenv(ROOT / ".env", override=False)
        load_dotenv(ROOT.parents[1] / ".env", override=False)
        profile = os.getenv("APP_PROFILE", "chat_sandbox")
        data_path = Path(os.getenv("BUSINESS_DB_PATH", "data/app.sqlite"))
        checkpoint_path = Path(os.getenv("CHECKPOINT_DB_PATH", "data/checkpoints.sqlite"))
        rate_path = Path(os.getenv("GEMINI_RATE_DB_PATH", "data/gemini_rate.sqlite"))
        groq_rate_path = Path(os.getenv("GROQ_RATE_DB_PATH", "data/groq_rate.sqlite"))
        data_path = data_path if data_path.is_absolute() else ROOT / data_path
        checkpoint_path = (
            checkpoint_path if checkpoint_path.is_absolute() else ROOT / checkpoint_path
        )
        rate_path = rate_path if rate_path.is_absolute() else ROOT / rate_path
        groq_rate_path = groq_rate_path if groq_rate_path.is_absolute() else ROOT / groq_rate_path
        llm_provider = os.getenv("LLM_PROVIDER", "groq").lower()
        gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        secret = os.getenv("APP_SECRET", "")
        if not secret:
            secret_path = data_path.parent / ".app_secret"
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            if not secret_path.exists():
                secret_path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
            secret = secret_path.read_text(encoding="utf-8").strip()
        settings = cls(
            profile=profile, secret=secret, database_path=data_path,
            checkpoint_path=checkpoint_path,
            gemini_rate_path=rate_path,
            llm_provider=llm_provider,
            gemini_key=os.getenv("GEMINI_API_KEY", ""),
            model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b") if llm_provider == "groq" else gemini_model,
            gemini_model=gemini_model,
            groq_key=os.getenv("GROQ_API_KEY", ""),
            groq_rpm=int(os.getenv("GROQ_RPM", "30")),
            groq_tpm=int(os.getenv("GROQ_TPM", "8000")),
            groq_rate_path=groq_rate_path,
            groq_timeout=float(os.getenv("GROQ_TIMEOUT_SECONDS", "8")),
            gemini_timeout=float(os.getenv("GEMINI_TIMEOUT_SECONDS", "12")),
            groq_reasoning_effort=os.getenv("GROQ_REASONING_EFFORT", "low"),
            llm_fallback_enabled=os.getenv("LLM_FALLBACK_ENABLED", "true").lower() == "true",
            llm_allow_degraded=os.getenv("LLM_ALLOW_DEGRADED", "false").lower() == "true",
            gemini_rpm=int(os.getenv("GEMINI_RPM", "15")),
            llm_timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "25")),
            max_llm_calls=int(os.getenv("MAX_LLM_CALLS_PER_TURN", "2")),
            maps_provider=os.getenv("MAPS_PROVIDER", "vietmap"),
            vietmap_key=os.getenv("VIETMAP_API_KEY", ""),
            quote_ttl=int(os.getenv("QUOTE_TTL_SECONDS", "120")),
            brand_name=os.getenv("BRAND_NAME", "ParrotGo"),
            weather_provider=os.getenv("WEATHER_PROVIDER", "open_meteo"),
            open_meteo_key=os.getenv("OPEN_METEO_API_KEY", ""),
            weather_cache_seconds=int(os.getenv("WEATHER_CACHE_SECONDS", "900")),
            inquiry_ttl=int(os.getenv("INQUIRY_TTL_SECONDS", "600")),
            read_timeout=float(os.getenv("READ_TIMEOUT_SECONDS", "5")),
            read_deadline=float(os.getenv("READ_DEADLINE_SECONDS", "15")),
            vehicle_catalog_path=Path(os.environ["VEHICLE_CATALOG_PATH"]).resolve() if os.getenv("VEHICLE_CATALOG_PATH") else None,
            pricing_path=Path(os.environ["PRICING_PATH"]).resolve() if os.getenv("PRICING_PATH") else None,
            local_alias_path=Path(os.environ["LOCAL_ALIAS_PATH"]).resolve() if os.getenv("LOCAL_ALIAS_PATH") else None,
            location_confirmation_enabled=os.getenv("LOCATION_CONFIRMATION_ENABLED", "true").lower() == "true",
            area_assistance_enabled=os.getenv("AREA_ASSISTANCE_ENABLED", "false").lower() == "true",
            assistance_policy_path=Path(os.environ["ASSISTANCE_POLICY_PATH"]).resolve() if os.getenv("ASSISTANCE_POLICY_PATH") else None,
            service_area_path=Path(os.environ["SERVICE_AREA_PATH"]).resolve() if os.getenv("SERVICE_AREA_PATH") else None,
            traffic_enabled=os.getenv("TRAFFIC_ENABLED", "true").lower() == "true",
            traffic_ttl_seconds=int(os.getenv("TRAFFIC_TTL_SECONDS", "60")),
            cookie_secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
            allowed_origins=tuple(x.strip() for x in os.getenv(
                "ALLOWED_ORIGINS", ",".join(cls.allowed_origins)
            ).split(",") if x.strip()),
        )
        if settings.profile not in {"fixture_demo", "chat_sandbox", "test"}:
            raise ValueError("APP_PROFILE must be fixture_demo, chat_sandbox or test")
        if not 1 <= settings.gemini_rpm <= 15:
            raise ValueError("GEMINI_RPM must be between 1 and 15")
        if settings.max_llm_calls not in {1, 2}:
            raise ValueError("MAX_LLM_CALLS_PER_TURN must be 1 or 2")
        if settings.llm_provider not in {"groq", "gemini"}:
            raise ValueError("LLM_PROVIDER must be groq or gemini")
        if min(settings.groq_rpm, settings.groq_tpm, settings.traffic_ttl_seconds) <= 0:
            raise ValueError("Groq quotas and traffic TTL must be positive")
        if any(not math.isfinite(value) or value <= 0 for value in (settings.llm_timeout, settings.groq_timeout, settings.gemini_timeout)):
            raise ValueError("LLM deadlines and provider timeouts must be positive finite numbers")
        if settings.groq_reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError("GROQ_REASONING_EFFORT must be low, medium or high")
        if settings.llm_provider == "groq" and settings.llm_fallback_enabled and settings.max_llm_calls != 2:
            raise ValueError("Groq fallback requires MAX_LLM_CALLS_PER_TURN=2")
        if settings.weather_provider not in {"open_meteo", "fixture", "disabled"}:
            raise ValueError("WEATHER_PROVIDER must be open_meteo, fixture or disabled")
        if min(settings.inquiry_ttl, settings.weather_cache_seconds, settings.read_timeout, settings.read_deadline) <= 0:
            raise ValueError("TTL and read deadlines must be positive")
        return settings

    def public(self) -> dict:
        offline = self.profile in {"fixture_demo", "test"}
        return {
            "api_version": "chat-api-3" if self.location_confirmation_enabled else "chat-api-2", "mode": "sandbox", "profile": self.profile,
            "brand_name": self.brand_name,
            "weather_provider": "fixture" if offline and self.weather_provider != "disabled" else self.weather_provider,
            "llm_provider": "fixture" if offline else self.llm_provider,
            "model": None if offline else (self.gemini_model if self.llm_provider == "gemini" and not self.model.startswith("gemini") else self.model),
            "fallback_provider": "gemini" if not offline and self.llm_provider == "groq" and self.llm_fallback_enabled else None,
            "fallback_model": self.gemini_model if not offline and self.llm_provider == "groq" and self.llm_fallback_enabled else None,
            "degraded_mode_enabled": self.llm_allow_degraded,
            "maps_provider": "fixture" if offline else self.maps_provider,
            "booking_provider": "sandbox", "gemini_rpm": self.gemini_rpm,
            "capabilities": {"asap": True, "scheduled": False, "multi_stop": False,
                "inquiry_v2": True, "address_auto_accept_v2": True, "local_address_v2": True,
                "location_confirmation": self.location_confirmation_enabled,
                "area_estimate": bool(self.service_area_path) or offline,
                "area_assistance": self.area_assistance_enabled and bool(self.assistance_policy_path),
                "text_only_chat": True,
                "electric_motorbike": False, "weather": self.weather_provider != "disabled"},
        }
