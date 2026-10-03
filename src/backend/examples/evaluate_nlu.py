"""Run annotated regression examples; live mode measures Gemini separately.

These examples informed implementation and are not a held-out dataset. Reports
must not be treated as a production Vietnamese language quality benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import unicodedata
from pathlib import Path

from app.adapters.extractor import CallBudget, ExtractorError, ExtractorRuntime, llm_extractor_func
from app.adapters.nlu_fixture import FixtureExtractorClient
from app.adapters.nlu_gemini import GeminiExtractorClient
from app.config import Settings
from app.evaluation.cases import CASES


def canonical(acts: list[dict]) -> list[str]:
    return sorted(json.dumps(act, ensure_ascii=False, sort_keys=True) for act in acts)


def known_alias_canonical(acts: list[dict]) -> list[str]:
    """Normalize only known unconditional ASAP synonyms, not arbitrary semantics."""
    normalized = []
    for act in acts:
        copied = dict(act)
        value = act.get("value")
        if act.get("target") == "pickup_time" and isinstance(value, str):
            folded = "".join(
                char
                for char in unicodedata.normalize("NFD", value.lower().replace("đ", "d"))
                if not unicodedata.combining(char)
            ).strip()
            if folded in {"ngay bay gio", "bay gio", "di ngay", "don ngay", "asap", "ngay"}:
                copied["value"] = "asap"
        normalized.append(copied)
    return canonical(normalized)


async def evaluate(args: argparse.Namespace) -> int:
    wanted = {case_id.strip() for case_id in args.case_ids.split(",") if case_id.strip()}
    selected = [case for case in CASES if not wanted or case.case_id in wanted]
    selected = selected[: args.limit] if args.limit else selected
    settings = Settings.load() if args.live else None
    if args.live and not settings.gemini_key:
        print("GEMINI_API_KEY chưa được cấu hình.")
        return 2
    client = (
        GeminiExtractorClient(
            api_key=settings.gemini_key,
            rpm=settings.gemini_rpm,
            limiter_path=settings.gemini_rate_path,
        )
        if settings
        else FixtureExtractorClient()
    )
    model = settings.model if settings else "offline-fixture"
    rows = []
    try:
        for index, case in enumerate(selected):
            # Pace live runs below 15 RPM, sharing persisted quota with the server.
            if settings and index:
                await asyncio.sleep(60 / settings.gemini_rpm + 0.1)
            start = time.monotonic()
            budget = CallBudget(limit=1)
            try:
                result = await llm_extractor_func(
                    case.projection(),
                    runtime=ExtractorRuntime(
                        client=client,
                        model=model,
                        timeout_seconds=settings.llm_timeout if settings else 12,
                        call_budget=budget,
                    ),
                )
                matched = canonical(result.model_dump()["dialogue_acts"]) == canonical(
                    case.expected
                )
                alias_matched = known_alias_canonical(
                    result.model_dump()["dialogue_acts"]
                ) == known_alias_canonical(case.expected)
                rows.append(
                    {
                        "case_id": case.case_id,
                        "valid": True,
                        "exact_act_match": matched,
                        "known_alias_act_match": alias_matched,
                        "actual_acts": result.model_dump()["dialogue_acts"],
                        "expected_acts": case.expected,
                        "model_calls": budget.used if settings else 0,
                        "latency_seconds": round(time.monotonic() - start, 3),
                    }
                )
                if args.progress:
                    print(
                        json.dumps(
                            {
                                key: value
                                for key, value in rows[-1].items()
                                if key not in {"actual_acts", "expected_acts"}
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            except ExtractorError as error:
                rows.append(
                    {
                        "case_id": case.case_id,
                        "valid": False,
                        "exact_act_match": False,
                        "known_alias_act_match": False,
                        "error": error.code,
                        "model_calls": budget.used,
                        "latency_seconds": round(time.monotonic() - start, 3),
                    }
                )
                if args.progress:
                    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
                if error.code == "RATE_LIMITED":
                    # Stop the evaluation without spinning or consuming more quota.
                    break
    finally:
        await client.close()
    report = {
        "evaluation_version": "booking-nlu-regression-1",
        "provider": "gemini" if settings else "fixture",
        "model": model,
        "dataset_kind": "development_regression_examples_not_holdout",
        "metric": "exact_typed_act_match_ignoring_act_order",
        "limitation": "Offline results verify fixture behavior only. Live exact matches do not measure all semantic equivalents or production language quality.",
        "requested": len(selected),
        "completed": len(rows),
        "schema_valid": sum(row["valid"] for row in rows),
        "exact_act_matches": sum(row["exact_act_match"] for row in rows),
        "known_alias_act_matches": sum(row["known_alias_act_match"] for row in rows),
        "known_aliases": "Only unconditional ASAP phrases; all other values remain exact.",
        "cases": rows,
    }
    destination = Path(args.report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "cases"},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"Report: {destination}")
    return 0 if len(rows) == len(selected) and all(row["valid"] for row in rows) else 1


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print safe per-case progress without text or secrets",
    )
    parser.add_argument("--limit", type=int, default=0, help="Maximum cases; 0 runs all")
    parser.add_argument(
        "--case-ids", default="", help="Comma-separated case IDs, e.g. F065,F067,F068"
    )
    parser.add_argument("--report", default="evaluation/nlu-report.json")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    if any(
        case_id.strip() not in {case.case_id for case in CASES}
        for case_id in args.case_ids.split(",")
        if case_id.strip()
    ):
        parser.error("--case-ids contains an unknown case")
    raise SystemExit(asyncio.run(evaluate(args)))


if __name__ == "__main__":
    main()
