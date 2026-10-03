"""Native Gemini generateContent HTTP adapter; one request per generate call.

Reference: https://ai.google.dev/api/generate-content and
https://ai.google.dev/gemini-api/docs/generate-content/structured-output .
Uses the current responseFormat JSON Schema interface, with validation still
performed by the extractor. No automatic transport retry or booking tools.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import httpx

from .extractor import ExtractorError, ModelReply
from .rate_limit import RateLimitError, RollingWindowRateLimiter, shared_gemini_limiter

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def gemini_output_schema(schema: Any) -> Any:
    """Copy the provider's portable subset; enforce full limits locally.

    The configured Flash-Lite endpoint rejects the full generic value union
    with these structural constraints (HTTP 400), but accepts its types,
    enum, anyOf and required shape. Preserve the authoritative local schema.
    """
    if isinstance(schema, dict):
        return {
            key: gemini_output_schema(value)
            for key, value in schema.items()
            if key not in {"additionalProperties", "minLength", "maxLength", "minimum", "maxItems"}
        }
    if isinstance(schema, list):
        return [gemini_output_schema(value) for value in schema]
    return schema


class GeminiExtractorClient:
    def __init__(
        self,
        *,
        api_key: str,
        rpm: int = 15,
        limiter_path: str | Path | None = None,
        project_bucket: str | None = None,
        client: httpx.AsyncClient | None = None,
        limiter: RollingWindowRateLimiter | None = None,
        base_url: str = GEMINI_BASE_URL,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("a Gemini API key is required")
        self._api_key = api_key
        self._limiter = limiter or shared_gemini_limiter(
            api_key, rpm=rpm, sqlite_path=limiter_path, project_bucket=project_bucket
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._base_url = base_url.rstrip("/")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> GeminiExtractorClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def generate(
        self,
        *,
        system_prompt: str,
        input_json: str,
        response_schema: dict[str, Any],
        model: str,
        timeout_seconds: float,
    ) -> ModelReply:
        model_id = model.removeprefix("models/")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", model_id):
            raise ExtractorError("PROVIDER_ERROR", "Gemini model identifier is invalid.")
        self._limiter.reserve()
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": input_json}]}],
            "generationConfig": {
                "responseFormat": {
                    # Current REST TextResponseFormat requires the enum; the
                    # guide's application/json example is not accepted on wire.
                    "text": {
                        "mimeType": "APPLICATION_JSON",
                        "schema": gemini_output_schema(response_schema),
                    }
                },
                "temperature": 0,
                "maxOutputTokens": 4096,
                "candidateCount": 1,
            },
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/models/{model_id}:generateContent",
                headers={"x-goog-api-key": self._api_key},
                json=payload,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException:
            raise ExtractorError(
                "PROVIDER_TIMEOUT", "Gemini request timed out.", retryable=True
            ) from None
        except httpx.HTTPError:
            raise ExtractorError(
                "PROVIDER_UNAVAILABLE", "Gemini connection failed.", retryable=True
            ) from None
        if response.status_code == 429:
            wait = _retry_after(response)
            self._limiter.cooldown(wait)
            raise RateLimitError(wait, request_sent=True)
        if not response.is_success:
            status = response.status_code
            if status in {401, 403}:
                code = "PROVIDER_AUTH_ERROR"
            elif status == 404:
                code = "PROVIDER_MODEL_UNAVAILABLE"
            else:
                code = (
                    "PROVIDER_UNAVAILABLE"
                    if status in {408, 409} or status >= 500
                    else "PROVIDER_ERROR"
                )
            error = ExtractorError(
                code,
                "Gemini rejected the model request.",
                retryable=status in {408, 409} or status >= 500,
            )
            # Preserve legacy codes while exposing safe routing information.
            # Request schema/auth/model 4xx failures need configuration repair.
            error.configuration_error = 400 <= status < 500 and status not in {408, 409}
            raise error from None
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("response object required")
            if body.get("promptFeedback", {}).get("blockReason"):
                return ModelReply(refusal="Gemini blocked extraction.")
            candidates = body.get("candidates")
            if not isinstance(candidates, list) or len(candidates) != 1:
                raise ValueError("one candidate required")
            candidate = candidates[0]
            reason = candidate.get("finishReason")
            if reason == "MAX_TOKENS":
                return ModelReply(incomplete=True)
            if reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}:
                return ModelReply(refusal="Gemini refused extraction.")
            if reason != "STOP":
                raise ValueError("response did not complete")
            parts = candidate["content"]["parts"]
            if not isinstance(parts, list):
                raise ValueError("parts array required")
            texts = [
                part["text"]
                for part in parts
                if isinstance(part, dict)
                and not part.get("thought")
                and isinstance(part.get("text"), str)
            ]
            if not texts:
                raise ValueError("response text missing")
            return ModelReply(text="".join(texts))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ExtractorError("PROVIDER_ERROR", "Gemini response was invalid.") from None


def _retry_after(response: httpx.Response) -> float:
    try:
        wait = float(response.headers.get("Retry-After", "60"))
        if not math.isfinite(wait) or wait <= 0:
            return 60.0
        return min(wait, 86_400.0)
    except ValueError:
        return 60.0
