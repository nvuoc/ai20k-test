"""Strict NLU projection and result contracts; no state reducer or booking I/O."""

from __future__ import annotations

import json
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)

from .registry import (
    CANDIDATE_TARGETS,
    INTENT_NAMES,
    LUGGAGE_SIZES,
    MAX_ACTS,
    MAX_ARRAY_ITEMS,
    MAX_CANDIDATES,
    MAX_CONTEXT_CHARS,
    MAX_IDENTIFIER_CHARS,
    MAX_SLOT_STRING_CHARS,
    MAX_UTTERANCE_CHARS,
    SLOT_NAMES,
    SPEECH_STATUSES,
    STRING_SLOTS,
    BookingStatus,
    BotAction,
    IntentName,
    LuggageSize,
    SlotName,
    SpeechStatus,
)


class StrictContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, revalidate_instances="always"
    )


def _nonblank_value(value: Any) -> Any:
    """Reject placeholder strings without rewriting the customer's wording."""
    if isinstance(value, str):
        text = value.strip()
        if not text or text.casefold() == "null":
            raise ValueError("value must be nonblank and must not be the string 'null'")
        if text.startswith(("{", "[")):
            try:
                embedded = json.loads(text)
            except (ValueError, RecursionError):
                pass
            else:
                if isinstance(embedded, (dict, list)):
                    raise ValueError(
                        "JSON objects/arrays must not be encoded inside a string value"
                    )
    return value


SlotString = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=MAX_SLOT_STRING_CHARS),
    BeforeValidator(_nonblank_value),
]
Identifier = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=MAX_IDENTIFIER_CHARS),
    BeforeValidator(_nonblank_value),
]
PassengerCount = Annotated[StrictInt, Field(ge=1)]
LuggageCount = Annotated[StrictInt, Field(ge=0)]
StringList = Annotated[list[SlotString], Field(max_length=MAX_ARRAY_ITEMS)]


class Luggage(StrictContract):
    count: LuggageCount | None
    size: LuggageSize

    @model_validator(mode="after")
    def validate_empty_luggage(self) -> Luggage:
        if (self.count == 0) != (self.size == "none"):
            raise ValueError("luggage count=0 must correspond exactly to size='none'")
        return self


ValueT = TypeVar("ValueT")


class SlotState(StrictContract, Generic[ValueT]):
    value: ValueT | None
    confirmed: StrictBool

    @model_validator(mode="after")
    def validate_confirmation(self) -> SlotState[ValueT]:
        if self.value is None and self.confirmed:
            raise ValueError("a slot with null value cannot be confirmed")
        return self


class BookingState(StrictContract):
    pickup: SlotState[SlotString]
    destination: SlotState[SlotString]
    pickup_time: SlotState[SlotString]
    passengers: SlotState[PassengerCount]
    vehicle_type: SlotState[SlotString]
    contact_phone: SlotState[SlotString]
    contact_name: SlotState[SlotString]
    pickup_note: SlotState[SlotString]
    luggage: SlotState[Luggage]
    payment_method: SlotState[SlotString]
    stops: SlotState[StringList]
    special_requests: SlotState[StringList]

    @model_validator(mode="after")
    def validate_request_set(self) -> BookingState:
        requests = self.special_requests.value
        if requests is not None and len(requests) != len(set(requests)):
            raise ValueError("special_requests must not contain duplicate codes")
        return self


def empty_booking_state() -> BookingState:
    """Create an independent, complete twelve-slot state with no confirmation."""
    return BookingState.model_validate(
        {slot: {"value": None, "confirmed": False} for slot in SLOT_NAMES}
    )


class Utterance(StrictContract):
    text: Annotated[StrictStr, StringConstraints(max_length=MAX_UTTERANCE_CHARS)] | None
    asr_confidence: Annotated[float, Field(ge=0, le=1)] | None


class ConversationContext(StrictContract):
    last_bot_message: Annotated[StrictStr, StringConstraints(max_length=MAX_CONTEXT_CHARS)] | None
    last_bot_action: BotAction | None
    current_focus: SlotName | None


class PlaceCandidate(StrictContract):
    candidate_id: Identifier
    candidate_set_id: Identifier
    target: Literal["pickup", "destination"]
    ordinal: Annotated[StrictInt, Field(ge=1)]
    label: SlotString

    @property
    def stop_ref(self) -> None:
        return None


class StopCandidate(StrictContract):
    candidate_id: Identifier
    candidate_set_id: Identifier
    target: Literal["stops"]
    ordinal: Annotated[StrictInt, Field(ge=1)]
    label: SlotString
    stop_ref: Identifier


Candidate = PlaceCandidate | StopCandidate


class NluInput(StrictContract):
    utterance: Utterance
    conversation_context: ConversationContext
    booking_state: BookingState
    candidates: Annotated[list[Candidate], Field(max_length=MAX_CANDIDATES)]
    booking_status: BookingStatus

    @model_validator(mode="after")
    def validate_candidate_batch(self) -> NluInput:
        if not self.candidates:
            return self
        scopes = {
            (candidate.candidate_set_id, candidate.target, candidate.stop_ref)
            for candidate in self.candidates
        }
        if len(scopes) != 1:
            raise ValueError("candidates must belong to one active set, target and stop reference")
        identifiers = [candidate.candidate_id for candidate in self.candidates]
        ordinals = [candidate.ordinal for candidate in self.candidates]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("candidate IDs must be unique within the active projection")
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("candidate ordinals must be unique within the active projection")
        return self


