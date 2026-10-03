"""OpenAI Responses API implementation of the extractor client protocol."""

from __future__ import annotations

from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from .extractor import ExtractorError, ModelReply


class OpenAIExtractorClient:
    """One non-streaming API call per generate; the outer runtime controls retries.

    Inject an AsyncOpenAI client to configure transport or for offline tests.
    close() closes only a client created by this adapter.
    """

    def __init__(self, *, api_key: str | None = None, client: Any = None) -> None:
        self._owns_client = client is None
        self._client = client if client is not None else AsyncOpenAI(api_key=api_key, max_retries=0)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()

    async def __aenter__(self) -> OpenAIExtractorClient:
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
        try:
            # Enforce disabled SDK retries even on injected clients.
            request_client = self._client.with_options(max_retries=0, timeout=timeout_seconds)
            response = await request_client.responses.create(
                model=model,
                instructions=system_prompt,
                input=[{"role": "user", "content": [{"type": "input_text", "text": input_json}]}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "booking_nlu_result",
                        "strict": True,
                        "schema": response_schema,
                    }
                },
                max_output_tokens=4096,
                store=False,
            )
        except APITimeoutError:
            raise ExtractorError(
                "PROVIDER_TIMEOUT", "OpenAI request timed out.", retryable=True
            ) from None
        except APIConnectionError:
            raise ExtractorError(
                "PROVIDER_UNAVAILABLE", "OpenAI connection failed.", retryable=True
            ) from None
        except APIStatusError as exc:
            retryable = exc.status_code in {408, 409, 429} or exc.status_code >= 500
            raise ExtractorError(
                "PROVIDER_UNAVAILABLE" if retryable else "PROVIDER_ERROR",
                "OpenAI rejected the model request.",
                retryable=retryable,
            ) from None
        except Exception:
            # Covers unexpected SDK response parsing errors without exposing bodies.
            # Caller cancellation remains a BaseException and propagates.
            raise ExtractorError("PROVIDER_ERROR", "OpenAI client failed unexpectedly.") from None

        status = getattr(response, "status", None)
        if status == "incomplete":
            return ModelReply(incomplete=True)
        if status != "completed" or getattr(response, "error", None) is not None:
            raise ExtractorError("PROVIDER_ERROR", "OpenAI response did not complete successfully.")
        for item in response.output:
            if getattr(item, "type", None) == "message":
                for content in item.content:
                    if getattr(content, "type", None) == "refusal":
                        return ModelReply(refusal=content.refusal)
        return ModelReply(text=response.output_text)
