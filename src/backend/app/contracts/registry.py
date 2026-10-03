"""Versioned vocabulary and structural limits for booking-slots-3.

Catalog entries describe values the extractor knows, not provider capabilities.
The adapter may supply a different pinned catalog without changing the shape
of the public NLU contract.
"""

from typing import Final, Literal, get_args

CONTRACT_VERSION: Final = "booking-slots-3"

SlotName = Literal[
    "pickup",
    "destination",
    "pickup_time",
    "passengers",
    "vehicle_type",
    "contact_phone",
    "contact_name",
    "pickup_note",
    "luggage",
    "payment_method",
    "stops",
    "special_requests",
]
IntentName = Literal[
    "provide_info",
    "change_info",
    "confirm",
    "deny",
    "select_candidate",
    "reject_candidate",
    "ask_question",
    "request_repeat",
    "cancel",
    "chit_chat",
    "out_of_scope",
    "no_understanding",
]
SpeechStatus = Literal["clear", "low_confidence", "no_speech", "noise"]
BookingStatus = Literal[
    "collecting_info",
    "awaiting_confirmation",
    "booking_in_progress",
    "booking_unknown",
    "booking_failed",
    "booked",
    "cancel_pending",
    "cancel_unknown",
    "cancel_failed",
    "amendment_pending",
    "amendment_unknown",
    "cancelled",
]
BotAction = Literal[
    "ask_slot",
    "ask_clarification",
    "offer_candidates",
    "confirm_booking_info",
    "confirm_booking",
    "confirm_location",
    "ask_area_detail",
    "choose_area_service",
    "confirm_assistance",
    "confirm_amendment",
    "confirm_cancellation",
    "answer_question",
    "repeat",
    "inform_pending",
    "inform_success",
    "inform_failure",
    "handoff",
    "goodbye",
    "silent",
]
CandidateTarget = Literal["pickup", "destination", "stops"]
LuggageSize = Literal["none", "cabin", "large", "mixed", "unknown"]

SLOT_NAMES: Final[tuple[str, ...]] = get_args(SlotName)
INTENT_NAMES: Final[tuple[str, ...]] = get_args(IntentName)
SPEECH_STATUSES: Final[tuple[str, ...]] = get_args(SpeechStatus)
BOOKING_STATUSES: Final[tuple[str, ...]] = get_args(BookingStatus)
BOT_ACTIONS: Final[tuple[str, ...]] = get_args(BotAction)
CANDIDATE_TARGETS: Final[tuple[str, ...]] = get_args(CandidateTarget)
LUGGAGE_SIZES: Final[tuple[str, ...]] = get_args(LuggageSize)
STRING_SLOTS: Final = frozenset(
    slot
    for slot in SLOT_NAMES
    if slot not in {"passengers", "luggage", "stops", "special_requests"}
)

DEFAULT_VEHICLE_CODES: Final = frozenset({"oto_4_cho", "oto_7_cho", "xe_may", "xe_may_dien"})
DEFAULT_PAYMENT_CODES: Final = frozenset({"cash", "linked_card", "corporate"})
DEFAULT_SPECIAL_REQUEST_CODES: Final = frozenset({"child_seat", "wheelchair_access", "pet"})

# Defensive MVP defaults. Lower runtime limits must reject rather than truncate.
MAX_ACTS: Final = 24
MAX_UTTERANCE_CHARS: Final = 8_000
MAX_CONTEXT_CHARS: Final = 8_000
MAX_SLOT_STRING_CHARS: Final = 2_000
MAX_IDENTIFIER_CHARS: Final = 256
MAX_ARRAY_ITEMS: Final = 12
MAX_CANDIDATES: Final = 20
