// Generated from FastAPI OpenAPI. Do not edit by hand.
// Regenerate: python src/backend/scripts/generate_api_types.py

export type AckInput = {
  "ack_id": string;
  "response_id": string;
  "generation": number;
  "delivery_type"?: "rendered";
};

export type ActionInput = {
  "client_action_id": string;
  "action": SelectCandidate | ConfirmBooking | CancelBooking | CancelDraft | ConfirmCancel | UseInquiryRoute | ChooseInquiryVehicle | ResumeBooking | DismissInquiry;
  "reply_to_response_id"?: string | null;
  "rendered_response_ids"?: Array<string>;
};

export type AddressComponents = {
  "detail"?: string | null;
  "street"?: string | null;
  "ward"?: string | null;
  "district"?: string | null;
  "province_city"?: string | null;
};

export type AddressSlot = {
  "raw"?: string | null;
  "formatted"?: string | null;
  "coords"?: Coordinates | null;
  "components"?: AddressComponents | null;
  "note"?: string | null;
  "is_mega_poi"?: boolean;
  "default_point_used"?: boolean;
  "status"?: "empty" | "extracted" | "confirmed" | "needs_clarification";
  "metadata"?: {
  [key: string]: unknown;
};
};

export type AssistantEvent = {
  "cursor": number;
  "event_id": string;
  "occurred_at": string;
  "type": "assistant_response";
  "payload": AssistantResponse | null;
};

export type AssistantResponse = {
  "response_id": string;
  "generation": number;
  "text": string;
  "action": string;
  "focus": string | null;
  "candidates": Array<PublicCandidate>;
  "summary": TripSummary | LegacyTripSummary | null;
  "presentation": PublicPresentation;
  "booking_status": "collecting" | "confirming" | "ready_to_book" | "canceled" | "operator_required" | "collecting_info" | "awaiting_confirmation" | "booking_in_progress" | "booking_unknown" | "booking_failed" | "booked" | "cancel_pending" | "cancel_unknown" | "cancel_failed" | "amendment_pending" | "amendment_unknown" | "cancelled";
  "reason": string | null;
  "booking": PublicBooking | null;
  "inquiry"?: PublicInquiry | null;
};

export type BookingEvent = {
  "cursor": number;
  "event_id": string;
  "occurred_at": string;
  "type": "booking_updated";
  "payload": PublicBooking | null;
};

export type BookingResponse = {
  "booking": PublicBooking | null;
  "status": "collecting" | "confirming" | "ready_to_book" | "canceled" | "operator_required" | "collecting_info" | "awaiting_confirmation" | "booking_in_progress" | "booking_unknown" | "booking_failed" | "booked" | "cancel_pending" | "cancel_unknown" | "cancel_failed" | "amendment_pending" | "amendment_unknown" | "cancelled";
};

export type Bootstrap = {
  "api_version": "chat-api-2" | "chat-api-3";
  "mode": "sandbox";
  "profile": "fixture_demo" | "chat_sandbox" | "test";
  "llm_provider": string;
  "model": string | null;
  "maps_provider": string;
  "booking_provider": "sandbox";
  "gemini_rpm": number;
  "capabilities": PublicCapabilities;
  "brand_name"?: string;
  "weather_provider"?: string;
  "vehicles"?: Array<PublicVehicle>;
  "fallback_provider"?: string | null;
  "fallback_model"?: string | null;
  "degraded_mode_enabled"?: boolean;
};

export type CancelBooking = {
  "type": "cancel_booking";
  "booking_id": string;
};

export type CancelDraft = {
  "type": "cancel_draft";
  "draft_id": string;
};

export type ChatSnapshot = {
  "api_version": "chat-api-2" | "chat-api-3";
  "session_id": string;
  "events": Array<AssistantEvent | UserMessageEvent | BookingEvent | TurnFailureEvent>;
  "next_cursor": number;
  "current_cursor": number;
  "has_more": boolean;
  "pending_count": number;
  "booking_status": "collecting" | "confirming" | "ready_to_book" | "canceled" | "operator_required" | "collecting_info" | "awaiting_confirmation" | "booking_in_progress" | "booking_unknown" | "booking_failed" | "booked" | "cancel_pending" | "cancel_unknown" | "cancel_failed" | "amendment_pending" | "amendment_unknown" | "cancelled";
  "draft_id": string;
  "active_response": AssistantResponse | null;
  "booking": PublicBooking | null;
  "needs_support"?: boolean;
  "blocked_count"?: number;
  "waiting_for_quota"?: boolean;
  "retry_at"?: number | null;
  "architecture_version"?: string | null;
  "customer_name"?: string | null;
  "customer_phone"?: string | null;
};

