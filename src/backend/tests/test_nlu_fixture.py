"""Regression examples for the deterministic offline provider, not Gemini eval."""

import asyncio
import json

import pytest

from app.adapters.extractor import CallBudget, ExtractorRuntime, llm_extractor_func
from app.adapters.nlu_fixture import FixtureExtractorClient
from app.evaluation.cases import CASES


def normalized(acts):
    return sorted(json.dumps(act, ensure_ascii=False, sort_keys=True) for act in acts)


@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_annotated_offline_turns(case):
    data = case.projection()
    before = data.model_dump()
    result = asyncio.run(
        llm_extractor_func(
            data,
            runtime=ExtractorRuntime(
                client=FixtureExtractorClient(), call_budget=CallBudget(limit=1)
            ),
        )
    )
    assert normalized(result.model_dump()["dialogue_acts"]) == normalized(case.expected), (
        result.model_dump()
    )
    assert data.model_dump() == before


def test_all_annotation_contracts_are_valid():
    assert len(CASES) >= 40
    for case in CASES:
        case.projection()
        case.expected_result()


def test_eval_asap_aliases_preserve_negation_and_other_fields():
    from app.evaluation.cases import provide
    from examples.evaluate_nlu import known_alias_canonical

    assert known_alias_canonical([provide("pickup_time", "đi ngay")]) == known_alias_canonical(
        [provide("pickup_time", "ngay bây giờ")]
    )
    assert known_alias_canonical(
        [provide("pickup_time", "không đi ngay")]
    ) != known_alias_canonical([provide("pickup_time", "ngay bây giờ")])
    assert known_alias_canonical([provide("destination", "đi ngay")]) != known_alias_canonical(
        [provide("destination", "ngay bây giờ")]
    )
