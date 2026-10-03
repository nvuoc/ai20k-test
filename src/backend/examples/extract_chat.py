"""Run an offline Vietnamese extraction example, or Gemini with --live."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.adapters.extractor import CallBudget, ExtractorError, ExtractorRuntime, llm_extractor_func
from app.adapters.nlu_fixture import FixtureExtractorClient
from app.adapters.nlu_gemini import GeminiExtractorClient
from app.config import Settings
from app.contracts.nlu import empty_booking_state

DEMO_TEXT = "Hai người, một vali lớn, đổi sang xe 7 chỗ nhé"


def make_demo_input(text: str = DEMO_TEXT) -> dict[str, Any]:
    state = empty_booking_state().model_dump()
    state["vehicle_type"] = {"value": "oto_4_cho", "confirmed": False}
    return {
        "utterance": {"text": text, "asr_confidence": None},
        "conversation_context": {
            "last_bot_message": "Mình đi mấy người và có hành lý gì ạ?",
            "last_bot_action": "ask_slot",
            "current_focus": "passengers",
        },
        "booking_state": state,
        "candidates": [],
        "booking_status": "collecting_info",
    }


async def run(args: argparse.Namespace) -> int:
    payload = (
        json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
        if args.input
        else make_demo_input(args.text or DEMO_TEXT)
    )
    try:
        if args.live:
            settings = Settings.load()
            if not settings.gemini_key:
                print(
                    "GEMINI_API_KEY chưa được cấu hình; hãy đặt trong src/backend/.env trước khi chạy --live."
                )
                return 2
            print("Mode: Gemini live (có gọi API).")
            async with GeminiExtractorClient(
                api_key=settings.gemini_key,
                rpm=settings.gemini_rpm,
                limiter_path=settings.gemini_rate_path,
            ) as client:
                result = await llm_extractor_func(
                    payload,
                    runtime=ExtractorRuntime(
                        client=client,
                        model=settings.model,
                        timeout_seconds=settings.llm_timeout,
                        call_budget=CallBudget(limit=settings.max_llm_calls),
                    ),
                )
        else:
            print(
                "Mode: fixture offline (không gọi LLM; chỉ kiểm tra ví dụ, không đánh giá Gemini)."
            )
            result = await llm_extractor_func(
                payload,
                runtime=ExtractorRuntime(
                    client=FixtureExtractorClient(), call_budget=CallBudget(limit=1)
                ),
            )
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    except ExtractorError as exc:
        print(f"Extractor failed: {exc.code}")
        return 1
    return 0


def main() -> None:
    # Piped stdout on Windows may otherwise use cp1252 and fail on Vietnamese.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Call Gemini using src/backend/.env instead of the fixture"
    )
    parser.add_argument("--input", help="Full five-field NluInput JSON file")
    parser.add_argument(
        "--text", help="Text for the demo projection (use --input for custom context)"
    )
    args = parser.parse_args()
    if args.input and args.text:
        parser.error("--input and --text are mutually exclusive")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
