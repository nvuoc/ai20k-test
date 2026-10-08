"""Legacy V1–V3 reducer and reusable reference helpers.

Current runtime uses domain.booking_engine.ConversationEngine. This reducer is
retained for archived state compatibility and historical regression fixtures.

The extractor supplies hypotheses, never permission to book. Revision, delivery,
quote, capability and consent guards are independent of the language provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
import unicodedata
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from app.adapters.extractor import ExtractorError
from app.contracts.nlu import (
    NluInput,
    NluResult,
    empty_booking_state,
    validate_candidate_references,
)
from app.contracts.registry import SLOT_NAMES
from app.domain.pickup_time import (
    ASAP,
    merge_time_clarification,
    parse_pickup_time,
    time_expression,
)

CORE = ("destination", "pickup", "pickup_time", "passengers", "vehicle_type", "contact_phone")
VEHICLE_LABELS = {"oto_4_cho": "Xe 4 chỗ", "oto_7_cho": "Xe 7 chỗ"}
VEHICLES = {
    "oto_4_cho": {"max_passengers": 4, "cabin": 2, "large": 1},
    "oto_7_cho": {"max_passengers": 6, "cabin": 4, "large": 2},
}
QUESTIONS = {
    "pickup": "Bạn muốn đón ở đâu? Hãy ghi địa chỉ hoặc địa điểm kèm tỉnh/thành phố.",
    "destination": "Bạn muốn đến đâu? Hãy ghi địa chỉ hoặc địa điểm kèm tỉnh/thành phố.",
    "pickup_time": "Bạn muốn đón ngay hay vào ngày và giờ nào? Ví dụ ‘ngày mai 08:00’ hoặc ‘30 phút nữa’.",
    "passengers": "Chuyến này có bao nhiêu người, tính cả trẻ em?",
    "vehicle_type": "Bạn chọn xe 4 chỗ hay xe 7 chỗ? Xe thử nghiệm chở tối đa 4 hoặc 6 khách.",
    "contact_phone": "Bạn cho mình số điện thoại để liên hệ với người đi nhé.",
    "contact_name": "Bạn đặt hộ ai? Cho mình tên người đi và số điện thoại của người đó nhé.",
    "luggage": "Bạn mang bao nhiêu kiện hành lý và kích cỡ gì? Nếu không có, hãy nói không mang hành lý.",
}


def normalized(text: str) -> str:
    result = unicodedata.normalize("NFD", text.lower()).replace("đ", "d")
    return "".join(c for c in result if unicodedata.category(c) != "Mn")


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def candidate_ref(candidate_set_id: str, place_id: str) -> str:
    """Keep opaque provider references outside the bounded NLU projection."""
    if len(place_id) <= 256:
        return place_id
    return "cand_" + fingerprint([candidate_set_id, place_id])[:32]


def new_state(session_id: str, draft_id: str | None = None) -> dict:
    state = {
        "booking_state": empty_booking_state().model_dump(mode="json"),
        "booking_status": "collecting_info",
        "conversation_context": {"last_bot_message": None, "last_bot_action": None,
                                 "current_focus": None},
        "candidates": [],
        "control": {"session_id": session_id, "draft_id": draft_id or uuid.uuid4().hex,
                    "schema_version": 4, "graph_version": "chat-graph-1",
                    "policy_version": "mvp-chat-policy-1", "booking_revision": 0,
                    "slot_revisions": dict.fromkeys(SLOT_NAMES, 0), "generation": 0,
                    "last_event_id": None, "booking_for": None},
        "resolution": {"locations": {}, "candidate_sets": {}, "route": None,
                       "quote": None, "contact": None},
        "confirmation": {"pending_prompt": None, "accepted_snapshot": None,
                         "slot_evidence": {}},
        "transaction": {"active_operation": None, "booking_result": None,
                        "committed_snapshot": None, "pending_cancel": False},
        "issues": {}, "turn": {}, "last_response": None, "response": None,
    }
    response_id = "welcome_" + session_id
    response = {"response_id": response_id, "generation": 0,
        "text": "Mình giúp bạn đặt xe bằng tin nhắn. Đây là chế độ thử nghiệm: tạo và hủy đơn sandbox, chưa gọi tài xế thật. Bạn muốn đến đâu?",
        "action": "ask_slot", "focus": "destination", "current_focus": "destination",
        "candidates": [], "summary": None, "delivery_status": "planned", "booking": None,
        "booking_status": "collecting_info", "reason": None,
        "presentation": {"contract_version": "chat-presentation-1", "response_id": response_id,
                         "generation": 0}}
    state["last_response"] = response
    state["response"] = response
    return state


def acknowledge(state: dict, response_ids: list[str] | None) -> dict:
    """Apply trusted UI render evidence only to the current response generation."""
    response = state.get("last_response")
    if response and response["response_id"] in (response_ids or []):
        response["delivery_status"] = "rendered"
        state["conversation_context"] = {
            "last_bot_message": response["text"], "last_bot_action": response["action"],
            "current_focus": response.get("focus"),
        }
        prompt = state["confirmation"].get("pending_prompt")
        if prompt and prompt["response_id"] == response["response_id"]:
            prompt["delivery_status"] = "rendered"
    return state


class ChatEngine:
    def __init__(self, extractor: Any, maps: Any, booking: Any, quote: Any = None,
                 quote_ttl_seconds: float = 120, clock: Any = time.time) -> None:
        self.extractor = extractor
        self.maps = maps
        self.booking = booking
        self.quote_adapter = quote
        self.quote_ttl_seconds = quote_ttl_seconds
        self.clock = clock
        self.vehicles = deepcopy(VEHICLES)
        self.vehicle_labels = dict(VEHICLE_LABELS)
        if quote is not None and hasattr(quote, "vehicle_catalog"):
            self.vehicles = {
                code: {"max_passengers": row["max_passengers"],
                       "cabin": row["luggage_cabin"], "large": row["luggage_large"]}
                for code, row in quote.vehicle_catalog.items() if row.get("bookable", True)
            }
            self.vehicle_labels = {code: row["label"] for code, row in quote.vehicle_catalog.items()}

    new_state = staticmethod(new_state)
    acknowledge = staticmethod(acknowledge)

    async def process(self, state: dict, text: str, *, action: dict | None = None,
                      delivered_response_ids: list[str] | None = None,
                      reply_to_response_id: str | None = None, event_id: str | None = None,
                      ingress_guard: Any = None, interpretation: dict | NluResult | None = None,
                      allow_dispatch: bool = True, occurred_at: float | None = None) -> dict:
        state = deepcopy(state)
        if event_id and state["control"].get("last_event_id") == event_id:
            return state
        acknowledge(state, delivered_response_ids)
        state["turn"] = {"event_id": event_id, "changed_slots": [], "issues": [],
                         "questions": [], "confirmation_resolution": {},
                         "occurred_at": occurred_at if occurred_at is not None else self.clock()}
        await self._recover_committed(state)
        before = deepcopy(state)
        previous_prompt = before["confirmation"].get("pending_prompt")
        state["control"]["last_event_id"] = event_id
        if state["booking_status"] in {"booking_unknown", "cancel_unknown"}:
            await self._reconcile(state)
        try:
            if interpretation is not None:
                result = interpretation if isinstance(interpretation, NluResult) else NluResult.model_validate(interpretation)
            elif action:
                acts = self._action_acts(state, action, reply_to_response_id)
                result = NluResult.model_validate({"speech_status": "clear", "dialogue_acts": acts})
            else:
                projection = NluInput.model_validate({
                    "utterance": {"text": text, "asr_confidence": None},
                    "conversation_context": state["conversation_context"],
                    "booking_state": state["booking_state"], "candidates": state["candidates"],
                    "booking_status": state["booking_status"],
                })
                result = await self.extractor(projection)
                if not isinstance(result, NluResult):
                    result = NluResult.model_validate(result)
                validate_candidate_references(result, projection)
        except ExtractorError as exc:
            return self._respond(state, "ask_clarification", self._nlu_error(exc),
                                 reason=exc.code)
        except (ValueError, TypeError):
            return self._respond(state, "ask_clarification",
                                 "Lựa chọn hoặc phản hồi này đã cũ. Hãy trả lời câu hỏi mới nhất nhé.",
                                 reason="INVALID_INTERPRETATION_OR_ACTION")
        if result.speech_status != "clear":
            return self._respond(state, "ask_clarification",
                                 "Mình chưa hiểu chắc thông tin này. Bạn viết rõ lại giúp mình nhé.")
        acts = [a.model_dump(mode="json") for a in result.dialogue_acts]
        state["turn"]["nlu_result"] = result.model_dump(mode="json")
        lower = normalized(text)
        cancel_requested = any(a["intent"] == "cancel" for a in acts)
        if action and action.get("type", action.get("action")) in {"cancel", "cancel_booking"}:
            cancel_requested = True
        if cancel_requested:
            if action is None and (
                re.search(r"\b(neu|mien la|chi khi|voi dieu kien)\b", lower)
                or re.search(r"\b(khong|dung|chua|khoan)\s+(?:co\s+|can\s+)?huy\b", lower)
                or re.search(r"\b(a thoi|giu chuyen|giu don|doi y|khong huy nua)\b", lower)
                or "?" in text or any(a["intent"] == "ask_question" for a in acts)
                or any(a["intent"] in {"provide_info", "change_info"} for a in acts)
            ):
                return self._respond(state, "ask_clarification",
                    "Mình chưa hủy yêu cầu này. Bạn muốn hủy hẳn hay giữ chuyến? Hủy đơn sandbox không mất phí.",
                    reason="AMBIGUOUS_CANCEL_REQUEST")
            if not allow_dispatch:
                state["turn"]["pending_cancel_requested"] = True
                return state
            return await self._cancel(state, event_id)
        if state["booking_status"] in {"booked", "cancel_failed"}:
            if any(a["intent"] in {"provide_info", "change_info", "deny"} for a in acts):
                state["transaction"]["requested_change"] = acts
                return self._respond(state, "handoff",
                    "Đơn thử nghiệm đã tạo chưa hỗ trợ sửa. Bạn có thể hủy đơn rồi mở chuyến mới.")
            return self._respond(state, "answer_question", self._booking_text(state))
        if state["booking_status"] == "cancelled":
            return self._respond(state, "goodbye", "Yêu cầu này đã hủy. Hãy mở chuyến mới nếu bạn muốn đặt tiếp.")
        if state["booking_status"] in {"booking_unknown", "cancel_unknown"}:
            return self._respond(state, "inform_pending", "Kết quả giao dịch đang được đối soát. Mình sẽ cập nhật khi xác định được; chưa tạo đơn khác.")
        semantic_block = self._semantic_review(state, lower, acts)
        changed: set[str] = set()
        denied: set[str] = set()
        confirms: set[str] = set()
        confirm_all = False
        for act in acts:
            intent, target, value = act["intent"], act["target"], act["value"]
            if intent in {"provide_info", "change_info"}:
                if target not in semantic_block:
                    if target == "pickup_time" and intent == "change_info":
                        state["turn"]["reanchor_pickup_time"] = True
                    self._replace_slot(state, target, value, changed)
            elif intent == "select_candidate":
                if self._select_candidate(state, target, value, action, before):
                    changed.add(target)
                else:
                    state["turn"]["issues"].append("STALE_CANDIDATE")
            elif intent == "reject_candidate":
                self._reject_candidates(state, target, value)
            elif intent == "deny":
                denied.add(target) if target else denied.update(SLOT_NAMES)
                state["confirmation"]["accepted_snapshot"] = None
                if target:
                    state["booking_state"][target]["confirmed"] = False
                    if not any(a["target"] == target and a["intent"] in {"provide_info", "change_info"} for a in acts):
                        state["issues"][f"uncertain_{target}"] = {
                            "target": target, "text": f"Bạn muốn sửa thông tin {self._slot_label(target)} như thế nào?"}
                else:
                    state["turn"]["issues"].append("DENIED_SUMMARY")
            elif intent == "confirm":
                confirms.add(target) if target else None
                confirm_all = confirm_all or target is None
            elif intent == "ask_question":
                state["turn"]["questions"].append(value)
            elif intent == "request_repeat":
                state["turn"]["repeat"] = True
            elif intent == "no_understanding":
                state["turn"]["issues"].append("NO_UNDERSTANDING")
        if changed:
            state["control"]["booking_revision"] += 1
            state["confirmation"]["accepted_snapshot"] = None
            state["confirmation"]["pending_prompt"] = None
            state["booking_status"] = "collecting_info"
            state["turn"]["changed_slots"] = sorted(changed)
        # Confirm only values which existed in the shown prompt, before this turn.
        prompt_valid = self._prompt_valid(before, previous_prompt, reply_to_response_id)
        if prompt_valid and (confirms or confirm_all):
            scope = set(previous_prompt["scope"])
            selected = scope if confirm_all else scope.intersection(confirms)
            if re.search(r"\b(chi|thoi)\b", lower):
                mentioned = self._mentioned_slots(lower)
                selected = selected.intersection(mentioned) if mentioned else set()
            selected -= changed | denied | semantic_block
            for slot in selected:
                if state["booking_state"][slot]["value"] is not None:
                    state["booking_state"][slot]["confirmed"] = True
                    state["confirmation"]["slot_evidence"][slot] = {
                        "revision": state["control"]["slot_revisions"][slot],
                        "event_id": event_id, "prompt_id": previous_prompt["prompt_id"]}
            consent = (confirm_all and selected == scope and not changed and not denied
                       and not semantic_block and not state["turn"]["issues"]
                       and not state["turn"]["questions"])
            state["turn"]["confirmation_resolution"] = {
                "acknowledged_slots": sorted(selected), "booking_consent": consent}
            if consent and previous_prompt["purpose"] == "confirm_booking":
                state["confirmation"]["accepted_snapshot"] = {
                    "snapshot_id": previous_prompt["snapshot_fingerprint"],
                    "prompt_id": previous_prompt["prompt_id"], "accepted_event_id": event_id,
                    "booking_revision": state["control"]["booking_revision"],
                    "snapshot_fingerprint": previous_prompt["snapshot_fingerprint"],
                    "quote_id": previous_prompt.get("quote_id"), "accepted_at": self.clock()}
        elif confirms or confirm_all:
            state["turn"]["issues"].append("UNSHOWN_OR_STALE_CONFIRMATION")
        await self._resolve(state, changed)
        self._validate_requirements(state)
        if self._can_create(state):
            if not allow_dispatch:
                state["turn"]["ready_to_dispatch"] = True
                return state
            if ingress_guard is not None:
                allowed = ingress_guard()
                allowed = await allowed if inspect.isawaitable(allowed) else allowed
                if not allowed:
                    state["confirmation"]["accepted_snapshot"] = None
                    return self._respond(state, "inform_pending", "Mình đã nhận thêm tin nhắn. Mình sẽ xử lý thông tin mới trước khi đặt.", reason="SUPERSEDED_BEFORE_DISPATCH")
            # Quote TTL is checked again at the dispatch boundary.
            if self._quote_valid(state):
                return await self._create(state)
            state["confirmation"]["accepted_snapshot"] = None
        return self._decide_response(state)

    @staticmethod
    def _nlu_error(exc: ExtractorError) -> str:
        if exc.code in {"RATE_LIMITED", "RATE_LIMIT", "QUOTA_EXCEEDED"}:
            wait = getattr(exc, "retry_after_seconds", None)
            duration = f"khoảng {wait} giây" if wait else "một lát"
            return f"Gemini đang đạt giới hạn yêu cầu. Bạn chờ {duration} rồi gửi lại nhé; thông tin chuyến vẫn được giữ."
        if exc.code == "PROVIDER_AUTH_ERROR":
            return "Gemini từ chối API key hoặc quyền truy cập. Hãy kiểm tra GEMINI_API_KEY ở cấu hình backend; thông tin chuyến vẫn được giữ."
        if exc.code == "PROVIDER_MODEL_UNAVAILABLE":
            return "Model Gemini trong cấu hình chưa khả dụng với API key này. Hãy kiểm tra GEMINI_MODEL ở backend; thông tin chuyến vẫn được giữ."
        return "Mình chưa xử lý được tin nhắn này. Bạn gửi lại hoặc viết ngắn hơn nhé; thông tin chuyến vẫn được giữ."

    def _action_acts(self, state: dict, action: dict, reply_to: str | None) -> list[dict]:
        kind = action.get("type", action.get("action"))
        if kind in {"confirm", "confirm_booking"}:
            prompt = state["confirmation"].get("pending_prompt")
            if not self._prompt_valid(state, prompt, action.get("response_id", reply_to)):
                raise ValueError("STALE_CONFIRMATION")
            for key in ("prompt_id", "snapshot_fingerprint", "booking_revision"):
                if key in action and action[key] != prompt.get(key):
                    raise ValueError("STALE_CONFIRMATION")
            return [{"intent": "confirm", "target": None, "value": None}]
        if kind in {"select_candidate", "candidate"}:
            batch = next((b for b in state["resolution"]["candidate_sets"].values()
                          if b["candidate_set_id"] == action.get("candidate_set_id")), None)
            target = action.get("target") or (batch["target"] if batch else None)
            return [{"intent": "select_candidate", "target": target,
                     "value": action.get("candidate_id")}]
        if kind == "cancel_booking":
            booking = state["transaction"].get("booking_result")
            if not booking or action.get("booking_id") != booking["booking_id"]:
                raise ValueError("BOOKING_REFERENCE_MISMATCH")
            return [{"intent": "cancel", "target": None, "value": None}]
        if kind == "cancel_draft":
            if action.get("draft_id") != state["control"]["draft_id"] or state["transaction"].get("booking_result"):
                raise ValueError("DRAFT_REFERENCE_MISMATCH")
            return [{"intent": "cancel", "target": None, "value": None}]
        if kind == "cancel":
            return [{"intent": "cancel", "target": None, "value": None}]
        if kind in {"repeat", "request_repeat"}:
            return [{"intent": "request_repeat", "target": None, "value": None}]
        raise ValueError("UNKNOWN_ACTION")

    def _semantic_review(self, state: dict, lower: str, acts: list[dict]) -> set[str]:
        blocked: set[str] = set()
        changes_by_target = {a["target"]: a["value"] for a in acts
                             if a["intent"] in {"provide_info", "change_info"}}
        hypothetical = "?" in lower or any(a["intent"] == "ask_question" for a in acts)
        # Preserve an explicit scheduling request even when NLU omitted its slot.
        requested_time = time_expression(lower)
        future = re.search(r"\b(ngay mai|sang mai|chieu mai|toi mai|dat truoc)\b", lower) or requested_time
        immediate = re.search(r"\b(di ngay|ngay bay gio|doi sang ngay|dat ngay)\b", lower)
        provided_time = changes_by_target.get("pickup_time")
        if provided_time and not (future and normalized(provided_time).strip() in ASAP):
            state["issues"].pop("raw_scheduled_request", None)
        elif immediate and not future:
            state["issues"].pop("raw_scheduled_request", None)
        elif future and not hypothetical:
            state["issues"]["raw_scheduled_request"] = {"target": "pickup_time",
                "text": "Bạn muốn đặt trước vào ngày nào và lúc mấy giờ? Mình cần giờ đón rõ ràng để cập nhật chuyến."}
            if provided_time and normalized(provided_time).strip() in ASAP:
                blocked.add("pickup_time")
        multi_stop = re.search(r"\b(ghe(?!\s+(?:tre em|em be|cho be))|them diem dung|dung o)\b|\bqua\b.+\broi den\b", lower)
        remove_stops = re.search(r"\b(bo (?:het )?diem dung|di thang|khong dung giua duong)\b", lower)
        if remove_stops or changes_by_target.get("stops") == []:
            state["issues"].pop("raw_stop_request", None)
        elif multi_stop and not hypothetical:
            state["issues"]["raw_stop_request"] = {"target": "stops",
                "text": "Mình đã ghi nhận bạn muốn dừng giữa đường. Bản thử nghiệm chưa hỗ trợ; hãy nói rõ bỏ hết điểm dừng nếu muốn đi thẳng."}
        unavailable_vehicle = re.search(r"\blimousine\b|\b(?:16|29|45)\s*cho\b", lower)
        if changes_by_target.get("vehicle_type") in self.vehicles and not unavailable_vehicle:
            state["issues"].pop("raw_vehicle_request", None)
        elif unavailable_vehicle and not hypothetical:
            state["issues"]["raw_vehicle_request"] = {"target": "vehicle_type",
                "text": "Mình đã ghi nhận loại xe ngoài phạm vi thử nghiệm. Bạn có muốn chọn xe 4 chỗ hoặc 7 chỗ không?"}
        payment = re.search(r"\b(thanh toan|tra|dung)\b.+\b(momo|zalopay|chuyen khoan|vi dien tu)\b", lower)
        if changes_by_target.get("payment_method") == "cash" and not payment:
            state["issues"].pop("raw_payment_request", None)
        elif payment and not hypothetical:
            state["issues"]["raw_payment_request"] = {"target": "payment_method",
                "text": "Mình đã ghi nhận phương thức thanh toán chưa hỗ trợ. Bản thử nghiệm chỉ ghi nhận tiền mặt; hãy nói rõ chọn tiền mặt nếu đồng ý."}
        support = re.search(r"\b(can ghe tre em|can xe (?:ho tro )?xe lan|cho thu cung|mang cho|mang meo)\b", lower)
        if changes_by_target.get("special_requests") == []:
            state["issues"].pop("raw_support_request", None)
        elif support and not hypothetical:
            state["issues"]["raw_support_request"] = {"target": "special_requests",
                "text": "Mình đã ghi nhận yêu cầu hỗ trợ đặc biệt; sandbox chưa bảo đảm đáp ứng. Hãy rút yêu cầu rõ ràng hoặc dùng dịch vụ có hỗ trợ nhu cầu này."}
        if re.search(r"\b(dat ho|goi ho|cho me|cho bo|cho ban toi)\b", lower):
            state["control"]["booking_for"] = "other"
        if re.search(r"\b(tu toi di|toi tu di|khong dat ho)\b", lower):
            state["control"]["booking_for"] = "self"
        unsupported = re.search(r"\b(khu hoi|hai chieu|thue theo gio|nhieu xe)\b", lower)
        withdrawal = re.search(r"\b(bo|khong can|khong di|huy yeu cau)\b.{0,25}(khu hoi|hai chieu|thue theo gio|nhieu xe)", lower) or "mot chieu thoi" in lower
        if withdrawal:
            state["issues"].pop("unsupported_journey", None)
        elif unsupported:
            state["issues"]["unsupported_journey"] = {"target": None,
                "text": "Bản thử nghiệm chỉ hỗ trợ một xe, một chiều. Bạn có muốn bỏ yêu cầu khứ hồi/thuê theo giờ để tiếp tục không?"}
        conditional = re.search(r"\b(neu|mien la|voi dieu kien|chi khi)\b", lower)
        uncertain = re.search(r"\b(chua chac|khong chac|co le|hoac)\b", lower)
        changes = any(a["intent"] in {"provide_info", "change_info"} for a in acts)
        if conditional:
            state["turn"]["issues"].append("CONDITIONAL_REQUEST")
            # A conditional proposal must not replace an existing unconditional value.
            blocked.update(a["target"] for a in acts if a["intent"] == "change_info")
        if uncertain:
            targets = self._mentioned_slots(lower)
            blocked.update(targets)
            if not targets:
                blocked.update(a["target"] for a in acts if a["target"] is not None)
            for target in blocked:
                state["booking_state"][target]["confirmed"] = False
                state["issues"][f"uncertain_{target}"] = {"target": target,
                    "text": f"Bạn xác định lại {self._slot_label(target)} giúp mình nhé."}
            state["turn"]["issues"].append("UNCERTAIN_REQUEST")
        if any(a["intent"] == "confirm" for a in acts):
            if re.search(r"\b(nhung|doi|sua|sai|khong phai|a thoi)\b", lower) and not changes:
                state["turn"]["issues"].append("UNEXTRACTED_CORRECTION")
            if (any(a["intent"] == "confirm" and a["target"] is None for a in acts)
                and (self._mentioned_slots(lower) or re.search(r"\bdia chi\b", lower))
                and not re.search(r"\b(dat (?:di|giup|chuyen|xe|theo)|dong y (?:tao|dat)|xac nhan dat)\b", lower)):
                state["turn"]["issues"].append("AMBIGUOUS_CONFIRM_SCOPE")
        if conditional or uncertain or state["turn"]["issues"]:
            state["confirmation"]["accepted_snapshot"] = None
        return blocked

    @staticmethod
    def _mentioned_slots(text: str) -> set[str]:
        patterns = {"pickup": r"(diem don|dia chi don|noi don|don o)",
                    "destination": r"(diem den|dia chi den|noi den)",
                    "pickup_time": r"\b(gio|thoi gian|luc)\b",
                    "vehicle_type": r"\b(xe|loai xe)\b",
                    "passengers": r"\b(nguoi|khach)\b",
                    "contact_phone": r"\b(so dien thoai|dien thoai|sdt)\b",
                    "luggage": r"\b(hanh ly|vali|kien)\b"}
        return {slot for slot, pattern in patterns.items() if re.search(pattern, text)}

    @staticmethod
    def _slot_label(slot: str) -> str:
        return {"pickup": "điểm đón", "destination": "điểm đến", "pickup_time": "giờ đi",
                "passengers": "số người", "vehicle_type": "loại xe", "contact_phone": "số điện thoại",
                "luggage": "hành lý"}.get(slot, slot)

    def _replace_slot(self, state: dict, target: str, value: Any, changed: set[str]) -> None:
        old = state["booking_state"][target]["value"]
        previous_time = state["resolution"].get("pickup_time") if target == "pickup_time" else None
        reference_at = None
        if target == "pickup_time" and state["conversation_context"].get("current_focus") == "pickup_time":
            value, reference_at = merge_time_clarification(previous_time, value)
        reanchor = target == "pickup_time" and state["turn"].get("reanchor_pickup_time") and bool(
            re.search(r"\b(?:sau|nua|som hon|muon hon)\b", normalized(value))
        )
        state["issues"].pop(f"uncertain_{target}", None)
        if old == value and not reanchor:
            return
        state["booking_state"][target] = {"value": deepcopy(value), "confirmed": False}
        state["control"]["slot_revisions"][target] += 1
        state["confirmation"]["slot_evidence"].pop(target, None)
        changed.add(target)
        if target == "pickup_time":
            schedule = parse_pickup_time(
                value, reference_at if reference_at is not None else state["turn"].get("occurred_at", self.clock()),
                previous_pickup_at=(previous_time or {}).get("pickup_at"),
            )
            schedule["source_slot_revision"] = state["control"]["slot_revisions"][target]
            state["resolution"]["pickup_time"] = schedule
        if target in {"pickup", "destination"}:
            state["resolution"]["locations"].pop(target, None)
            state["resolution"]["candidate_sets"].pop(target, None)
            state["candidates"] = [c for c in state["candidates"] if c["target"] != target]
        if target in {"pickup", "destination", "pickup_time", "vehicle_type", "passengers", "luggage", "stops", "special_requests", "payment_method"}:
            state["resolution"]["quote"] = None
        if target in {"pickup", "destination", "pickup_time", "vehicle_type", "stops"}:
            state["resolution"]["route"] = None
        if target == "pickup_note" and re.search(r"\b(cong|cua|ga|san don|diem hen)\b", normalized(value)):
            previous = state["resolution"]["locations"].get("pickup")
            if previous:
                state["turn"]["previous_pickup_location"] = deepcopy(previous)
            state["resolution"]["locations"].pop("pickup", None)
            state["resolution"]["route"] = None
            state["resolution"]["quote"] = None

    def _select_candidate(self, state: dict, target: str, candidate_id: str,
                          action: dict | None, before: dict) -> bool:
        batch = before["resolution"]["candidate_sets"].get(target)
        response = before.get("last_response")
        if not batch or not response or response.get("delivery_status") != "rendered":
            return False
        if batch["expires_at"] <= self.clock() or batch["revision"] != before["control"]["slot_revisions"][target]:
            return False
        if response["response_id"] != batch.get("presented_response_id"):
            return False
        if action and action.get("candidate_set_id", batch["candidate_set_id"]) != batch["candidate_set_id"]:
            return False
        place = next((p for p in batch["places"]
                      if candidate_ref(batch["candidate_set_id"], p["id"]) == candidate_id), None)
        if place is None or batch.get("exploratory") or place.get("exploratory"):
            return False
        state["booking_state"][target] = {"value": place["label"], "confirmed": True}
        state["control"]["slot_revisions"][target] += 1
        state["resolution"]["locations"][target] = {
            "status": "valid", "place": deepcopy(place),
            "source_slot_revision": state["control"]["slot_revisions"][target]}
        state["confirmation"]["slot_evidence"][target] = {
            "revision": state["control"]["slot_revisions"][target],
            "candidate_set_id": batch["candidate_set_id"]}
        state["resolution"]["candidate_sets"].pop(target, None)
        state["candidates"] = []
        state["resolution"]["quote"] = None
        state["resolution"]["route"] = None
        return True

    def _reject_candidates(self, state: dict, target: str, candidate_id: str | None) -> None:
        batch = state["resolution"]["candidate_sets"].get(target)
        if batch:
            batch["places"] = [p for p in batch["places"] if candidate_id and
                               candidate_ref(batch["candidate_set_id"], p["id"]) != candidate_id]
            batch["candidate_set_id"] = uuid.uuid4().hex
            if not batch["places"]:
                state["resolution"]["candidate_sets"].pop(target, None)
                state["resolution"]["locations"][target] = {"status": "not_found",
                    "source_slot_revision": state["control"]["slot_revisions"][target]}
        state["candidates"] = []

    def _prompt_valid(self, state: dict, prompt: dict | None, reply_to: str | None) -> bool:
        if not prompt or prompt.get("delivery_status") != "rendered":
            return False
        if reply_to is not None and reply_to != prompt["response_id"]:
            return False
        return (prompt["booking_revision"] == state["control"]["booking_revision"]
                and prompt["snapshot_fingerprint"] == self._snapshot_fingerprint(state)
                and all(prompt["slot_revisions"][s] == state["control"]["slot_revisions"][s] for s in prompt["scope"]))

    def _resolve_pickup_time(self, state):
        raw = state["booking_state"]["pickup_time"]["value"]
        if not raw:
            state["resolution"].pop("pickup_time", None)
            return None
        revision = state["control"]["slot_revisions"]["pickup_time"]
        schedule = state["resolution"].get("pickup_time")
        if not schedule or schedule.get("raw") != raw or schedule.get("source_slot_revision") != revision:
            schedule = parse_pickup_time(raw, state["turn"].get("occurred_at", self.clock()))
            schedule["source_slot_revision"] = revision
            state["resolution"]["pickup_time"] = schedule
        return schedule

    def _pickup_time_valid(self, state):
        schedule = state["resolution"].get("pickup_time") or {}
        if schedule.get("status") != "valid":
            return False
        return schedule.get("mode") == "asap" or (
            schedule.get("mode") == "scheduled" and schedule.get("pickup_at")
            and datetime.fromisoformat(schedule["pickup_at"]).timestamp() > self.clock()
        )

    @staticmethod
    def _pickup_schedule(state):
        schedule = state["resolution"].get("pickup_time") or {}
        return {key: schedule.get(key) for key in ("mode", "pickup_at", "timezone")}

    async def _resolve(self, state: dict, changed: set[str]) -> None:
        schedule = self._resolve_pickup_time(state)
        slots = state["booking_state"]
        async def resolve_slot(target: str) -> tuple[str, int, dict]:
            query = slots[target]["value"]
            note = slots["pickup_note"]["value"]
            context = {"pickup_note": note, "session_id": state["control"]["session_id"]}
            if target == "pickup" and note and re.search(r"\b(cong|cua|ga|san don|diem hen)\b", normalized(note)):
                query += ", " + note
            revision = state["control"]["slot_revisions"][target]
            try:
                result = await self.maps.resolve(query, target=target, context=context)
            except Exception:
                result = {"status": "unavailable"}
            return target, revision, result
        needed = []
        for target in ("pickup", "destination"):
            value = slots[target]["value"]
            location = state["resolution"]["locations"].get(target)
            if value and (not location or location.get("status") == "unavailable"
                          or location.get("source_slot_revision") != state["control"]["slot_revisions"][target]):
                needed.append(resolve_slot(target))
        for target, revision, result in await asyncio.gather(*needed):
            if revision != state["control"]["slot_revisions"][target]:
                continue
            status = result.get("status", "unavailable")
            location = {"status": status, "source_slot_revision": revision}
            if result.get("area"):
                location.update(entity_kind="area", area=deepcopy(result["area"]))
            if status in {"resolved", "unique", "valid"} and result.get("place") and not result.get("exploratory"):
                old_place = state["turn"].get("previous_pickup_location", {}).get("place") if target == "pickup" else None
                if old_place and old_place.get("id") != result["place"].get("id"):
                    # A gate is part of the operational pickup, not merely a note.
                    state["booking_state"]["pickup"] = {"value": result["place"]["label"], "confirmed": False}
                    state["control"]["slot_revisions"]["pickup"] += 1
                    state["confirmation"]["slot_evidence"].pop("pickup", None)
                    location["source_slot_revision"] = state["control"]["slot_revisions"]["pickup"]
                    state["turn"]["changed_slots"] = sorted(set(state["turn"]["changed_slots"]) | {"pickup"})
                location.update({"status": "valid", "place": result["place"]})
                state["resolution"]["candidate_sets"].pop(target, None)
            elif status == "ambiguous":
                places = result.get("candidates", [])[:10]
                if places:
                    state["resolution"]["candidate_sets"][target] = {
                        "candidate_set_id": uuid.uuid4().hex, "target": target, "revision": revision,
                        "places": places, "expires_at": self.clock() + 300,
                        "exploratory": bool(result.get("exploratory")), "presented_response_id": None}
                location["message"] = result.get("message") or result.get("clarification")
            else:
                location["status"] = status if status in {"not_found", "unavailable", "needs_clarification", "unsupported"} else "needs_clarification"
                location["message"] = result.get("message") or result.get("clarification")
            state["resolution"]["locations"][target] = location
        locations = state["resolution"]["locations"]
        if all(locations.get(t, {}).get("status") == "valid" for t in ("pickup", "destination")) and slots["vehicle_type"]["value"] in self.vehicles:
            pickup = locations["pickup"]["place"]
            destination = locations["destination"]["place"]
            departure = schedule.get("pickup_at") if schedule and schedule.get("status") == "valid" else None
            route_fp = fingerprint([pickup, destination, slots["vehicle_type"]["value"], departure])
            route = state["resolution"].get("route")
            if not route or route.get("dependency_fingerprint") != route_fp:
                try:
                    kwargs = {"vehicle_type": slots["vehicle_type"]["value"]}
                    parameters = inspect.signature(self.maps.route).parameters
                    if departure and ("departure_time" in parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())):
                        kwargs["departure_time"] = datetime.fromisoformat(departure)
                    route = await self.maps.route(pickup, destination, **kwargs)
                    if route and route.get("status", "ok") in {"ok", "resolved", "available", "success"}:
                        route["dependency_fingerprint"] = route_fp
                        state["resolution"]["route"] = route
                    else:
                        state["resolution"]["route"] = None
                except Exception:
                    state["resolution"]["route"] = None
            if state["resolution"]["route"] and not self._quote_valid(state):
                state["confirmation"]["accepted_snapshot"] = None
                try:
                    if self.quote_adapter is not None:
                        route_input = {k: v for k, v in state["resolution"]["route"].items() if k != "dependency_fingerprint"}
                        quote = self.quote_adapter.quote(route_input, slots["vehicle_type"]["value"])
                        quote = await quote if inspect.isawaitable(quote) else quote
                    else:
                        distance = state["resolution"]["route"].get("distance_m", 0)
                        base, per_km = (18000, 11500) if slots["vehicle_type"]["value"] == "oto_4_cho" else (24000, 14500)
                        quote = {"amount": int((base + distance / 1000 * per_km + 999) // 1000 * 1000),
                                 "currency": "VND", "provider": "sandbox", "kind": "sandbox"}
                    if quote and quote.get("status", "ok") in {"ok", "available", "success", "quoted"}:
                        quote = deepcopy(quote)
                        quote.setdefault("quote_id", quote.get("id", uuid.uuid4().hex))
                        quote.setdefault("expires_at", self.clock() + self.quote_ttl_seconds)
                        if isinstance(quote["expires_at"], str):
                            quote["expires_at"] = datetime.fromisoformat(quote["expires_at"].replace("Z", "+00:00")).timestamp()
                        quote["dependency_fingerprint"] = self._quote_fingerprint(state)
                        state["resolution"]["quote"] = quote
                    else:
                        state["resolution"]["quote"] = None
                except Exception:
                    state["resolution"]["quote"] = None

    def _required(self, state: dict) -> list[str]:
        required = list(CORE)
        if state["control"].get("booking_for") == "other":
            required.append("contact_name")
        for location in state["resolution"]["locations"].values():
            place = location.get("place", {})
            types = place.get("place_types", place.get("types", []))
            if "airport" in types or place.get("is_airport"):
                required.append("luggage")
                break
        return required

    def _validate_requirements(self, state: dict) -> None:
        for key in list(state["issues"]):
            if key.startswith("validation_"):
                del state["issues"][key]
        slots = state["booking_state"]
        def issue(key: str, target: str, text: str) -> None:
            state["issues"]["validation_" + key] = {"target": target, "text": text}
        schedule = self._resolve_pickup_time(state)
        if schedule and not self._pickup_time_valid(state):
            issue("pickup_time", "pickup_time", schedule.get("message") or "Giờ đón đã qua. Bạn cho mình một thời điểm đón mới trong tương lai nhé.")
            state["confirmation"]["accepted_snapshot"] = None
        phone = slots["contact_phone"]["value"]
        if phone:
            phone = re.sub(r"[\s.()\-]", "", phone)
            if phone.startswith("+84"):
                phone = "0" + phone[3:]
            elif phone.startswith("84") and len(phone) == 11:
                phone = "0" + phone[2:]
            if not re.fullmatch(r"0(?:[35789]\d{8}|2\d{9})", phone):
                issue("phone", "contact_phone", "Số điện thoại chưa hợp lệ. Bạn nhập số Việt Nam 10 chữ số (hoặc +84) nhé.")
                state["resolution"]["contact"] = None
            else:
                state["resolution"]["contact"] = {"phone": phone, "name": slots["contact_name"]["value"]}
        vehicle = slots["vehicle_type"]["value"]
        if vehicle and vehicle not in self.vehicles:
            issue("vehicle", "vehicle_type", "Bản thử nghiệm hỗ trợ xe 4 chỗ và 7 chỗ. Bạn muốn chọn loại nào?")
        passengers = slots["passengers"]["value"]
        if vehicle in self.vehicles and passengers and passengers > self.vehicles[vehicle]["max_passengers"]:
            issue("capacity", "vehicle_type", f"{self.vehicle_labels[vehicle]} thử nghiệm chở tối đa {self.vehicles[vehicle]['max_passengers']} khách. Bạn cần đổi loại xe hoặc điều chỉnh yêu cầu.")
        luggage = slots["luggage"]["value"]
        if luggage:
            count, size = luggage["count"], luggage["size"]
            if count is None or size == "unknown":
                issue("luggage", "luggage", "Bạn mang bao nhiêu kiện hành lý, cỡ xách tay hay vali lớn?")
            elif vehicle in self.vehicles and size != "none" and count > self.vehicles[vehicle]["cabin" if size == "cabin" else "large"]:
                issue("luggage_capacity", "vehicle_type", "Lượng hành lý vượt sức chứa thử nghiệm của xe đã chọn. Bạn có muốn đổi sang xe lớn hơn không?")
        if slots["payment_method"]["value"] not in {None, "cash"}:
            issue("payment", "payment_method", "Bản thử nghiệm hỗ trợ tiền mặt. Bạn muốn đổi sang tiền mặt không?")
        if slots["stops"]["value"]:
            issue("stops", "stops", "Bản thử nghiệm chưa hỗ trợ điểm dừng giữa đường. Hãy nói bỏ hết điểm dừng nếu bạn muốn đi thẳng.")
        if slots["special_requests"]["value"]:
            issue("special_requests", "special_requests", "Bản thử nghiệm chưa bảo đảm ghế trẻ em, xe lăn hoặc thú cưng. Bạn có muốn rút yêu cầu này để tiếp tục không?")
        locations = state["resolution"]["locations"]
        pickup, destination = locations.get("pickup", {}).get("place"), locations.get("destination", {}).get("place")
        if pickup and destination and pickup.get("id") == destination.get("id"):
            issue("same_place", "destination", "Điểm đón và điểm đến đang trùng nhau. Bạn muốn đến đâu khác?")

    def _quote_fingerprint(self, state: dict) -> str:
        slots = state["booking_state"]
        return fingerprint({"route": state["resolution"].get("route"),
            "inputs": {s: slots[s]["value"] for s in ("pickup_time", "vehicle_type", "passengers", "luggage", "payment_method", "stops", "special_requests")},
            "pickup_schedule": self._pickup_schedule(state)})

    def _quote_valid(self, state: dict) -> bool:
        quote = state["resolution"].get("quote")
        return bool(quote and quote.get("expires_at", 0) > self.clock()
                    and quote.get("dependency_fingerprint") == self._quote_fingerprint(state))

    def _snapshot_payload(self, state: dict) -> dict:
        return deepcopy({"session_id": state["control"]["session_id"],
                "draft_id": state["control"]["draft_id"],
                "booking_revision": state["control"]["booking_revision"],
                "slots": {s: state["booking_state"][s]["value"] for s in SLOT_NAMES},
                "pickup_schedule": self._pickup_schedule(state),
                "pickup": state["resolution"]["locations"].get("pickup", {}).get("place"),
                "destination": state["resolution"]["locations"].get("destination", {}).get("place"),
                "contact": state["resolution"].get("contact"),
                "route": state["resolution"].get("route"),
                "quote": state["resolution"].get("quote"),
                "policy_version": state["control"]["policy_version"]})

    def _snapshot_fingerprint(self, state: dict) -> str:
        return fingerprint(self._snapshot_payload(state))

    def _can_create(self, state: dict) -> bool:
        accepted = state["confirmation"].get("accepted_snapshot")
        supplied = [s for s in SLOT_NAMES if state["booking_state"][s]["value"] is not None]
        return bool(accepted and not state["issues"] and not state["turn"]["issues"]
            and not state["turn"]["questions"] and not state["turn"]["changed_slots"]
            and state["booking_status"] == "awaiting_confirmation"
            and state["transaction"]["booking_result"] is None
            and all(state["booking_state"][s]["value"] is not None for s in self._required(state))
            and all(state["booking_state"][s]["confirmed"] for s in supplied)
            and all(state["resolution"]["locations"].get(s, {}).get("status") == "valid" for s in ("pickup", "destination"))
            and state["resolution"].get("contact") and self._quote_valid(state) and self._pickup_time_valid(state)
            and accepted["snapshot_fingerprint"] == self._snapshot_fingerprint(state))

    async def _create(self, state: dict) -> dict:
        if not self._pickup_time_valid(state):
            state["confirmation"]["accepted_snapshot"] = None
            self._validate_requirements(state)
            self._decide_response(state)
            state["last_response"]["reason"] = "PICKUP_TIME_ELAPSED"
            return state
        payload = self._snapshot_payload(state)
        key = "create:" + state["control"]["draft_id"] + ":" + fingerprint(payload)
        state["transaction"]["active_operation"] = {"type": "create", "idempotency_key": key,
            "payload": payload, "status": "dispatched"}
        state["booking_status"] = "booking_in_progress"
        try:
            result = await self.booking.create(payload, key)
        except TimeoutError:
            state["booking_status"] = "booking_unknown"
            state["transaction"]["active_operation"]["status"] = "unknown"
            return self._respond(state, "inform_pending", "Đang kiểm tra kết quả tạo đơn thử nghiệm. Mình sẽ đối soát đơn này trước khi tạo yêu cầu khác.")
        self._integrate_create(state, result)
        return self._respond(state, "inform_success" if state["booking_status"] == "booked" else "inform_failure", self._booking_text(state))

    def _integrate_create(self, state: dict, result: dict) -> None:
        operation = state["transaction"]["active_operation"]
        if result.get("status") == "succeeded" and result.get("booking_id"):
            state["booking_status"] = "cancelled" if result.get("provider_status") == "cancelled" else "booked"
            state["transaction"]["booking_result"] = result
            state["transaction"]["committed_snapshot"] = result.get("payload", operation["payload"])
            operation["status"] = "succeeded"
        else:
            state["booking_status"] = "booking_failed"
            operation["status"] = "rejected"
        state["confirmation"]["pending_prompt"] = None
        state["confirmation"]["accepted_snapshot"] = None

    async def _recover_committed(self, state: dict) -> None:
        """Ledger/provider outcome wins when a crash predates the final checkpoint."""
        if state["transaction"].get("booking_result") or not hasattr(self.booking, "find_by_draft"):
            return
        recovered = await self.booking.find_by_draft(state["control"]["draft_id"])
        if not recovered:
            return
        payload = recovered["payload"]
        state["transaction"]["active_operation"] = {
            "type": "create", "idempotency_key": "create:" + payload["draft_id"] + ":" + fingerprint(payload),
            "payload": payload, "status": "succeeded"}
        self._integrate_create(state, recovered | {"status": "succeeded"})

    async def _cancel(self, state: dict, event_id: str | None) -> dict:
        if state["booking_status"] == "cancelled":
            return self._respond(state, "goodbye", "Yêu cầu này đã hủy trước đó.")
        if state["booking_status"] in {"booking_unknown", "cancel_unknown"}:
            state["transaction"]["pending_cancel"] = True
            await self._reconcile(state)
            if state["booking_status"] in {"booking_unknown", "cancel_unknown"}:
                return self._respond(state, "inform_pending", "Mình đã ghi nhận yêu cầu hủy và đang đối soát kết quả giao dịch.")
        booking = state["transaction"].get("booking_result")
        if not booking:
            state["booking_status"] = "cancelled"
            state["transaction"]["last_outcome"] = {"status": "cancelled", "source": "local_draft"}
            state["confirmation"]["pending_prompt"] = None
            state["confirmation"]["accepted_snapshot"] = None
            return self._respond(state, "goodbye", "Đã hủy bản nháp. Chưa có đơn xe nào được tạo.")
        key = "cancel:" + booking["booking_id"]
        state["transaction"]["active_operation"] = {"type": "cancel", "idempotency_key": key,
            "booking_id": booking["booking_id"], "status": "dispatched"}
        state["booking_status"] = "cancel_pending"
        try:
            result = await self.booking.cancel(booking["booking_id"], key)
        except TimeoutError:
            state["booking_status"] = "cancel_unknown"
            state["transaction"]["active_operation"]["status"] = "unknown"
            return self._respond(state, "inform_pending", "Đang đối soát yêu cầu hủy đơn thử nghiệm. Chưa xác định hủy thành công.")
        self._integrate_cancel(state, result)
        return self._respond(state, "inform_success" if state["booking_status"] == "cancelled" else "inform_failure", self._booking_text(state))

    @staticmethod
    def _integrate_cancel(state: dict, result: dict) -> None:
        success = result.get("status") == "succeeded" and result.get("provider_status") == "cancelled"
        state["booking_status"] = "cancelled" if success else "cancel_failed"
        state["transaction"]["active_operation"]["status"] = "succeeded" if success else "rejected"
        state["transaction"]["last_outcome"] = result
        state["transaction"]["pending_cancel"] = False
        if success:
            state["transaction"]["booking_result"]["provider_status"] = "cancelled"

    async def _reconcile(self, state: dict) -> None:
        operation = state["transaction"].get("active_operation")
        if not operation:
            return
        try:
            result = await self.booking.lookup(operation["idempotency_key"])
        except Exception:
            return
        if not result:
            return
        if operation["type"] == "create":
            self._integrate_create(state, result)
            if state["transaction"].get("pending_cancel") and state["booking_status"] == "booked":
                await self._cancel(state, None)
        elif operation["type"] == "cancel":
            self._integrate_cancel(state, result)

    async def reconcile(self, state: dict, event_id: str | None = None) -> dict:
        state = deepcopy(state)
        await self._reconcile(state)
        if event_id:
            state["control"]["last_event_id"] = event_id
        return self._respond(state, "inform_pending" if state["booking_status"] in {"booking_unknown", "cancel_unknown"} else "inform_success", self._booking_text(state))

    @staticmethod
    def _booking_text(state: dict) -> str:
        booking = state["transaction"].get("booking_result")
        booking_id = booking.get("booking_id", "") if booking else ""
        if state["booking_status"] == "booked":
            schedule = (state["transaction"].get("committed_snapshot") or {}).get("pickup_schedule") or {}
            when = ""
            if schedule.get("mode") == "scheduled" and schedule.get("pickup_at"):
                target = datetime.fromisoformat(schedule["pickup_at"])
                when = " Giờ đón: " + target.strftime("%H:%M ngày %d/%m/%Y") + " (giờ Việt Nam)."
            return f"Đã tạo đơn thử nghiệm {booking_id}.{when} Đây là đơn sandbox; chưa điều phối tài xế thật. Bạn có thể hủy đơn nếu cần."
        if state["booking_status"] == "cancelled":
            return f"Đã hủy đơn thử nghiệm {booking_id}." if booking_id else "Đã hủy bản nháp."
        if state["booking_status"] == "booking_failed":
            return "Chưa tạo được đơn thử nghiệm. Bạn có thể kiểm tra thông tin và xác nhận lại."
        if state["booking_status"] == "cancel_failed":
            return f"Chưa hủy được đơn thử nghiệm {booking_id}. Đơn vẫn được giữ để kiểm tra."
        return "Đang đối soát kết quả giao dịch thử nghiệm."

    def _decide_response(self, state: dict) -> dict:
        prefix = ""
        if state["turn"]["questions"]:
            questions = normalized(" ".join(state["turn"]["questions"]))
            quote = state["resolution"].get("quote")
            if re.search(r"\b(gia|tien|bao nhieu|cuoc)\b", questions):
                prefix = f"Giá thử nghiệm là {quote['amount']:,.0f} đ. " if self._quote_valid(state) else "Mình cần điểm đón, điểm đến và loại xe để lấy giá thử nghiệm. "
            elif re.search(r"\b(tai xe|bien so|bao lau|eta)\b", questions):
                prefix = "Đây là sandbox; chưa có tài xế hoặc thời gian đón thật. "
            else:
                prefix = "Mình hỗ trợ tạo, xem và hủy đơn thử nghiệm đi ngay hoặc đặt trước, một chiều. "
        if state["issues"]:
            issue = next(iter(state["issues"].values()))
            return self._respond(state, "ask_clarification", prefix + issue["text"], issue.get("target"))
        if "DENIED_SUMMARY" in state["turn"]["issues"]:
            return self._respond(state, "ask_clarification", prefix + "Bạn muốn sửa phần nào: điểm đón, điểm đến, loại xe hay thông tin khác?")
        if any(code in state["turn"]["issues"] for code in ("CONDITIONAL_REQUEST", "UNCERTAIN_REQUEST", "UNEXTRACTED_CORRECTION", "AMBIGUOUS_CONFIRM_SCOPE", "NO_UNDERSTANDING")):
            return self._respond(state, "ask_clarification", prefix + "Bạn nói rõ thông tin muốn sửa hoặc điều kiện cần đáp ứng nhé. Mình sẽ tóm tắt lại trước khi đặt.")
        for target in ("destination", "pickup"):
            if state["booking_state"][target]["value"] is None:
                return self._respond(state, "ask_slot", prefix + QUESTIONS[target], target)
            batch = state["resolution"]["candidate_sets"].get(target)
            if batch:
                if batch["expires_at"] <= self.clock():
                    state["resolution"]["locations"].pop(target, None)
                    state["resolution"]["candidate_sets"].pop(target, None)
                    return self._respond(state, "ask_clarification", prefix + "Danh sách địa điểm đã hết hạn. Bạn gửi lại địa chỉ đầy đủ nhé.", target)
                return self._offer_candidates(state, batch, prefix)
            location = state["resolution"]["locations"].get(target, {})
            if location.get("status") != "valid":
                if location.get("status") == "unavailable":
                    message = "Dịch vụ bản đồ đang không phản hồi. Bạn thử lại sau một lát nhé; địa chỉ đã nhập vẫn được giữ."
                else:
                    message = location.get("message") or f"Mình chưa xác định được {self._slot_label(target)}. Bạn ghi rõ số nhà, đường và tỉnh/thành phố hoặc một điểm hẹn cụ thể nhé."
                return self._respond(state, "ask_clarification", prefix + message, target)
        for slot in self._required(state):
            if state["booking_state"][slot]["value"] is None:
                return self._respond(state, "ask_slot", prefix + QUESTIONS[slot], slot)
        if not self._quote_valid(state):
            return self._respond(state, "inform_failure", prefix + "Chưa lấy được tuyến đường hoặc giá thử nghiệm. Bạn thử lại sau; thông tin chuyến vẫn được giữ.")
        return self._summary(state, prefix)

    def _offer_candidates(self, state: dict, batch: dict, prefix: str) -> dict:
        target, batch_id = batch["target"], batch["candidate_set_id"]
        candidates = [{"candidate_id": candidate_ref(batch_id, p["id"]), "candidate_set_id": batch_id,
            "target": target, "ordinal": i, "label": p["label"]}
            for i, p in enumerate(batch["places"], 1)]
        state["candidates"] = candidates
        names = "\n".join(f"{c['ordinal']}. {c['label']}" for c in candidates)
        response = self._respond(state, "offer_candidates", prefix + f"Bạn chọn {self._slot_label(target)} nào?\n{names}", target, candidates=candidates)
        batch["presented_response_id"] = response["last_response"]["response_id"]
        return response

    def _summary(self, state: dict, prefix: str = "") -> dict:
        slots = state["booking_state"]
        scope = [s for s in SLOT_NAMES if slots[s]["value"] is not None]
        payload = self._snapshot_payload(state)
        quote = state["resolution"]["quote"]
        summary = {"pickup": payload["pickup"]["label"], "destination": payload["destination"]["label"],
            "pickup_time": state["resolution"]["pickup_time"]["label"], "passengers": slots["passengers"]["value"],
            "vehicle_type": slots["vehicle_type"]["value"],
            "vehicle_label": self.vehicle_labels[slots["vehicle_type"]["value"]],
            "contact_phone": payload["contact"]["phone"],
            "contact_name": slots["contact_name"]["value"], "pickup_note": slots["pickup_note"]["value"],
            "luggage": slots["luggage"]["value"], "payment_method": slots["payment_method"]["value"],
            "stops": slots["stops"]["value"], "special_requests": slots["special_requests"]["value"],
            "fare": quote["amount"], "currency": quote.get("currency", "VND"),
            "quote_expires_at": quote["expires_at"], "booking_revision": state["control"]["booking_revision"],
            "snapshot_fingerprint": self._snapshot_fingerprint(state)}
        lines = [f"Đón: {summary['pickup']}", f"Đến: {summary['destination']}",
            f"{'Đi ngay' if payload['pickup_schedule']['mode'] == 'asap' else 'Giờ đón: ' + summary['pickup_time']} · {summary['passengers']} người · {summary['vehicle_label']}",
            f"Liên hệ: {summary['contact_phone']}"]
        if summary["contact_name"]:
            lines.append("Người đi: " + summary["contact_name"])
        if summary["pickup_note"]:
            lines.append("Ghi chú đón: " + summary["pickup_note"])
        if summary["luggage"]:
            luggage = summary["luggage"]
            label = {"none": "không mang", "cabin": "xách tay", "large": "vali lớn", "mixed": "nhiều kích cỡ"}[luggage["size"]]
            lines.append(f"Hành lý: {luggage['count']} kiện, {label}")
        if summary["payment_method"]:
            lines.append("Thanh toán: tiền mặt (chỉ ghi nhận, không thu tiền)")
        if summary["stops"] == []:
            lines.append("Không có điểm dừng giữa đường")
        if summary["special_requests"] == []:
            lines.append("Không có yêu cầu hỗ trợ thêm")
        lines.extend([f"Giá thử nghiệm: {summary['fare']:,.0f} đ", "Bạn đồng ý tạo đơn thử nghiệm theo đúng thông tin này chứ?"])
        state["booking_status"] = "awaiting_confirmation"
        self._respond(state, "confirm_booking", prefix + "\n".join(lines), summary=summary)
        response = state["last_response"]
        prompt = {"prompt_id": "prompt_" + response["response_id"], "response_id": response["response_id"],
            "purpose": "confirm_booking", "scope": scope,
            "booking_revision": state["control"]["booking_revision"],
            "slot_revisions": {s: state["control"]["slot_revisions"][s] for s in scope},
            "snapshot_fingerprint": summary["snapshot_fingerprint"],
            "quote_id": quote["quote_id"], "delivery_status": "planned"}
        state["confirmation"]["pending_prompt"] = prompt
        summary["prompt_id"] = prompt["prompt_id"]
        response["presentation"].update({"prompt_id": prompt["prompt_id"],
            "booking_revision": prompt["booking_revision"],
            "snapshot_fingerprint": prompt["snapshot_fingerprint"],
            "valid_until": quote["expires_at"]})
        return state

    def _respond(self, state: dict, action: str, text: str, focus: str | None = None,
                 candidates: list | None = None, summary: dict | None = None,
                 reason: str | None = None) -> dict:
        state["control"]["generation"] += 1
        event_id = state["control"].get("last_event_id")
        response_id = "res_" + (fingerprint([state["control"]["session_id"], event_id])[:24] if event_id else uuid.uuid4().hex)
        if action != "confirm_booking":
            state["confirmation"]["pending_prompt"] = None
        response = {"response_id": response_id, "generation": state["control"]["generation"],
            "text": text, "action": action, "focus": focus, "current_focus": focus,
            "candidates": candidates or [], "summary": summary, "delivery_status": "planned",
            "booking": deepcopy(state["transaction"].get("booking_result")),
            "booking_status": state["booking_status"], "reason": reason,
            "presentation": {"contract_version": "chat-presentation-1", "response_id": response_id,
                             "generation": state["control"]["generation"]}}
        if action != "offer_candidates":
            state["candidates"] = []
        state["last_response"] = response
        state["response"] = response
        return state
