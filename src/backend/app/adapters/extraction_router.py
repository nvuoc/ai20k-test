"""Provider routing surrounds extraction and its full business validation.

Each provider receives the original input and one attempt, with a shared budget
and monotonic deadline. Invalid primary output never enters the fallback prompt.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from typing import Any

from .extractor import (
    CallBudget,
    ExtractorClient,
    ExtractorError,
    ExtractorRuntime,
    llm_extractor_func,
)
from .rate_limit import RateLimitError


@dataclass(frozen=True)
class ProviderAttempt:
    name: str
    client: ExtractorClient
    model: str
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not self.name or not self.model or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("provider name, model and positive timeout are required")


@dataclass(frozen=True)
class ProviderFailure:
    provider: str
    code: str
    retryable: bool
    retry_after_seconds: int | None = None
    configuration_error: bool = False


FALLBACK_CODES = frozenset({
    "OUTPUT_INVALID", "MODEL_INCOMPLETE", "PROVIDER_TIMEOUT", "PROVIDER_UNAVAILABLE",
    "PROVIDER_ERROR", "RATE_LIMITED",
})
CONFIG_CODES = frozenset({"PROVIDER_AUTH_ERROR", "PROVIDER_MODEL_UNAVAILABLE", "PROVIDER_CONFIG_ERROR"})


class ExtractionRouter:
    def __init__(self, providers: list[ProviderAttempt], *, deadline_seconds: float = 25,
                 allow_degraded: bool = False) -> None:
        if not 1 <= len(providers) <= 2 or len({p.name for p in providers}) != len(providers):
            raise ValueError("configure one or two distinct providers")
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("extraction deadline must be positive")
        self.providers = tuple(providers)
        self.deadline_seconds = deadline_seconds
        self.allow_degraded = allow_degraded

    async def extract(self, projection: Any, *, runtime: ExtractorRuntime | None = None):
        budget = runtime.call_budget if runtime else CallBudget(limit=len(self.providers))
        template = runtime or ExtractorRuntime(client=self.providers[0].client, call_budget=budget)
        deadline = template.deadline if template.deadline is not None else time.monotonic() + self.deadline_seconds
        failures: list[ProviderFailure] = []
        for provider in self.providers:
            if deadline <= time.monotonic():
                error = ExtractorError("DEADLINE_EXCEEDED", "Extraction deadline has expired.", retryable=True)
                error.failures = tuple(failures)
                raise error
            if budget.remaining == 0:
                break
            before = budget.used
            try:
                return await llm_extractor_func(projection, runtime=replace(
                    template, client=provider.client, model=provider.model,
                    timeout_seconds=provider.timeout_seconds, deadline=deadline, max_attempts=1,
                ))
            except ExtractorError as error:
                if error.code == "DEADLINE_EXCEEDED":
                    error.retryable = True
                # Local quota rejection made no HTTP request and needs no HTTP allocation.
                if getattr(error, "request_sent", None) is False and budget.used > before:
                    budget.used -= 1
                configuration_error = error.code in CONFIG_CODES or bool(getattr(error, "configuration_error", False))
                failures.append(ProviderFailure(provider.name, error.code, error.retryable,
                    getattr(error, "retry_after_seconds", None), configuration_error))
                permitted = (self.allow_degraded if configuration_error else error.code in FALLBACK_CODES)
                if not permitted:
                    error.failures = tuple(failures)
                    raise
        if not failures:
            raise ExtractorError("CALL_BUDGET_EXHAUSTED", "No model calls remain for this turn.")
        if all(f.code == "RATE_LIMITED" for f in failures):
            final = RateLimitError(min(f.retry_after_seconds or 60 for f in failures), provider="LLM", request_sent=budget.used > 0)
        else:
            final = ExtractorError("EXTRACTION_UNAVAILABLE", "No provider returned a valid interpretation.",
                retryable=any(f.retryable or f.code in {"OUTPUT_INVALID", "MODEL_INCOMPLETE", "RATE_LIMITED"} for f in failures))
        final.failures = tuple(failures)
        raise final from None