ActValue = SlotString | StrictInt | Luggage | StringList | None


class DialogueAct(StrictContract):
    intent: IntentName
    target: SlotName | None
    value: ActValue

    @model_validator(mode="after")
    def validate_intent_target_value(self) -> DialogueAct:
        if self.intent in {"provide_info", "change_info"}:
            if self.target is None or self.value is None:
                raise ValueError("information acts require a slot target and nonnull value")
            if self.target in STRING_SLOTS:
                if not isinstance(self.value, str):
                    raise ValueError("string slot requires a string value")
            elif self.target == "passengers":
                if type(self.value) is not int or self.value < 1:
                    raise ValueError("passengers requires an integer >= 1, excluding boolean")
            elif self.target == "luggage":
                if not isinstance(self.value, Luggage):
                    raise ValueError("luggage requires an object containing count and size")
            elif self.target in {"stops", "special_requests"}:
                if not isinstance(self.value, list):
                    raise ValueError("list slot requires an array of strings")
                if self.target == "special_requests" and len(self.value) != len(set(self.value)):
                    raise ValueError("special_requests must not contain duplicate codes")
            return self

        if self.intent in {"confirm", "deny"}:
            if self.value is not None:
                raise ValueError("confirm/deny must have null value")
            return self

        if self.intent in {"select_candidate", "reject_candidate"}:
            if self.target not in CANDIDATE_TARGETS:
                raise ValueError("candidate acts must target pickup, destination or stops")
            if self.value is None and self.intent == "reject_candidate":
                return self
            if not isinstance(self.value, str):
                raise ValueError("candidate act value must be a candidate ID")
            if len(self.value) > MAX_IDENTIFIER_CHARS:
                raise ValueError("candidate ID exceeds identifier length limit")
            return self

        if self.target is not None:
            raise ValueError("this intent requires a null target")
        if self.intent == "ask_question":
            if not isinstance(self.value, str):
                raise ValueError("ask_question requires a nonblank string value")
        elif self.intent in {"chit_chat", "out_of_scope"}:
            if self.value is not None and not isinstance(self.value, str):
                raise ValueError("chit_chat/out_of_scope require string or null value")
        elif self.value is not None:
            raise ValueError("control intents require a null value")
        return self


class NluResult(StrictContract):
    speech_status: SpeechStatus
    dialogue_acts: Annotated[list[DialogueAct], Field(max_length=MAX_ACTS)]

    @model_validator(mode="after")
    def validate_speech_acts(self) -> NluResult:
        if self.speech_status == "clear" and not self.dialogue_acts:
            raise ValueError("clear speech requires at least one dialogue act")
        if self.speech_status in {"no_speech", "noise"} and self.dialogue_acts:
            raise ValueError("no_speech/noise must have an empty dialogue_acts array")
        return self


def validate_candidate_references(result: NluResult, nlu_input: NluInput) -> None:
    """Check only projection references; delivery, TTL and revision are domain guards."""
    candidate_by_id = {candidate.candidate_id: candidate for candidate in nlu_input.candidates}
    for act in result.dialogue_acts:
        if act.intent not in {"select_candidate", "reject_candidate"}:
            continue
        if act.value is None:
            if not nlu_input.candidates or any(
                candidate.target != act.target for candidate in nlu_input.candidates
            ):
                raise ValueError("reject_candidate must refer to the active target's candidate set")
            continue
        candidate = candidate_by_id.get(act.value)
        if candidate is None or candidate.target != act.target:
            raise ValueError("candidate reference is absent from the current target's projection")


def provider_output_schema() -> dict[str, Any]:
    """Provider-compatible strict root schema, followed by cross-field validation.

    A generic value union avoids unsupported discriminator/conditional schemas.
    It deliberately cannot prove intent/target consistency or catalog membership;
    the server always validates the resulting NluResult and runtime references.
    """
    string_schema: dict[str, Any] = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_SLOT_STRING_CHARS,
    }
    luggage_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "count": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
            "size": {"type": "string", "enum": list(LUGGAGE_SIZES)},
        },
        "required": ["count", "size"],
        "additionalProperties": False,
    }
    act_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": list(INTENT_NAMES)},
            "target": {"anyOf": [{"type": "string", "enum": list(SLOT_NAMES)}, {"type": "null"}]},
            "value": {
                "anyOf": [
                    dict(string_schema),
                    {"type": "integer", "minimum": 1},
                    luggage_schema,
                    {"type": "array", "items": dict(string_schema), "maxItems": MAX_ARRAY_ITEMS},
                    {"type": "null"},
                ]
            },
        },
        "required": ["intent", "target", "value"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "speech_status": {"type": "string", "enum": list(SPEECH_STATUSES)},
            "dialogue_acts": {"type": "array", "items": act_schema, "maxItems": MAX_ACTS},
        },
        "required": ["speech_status", "dialogue_acts"],
        "additionalProperties": False,
    }
