"""Public HTTP contracts, independent of GraphState and provider payloads."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.contracts.registry import BookingStatus

Key = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
PlaceRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionInput(InputModel):
    client_session_key: Key


class MessageInput(InputModel):
    client_message_id: Key
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    reply_to_response_id: Key | None = None
    rendered_response_ids: list[Key] = Field(default_factory=list, max_length=20)


class SelectCandidate(InputModel):
    type: Literal["select_candidate"]
    candidate_set_id: Key
    candidate_id: PlaceRef


class ConfirmBooking(InputModel):
    type: Literal["confirm_booking"]
    prompt_id: Key
    booking_revision: int = Field(ge=0)
    snapshot_fingerprint: Key


class CancelBooking(InputModel):
    type: Literal["cancel_booking"]
    booking_id: Key


class CancelDraft(InputModel):
    type: Literal["cancel_draft"]
    draft_id: Key


class UseInquiryRoute(InputModel):
    type: Literal["use_inquiry_route"]
    inquiry_id: Key
    inquiry_revision: int = Field(ge=0)
    route_fingerprint: Key
    booking_revision: int = Field(ge=0)


class ChooseInquiryVehicle(InputModel):
    type: Literal["choose_inquiry_vehicle"]
    inquiry_id: Key
    inquiry_revision: int = Field(ge=0)
    vehicle_type: Literal["oto_4_cho", "oto_7_cho", "xe_may_dien"]


class ResumeBooking(InputModel):
    type: Literal["resume_booking"]


class DismissInquiry(InputModel):
    type: Literal["dismiss_inquiry"]
    inquiry_id: Key
    inquiry_revision: int = Field(ge=0)


class ActionInput(InputModel):
    client_action_id: Key
    action: Annotated[
        SelectCandidate | ConfirmBooking | CancelBooking | CancelDraft | UseInquiryRoute |
        ChooseInquiryVehicle | ResumeBooking | DismissInquiry,
        Field(discriminator="type"),
    ]
    reply_to_response_id: Key | None = None
    rendered_response_ids: list[Key] = Field(default_factory=list, max_length=20)


class AckInput(InputModel):
    ack_id: Key
    response_id: Key
    generation: int = Field(ge=0)
    delivery_type: Literal["rendered"] = "rendered"


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Receipt(PublicModel):
    api_version: Literal["chat-api-2", "chat-api-3"]
    session_id: str
    message_id: str
    event_id: str
    ingress_seq: int = Field(ge=1)
    status: Literal["received", "queued", "processing", "completed", "failed"]


class PublicBooking(PublicModel):
    booking_id: str | None
    status: str | None
    provider_status: str | None
    provider: Literal["sandbox"] | None


class PublicCandidate(PublicModel):
    candidate_id: str
    candidate_set_id: str
    target: Literal["pickup", "destination", "stops"]
    ordinal: int = Field(ge=1)
    label: str
    stop_ref: str | None = None
    scope_kind: Literal["booking", "inquiry"] = "booking"
    scope_id: str | None = None
    revision: int | None = None


class PublicLuggage(PublicModel):
    count: int = Field(ge=0)
    size: Literal["none", "cabin", "large", "mixed", "unknown"]


class TripSummary(PublicModel):
    pickup: str
    destination: str
    pickup_time: str
    passengers: int = Field(ge=1)
    vehicle_type: Literal["oto_4_cho", "oto_7_cho", "xe_may_dien"]
    vehicle_label: str
    contact_phone: str
    contact_name: str | None
    pickup_note: str | None
    luggage: PublicLuggage | None
    payment_method: Literal["cash"] | None
    stops: list[str] | None
    special_requests: list[str] | None
    fare: int = Field(ge=0)
    currency: Literal["VND"]
    quote_expires_at: float
    booking_revision: int = Field(ge=0)
    snapshot_fingerprint: str
    prompt_id: str
    travel_party: dict[str, int | None] | None = None
    base_fare: int | None = Field(default=None, ge=0)
    assistance_fee: int | None = Field(default=None, ge=0)
    provisional: bool = False


class PublicPresentation(PublicModel):
    contract_version: Literal["chat-presentation-1", "chat-presentation-2", "chat-presentation-3"]
    response_id: str
    generation: int = Field(ge=0)
    prompt_id: str | None = None
    snapshot_fingerprint: str | None = None
    booking_revision: int | None = None
    candidate_set_id: str | None = None
    valid_until: float | None = None
    scope_kind: Literal["booking", "inquiry"] = "booking"
    scope_id: str | None = None
    inquiry_revision: int | None = None


class PublicInquiry(PublicModel):
    inquiry_id: str
    revision: int
    origin: str | None
    destination: str | None
    vehicle: str | None
    status: str
    expires_at: float
    route_fingerprint: str | None
    can_use_route: bool
    booking_revision: int


class AssistantResponse(PublicModel):
    response_id: str
    generation: int = Field(ge=0)
    text: str
    action: str
    focus: str | None
    candidates: list[PublicCandidate]
    summary: TripSummary | None
    presentation: PublicPresentation
    booking_status: BookingStatus
    reason: str | None
    booking: PublicBooking | None
    inquiry: PublicInquiry | None = None


class UserMessage(PublicModel):
    message_id: str
    event_id: str
    role: Literal["user"]
    text: str


class TurnFailure(PublicModel):
    event_id: str
    code: str
    retryable: bool
    needs_support: bool = False
    message: str


class EventEnvelope(PublicModel):
    cursor: int = Field(ge=1)
    event_id: str
    occurred_at: str


class AssistantEvent(EventEnvelope):
    type: Literal["assistant_response"]
    payload: AssistantResponse | None


class UserMessageEvent(EventEnvelope):
    type: Literal["message_received"]
    payload: UserMessage


class BookingEvent(EventEnvelope):
    type: Literal["booking_updated"]
    payload: PublicBooking | None


class TurnFailureEvent(EventEnvelope):
    type: Literal["turn_failed"]
    payload: TurnFailure


ChatEvent = Annotated[
    AssistantEvent | UserMessageEvent | BookingEvent | TurnFailureEvent,
    Field(discriminator="type"),
]


class ChatSnapshot(PublicModel):
    api_version: Literal["chat-api-2", "chat-api-3"]
    session_id: str
    events: list[ChatEvent]
    next_cursor: int = Field(ge=0)
    current_cursor: int = Field(ge=0)
    has_more: bool
    pending_count: int = Field(ge=0)
    booking_status: BookingStatus
    draft_id: str
    active_response: AssistantResponse | None
    booking: PublicBooking | None
    needs_support: bool = False
    blocked_count: int = Field(default=0, ge=0)
    waiting_for_quota: bool = False
    retry_at: float | None = None


class PublicCapabilities(PublicModel):
    asap: bool
    scheduled: bool
    multi_stop: bool
    inquiry_v2: bool = True
    address_auto_accept_v2: bool = True
    local_address_v2: bool = True
    electric_motorbike: bool = False
    weather: bool = False
    location_confirmation: bool = False
    area_estimate: bool = False
    area_assistance: bool = False
    text_only_chat: bool = True


class PublicVehicle(PublicModel):
    code: Literal["oto_4_cho", "oto_7_cho", "xe_may_dien"]
    label: str
    max_passengers: int = Field(ge=1)


class Bootstrap(PublicModel):
    api_version: Literal["chat-api-2", "chat-api-3"]
    mode: Literal["sandbox"]
    profile: Literal["fixture_demo", "chat_sandbox", "test"]
    llm_provider: str
    model: str | None
    maps_provider: str
    booking_provider: Literal["sandbox"]
    gemini_rpm: int = Field(ge=1, le=15)
    capabilities: PublicCapabilities
    brand_name: str = "ParrotGo"
    weather_provider: str = "disabled"
    vehicles: list[PublicVehicle] = Field(default_factory=list)
    fallback_provider: str | None = None
    fallback_model: str | None = None
    degraded_mode_enabled: bool = False


class ReadyResponse(Bootstrap):
    status: Literal["ready"]


class HealthResponse(PublicModel):
    status: Literal["ok"]


class DeliveryAck(PublicModel):
    status: Literal["acknowledged"]


class BookingResponse(PublicModel):
    booking: PublicBooking | None
    status: BookingStatus


class PublicError(PublicModel):
    code: str
    message: str
    retryable: bool


class HttpError(PublicModel):
    detail: str
