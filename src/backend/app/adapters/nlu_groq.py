"""Groq native HTTP extraction with strict JSON Schema and one HTTP request.

Wire contract: https://console.groq.com/docs/structured-outputs and
https://console.groq.com/docs/api-reference . No SDK retry, tools or streaming.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import httpx

from .extractor import ExtractorError, ModelReply
from .groq_rate_limit import GroqRateLimiter, shared_groq_limiter
from .rate_limit import RateLimitError

DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def _reset_seconds(value: str | None) -> float:
    if not value:
        return 60.0
    try:
        seconds = float(value)
    except ValueError:
        matches = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", value)
        if not matches or "".join(number + unit for number, unit in matches) != value:
            return 60.0
        units = {"ms": .001, "s": 1, "m": 60, "h": 3600, "d": 86400}
        seconds = sum(float(number) * units[unit] for number, unit in matches)
    return min(seconds, 86400.0) if math.isfinite(seconds) and seconds > 0 else 60.0


class GroqExtractorClient:
    def __init__(self, *, api_key: str, rpm: int = 30, tpm: int = 8000,
                 limiter_path: str | Path | None = None, limiter: GroqRateLimiter | None = None,
                 client: httpx.AsyncClient | None = None, base_url: str = GROQ_BASE_URL,
                 model: str = DEFAULT_GROQ_MODEL, reasoning_effort: str = "low",
                 max_output_tokens: int = 4096):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("a Groq API key is required")
        if reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError("Groq reasoning effort must be low, medium or high")
        if type(max_output_tokens) is not int or max_output_tokens < 1:
            raise ValueError("Groq maximum output tokens must be positive")
        self._api_key = api_key
        self._limiter = limiter or shared_groq_limiter(api_key, rpm=rpm, tpm=tpm,
            sqlite_path=limiter_path, model=model)
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self._base_url = base_url.rstrip("/")
        self._reasoning_effort = reasoning_effort
        self._max_output_tokens = max_output_tokens

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def generate(self, *, system_prompt: str, input_json: str,
                       response_schema: dict[str, Any], model: str, timeout_seconds: float) -> ModelReply:
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", model) or ".." in model:
            raise ExtractorError("PROVIDER_CONFIG_ERROR", "Groq model identifier is invalid.")
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": input_json}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "parrotgo_interpretation", "strict": True, "schema": response_schema,
            }},
            "reasoning_effort": self._reasoning_effort, "temperature": 0,
            "max_completion_tokens": self._max_output_tokens, "stream": False, "n": 1,
        }
        # Planning estimate only; responses reconcile actual total token usage.
        estimated = math.ceil(len((system_prompt + input_json).encode("utf-8")) / 4) + 512
        reservation = self._limiter.reserve(estimated)
        try:
            response = await self._client.post(f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload, timeout=timeout_seconds)
        except httpx.TimeoutException:
            raise ExtractorError("PROVIDER_TIMEOUT", "Groq request timed out.", retryable=True) from None
        except httpx.HTTPError:
            raise ExtractorError("PROVIDER_UNAVAILABLE", "Groq connection failed.", retryable=True) from None
        if response.status_code == 429:
            wait = _reset_seconds(response.headers.get("Retry-After"))
            self._limiter.cooldown(wait)
            raise RateLimitError(wait, provider="Groq", request_sent=True)
        for resource in ("requests", "tokens"):
            try:
                exhausted = float(response.headers.get(f"x-ratelimit-remaining-{resource}", "1")) <= 0
            except ValueError:
                exhausted = False
            if exhausted:
                self._limiter.cooldown(_reset_seconds(response.headers.get(f"x-ratelimit-reset-{resource}")))
        if not response.is_success:
            status = response.status_code
            code = ("PROVIDER_AUTH_ERROR" if status in {401, 403} else
                    "PROVIDER_MODEL_UNAVAILABLE" if status == 404 else
                    "PROVIDER_UNAVAILABLE" if status in {408, 409} or status >= 500 else
                    "PROVIDER_CONFIG_ERROR")
            raise ExtractorError(code, "Groq rejected the model request.",
                retryable=status in {408, 409} or status >= 500) from None
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("response object required")
            total = body.get("usage", {}).get("total_tokens")
            if type(total) is int and total >= 0:
                self._limiter.reconcile(reservation, total)
            choices = body.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("one choice required")
            choice = choices[0]
            message = choice["message"]
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                return ModelReply(refusal="Groq refused extraction.")
            if choice.get("finish_reason") == "length":
                return ModelReply(incomplete=True)
            if choice.get("finish_reason") != "stop" or not isinstance(message.get("content"), str):
                raise ValueError("completed text required")
            return ModelReply(text=message["content"])
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ExtractorError("PROVIDER_ERROR", "Groq response was invalid.") from None
