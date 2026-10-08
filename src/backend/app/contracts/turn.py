"""ParrotGo v2: one interpretation with separate booking and inquiry scopes."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from app.contracts.nlu import DialogueAct, NluInput, StrictContract
from app.contracts.registry import SpeechStatus

TURN_VERSION = "parrotgo-turn-2"
QuestionType = Literal[
    "identity",
    "fare_estimate",
    "route_distance",
    "vehicle_catalog",
    "pickup_availability",
    "place_location",
    "weather_forecast",
    "price_objection",
    "travel_duration",
    "travel_duration_explanation",
    "other_booking_question",
    "chit_chat",
    "out_of_scope",
    "unclear",
    "static_faq",
    "session_question",
    "route_membership",
]


class EvidenceSpan(StrictContract):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str | None = Field(default=None, min_length=1, max_length=4000)

    def extract(self, text: str) -> str:
        if self.start >= self.end or self.end > len(text):
            raise ValueError("invalid evidence span")
        return text[self.start : self.end]


class BookingAct(DialogueAct):
    evidence_span: EvidenceSpan


class QuestionIntent(StrictContract):
    question_id: str = Field(min_length=1, max_length=128)
    type: QuestionType
    raw_text: str = Field(min_length=1, max_length=2000)
    evidence_span: EvidenceSpan
    route_scope: Literal["explicit_pair", "active_inquiry", "current_booking", "unresolved"]
    origin: str | None = Field(max_length=500)
    destination: str | None = Field(max_length=500)
    vehicle_ref: str | None
    departure_time_ref: str | None = Field(max_length=128)
    weather_target: Literal["pickup", "destination", "both", "explicit_location"] | None
    relation_to_booking: Literal["read_only", "hypothetical", "explicit_update"]


class InquiryAction(StrictContract):
    type: Literal[
        "update",
        "select_candidate",
        "reject_candidate",
        "promote",
        "reverse",
        "dismiss",
        "resume_booking",
    ]
    inquiry_id: str | None
    field: Literal["origin", "destination", "vehicle", "departure_time"] | None
    value: str | None = Field(max_length=500)
    evidence_span: EvidenceSpan


class TravelParty(StrictContract):
    adults: int | None = Field(ge=0, le=20)
    children: int | None = Field(ge=0, le=20)
    evidence_span: EvidenceSpan


class InquiryProjection(StrictContract):
    inquiry_id: str
    revision: int
    origin: str | None
    destination: str | None
    vehicle: str | None
    departure_time: str | None
    status: str


class PromptProjection(StrictContract):
    purpose: str
    scope_kind: Literal["booking", "inquiry"]
    scope_id: str
    field: str | None
    revision: int
    proposal_id: str | None = None
    proposed_label: str | None = None
    fee_amount: int | None = None
    policy_version: str | None = None


class TurnInput(NluInput):
    contract_version: Literal["parrotgo-turn-2", "parrotgo-turn-3"] = TURN_VERSION
    inquiries: list[InquiryProjection] = Field(default_factory=list, max_length=5)
    active_inquiry_id: str | None = None
    pending_prompt: PromptProjection | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
    occurred_at: str
    timezone: str = "Asia/Ho_Chi_Minh"
    architecture_state: dict[str, Any] | None = None


class LocationDecision(StrictContract):
    decision: Literal[
        "confirm",
        "reject",
        "unknown_detail",
        "request_assistance",
        "decline_assistance",
        "accept_fee",
    ]
    evidence_span: EvidenceSpan


class TurnResult(StrictContract):
    contract_version: Literal["parrotgo-turn-2", "parrotgo-turn-3"]
    speech_status: SpeechStatus
    booking_acts: Annotated[list[BookingAct], Field(max_length=24)]
    questions: Annotated[list[QuestionIntent], Field(max_length=8)]
    inquiry_actions: Annotated[list[InquiryAction], Field(max_length=8)]
    conversational_acts: list[
        Literal["greeting", "thanks", "repeat", "out_of_scope", "unclear"]
    ] = Field(max_length=8)
    travel_party: TravelParty | None
    location_decisions: list[LocationDecision] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def valid_speech(self) -> TurnResult:
        has_acts = bool(
            self.booking_acts
            or self.questions
            or self.inquiry_actions
            or self.conversational_acts
            or self.travel_party
            or self.location_decisions
        )
        if self.speech_status == "clear" and not has_acts:
            raise ValueError("clear speech needs an interpretation")
        if self.speech_status in {"noise", "no_speech"} and has_acts:
            raise ValueError("absent speech cannot have acts")
        if any(act.intent == "ask_question" for act in self.booking_acts):
            raise ValueError("questions belong in questions, not booking_acts")
        return self

    def validate_evidence(self, data: TurnInput) -> None:
        text = data.utterance.text or ""
        ids = {item.inquiry_id for item in data.inquiries}
        for item in [
            *self.booking_acts,
            *self.questions,
            *self.inquiry_actions,
            *self.location_decisions,
        ]:
            evidence = item.evidence_span.extract(text)
            if item.evidence_span.text is not None and item.evidence_span.text != evidence:
                raise ValueError("evidence text must equal its source span")
            if isinstance(item, QuestionIntent) and item.raw_text != evidence:
                raise ValueError("question text must equal its source span")
            if isinstance(item, InquiryAction) and item.inquiry_id and item.inquiry_id not in ids:
                raise ValueError("unknown inquiry reference")
        if self.travel_party:
            evidence = self.travel_party.evidence_span.extract(text)
            if (
                self.travel_party.evidence_span.text is not None
                and self.travel_party.evidence_span.text != evidence
            ):
                raise ValueError("travel party text must equal its source span")
        if len({q.question_id for q in self.questions}) != len(self.questions):
            raise ValueError("duplicate question ID")

    def align_literal_evidence(self, data: TurnInput) -> None:
        """Correct character counting only when an exact literal occurs once.

        Never guess text, intent, scope or repeated occurrences. Providers can
        copy Vietnamese text reliably but sometimes miscount its offsets.
        """
        source = data.utterance.text or ""
        items = [
            *self.booking_acts,
            *self.questions,
            *self.inquiry_actions,
            *self.location_decisions,
        ]
        if self.travel_party:
            items.append(self.travel_party)
        for item in items:
            span = item.evidence_span
            literal = item.raw_text if isinstance(item, QuestionIntent) else span.text
            if literal is None:
                continue
            if source[span.start : span.end] == literal and span.end <= len(source):
                continue
            first = source.find(literal)
            if first < 0 or source.find(literal, first + 1) >= 0:
                raise ValueError("evidence is missing or has ambiguous offsets")
            span.start, span.end = first, first + len(literal)


def turn_output_schema() -> dict[str, Any]:
    """Inline local schemas for the provider; full constraints stay authoritative."""
    schema = TurnResult.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(value: Any) -> Any:
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return inline(definitions[value["$ref"].rsplit("/", 1)[-1]])
        result = {
            key: inline(item) for key, item in value.items() if key not in {"title", "default"}
        }
        if result.get("type") == "object":
            result["required"] = list(result.get("properties", {}))
        return result

    return inline(schema)