export type ChooseInquiryVehicle = {
  "type": "choose_inquiry_vehicle";
  "inquiry_id": string;
  "inquiry_revision": number;
  "vehicle_type": "oto_4_cho" | "oto_7_cho" | "xe_may_dien";
};

export type ConfirmBooking = {
  "type": "confirm_booking";
  "prompt_id": string;
  "booking_revision": number;
  "snapshot_fingerprint": string;
};

export type ConfirmCancel = {
  "type": "confirm_cancel";
  "prompt_id": string;
};

export type Coordinates = {
  "lat": number;
  "lng": number;
};

export type DeliveryAck = {
  "status": "acknowledged";
};

export type DismissInquiry = {
  "type": "dismiss_inquiry";
  "inquiry_id": string;
  "inquiry_revision": number;
};

export type HealthResponse = {
  "status": "ok";
};

export type HttpError = {
  "detail": string;
};

export type LegacyTripSummary = {
  "pickup": string;
  "destination": string;
  "pickup_time": string;
  "passengers": number;
  "vehicle_type": "oto_4_cho" | "oto_7_cho" | "xe_may_dien";
  "vehicle_label": string;
  "contact_phone": string;
  "contact_name": string | null;
  "pickup_note": string | null;
  "luggage": PublicLuggage | null;
  "payment_method": "cash" | null;
  "stops": Array<string> | null;
  "special_requests": Array<string> | null;
  "fare": number;
  "currency": "VND";
  "quote_expires_at": number;
  "booking_revision": number;
  "snapshot_fingerprint": string;
  "prompt_id": string;
  "travel_party"?: {
  [key: string]: number | null;
} | null;
  "base_fare"?: number | null;
  "assistance_fee"?: number | null;
  "provisional"?: boolean;
};

export type MessageInput = {
  "client_message_id": string;
  "text": string;
  "reply_to_response_id"?: string | null;
  "rendered_response_ids"?: Array<string>;
};

export type PublicBooking = {
  "booking_id": string | null;
  "status": string | null;
  "provider_status": string | null;
  "provider": "sandbox" | null;
};

export type PublicCandidate = {
  "candidate_id": string;
  "candidate_set_id": string;
  "target": "pickup" | "destination" | "stops";
  "ordinal": number;
  "label": string;
  "stop_ref"?: string | null;
  "scope_kind"?: "booking" | "inquiry";
  "scope_id"?: string | null;
  "revision"?: number | null;
};

export type PublicCapabilities = {
  "asap": boolean;
  "scheduled": boolean;
  "multi_stop": boolean;
  "inquiry_v2"?: boolean;
  "address_auto_accept_v2"?: boolean;
  "local_address_v2"?: boolean;
  "electric_motorbike"?: boolean;
  "motorbike"?: boolean;
  "weather"?: boolean;
  "location_confirmation"?: boolean;
  "area_estimate"?: boolean;
  "area_assistance"?: boolean;
  "text_only_chat"?: boolean;
  "voice_booking"?: boolean;
};

export type PublicError = {
  "code": string;
  "message": string;
  "retryable": boolean;
};

export type PublicInquiry = {
  "inquiry_id": string;
  "revision": number;
  "origin": string | null;
  "destination": string | null;
  "vehicle": string | null;
  "status": string;
  "expires_at": number;
  "route_fingerprint": string | null;
  "can_use_route": boolean;
  "booking_revision": number;
};

export type PublicLuggage = {
  "count": number;
  "size": "none" | "cabin" | "large" | "mixed" | "unknown";
};

