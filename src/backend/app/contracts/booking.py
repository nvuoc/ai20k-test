"""Authoritative booking state from architecture_fixed.md.

Transport projections and provider receipts are not booking slots.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Intent = Literal["provide_info", "confirm", "deny", "ask_question", "change_info",
                 "cancel", "chit_chat", "unclear", "repeat_request"]
SlotStatus = Literal["empty", "extracted", "confirmed", "needs_clarification"]
BookingStatus = Literal["collecting", "confirming", "ready_to_book", "booked",
                        "cancel_pending", "canceled", "operator_required"]


def normalize_phone(value: str) -> str:
    phone = re.sub(r"[\s.()\-]", "", value)
    if phone.startswith("+84"):
        phone = "0" + phone[3:]
    elif phone.startswith("84") and len(phone) == 11:
        phone = "0" + phone[2:]
    if not re.fullmatch(r"0(?:[35789]\d{8}|2\d{9})", phone):
        raise ValueError("Số điện thoại Việt Nam không hợp lệ")
    return phone


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Customer(Model):
    customer_phone: str
    customer_name: str = Field(min_length=1, max_length=128)

    _phone = field_validator("customer_phone")(normalize_phone)

    @field_validator("customer_name")
    @classmethod
    def name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Tên khách hàng là bắt buộc")
        return value


class Coordinates(Model):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)

    @model_validator(mode="after")
    def usable(self):
        if self.lat == self.lng == 0:
            raise ValueError("Coordinates must identify a real location")
        return self


class AddressComponents(Model):
    detail: str | None = None
    street: str | None = None
    ward: str | None = None
    district: str | None = None
    province_city: str | None = None


class AddressSlot(Model):
    raw: str | None = None
    formatted: str | None = None
    coords: Coordinates | None = None
    components: AddressComponents | None = None
    note: str | None = None
    is_mega_poi: bool = False
    default_point_used: bool = False
    status: SlotStatus = "empty"
    # Keep source and defaults with the address, across turns/checkpoints.
    metadata: dict[str, Any] = Field(default_factory=dict)


class StopoverSlot(Model):
    address: AddressSlot
    order: int = Field(ge=1)


class ValueSlot(Model):
    value: Any = None
    status: SlotStatus = "empty"


class BookingSlots(Model):
    pickup: AddressSlot = Field(default_factory=AddressSlot)
    destination: AddressSlot = Field(default_factory=AddressSlot)
    stopovers: list[StopoverSlot] = Field(default_factory=list)
    pickup_time: ValueSlot = Field(default_factory=ValueSlot)
    vehicle_type: ValueSlot = Field(default_factory=ValueSlot)
    passengers: ValueSlot = Field(default_factory=ValueSlot)
    general_note: str | None = None
    distance_km: float | None = Field(default=None, ge=0)
    duration_minutes: float | None = Field(default=None, ge=0)


class ExtractedSlotUpdate(Model):
    slot_name: str
    value: Any
    source_text: str


class BotAction(Model):
    action_type: Literal["ask_slot", "confirm_slots", "confirm_booking", "clarify_address",
                         "answer_question", "inform_success", "general_reply",
                         "confirm_cancel", "human_handoff"]
    target_slots: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


class BotState(Customer):
    session_id: str
    messages: list[dict[str, str]] = Field(default_factory=list)
    booking_slots: BookingSlots = Field(default_factory=BookingSlots)
    turn_extracted_slots: list[ExtractedSlotUpdate] = Field(default_factory=list)
    intents: list[Intent] = Field(default_factory=list)
    primary_intent: Intent | None = None
    current_focus: str | None = None
    last_bot_action: BotAction | None = None
    tool_status: Literal["SUCCESS", "NOT_FOUND", "AMBIGUOUS", "API_ERROR"] | None = None
    fallback_count: int = 0
    booking_status: BookingStatus = "collecting"
    final_response_text: str | None = None


def ready_to_book(state: dict) -> bool:
    """Readiness is derived; it never grants consent to dispatch a booking."""
    slots = state["booking_slots"]
    if not all(slots[name]["status"] == "confirmed"
               for name in ("pickup", "destination", "pickup_time", "vehicle_type")):
        return False
    for address in [slots["pickup"], slots["destination"],
                    *(stop["address"] for stop in slots["stopovers"])]:
        place = address.get("metadata", {}).get("place") or {}
        if (not address.get("coords") or address["status"] != "confirmed" or not address.get("formatted")
                or not place.get("source") or address["coords"] != {"lat": place.get("lat"), "lng": place.get("lon")}):
            return False
    pickup = slots["pickup"]
    if pickup["is_mega_poi"] and pickup["default_point_used"] and not pickup.get("note"):
        return False
    return True
