"""Literal evidence recovery never invents or guesses an ambiguous source."""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.adapters.extractor import (
    CallBudget,
    ExtractorError,
    ExtractorRuntime,
    ModelReply,
    llm_extractor_func,
)
from app.adapters.turn_fixture import extract_turn_fixture
from app.contracts.nlu import empty_booking_state
from app.contracts.turn import TurnInput


def projection(text):
    return TurnInput(
        utterance={"text": text, "asr_confidence": None},
        conversation_context={
            "last_bot_message": None,
            "last_bot_action": None,
            "current_focus": None,
        },
        booking_state=empty_booking_state(),
        booking_status="collecting_info",
        candidates=[],
        occurred_at=datetime.now(UTC).isoformat(),
    )


def extract(data, payload):
    class Client:
        calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            return ModelReply(text=json.dumps(payload, ensure_ascii=False))

    client = Client()
    result = asyncio.run(
        llm_extractor_func(data, runtime=ExtractorRuntime(client=client, call_budget=CallBudget(1)))
    )
    assert client.calls == 1
    return result


def test_unique_question_literal_corrects_offset_without_second_call():
    data = projection("Thời tiết ở Nhà hát Lớn Hà Nội bây giờ có mưa không?")
    payload = extract_turn_fixture(data).model_dump()
    payload["questions"][0]["evidence_span"]["end"] -= 2
    result = extract(data, payload)
    assert (
        result.questions[0].evidence_span.extract(data.utterance.text)
        == result.questions[0].raw_text
    )


def test_booking_evidence_literal_corrects_counting_only():
    data = projection("Đổi sang xe 7 chỗ nhé")
    payload = extract_turn_fixture(data).model_dump()
    for act in payload["booking_acts"]:
        act["evidence_span"] = {"start": 1, "end": 5, "text": data.utterance.text}
    result = extract(data, payload)
    assert result.booking_acts[0].value == "oto_7_cho"
    assert result.booking_acts[0].evidence_span.start == 0


@pytest.mark.parametrize(
    "source,literal",
    [("Giá bao nhiêu?", "Thời tiết thế nào?"), ("Giá bao nhiêu? Giá bao nhiêu?", "Giá bao nhiêu?")],
)
def test_missing_or_repeated_literal_with_wrong_offsets_is_rejected(source, literal):
    data = projection(source)
    payload = extract_turn_fixture(data).model_dump()
    payload["questions"][0].update(raw_text=literal, evidence_span={"start": 1, "end": 3})
    with pytest.raises(ExtractorError) as error:
        extract(data, payload)
    assert error.value.code == "OUTPUT_INVALID"
