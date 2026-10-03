"""Small live v2 smoke; isolated databases, shared Gemini quota, no transactions."""

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.weather_open_meteo import OpenMeteoWeatherAdapter  # noqa: E402
from app.config import Settings  # noqa: E402
from app.contracts.chat import ActionInput  # noqa: E402
from app.contracts.weather import WeatherRequest  # noqa: E402
from app.text import TextBot  # noqa: E402


async def run(live: bool, output: Path):
    settings = Settings.load() if live else Settings(profile="test", secret="v2-smoke")
    if live and (not settings.gemini_key or not settings.vietmap_key):
        raise SystemExit("Live smoke requires configured Gemini and VietMap keys")
    with TemporaryDirectory() as folder:
        settings = replace(
            settings,
            profile="chat_sandbox" if live else "test",
            database_path=Path(folder) / "app.sqlite",
            checkpoint_path=Path(folder) / "graph.sqlite",
        )
        report = {
            "kind": "live" if live else "fixture",
            "holdout": False,
            "checked_at": datetime.now(UTC).isoformat(),
            "turn_version": "parrotgo-turn-2",
            "model": settings.model if live else "fixture",
            "cases": [],
        }
        async with TextBot(settings) as bot:
            texts = [
                "Bạn là ai?",
                "Từ Nhà hát Lớn Hà Nội đến Ga Hà Nội bao nhiêu tiền, bao nhiêu km và mất bao lâu?",
                "Thời tiết ở Nhà hát Lớn Hà Nội bây giờ có mưa không?",
            ]
            for index, text in enumerate(texts):
                reply = await bot.ask(text, session_id=f"smoke-{index}")
                sid = bot.store.find_session("local-text", f"smoke-{index}")
                state = bot.store.snapshot(sid)["state"]
                selections = []
                # An explicit smoke choice exercises the candidate path. It
                # does not claim the first provider result is ground truth.
                for selection_index in range(2):
                    response = state["last_response"]
                    if not response.get("candidates"):
                        break
                    candidate = response["candidates"][0]
                    selections.append(
                        {"label": candidate["label"], "scope_kind": candidate["scope_kind"]}
                    )
                    action = ActionInput(
                        client_action_id=f"smoke-select-{index}-{selection_index}",
                        action={
                            "type": "select_candidate",
                            "candidate_set_id": candidate["candidate_set_id"],
                            "candidate_id": candidate["candidate_id"],
                        },
                        reply_to_response_id=response["response_id"],
                        rendered_response_ids=[response["response_id"]],
                    )
                    receipt = bot.store.enqueue(
                        sid, "action", action.client_action_id, action.model_dump()
                    )
                    bot.coordinator.wakeup.set()
                    for _ in range(300):
                        state = bot.store.snapshot(sid)["state"]
                        if state["control"]["last_event_id"] == receipt["event_id"]:
                            break
                        await asyncio.sleep(0.05)
                    reply = state["last_response"]["text"]
                report["cases"].append(
                    {
                        "input": text,
                        "reply": reply,
                        "reason": state["last_response"].get("reason"),
                        "selections": selections,
                        "selection_policy": "explicit first candidate for adapter smoke, not address gold",
                        "booking_unchanged": all(
                            s["value"] is None for s in state["booking_state"].values()
                        ),
                        "booking_count": bot.engine.booking.booking_count(),
                    }
                )
        if live:
            # Direct adapter smoke is separate from resolving a conversational
            # location. These are explicitly supplied test coordinates.
            adapter = OpenMeteoWeatherAdapter(api_key=settings.open_meteo_key)
            try:
                fact = await adapter.forecast(
                    WeatherRequest(
                        request_id="weather-smoke",
                        scope_kind="inquiry",
                        scope_id="smoke",
                        location_ref="supplied-test-coordinate",
                        latitude=21.0245,
                        longitude=105.8575,
                        target_time=datetime.now(UTC),
                        dependency_fingerprint="weather-smoke",
                    )
                )
                report["open_meteo"] = fact.model_dump(mode="json")
            finally:
                await adapter.aclose()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "report": str(output),
                    "kind": report["kind"],
                    "cases": len(report["cases"]),
                    "booking_unchanged": all(c["booking_unchanged"] for c in report["cases"]),
                    "weather_status": report.get("open_meteo", {}).get("status"),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--report", type=Path,
        default=Path(__file__).resolve().parents[1] / "evaluation/ver2/smoke-report.json",
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run(args.live, args.report))