export type PublicPresentation = {
  "contract_version": "chat-presentation-1" | "chat-presentation-2" | "chat-presentation-3";
  "response_id": string;
  "generation": number;
  "prompt_id"?: string | null;
  "snapshot_fingerprint"?: string | null;
  "booking_revision"?: number | null;
  "candidate_set_id"?: string | null;
  "valid_until"?: number | null;
  "scope_kind"?: "booking" | "inquiry";
  "scope_id"?: string | null;
  "inquiry_revision"?: number | null;
};

export type PublicVehicle = {
  "code": "xe_may" | "oto_4_cho" | "oto_7_cho" | "xe_may_dien";
  "label": string;
  "max_passengers": number;
};

export type ReadyResponse = {
  "api_version": "chat-api-2" | "chat-api-3";
  "mode": "sandbox";
  "profile": "fixture_demo" | "chat_sandbox" | "test";
  "llm_provider": string;
  "model": string | null;
  "maps_provider": string;
  "booking_provider": "sandbox";
  "gemini_rpm": number;
  "capabilities": PublicCapabilities;
  "brand_name"?: string;
  "weather_provider"?: string;
  "vehicles"?: Array<PublicVehicle>;
  "fallback_provider"?: string | null;
  "fallback_model"?: string | null;
  "degraded_mode_enabled"?: boolean;
  "status": "ready";
};

export type Receipt = {
  "api_version": "chat-api-2" | "chat-api-3";
  "session_id": string;
  "message_id": string;
  "event_id": string;
  "ingress_seq": number;
  "status": "received" | "queued" | "processing" | "completed" | "failed";
};

export type ResumeBooking = {
  "type": "resume_booking";
};

export type SelectCandidate = {
  "type": "select_candidate";
  "candidate_set_id": string;
  "candidate_id": string;
};

export type SessionInput = {
  "client_session_key": string;
  "customer_phone": string;
  "customer_name": string;
};

export type StopoverSlot = {
  "address": AddressSlot;
  "order": number;
};

export type Tariff = {
  "per_km": number;
  "currency": "VND";
  "vehicle_type": "xe_may" | "oto_4_cho" | "oto_7_cho";
  "source": string;
  "final_amount_basis": "meter";
};

export type TripSummary = {
  "pickup": string;
  "destination": string;
  "pickup_time": string;
  "passengers"?: number | null;
  "vehicle_type": "xe_may" | "oto_4_cho" | "oto_7_cho";
  "vehicle_label": string;
  "customer_phone": string;
  "customer_name": string;
  "pickup_note": string | null;
  "general_note": string | null;
  "stopovers": Array<StopoverSlot>;
  "tariff": Tariff;
  "distance_km": number | null;
  "duration_minutes": number | null;
  "booking_revision": number;
  "snapshot_fingerprint": string;
  "prompt_id": string;
};

export type TurnFailure = {
  "event_id": string;
  "code": string;
  "retryable": boolean;
  "needs_support"?: boolean;
  "message": string;
};

export type TurnFailureEvent = {
  "cursor": number;
  "event_id": string;
  "occurred_at": string;
  "type": "turn_failed";
  "payload": TurnFailure;
};

export type UseInquiryRoute = {
  "type": "use_inquiry_route";
  "inquiry_id": string;
  "inquiry_revision": number;
  "route_fingerprint": string;
  "booking_revision": number;
};

export type UserMessage = {
  "message_id": string;
  "event_id": string;
  "role": "user";
  "text": string;
};

export type UserMessageEvent = {
  "cursor": number;
  "event_id": string;
  "occurred_at": string;
  "type": "message_received";
  "payload": UserMessage;
};

export type WeatherFact = {
  "status": "available" | "unavailable";
  "request_id": string;
  "scope_kind": "booking" | "inquiry";
  "scope_id": string;
  "dependency_fingerprint": string;
  "provider": string;
  "source_ref": string;
  "attribution": string;
  "fetched_at": string;
  "valid_until": string;
  "timezone": string;
  "coverage_start"?: string | null;
  "coverage_end"?: string | null;
  "sample"?: WeatherSample | null;
  "unavailable_reason"?: string | null;
  "missing_fields"?: Array<string>;
  "time_basis"?: string;
};

export type WeatherSample = {
  "timestamp": string;
  "weather_code"?: number | null;
  "temperature_c"?: number | null;
  "precipitation_probability"?: number | null;
  "precipitation_mm"?: number | null;
  "wind_speed_kmh"?: number | null;
};
