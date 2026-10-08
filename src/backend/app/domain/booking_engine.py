"""Channel-independent implementation of architecture_fixed.md.

booking_slots is authoritative. booking_state/resolution are read projections for
the existing NLU and read-only inquiry adapters, never an alternative reducer.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
import uuid
from copy import deepcopy
from datetime import UTC, datetime

from app.adapters.extractor import ExtractorError
from app.adapters.knowledge_base import METER_NOTE, KnowledgeBase
from app.contracts.booking import AddressSlot, BotState, Customer, ready_to_book
from app.contracts.maps import MapResolution, Place, RouteResult, search_key
from app.contracts.nlu import NluResult, empty_booking_state
from app.contracts.turn import TurnInput, TurnResult
from app.domain.engine import candidate_ref, fingerprint
from app.domain.inquiries import InquiryService
from app.domain.mega_poi import MegaPOIRegistry
from app.domain.pickup_time import merge_time_clarification, parse_pickup_time

REQUIRED = ("pickup", "destination", "pickup_time", "vehicle_type")
ASK = {"pickup": "Bạn muốn đón ở đâu? Cho mình số nhà, đường hoặc địa điểm dễ tìm nhé.",
       "destination": "Bạn muốn đến đâu?", "pickup_time": "Bạn muốn đón ngay hay vào ngày, giờ nào?",
       "vehicle_type": "Bạn chọn xe máy, ô tô 4 chỗ hay ô tô 7 chỗ?"}
LABELS = {"pickup": "điểm đón", "destination": "điểm đến", "pickup_time": "giờ đón",
          "vehicle_type": "loại xe", "passengers": "số hành khách", "stopovers": "điểm dừng"}
ALIASES = {"request_repeat": "repeat_request", "no_understanding": "unclear",
           "out_of_scope": "unclear"}
UNCERTAIN_POSITION = r"\b(?:dang di bo|dang di chuyen|khong ro (?:day|o dau)|khong biet (?:day|o dau)|khong xac dinh)\b"
REFUSE_DETAIL = r"\b(?:khong biet|khong ro|khong nho|khong muon|dung hoi|hoi nhieu|kho chiu|thoi di)\b"


class ConversationEngine:
    def __init__(self, extractor, maps, booking, quote=None, *, crm=None, kb=None,
                 mega_pois=None, weather=None, brand_name="ParrotGo", inquiry_ttl=600,
                 read_deadline=15, clock=None, **_legacy_options):
        self.extractor, self.maps, self.booking = extractor, maps, booking
        self.crm = crm
        self.kb = kb or KnowledgeBase()
        self.quote_adapter = self.kb
        self.vehicle_catalog = self.kb.vehicle_catalog
        self.mega_pois = mega_pois or MegaPOIRegistry()
        self.brand_name, self.clock = brand_name, clock or time.time
        self.location_confirmation = True
        self.area_assistance_enabled = False
        self.inquiry_service = InquiryService(self, weather=weather, ttl=inquiry_ttl,
                                             read_deadline=read_deadline)

    def new_state(self, session_id, draft_id=None, *, customer_phone, customer_name):
        session_id = str(uuid.UUID(session_id))
        customer = Customer(customer_phone=customer_phone, customer_name=customer_name)
        state = BotState(session_id=session_id, **customer.model_dump()).model_dump(mode="json")
        state.update(
            control={"session_id": session_id, "draft_id": draft_id or str(uuid.uuid4()),
                     "schema_version": 7, "graph_version": "architecture-fixed-1",
                     "policy_version": "architecture-fixed-1", "booking_revision": 0,
                     "generation": 0, "last_event_id": None, "booking_for": None},
            transaction={"active_operation": None, "booking_result": None,
                         "committed_snapshot": None, "pending_cancel": False},
            confirmation={"pending_prompt": None, "accepted_snapshot": None},
            resolution={"locations": {}, "candidate_sets": {}, "route": None,
                        "quote": None, "contact": customer.model_dump()},
            dialogue={"pending_prompt": None, "pending_questions": [], "booking_started": False},
            inquiries={}, active_inquiry_id=None, read_requests={}, read_facts={},
            candidates=[], issues={}, turn={}, schedule=None, cancel_context=None,
        )
        self._views(state)
        self._respond(state, "general_reply",
                      f"Chào {customer.customer_name}, mình là trợ lý đặt xe {self.brand_name}. "
                      "Bạn muốn đón ở đâu và đến đâu?", target_slots=[])
        if self.crm:
            self.crm.customer(customer.customer_phone, customer.customer_name)
        return state

    def migrate_state(self, old):
        if "booking_slots" in old:
            return deepcopy(old)
        # Never silently reuse unconfirmed old slots or consent after a policy change.
        old = deepcopy(old)
        values = old.get("booking_state", {})
        phone = (values.get("contact_phone") or {}).get("value")
        name = (values.get("contact_name") or {}).get("value")
        try:
            profile = Customer(customer_phone=phone, customer_name=name).model_dump()
        except (ValueError, TypeError):
            profile = {"customer_phone": "", "customer_name": ""}
        state = BotState.model_construct(session_id=old["control"]["session_id"], **profile).model_dump(mode="json")
        state.update({key: deepcopy(old.get(key, default)) for key, default in {
            "control": {}, "transaction": {}, "confirmation": {}, "resolution": {},
            "dialogue": {}, "inquiries": {}, "active_inquiry_id": None,
            "read_requests": {}, "read_facts": {}, "candidates": [], "issues": {}, "turn": {},
        }.items()})
        state["control"].update(schema_version=7, graph_version="architecture-fixed-1", policy_version="architecture-fixed-1")
        state["control"].setdefault("booking_revision", 0)
        state["control"].setdefault("generation", 0)
        state["confirmation"] = {"pending_prompt": None, "accepted_snapshot": None}
        state["schedule"], state["cancel_context"] = None, None
        state["last_response"] = old.get("last_response")
        for name in REQUIRED + ("passengers",):
            value = (values.get(name) or {}).get("value")
            if value is not None:
                self._update(state, name, value, "migration")
        state["booking_status"] = "booked" if old.get("booking_status") == "booked" else "canceled" if old.get("booking_status") == "cancelled" else "collecting"
        self._views(state)
        return state

    def _views(self, state):
        """Generate bounded compatibility input; only booking_slots owns values."""
        slots = state["booking_slots"]
        view = empty_booking_state().model_dump(mode="json")
        for name in REQUIRED + ("passengers",):
            slot = slots[name]
            view[name] = {"value": slot.get("raw") if name in {"pickup", "destination"} else slot["value"],
                          "confirmed": slot["status"] == "confirmed"}
        view["contact_phone"] = {"value": state["customer_phone"], "confirmed": True}
        view["contact_name"] = {"value": state["customer_name"], "confirmed": True}
        view["pickup_note"] = {"value": slots["pickup"].get("note"), "confirmed": False}
        view["general_note"] = {"value": slots["general_note"], "confirmed": False}
        view["stops"] = {"value": [stop["address"]["raw"] for stop in slots["stopovers"]] or None,
                         "confirmed": bool(slots["stopovers"]) and all(stop["address"]["status"] == "confirmed" for stop in slots["stopovers"])}
        state["booking_state"] = view
        state["conversation_context"] = {
            "last_bot_message": state.get("final_response_text"),
            "last_bot_action": self._nlu_action(state.get("last_bot_action")),
            "current_focus": "stops" if (state.get("current_focus") or "").startswith("stopovers") else state.get("current_focus"),
        }
        locations = {}
        for name in ("pickup", "destination"):
            address = slots[name]
            if address["coords"]:
                locations[name] = {"status": "valid", "place": self._place(address)}
        state["resolution"]["locations"] = locations
        state["resolution"]["pickup_time"] = deepcopy(state.get("schedule"))
        state["resolution"]["contact"] = {"phone": state["customer_phone"], "name": state["customer_name"]}

    @staticmethod
    def _nlu_action(action):
        if not action:
            return None
        return {"confirm_slots": "confirm_booking_info", "clarify_address": "ask_clarification",
                "general_reply": "answer_question", "confirm_cancel": "confirm_cancellation",
                "human_handoff": "handoff"}.get(action["action_type"], action["action_type"])

    def projection(self, state, text, occurred_at):
        self._views(state)
        prompt = state["dialogue"].get("pending_prompt")
        # Canonical confirmation metadata, not a global confirmation flag.
        if not prompt and state.get("last_bot_action"):
            action = state["last_bot_action"]
            prompt = {"purpose": self._nlu_action(action), "scope_kind": "booking",
                      "scope_id": state["control"]["draft_id"],
                      "field": state.get("current_focus"), "revision": state["control"]["booking_revision"]}
        return TurnInput.model_validate({
            "contract_version": "parrotgo-turn-3",
            "utterance": {"text": text, "asr_confidence": None},
            "conversation_context": state["conversation_context"],
            "booking_state": state["booking_state"], "booking_status": state["booking_status"],
            "candidates": [{key: row[key] for key in ("candidate_id", "candidate_set_id", "target", "ordinal", "label", "stop_ref") if key in row and (key != "stop_ref" or row["target"] == "stops")}
                           for row in state.get("candidates", [])],
            "inquiries": [{key: row.get(key) for key in ("inquiry_id", "revision", "origin", "destination", "vehicle", "departure_time", "status")}
                          for row in state["inquiries"].values()],
            "active_inquiry_id": state["active_inquiry_id"], "pending_prompt": prompt,
            "capabilities": {"scheduled": True, "multi_stop": True, "motorbike": True},
            "occurred_at": datetime.fromtimestamp(occurred_at, UTC).isoformat(),
            "architecture_state": {key: state[key] for key in BotState.model_fields},
        })

    async def interpret(self, state, event):
        when = event.get("occurred_at", self.clock())
        if event.get("action"):
            return {"kind": "action", "occurred_at": when}
        state = self.acknowledge(self.migrate_state(state), event.get("delivered_response_ids", []))
        try:
            data = self.projection(state, event["text"], when)
            if getattr(self, "voice_mode", False) and state.get("candidates"):
                from app.voice.selection import ambiguous_choice, match_candidate
                selected = match_candidate(event["text"], state["candidates"])
                if selected:
                    return {"kind": "legacy", "occurred_at": when, "result": {
                        "speech_status": "clear", "dialogue_acts": [{
                            "intent": "select_candidate", "target": selected["target"],
                            "value": selected["candidate_id"],
                        }],
                    }}
                if ambiguous_choice(event["text"], state["candidates"]):
                    return {"kind": "legacy", "occurred_at": when, "result": {
                        "speech_status": "low_confidence", "dialogue_acts": [],
                    }}
            result = await self.extractor(data)
            if isinstance(result, TurnResult) or (isinstance(result, dict) and "contract_version" in result):
                result = result if isinstance(result, TurnResult) else TurnResult.model_validate(result)
                result.validate_evidence(data)
                return {"kind": "turn", "result": result.model_dump(mode="json"), "occurred_at": when}
            result = result if isinstance(result, NluResult) else NluResult.model_validate(result)
            return {"kind": "legacy", "result": result.model_dump(mode="json"), "occurred_at": when}
        except ExtractorError as exc:
            if exc.retryable or exc.code in {"RATE_LIMITED", "RATE_LIMIT", "QUOTA_EXCEEDED"}:
                raise
            return {"kind": "error", "code": exc.code, "occurred_at": when}
        except (ValueError, TypeError):
            return {"kind": "error", "code": "OUTPUT_INVALID", "occurred_at": when}

    def acknowledge(self, state, response_ids):
        state = deepcopy(state)
        response = state.get("last_response")
        if response and response["response_id"] in (response_ids or []):
            response["delivery_status"] = "rendered"
        return state

    async def prepare(self, state, event, interpretation):
        state = self.acknowledge(self.migrate_state(state), event.get("delivered_response_ids"))
        if state["control"].get("last_event_id") == event["event_id"]:
            return state
        state["control"]["last_event_id"] = event["event_id"]
        state["messages"].append({"role": "user", "content": event["text"], "event_id": event["event_id"]})
        state["turn"] = {"issues": [], "questions": [], "changed_slots": [],
                         "occurred_at": interpretation["occurred_at"]}
        state["turn_extracted_slots"] = []
        state["intents"], state["primary_intent"] = [], None
        if ((not state["customer_phone"] or not state["customer_name"])
                and not state["transaction"].get("booking_result") and not state["transaction"].get("active_operation")):
            return self._respond(state, "human_handoff", "Phiên cũ chưa có hồ sơ khách hàng. Hãy mở phiên mới với tên và số điện thoại; đơn cũ vẫn được giữ để đối soát.")
        await self._recover(state)
        if self._unknown(state):
            return self._respond(state, "general_reply", "Giao dịch đang được đối soát; mình chưa tạo hoặc hủy thêm đơn.")
        if interpretation["kind"] == "error":
            state["fallback_count"] += 1
            if interpretation["code"] in {"PROVIDER_AUTH_ERROR", "PROVIDER_MODEL_UNAVAILABLE", "PROVIDER_CONFIG_ERROR"}:
                state["tool_status"] = "API_ERROR"
                return self._respond(state, "general_reply", "Dịch vụ AI đang gặp lỗi cấu hình. Thông tin chuyến vẫn được giữ; bạn có thể gửi tiếp hoặc thử lại sau.", reason=interpretation["code"])
            state["intents"] = ["unclear"]
            return self._respond(state, "general_reply", "Mình chưa hiểu chắc tin nhắn. Bạn viết rõ hơn giúp mình nhé.", reason=interpretation["code"])
        result = interpretation.get("result") or {}
        acts = deepcopy(result.get("booking_acts", result.get("dialogue_acts", [])))
        questions = deepcopy(result.get("questions", []))
        conversational = result.get("conversational_acts", [])
        low_confidence = result.get("speech_status", "clear") != "clear"
        inquiry_prompt = state["dialogue"].get("pending_prompt") or {}
        inquiry_confirm = inquiry_prompt.get("scope_kind") == "inquiry" and inquiry_prompt.get("purpose") == "confirm_location"
        decisions = list(result.get("location_decisions", []))
        if inquiry_confirm and not decisions:
            decisions = [{"decision": "confirm" if a["intent"] == "confirm" else "reject"}
                         for a in acts if a["intent"] in {"confirm", "deny"}]
        if inquiry_confirm:
            acts = [a for a in acts if a["intent"] not in {"confirm", "deny"}]
            item = state["inquiries"].get(inquiry_prompt.get("scope_id"))
            field = inquiry_prompt.get("field")
            proposal = (item or {}).get("location_proposals", {}).get(field)
            for decision in decisions:
                if (not low_confidence and proposal and self._prompt_valid(state, event)
                        and proposal.get("presented_response_id") == state["last_response"]["response_id"]
                        and proposal["revision"] == item["revision"] and proposal["expires_at"] > self.clock()):
                    if decision["decision"] == "confirm":
                        item["locations"][field] = {"status": "valid", "place": deepcopy(proposal["place"]), "confirmed": True,
                                                    "area_preview": proposal["area_preview"]}
                        item["location_proposals"].pop(field, None)
                    else:
                        item[field] = None
                        item["revision"] += 1
                        item["locations"].pop(field, None)
                        item["location_proposals"], item["candidate_sets"] = {}, {}
                        item["routes"], item["quotes"], item["route_fingerprint"] = {}, {}, None
                    questions.extend({**q, "route_scope": "active_inquiry", "origin": None, "destination": None}
                                     for q in state["dialogue"].get("pending_questions", []))
                    state["intents"].append("confirm" if decision["decision"] == "confirm" else "deny")
                elif decisions:
                    state["turn"]["issues"].append("STALE_LOCATION_PROPOSAL")
        for decision in result.get("location_decisions", []):
            if not inquiry_confirm and decision["decision"] in {"confirm", "reject"}:
                acts.append({"intent": "confirm" if decision["decision"] == "confirm" else "deny", "target": None, "value": None})
        if "repeat" in conversational:
            acts.append({"intent": "repeat_request", "target": None, "value": None})
        if "unclear" in conversational or low_confidence:
            state["fallback_count"] += 1
            state["intents"].append("unclear")
        if any(item in conversational for item in ("greeting", "thanks")):
            state["intents"].append("chit_chat")
        action = event.get("action")
        if action:
            kind = action.get("type")
            if kind in {"cancel_draft", "cancel_booking"}:
                booking = state["transaction"].get("booking_result") or {}
                valid = (action.get("draft_id") == state["control"]["draft_id"] and not booking
                         if kind == "cancel_draft" else action.get("booking_id") == booking.get("booking_id") and bool(booking))
                if not valid:
                    return self._respond(state, "general_reply", "Tham chiếu yêu cầu hủy không hợp lệ; thông tin chuyến vẫn được giữ.", reason="STALE_PRESENTATION")
                acts.append({"intent": "cancel", "target": None, "value": None})
            elif kind == "confirm_cancel":
                previous = state.get("last_bot_action") or {}
                if (state["booking_status"] != "cancel_pending" or previous.get("action_type") != "confirm_cancel"
                        or action.get("prompt_id") != previous.get("metadata", {}).get("prompt_id")):
                    return self._respond(state, "general_reply", "Xác nhận hủy đã cũ. Bạn kiểm tra câu hỏi mới nhé.", reason="STALE_PRESENTATION")
                acts.append({"intent": "confirm", "target": None, "value": None})
            elif kind == "confirm_booking":
                if not self._action_matches(state, action):
                    return self._respond(state, "general_reply", "Xác nhận đã cũ. Bạn kiểm tra tóm tắt mới nhé.", reason="STALE_PRESENTATION")
                acts.append({"intent": "confirm", "target": None, "value": None})
            elif kind == "select_candidate":
                acts.append({"intent": "select_candidate", "target": action.get("target"), "value": action.get("candidate_id"), "candidate_set_id": action.get("candidate_set_id")})
            elif kind in {"use_inquiry_route", "choose_inquiry_vehicle", "resume_booking", "dismiss_inquiry"}:
                await self._inquiry_action(state, kind, action, event, acts, questions)
        for command in result.get("inquiry_actions", []):
            await self._inquiry_action(state, command["type"], command, event, acts, questions)
        # Retain all entities even when the overall utterance is unclear or asks to cancel.
        changed = set()
        for act in acts:
            intent = ALIASES.get(act["intent"], act["intent"])
            if intent in {"provide_info", "change_info", "confirm", "deny", "cancel", "repeat_request", "unclear", "chit_chat", "ask_question"}:
                state["intents"].append(intent)
            if intent in {"provide_info", "change_info"}:
                evidence = act.get("evidence_span") or {}
                source = evidence.get("text") or event["text"][evidence.get("start", 0):evidence.get("end", len(event["text"]))]
                target = self._update(state, act.get("target"), act.get("value"), source, intent=intent)
                if target:
                    changed.add(target)
            elif intent == "ask_question":
                questions.append(self._question(act.get("value") or event["text"]))
            elif intent == "select_candidate":
                state["intents"].append("provide_info")
                if (state.get("last_bot_action") or {}).get("metadata", {}).get("scope_kind") == "inquiry":
                    try:
                        item = self.inquiry_service.validate_action(state, {}, event.get("reply_to_response_id"))
                        self.inquiry_service.select(state, item, act.get("value"), act.get("candidate_set_id"))
                        questions.extend({**q, "route_scope": "active_inquiry", "origin": None, "destination": None}
                                         for q in item.get("questions", []))
                    except ValueError:
                        state["turn"]["issues"].append("STALE_CANDIDATE")
                else:
                    selected = self._select(state, act, event)
                    if selected:
                        changed.add(selected)
            elif intent == "reject_candidate":
                state["intents"].append("deny")
                target = self._target(act.get("target"), state)
                state["resolution"]["candidate_sets"].pop(target, None)
                if target:
                    self._slot(state, target)["status"] = "needs_clarification"
        state["intents"] = list(dict.fromkeys(state["intents"] + (["ask_question"] if questions else [])))
        priority = ("cancel", "change_info", "provide_info", "confirm", "deny", "ask_question", "repeat_request", "unclear", "chit_chat")
        state["primary_intent"] = next((i for i in priority if i in state["intents"]), None)
        state["turn"]["changed_slots"] = sorted(changed)
        if changed:
            state["confirmation"]["accepted_snapshot"] = None
        previous = state.get("last_bot_action") or {}
        cancel_pending = state["booking_status"] == "cancel_pending"
        valid_prompt = self._prompt_valid(state, event)
        confirms = [a for a in acts if a["intent"] == "confirm"]
        denials = [a for a in acts if a["intent"] == "deny"]
        if low_confidence:
            confirms, denials = [], []
        full_text = search_key(event["text"])
        if re.search(r"\b(?:neu|chi khi|mien la|voi dieu kien|nhung|khoan|chua dat|dung dat)\b", full_text):
            confirms = []
        if confirms and re.search(r"\b(?:chi|thoi)\b", full_text):
            from app.domain.engine import ChatEngine
            mentioned = ChatEngine._mentioned_slots(full_text)
            confirms = [{**a, "target": target} for a in confirms for target in mentioned]
        if cancel_pending and valid_prompt and confirms and not changed and not questions:
            state["turn"]["cancel_authorized"] = True
        elif cancel_pending and valid_prompt and denials:
            state["booking_status"] = (state.get("cancel_context") or {}).get("status", "collecting")
            state["cancel_context"] = None
        elif not cancel_pending and state["booking_status"] not in {"booked", "canceled", "operator_required"}:
            for act in denials:
                target = self._target(act.get("target"), state)
                targets = [target] if target else previous.get("target_slots", [])
                if not target and len(targets) != 1:
                    state["turn"]["deny_unspecified"] = True
                    continue
                for name in targets:
                    if name in changed:
                        continue
                    if name in {"general_note", "pickup_note"}:
                        if name == "general_note":
                            state["booking_slots"]["general_note"] = None
                        else:
                            state["booking_slots"]["pickup"]["note"] = None
                        changed.add(name)
                        state["control"]["booking_revision"] += 1
                        state["confirmation"]["accepted_snapshot"] = None
                        continue
                    if name not in REQUIRED + ("passengers",) and not name.startswith("stopovers."):
                        continue
                    slot = self._slot(state, name)
                    if (name == "pickup" and slot["is_mega_poi"] and not slot["coords"]
                            and slot["metadata"].get("clarification_count") and previous.get("action_type") == "clarify_address"):
                        continue
                    if slot.get("metadata", {}).get("crm_suggestion"):
                        slot.update(AddressSlot().model_dump())
                        state["control"]["booking_revision"] += 1
                        state["confirmation"]["accepted_snapshot"] = None
                        continue
                    slot["status"] = "needs_clarification"
                    if name.startswith("stopovers") or name in {"pickup", "destination"}:
                        slot.update(coords=None, formatted=None)
                        slot["metadata"].update(rejected=True, clarification="Bạn cho địa chỉ thay thế cho điểm chưa đúng nhé.")
                    else:
                        slot["value"] = None
                    state["confirmation"]["accepted_snapshot"] = None
                    state["control"]["booking_revision"] += 1
            if valid_prompt and confirms and not state["turn"].get("deny_unspecified"):
                requested = {self._target(a.get("target"), state) for a in confirms if a.get("target")}
                scope = set(previous.get("target_slots", []))
                selected = (scope & requested if requested else scope) - changed
                denied = {self._target(a.get("target"), state) for a in denials}
                selected -= denied
                for name in selected:
                    if name not in REQUIRED + ("passengers",) and not name.startswith("stopovers."):
                        continue
                    slot = self._slot(state, name)
                    has_value = slot.get("coords") if "raw" in slot else slot.get("value") is not None
                    if has_value and slot["status"] != "needs_clarification":
                        slot["status"] = "confirmed"
                if previous.get("action_type") == "confirm_booking" and not changed and not denials and not questions and not requested:
                    state["confirmation"]["accepted_snapshot"] = (previous.get("metadata") or {}).get("snapshot_fingerprint")
        # Readiness requires current, valid time and address evidence.
        state["turn"]["changed_slots"] = sorted(changed)
        await self._resolve_addresses(state, event["text"])
        self._validate(state)
        self._views(state)
        if (state["booking_status"] not in {"booked", "canceled", "operator_required"}
                and not state["resolution"].get("route")
                and slots_have_coordinates(state)):
            await self._route(state)
        answers = []
        state["turn"]["questions"] = deepcopy(questions)
        state["dialogue"]["pending_questions"] = deepcopy(questions)
        for question in questions:
            q = question if isinstance(question, dict) else question.model_dump()
            answer = await self._answer(state, q)
            if answer:
                answers.append(answer)
        if "repeat_request" in state["intents"]:
            repeat_target = next((self._target(a.get("target"), state) for a in acts
                                  if ALIASES.get(a["intent"], a["intent"]) == "repeat_request" and a.get("target")), None)
            answers.append(self._summary_text(state, repeat_target))
        prefix = "\n".join(dict.fromkeys(answers))
        if state["turn"].get("cancel_authorized"):
            return state
        cancel_request = "cancel" in state["intents"] and not low_confidence
        if cancel_request:
            # Negations/conditions cannot be converted to a destructive intent.
            key = search_key(event["text"])
            if re.search(r"\b(?:khong|dung|chua|khoan)\s+(?:co\s+|can\s+)?huy\b|\b(?:neu|chi khi|mien la)\b", key):
                return self._respond(state, "general_reply", self._join(prefix, "Bạn muốn hủy hay tiếp tục chuyến? Mình chưa hủy."))
            if not cancel_pending:
                state["cancel_context"] = {"status": state["booking_status"]}
            state["booking_status"] = "cancel_pending"
            return self._respond(state, "confirm_cancel", self._join(prefix, "Bạn xác nhận hủy yêu cầu đặt xe này chứ?"))
        if state["turn"].get("cancel_authorized"):
            return state
        if state["booking_status"] == "cancel_pending":
            return self._respond(state, "confirm_cancel", self._join(prefix, "Bạn muốn hủy hay tiếp tục chuyến?"))
        if state["booking_status"] in {"booked", "canceled", "operator_required"}:
            if answers and state["booking_status"] in {"booked", "canceled"} and state["active_inquiry_id"]:
                self._respond(state, "answer_question", self._join(prefix, self._terminal_text(state)))
                self.inquiry_service.attach_presentation(state)
                return state
            return self._respond(state, "human_handoff" if state["booking_status"] == "operator_required" else "general_reply",
                                 self._join(prefix, self._terminal_text(state)))
        if state["turn"]["issues"]:
            return self._respond(state, "general_reply", self._join(prefix, "Lựa chọn hoặc tham chiếu đã cũ. Bạn trả lời câu hỏi mới nhất nhé."), reason=state["turn"]["issues"][0])
        if state["turn"].get("deny_unspecified"):
            return self._respond(state, "general_reply", self._join(prefix, "Bạn muốn sửa điểm đón, điểm đến, giờ đón hay loại xe? Các thông tin còn lại vẫn được giữ."))
        if answers and state["active_inquiry_id"] and any(q.get("type") not in {"static_faq", "session_question"} for q in questions):
            self._respond(state, "answer_question", prefix)
            self.inquiry_service.attach_presentation(state)
            return state
        if state["issues"]:
            issue = next(iter(state["issues"].values()))
            return self._respond(state, "clarify_address" if issue["target"] in {"pickup", "destination"} or issue["target"].startswith("stopovers") else "ask_slot",
                                 self._join(prefix, issue["text"]), target_slots=[issue["target"]])
        if ready_to_book(state):
            state["booking_status"] = "ready_to_book"
            if state["confirmation"].get("accepted_snapshot") == self._snapshot_fingerprint(state) and not changed and not questions:
                state["turn"]["ready_to_dispatch"] = True
                return state
            return self._booking_summary(state, prefix)
        if answers and not state["dialogue"].get("booking_started"):
            self._respond(state, "answer_question", prefix)
            self.inquiry_service.attach_presentation(state)
            return state
        return self._next(state, prefix)

    @staticmethod
    def _join(prefix, message):
        return (prefix + "\n" if prefix else "") + message

    @staticmethod
    def _question(text):
        return {"question_id": "legacy", "type": "other_booking_question", "raw_text": text,
                "evidence_span": {"start": 0, "end": len(text)}, "route_scope": "current_booking",
                "origin": None, "destination": None, "vehicle_ref": None,
                "departure_time_ref": None, "weather_target": None, "relation_to_booking": "read_only"}

    def _target(self, target, state):
        if target == "stops":
            focus = state.get("current_focus") or ""
            return focus if focus.startswith("stopovers.") else "stopovers"
        return {"pickup_note": "pickup", "general_note": "general_note"}.get(target, target)

    def _slot(self, state, name):
        if name.startswith("stopovers."):
            return state["booking_slots"]["stopovers"][int(name.split(".")[1])]["address"]
        return state["booking_slots"][name]

    def _update(self, state, target, value, source, *, intent="provide_info"):
        if state["booking_status"] in {"booked", "canceled", "operator_required"}:
            return None
        slots = state["booking_slots"]
        if target in {"contact_phone", "contact_name"}:
            # Session identity is supplied at initialization; passenger contact isn't CRM identity.
            return None
        if target in {"pickup_note", "general_note", "special_requests", "luggage", "payment_method"}:
            note = (source if target in {"special_requests", "luggage", "payment_method"}
                    else str(value) if value is not None else None)
            if target == "pickup_note":
                slots["pickup"]["note"] = note
            else:
                slots["general_note"] = note if intent == "change_info" or note is None else self._join(slots["general_note"], note)
            target = "general_note" if target != "pickup_note" else "pickup_note"
        elif target == "stops":
            focus = state.get("current_focus") or ""
            if focus.startswith("stopovers.") and value and len(value) == 1:
                index = int(focus.split(".")[1])
                slots["stopovers"][index]["address"] = AddressSlot(raw=str(value[0]), status="extracted").model_dump()
            else:
                slots["stopovers"] = [{"address": AddressSlot(raw=str(raw), status="extracted").model_dump(), "order": index}
                                      for index, raw in enumerate(value or [], 1)]
            target = "stopovers"
        elif target in {"pickup", "destination"}:
            old = slots[target]
            if (target == "pickup" and old["is_mega_poi"] and not old["coords"]
                    and old["metadata"].get("clarification_count") and re.search(REFUSE_DETAIL, search_key(value or ""))):
                return None
            if old["raw"] == value:
                return None
            entry = self.mega_pois.lookup(value or "")
            old_entry = old.get("metadata", {}).get("mega_poi")
            if not entry and old_entry and value and re.match(r"(?:cong|sanh|toa|cua|ga|quan)\b", search_key(value)):
                value = old_entry["name"] + " " + value
                entry = old_entry
            slots[target] = AddressSlot(raw=value, status="extracted" if value else "empty").model_dump()
            if entry:
                slots[target]["is_mega_poi"] = True
                slots[target]["metadata"].update(mega_poi=deepcopy(entry), clarification_count=old.get("metadata", {}).get("clarification_count", 0) if old_entry and old_entry["id"] == entry["id"] else 0)
            state["resolution"]["candidate_sets"].pop(target, None)
        elif target in {"pickup_time", "vehicle_type", "passengers"}:
            if target == "pickup_time":
                value, anchor = merge_time_clarification(state.get("schedule"), value)
                schedule = parse_pickup_time(value, anchor if anchor is not None else state["turn"].get("occurred_at", self.clock()),
                                             previous_pickup_at=(state.get("schedule") or {}).get("pickup_at"))
                state["schedule"] = schedule
            slots[target] = {"value": value, "status": "extracted" if value is not None else "empty"}
        else:
            return None
        state["turn_extracted_slots"].append({"slot_name": target, "value": deepcopy(value), "source_text": source})
        state["control"]["booking_revision"] += 1
        state["dialogue"]["booking_started"] = True
        slots["distance_km"], slots["duration_minutes"] = None, None
        state["resolution"]["route"] = None
        state["confirmation"]["accepted_snapshot"] = None
        return target

    def _prompt_valid(self, state, event):
        response = state.get("last_response") or {}
        metadata = (state.get("last_bot_action") or {}).get("metadata") or {}
        return bool(response.get("delivery_status") == "rendered"
                    and (not event.get("reply_to_response_id") or event["reply_to_response_id"] == response["response_id"])
                    and metadata.get("response_id") == response.get("response_id"))

    def _action_matches(self, state, action):
        metadata = (state.get("last_bot_action") or {}).get("metadata") or {}
        return (action.get("prompt_id") == metadata.get("prompt_id")
                and action.get("booking_revision") == state["control"]["booking_revision"]
                and action.get("snapshot_fingerprint") == metadata.get("snapshot_fingerprint"))

    def _select(self, state, act, event):
        target = self._target(act.get("target"), state)
        if target is None:
            target = next((name for name, item in state["resolution"]["candidate_sets"].items()
                           if item["candidate_set_id"] == act.get("candidate_set_id")
                           and name in state["last_bot_action"]["target_slots"]), None)
        batch = state["resolution"]["candidate_sets"].get(target)
        if not batch or not self._prompt_valid(state, event) or batch["expires_at"] <= self.clock():
            state["turn"]["issues"].append("STALE_CANDIDATE")
            return None
        if act.get("candidate_set_id") and act["candidate_set_id"] != batch["candidate_set_id"]:
            return None
        place = next((p for p in batch["places"] if candidate_ref(batch["candidate_set_id"], p["id"]) == act.get("value")), None)
        if not place:
            return None
        self._bind(self._slot(state, target), place)
        state["resolution"]["candidate_sets"].pop(target, None)
        state["control"]["booking_revision"] += 1
        return target

    @staticmethod
    def _place(address):
        return deepcopy(address["metadata"]["place"])

    def _bind(self, address, place, *, default=False):
        place = Place.model_validate(place).model_dump(mode="json")
        entry = self.mega_pois.lookup(place["label"])
        if entry or place.get("metadata", {}).get("is_mega_poi") or place.get("airport"):
            address["is_mega_poi"] = True
            if entry:
                address["metadata"].setdefault("mega_poi", deepcopy(entry))
                address["metadata"].setdefault("clarification_count", 0)
        components = place.get("address_components") or {}
        address.update(formatted=place["label"], coords={"lat": place["lat"], "lng": place["lon"]},
                       components={"detail": components.get("hs_num") or components.get("house_number") or place.get("meeting_point"),
                                   "street": components.get("street"), "ward": components.get("ward"),
                                   "district": components.get("district"),
                                   "province_city": components.get("city") or place.get("metadata", {}).get("locality")},
                       status="extracted", default_point_used=default)
        address["metadata"]["place"] = place
        if default:
            address["note"] = self._join(address.get("note"), "Dùng điểm mặc định của khu vực lớn; tài xế chủ động gọi khách để xác định vị trí.")

    async def _map(self, query, target, state):
        map_target = "stops" if target.startswith("stopovers") else target
        binding = {"scope_kind": "booking", "scope_id": state["session_id"],
                   "revision": state["control"]["booking_revision"],
                   "dependency_fingerprint": fingerprint([target, query, state["control"]["booking_revision"]])}
        result = await self.maps.resolve(query, "stops" if target.startswith("stopovers") else target,
                                         context={**binding, "area": getattr(self.maps, "default_area", None)})
        result = MapResolution.model_validate(result).model_dump(mode="json")
        if not all(datetime.fromisoformat(result[key]).tzinfo is not None for key in ("resolved_at", "expires_at")):
            raise ValueError("Map timestamps need a timezone")
        if (result["target"] != map_target or search_key(result["query"]) != search_key(query)
                or any(result["binding"].get(key) != value for key, value in binding.items())
                or datetime.fromisoformat(result["expires_at"]).timestamp() <= self.clock()
                or datetime.fromisoformat(result["resolved_at"]).timestamp() > self.clock() + 5):
            raise ValueError("Stale or unrelated map result")
        return result

    async def _resolve_addresses(self, state, text):
        slots = state["booking_slots"]
        addresses = [(name, slots[name]) for name in ("pickup", "destination")]
        addresses += [(f"stopovers.{i}", row["address"]) for i, row in enumerate(slots["stopovers"])]
        for target, address in addresses:
            if not address["raw"] or address["coords"]:
                continue
            if address["metadata"].get("rejected"):
                continue
            if target in state["resolution"]["candidate_sets"]:
                continue
            key = search_key(address["raw"])
            # CRM suggestions are tentative and still require scoped confirmation.
            history_label = "home" if key in {"nha", "o nha", "nha toi", "nha anh", "nha em", "nha chi"} else "work" if key in {"cong ty", "cong ty toi", "cong ty anh", "cong ty chi", "cong ty em"} else None
            habitual = history_label or re.search(r"\b(?:cho cu|nhu moi khi|cho quen)\b", key)
            if habitual:
                suggestions = self.crm.suggestions(state["customer_phone"], history_label) if self.crm else []
                if suggestions:
                    proposed = suggestions[0]
                    raw = address["raw"]
                    address.update(deepcopy(proposed))
                    address.update(raw=raw, status="extracted")
                    address["metadata"]["crm_suggestion"] = True
                    continue
                address["status"] = "needs_clarification"
                address["metadata"]["clarification"] = "Mình chưa có địa chỉ quen thuộc này trong lịch sử. Bạn cho địa chỉ cụ thể nhé."
                continue
            entry = address["metadata"].get("mega_poi")
            query = address["raw"]
            default = False
            if entry and not self.mega_pois.specific(query, entry):
                address["is_mega_poi"] = True
                if target == "pickup" and not address["metadata"].get("clarification_count"):
                    address["status"] = "needs_clarification"
                    address["metadata"]["clarification"] = f"{entry['name']} hơi rộng. Bạn đang gần tòa, cổng, sảnh hoặc điểm mốc nào dễ thấy?"
                    continue
                query = entry.get("default_pickup_point" if target == "pickup" else "default_dropoff_point")
                if not query:
                    state["booking_status"] = "operator_required"
                    continue
                default = True
            moving = bool(re.search(UNCERTAIN_POSITION, key))
            if moving:
                from app.domain.location_parser import parse_location
                parsed = parse_location(query)
                if not parsed.anchor_name:
                    relation = re.search(r"(?:đối diện|bên trái|bên phải|cạnh|gần)\s+.+", query, re.I)
                    if relation:
                        parsed = parse_location(relation[0])
                if parsed.anchor_name:
                    query = parsed.anchor_name
                    address["note"] = self._join(address.get("note"), "Vị trí khách: " + address["raw"] + "; tài xế gọi khách để xác định điểm gặp.")
                else:
                    state["booking_status"] = "operator_required"
                    continue
            try:
                result = await self._map(query, target, state)
            except Exception:
                address["status"] = "needs_clarification"
                address["metadata"]["clarification"] = "Dịch vụ bản đồ đang không phản hồi. Địa chỉ đã nhập vẫn được giữ."
                state["tool_status"] = "API_ERROR"
                if moving:
                    state["booking_status"] = "operator_required"
                continue
            if moving and result["status"] != "resolved":
                state["booking_status"] = "operator_required"
                continue
            if result.get("anchors"):
                parsed = result.get("parsed_location") or {}
                if parsed.get("relation", "none") != "none" and not parsed.get("uncertainties"):
                    address["note"] = self._join(address.get("note"), f"Đón khách ở {address['raw']}" if target == "pickup" else address["raw"])
                    if len(result["anchors"]) == 1:
                        self._bind(address, result["anchors"][0])
                        continue
                    result["candidates"] = result["anchors"]
            if entry and target == "pickup" and result["status"] != "resolved" and address["metadata"].get("clarification_count"):
                try:
                    fallback = await self._map(entry["default_pickup_point"], target, state)
                    if fallback["status"] == "resolved":
                        self._bind(address, fallback["place"], default=True)
                        state["fallback_count"] += 1
                        continue
                except Exception:
                    pass
                state["booking_status"] = "operator_required"
                continue
            state["tool_status"] = {"resolved": "SUCCESS", "ambiguous": "AMBIGUOUS", "not_found": "NOT_FOUND", "unavailable": "API_ERROR"}[result["status"]]
            if result["status"] == "resolved":
                place = result["place"]
                # Reject a street/administrative centroid as pickup even if unique.
                types = set(place.get("place_types", []))
                if target == "pickup" and types & {"street", "road", "ward", "district", "city", "province", "village", "administrative", "locality", "sub_locality"} and not place.get("meeting_point"):
                    address["status"] = "needs_clarification"
                    address["metadata"]["clarification"] = "Bạn cho thêm số nhà, ngõ hoặc điểm mốc cụ thể để tài xế tìm được mình nhé."
                    continue
                self._bind(address, place, default=default)
                if default:
                    state["fallback_count"] += 1
                parsed = result.get("parsed_location") or {}
                if parsed.get("relation") and parsed["relation"] != "none":
                    address["note"] = self._join(address.get("note"), f"Đón khách ở {address['raw']}" if target == "pickup" else address["raw"])
                if re.search(r"\b(?:nga ba|nga tu|giao voi|giao)\b", key):
                    address["note"] = self._join(address.get("note"), "Tài xế chủ động gọi khách khi tới giao lộ.")
            elif result["candidates"]:
                batch_id = "set_" + fingerprint([state["session_id"], target, state["control"]["booking_revision"], result["candidates"]])[:24]
                state["resolution"]["candidate_sets"][target] = {"candidate_set_id": batch_id, "places": result["candidates"], "expires_at": self.clock() + 300}
                address["status"] = "needs_clarification"
            else:
                address["status"] = "needs_clarification"
                address["metadata"]["clarification"] = result.get("clarification") or "Mình chưa tìm được địa chỉ. Bạn bổ sung số nhà, đường hoặc điểm mốc nhé."
                if result.get("entity_kind") == "area" and result.get("area"):
                    address["metadata"]["area"] = result["area"]
                    # A sourced representative is acceptable for destination, never an administrative pickup.
                    points = result["area"].get("representative_points", [])
                    if target != "pickup" and points:
                        self._bind(address, points[0], default=True)
        # After one Mega POI clarification, any subsequent insufficient reply uses its configured default.
        pickup = slots["pickup"]
        if pickup["is_mega_poi"] and not pickup["coords"] and pickup["metadata"].get("clarification_count") and re.search(REFUSE_DETAIL, search_key(text)):
            entry = pickup["metadata"].get("mega_poi")
            if not entry:
                state["booking_status"] = "operator_required"

    def _validate(self, state):
        state["issues"] = {}
        if state["booking_status"] in {"booked", "canceled"} or (state["booking_status"] == "cancel_pending" and state["transaction"].get("booking_result")):
            return
        slots = state["booking_slots"]
        schedule = state.get("schedule")
        if slots["pickup_time"]["value"]:
            if not schedule or schedule.get("status") != "valid":
                slots["pickup_time"]["status"] = "needs_clarification"
                state["issues"]["time"] = {"target": "pickup_time", "text": (schedule or {}).get("message") or "Bạn cho đủ ngày, giờ và buổi đón nhé."}
            elif schedule.get("mode") == "scheduled" and datetime.fromisoformat(schedule["pickup_at"]).timestamp() <= self.clock():
                slots["pickup_time"]["status"] = "needs_clarification"
                state["issues"]["time"] = {"target": "pickup_time", "text": "Giờ đón đã qua. Bạn chọn thời điểm mới nhé."}
        vehicle = slots["vehicle_type"]["value"]
        if vehicle and vehicle not in self.vehicle_catalog:
            slots["vehicle_type"]["status"] = "needs_clarification"
            state["issues"]["vehicle"] = {"target": "vehicle_type", "text": ASK["vehicle_type"]}
        passengers = slots["passengers"]["value"]
        if passengers is not None and (not isinstance(passengers, int) or passengers < 1 or (vehicle in self.vehicle_catalog and passengers > self.vehicle_catalog[vehicle]["max_passengers"])):
            slots["passengers"]["status"] = "needs_clarification"
            state["issues"]["capacity"] = {"target": "passengers", "text": "Số hành khách chưa phù hợp sức chứa xe. Bạn điều chỉnh số khách hoặc loại xe nhé."}

    def _next(self, state, prefix=""):
        slots = state["booking_slots"]
        for name, address in [("pickup", slots["pickup"]), ("destination", slots["destination"])] + [(f"stopovers.{i}", row["address"]) for i, row in enumerate(slots["stopovers"])]:
            batch = state["resolution"]["candidate_sets"].get(name)
            if batch:
                if batch["expires_at"] <= self.clock():
                    state["resolution"]["candidate_sets"].pop(name)
                    return self._respond(state, "clarify_address", self._join(prefix, "Danh sách địa điểm đã hết hạn. Bạn gửi lại địa chỉ nhé."), target_slots=[name])
                if getattr(self, "voice_mode", False):
                    batch["places"] = batch["places"][:2]
                candidates = [{"candidate_id": candidate_ref(batch["candidate_set_id"], p["id"]), "candidate_set_id": batch["candidate_set_id"],
                               "target": "stops" if name.startswith("stopovers") else name, "ordinal": i,
                               "label": p["label"], "stop_ref": name if name.startswith("stopovers") else None}
                              for i, p in enumerate(batch["places"], 1)]
                text = "Bạn chọn địa điểm nào?\n" + "\n".join(f"{row['ordinal']}. {row['label']}" for row in candidates)
                if getattr(self, "voice_mode", False):
                    text += ("\nBạn nói thứ nhất, thứ hai hoặc tên địa điểm nhé."
                             if len(candidates) == 2 else "\nBạn nói tên địa điểm hoặc thứ nhất nhé.")
                    text += " Nếu chưa đúng, bạn nói lại địa chỉ giúp mình."
                return self._respond(state, "clarify_address", self._join(prefix, text), target_slots=[name], candidates=candidates)
            if address["status"] == "empty":
                return self._respond(state, "ask_slot", self._join(prefix, ASK.get(name, "Bạn cho địa chỉ điểm dừng nhé.")), target_slots=[name])
            if address["status"] == "needs_clarification":
                if name == "pickup" and address["is_mega_poi"]:
                    count = address["metadata"].get("clarification_count", 0)
                    if count:
                        state["booking_status"] = "operator_required"
                        return self._respond(state, "human_handoff", self._join(prefix, "Mình cần nhân viên hỗ trợ xác định điểm đón; chưa tìm được điểm mặc định đủ rõ."))
                    address["metadata"]["clarification_count"] = 1
                return self._respond(state, "clarify_address", self._join(prefix, address["metadata"].get("clarification") or "Bạn cho địa chỉ cụ thể hơn nhé."), target_slots=[name])
            if address["status"] == "extracted":
                state["booking_status"] = "confirming"
                note = "\nGhi chú: " + address["note"] if address.get("note") else ""
                return self._respond(state, "confirm_slots", self._join(prefix, f"Có phải {'đón bạn' if name == 'pickup' else 'đến'} tại {address['formatted']} không?" + note), target_slots=[name])
        for name in ("pickup_time", "vehicle_type"):
            if slots[name]["status"] in {"empty", "needs_clarification"}:
                state["booking_status"] = "collecting"
                return self._respond(state, "ask_slot", self._join(prefix, ASK[name]), target_slots=[name])
        targets = [name for name in ("pickup_time", "vehicle_type", "passengers") if slots[name]["value"] is not None and slots[name]["status"] == "extracted"]
        if targets:
            state["booking_status"] = "confirming"
            return self._respond(state, "confirm_slots", self._join(prefix, self._summary_text(state) + "\nBạn xác nhận các thông tin này đúng chứ?"), target_slots=targets)
        return self._respond(state, "general_reply", self._join(prefix, "Bạn muốn bổ sung hay sửa thông tin nào?"))

    def _summary_text(self, state, target=None):
        slots = state["booking_slots"]
        rows = {"pickup": "Đón: " + (slots["pickup"]["formatted"] or slots["pickup"]["raw"] or "chưa có"),
                "destination": "Đến: " + (slots["destination"]["formatted"] or slots["destination"]["raw"] or "chưa có"),
                "pickup_time": "Giờ đón: " + ((state.get("schedule") or {}).get("label") or slots["pickup_time"]["value"] or "chưa có"),
                "vehicle_type": "Loại xe: " + self.vehicle_catalog.get(slots["vehicle_type"]["value"], {}).get("label", "chưa có")}
        if slots["passengers"]["value"] is not None:
            rows["passengers"] = f"Số khách: {slots['passengers']['value']}"
        if target in rows:
            return rows[target]
        text = "\n".join(rows.values())
        for stop in slots["stopovers"]:
            text += f"\nĐiểm dừng {stop['order']}: {stop['address']['formatted'] or stop['address']['raw']}"
        if slots["pickup"]["note"]:
            text += "\nGhi chú đón: " + slots["pickup"]["note"]
        if slots["general_note"]:
            text += "\nGhi chú: " + slots["general_note"]
        return text

    def _booking_summary(self, state, prefix=""):
        slots = state["booking_slots"]
        text = self._summary_text(state) + "\n" + self.kb.price_text(slots["vehicle_type"]["value"])
        text += "\nBạn đồng ý tạo cuốc xe theo các thông tin này chứ?"
        scope = list(REQUIRED) + [f"stopovers.{i}" for i in range(len(slots["stopovers"]))]
        snapshot = self._snapshot_fingerprint(state)
        summary = {"pickup": slots["pickup"]["formatted"], "destination": slots["destination"]["formatted"],
                   "pickup_time": (state.get("schedule") or {}).get("label", ""), "passengers": slots["passengers"]["value"],
                   "vehicle_type": slots["vehicle_type"]["value"], "vehicle_label": self.vehicle_catalog[slots["vehicle_type"]["value"]]["label"],
                   "customer_phone": state["customer_phone"], "customer_name": state["customer_name"],
                   "pickup_note": slots["pickup"]["note"], "general_note": slots["general_note"],
                   "stopovers": deepcopy(slots["stopovers"]), "tariff": self.kb.tariff(slots["vehicle_type"]["value"]),
                   "distance_km": slots["distance_km"], "duration_minutes": slots["duration_minutes"],
                   "booking_revision": state["control"]["booking_revision"], "snapshot_fingerprint": snapshot}
        self._respond(state, "confirm_booking", self._join(prefix, text), target_slots=scope,
                      summary=summary, metadata={"snapshot_fingerprint": snapshot})
        summary["prompt_id"] = state["last_bot_action"]["metadata"]["prompt_id"]
        state["confirmation"]["pending_prompt"] = deepcopy(state["last_bot_action"]["metadata"])
        return state

    def _respond(self, state, action, text, focus=None, *, target_slots=None, metadata=None,
                 candidates=None, summary=None, reason=None):
        targets = target_slots if target_slots is not None else [focus] if focus else []
        state["control"]["generation"] += 1
        generation = state["control"]["generation"]
        response_id = "res_" + fingerprint([state["session_id"], state["control"].get("last_event_id"), generation])[:24]
        meta = {"response_id": response_id, "prompt_id": "prompt_" + response_id,
                "booking_revision": state["control"]["booking_revision"], **(metadata or {})}
        canonical_action = {"offer_candidates": "clarify_address", "confirm_location": "confirm_slots",
                            "ask_clarification": "clarify_address", "goodbye": "general_reply",
                            "handoff": "human_handoff", "inform_pending": "general_reply"}.get(action, action)
        state["last_bot_action"] = {"action_type": canonical_action, "target_slots": targets, "metadata": meta}
        state["current_focus"] = targets[0] if len(targets) == 1 else None
        state["final_response_text"] = text
        state["candidates"] = candidates or []
        if canonical_action == "human_handoff" and state["booking_status"] not in {"booked", "canceled"}:
            state["booking_status"] = "operator_required"
        presentation = {"contract_version": "chat-presentation-3", "response_id": response_id,
                        "generation": generation, "prompt_id": meta["prompt_id"],
                        "booking_revision": state["control"]["booking_revision"],
                        "snapshot_fingerprint": meta.get("snapshot_fingerprint")}
        response = {"response_id": response_id, "generation": generation, "text": text,
                    "action": canonical_action, "focus": state["current_focus"], "current_focus": state["current_focus"],
                    "candidates": candidates or [], "summary": summary, "presentation": presentation,
                    "delivery_status": "planned", "booking_status": state["booking_status"], "reason": reason,
                    "booking": deepcopy(state["transaction"].get("booking_result")), "inquiry": None}
        state["last_response"], state["response"] = response, response
        state["messages"].append({"role": "assistant", "content": text, "response_id": response_id})
        pending = state["dialogue"].get("pending_prompt") or {}
        if canonical_action != "answer_question" or pending.get("scope_kind") != "inquiry":
            state["dialogue"]["pending_prompt"] = None
        if canonical_action != "confirm_booking":
            state["confirmation"]["pending_prompt"] = None
        self._views(state)
        return state

    async def _answer(self, state, question):
        text = question["raw_text"]
        key = search_key(text)
        vehicle = question.get("vehicle_ref") or state["booking_slots"]["vehicle_type"]["value"]
        if question["type"] == "route_membership":
            return await self._route_membership(state, text)
        if (question["type"] in {"fare_estimate", "price_objection"} or re.search(r"\b(?:gia|cuoc|bao nhieu tien|mot cay|moi km)\b", key)) and question.get("route_scope") not in {"explicit_pair", "active_inquiry"}:
            return self.kb.price_text(vehicle)
        hits = self.kb.retrieve(text, vehicle)
        if hits:
            state["turn"].setdefault("knowledge_sources", []).extend(hits)
            return "\n".join(row["text"] for row in hits)
        if re.search(r"\b(?:da chon xe|chon xe gi|dat di dau|nay toi|doc lai|nhac lai|thong tin chuyen)\b", key):
            return self._summary_text(state, "vehicle_type" if "xe gi" in key else "destination" if "di dau" in key else None)
        async with asyncio.timeout(self.inquiry_service.read_deadline):
            try:
                from app.contracts.turn import QuestionIntent
                return await self.inquiry_service.answer(state, [QuestionIntent.model_validate(question)])
            except (TimeoutError, ValueError, KeyError):
                return "Mình chưa lấy được thông tin có nguồn cho câu hỏi này. Bạn hỏi rõ hơn hoặc thử lại nhé."

    async def _route_membership(self, state, text):
        """Sourced detour estimate; no fabricated route geometry or booking edits."""
        slots = state["booking_slots"]
        if not slots["pickup"]["coords"] or not slots["destination"]["coords"]:
            return "Bạn cho điểm đón và điểm đến để mình kiểm tra điểm này gần tuyến đi không nhé."
        match = re.search(r"(?:qua|ở|tại)\s+(.+?)(?=\s+(?:không|có|nằm|trên|gần|trong)|[?]|$)", text, re.I)
        query = match.group(1) if match else re.split(r"\s+có\s+(?:nằm|ở|gần)", text, flags=re.I)[0]
        try:
            async with asyncio.timeout(self.inquiry_service.read_deadline):
                result = await self._map(query.strip(), "destination", state)
                if result["status"] != "resolved":
                    return "Mình chưa xác định duy nhất điểm cần kiểm tra. Bạn cho tên và địa chỉ cụ thể của điểm đó nhé."
                point = result["place"]
                addresses = [slots["pickup"], *(row["address"] for row in slots["stopovers"]), slots["destination"]]
                if any(not address["coords"] for address in addresses):
                    return "Mình cần xác định các điểm dừng trước khi kiểm tra tuyến đầy đủ."
                vehicle = slots["vehicle_type"]["value"] or "oto_4_cho"
                detours, sources = [], set()
                for a, b in zip(addresses, addresses[1:]):
                    origin, destination = self._place(a), self._place(b)
                    if point["id"] in {origin["id"], destination["id"]}:
                        return f"{point['label']} chính là một điểm đã có trong chuyến đi. Nguồn: {point['source']}."
                    legs = await asyncio.gather(*(self.maps.route(x, y, vehicle_type=vehicle) for x, y in
                                                 ((origin, destination), (origin, point), (point, destination))))
                    checked = [RouteResult.model_validate(leg).model_dump() for leg in legs]
                    if any(leg["pickup_id"] != x["id"] or leg["destination_id"] != y["id"]
                           for leg, (x, y) in zip(checked, ((origin, destination), (origin, point), (point, destination)))):
                        raise ValueError("Unrelated detour route")
                    detours.append(max(0, checked[1]["distance_m"] + checked[2]["distance_m"] - checked[0]["distance_m"]))
                    sources.update(leg["source"] for leg in checked)
                extra = min(detours) / 1000
                conclusion = "có thể gần tuyến đi" if extra <= .5 else "cần đi vòng thêm"
                return (f"Theo so sánh các tuyến đường qua {point['label']}, điểm này {conclusion}; "
                        f"quãng đường tăng ước tính ít nhất {extra:.2f} km. Đây là so sánh lộ trình, chưa xác nhận điểm nằm chính xác trên tuyến. "
                        f"Nguồn: {', '.join(sorted(sources))}.")
        except Exception:
            state["tool_status"] = "API_ERROR"
            return "Dịch vụ bản đồ chưa đủ dữ liệu để kiểm tra điểm này trên hoặc gần tuyến; bạn có thể thử lại."

    async def _inquiry_action(self, state, kind, command, event, acts, questions):
        service = self.inquiry_service
        try:
            item = service.validate_action(state, command, event.get("reply_to_response_id"))
            if kind in {"use_inquiry_route", "promote"}:
                acts.extend(service.promote(state, item, command if kind == "use_inquiry_route" else None))
            elif kind in {"resume_booking", "dismiss", "dismiss_inquiry"}:
                state["active_inquiry_id"] = None
                state["dialogue"]["pending_prompt"] = None
            elif kind in {"select_candidate", "reject_candidate"}:
                if kind == "select_candidate":
                    service.select(state, item, command.get("value"))
                else:
                    item["candidate_sets"] = {}
            elif kind == "reverse":
                origin, destination = item["origin"], item["destination"]
                service.update(item, "origin", destination)
                service.update(item, "destination", origin)
            else:
                service.update(item, "vehicle" if kind == "choose_inquiry_vehicle" else command["field"], command.get("vehicle_type") or command.get("value"))
            if kind not in {"promote", "use_inquiry_route", "resume_booking", "dismiss", "dismiss_inquiry"}:
                questions.extend({**q, "route_scope": "active_inquiry", "origin": None, "destination": None} for q in item.get("questions", []))
        except (ValueError, TypeError, KeyError):
            state["turn"]["issues"].append("STALE_INQUIRY")

    def _snapshot_payload(self, state):
        self._views(state)
        slots = state["booking_slots"]
        return deepcopy({"session_id": state["session_id"], "draft_id": state["control"]["draft_id"],
                         "customer_phone": state["customer_phone"], "customer_name": state["customer_name"],
                         "booking_slots": slots, "pickup_schedule": state.get("schedule"),
                         "pickup": self._place(slots["pickup"]), "destination": self._place(slots["destination"]),
                         "slots": {k: v["value"] for k, v in state["booking_state"].items()},
                         "stopovers": slots["stopovers"], "route": state["resolution"].get("route"),
                         "tariff": self.kb.tariff(slots["vehicle_type"]["value"]),
                         "policy_version": "architecture-fixed-1"})

    def _snapshot_fingerprint(self, state):
        payload = self._snapshot_payload(state)
        # Optional route estimates are informational and do not silently alter consent.
        payload.pop("route", None)
        payload["booking_slots"].pop("distance_km", None)
        payload["booking_slots"].pop("duration_minutes", None)
        return fingerprint(payload)

    async def _route(self, state):
        slots = state["booking_slots"]
        points = [self._place(slots["pickup"]), *(self._place(stop["address"]) for stop in slots["stopovers"]), self._place(slots["destination"])]
        distance = duration = 0
        legs = []
        when = (state.get("schedule") or {}).get("pickup_at")
        try:
            for a, b in zip(points, points[1:]):
                kwargs = {"vehicle_type": slots["vehicle_type"]["value"]}
                if when and "departure_time" in inspect.signature(self.maps.route).parameters:
                    kwargs["departure_time"] = datetime.fromisoformat(when)
                leg = RouteResult.model_validate(await self.maps.route(a, b, **kwargs)).model_dump(mode="json")
                if (leg["pickup_id"] != a["id"] or leg["destination_id"] != b["id"]
                        or leg["vehicle_profile"] != self.vehicle_catalog[slots["vehicle_type"]["value"]]["route_profile"]):
                    raise ValueError("Unrelated route result")
                distance += leg["distance_m"]
                duration += leg["duration_s"]
                legs.append(leg)
        except Exception:
            state["tool_status"] = "API_ERROR"
            return None
        slots["distance_km"], slots["duration_minutes"] = distance / 1000, duration / 60
        state["resolution"]["route"] = {"success": True, "distance_m": distance, "duration_s": duration,
                                         "distance_km": distance / 1000, "duration_minutes": duration / 60,
                                         "waypoints_count": len(points), "legs": legs,
                                         "source": ";".join(dict.fromkeys(leg["source"] for leg in legs)),
                                         "vehicle_profile": self.vehicle_catalog[slots["vehicle_type"]["value"]]["route_profile"],
                                         "pickup_id": points[0]["id"], "destination_id": points[-1]["id"]}
        return state["resolution"]["route"]

    @staticmethod
    def _unknown(state):
        return (state["transaction"].get("active_operation") or {}).get("status") in {"dispatched", "unknown"}

    async def finalize(self, state, event, ingress_guard=None):
        result_before = state["transaction"].get("booking_result")
        await self._recover(state)
        if not result_before and state["transaction"].get("booking_result"):
            return self._respond(state, "inform_success", self._terminal_text(state))
        if state["turn"].get("cancel_authorized"):
            return await self._cancel(state)
        if state["transaction"].get("booking_result") or self._unknown(state) or not state["turn"].get("ready_to_dispatch") or not ready_to_book(state):
            return state
        self._validate(state)
        if state["issues"]:
            state["confirmation"]["accepted_snapshot"] = None
            return self._next(state)
        if ingress_guard:
            allowed = ingress_guard()
            allowed = await allowed if inspect.isawaitable(allowed) else allowed
            if not allowed:
                state["confirmation"]["accepted_snapshot"] = None
                return self._respond(state, "general_reply", "Mình đã nhận thêm tin nhắn, sẽ xử lý thông tin mới trước khi đặt.", reason="SUPERSEDED_BEFORE_DISPATCH")
        await self._route(state)
        payload = self._snapshot_payload(state)
        key = "create:" + state["control"]["draft_id"] + ":" + fingerprint(payload)
        state["transaction"]["active_operation"] = {"type": "create", "idempotency_key": key, "payload": payload, "status": "dispatched"}
        try:
            result = await self.booking.create(payload, key)
        except TimeoutError:
            state["transaction"]["active_operation"]["status"] = "unknown"
            return self._respond(state, "general_reply", "Đang đối soát kết quả đặt xe. Mình chưa tạo đơn khác.")
        self._integrate_create(state, result, payload)
        return self._respond(state, "inform_success" if state["booking_status"] == "booked" else "general_reply", self._terminal_text(state))

    def _integrate_create(self, state, result, payload):
        state["transaction"]["active_operation"]["status"] = result["status"]
        if result["status"] != "succeeded":
            state["confirmation"]["accepted_snapshot"] = None
            return
        state["transaction"]["booking_result"] = deepcopy(result)
        state["transaction"]["committed_snapshot"] = deepcopy(payload)
        state["booking_status"] = "booked"
        if "booking_slots" in payload:
            state["booking_slots"] = deepcopy(payload["booking_slots"])
            state["schedule"] = deepcopy(payload.get("pickup_schedule"))
        else:
            # Completed legacy transactions keep their real provider facts and IDs.
            for target in ("pickup", "destination"):
                if payload.get(target):
                    self._bind(state["booking_slots"][target], payload[target])
                    state["booking_slots"][target]["status"] = "confirmed"
        if self.crm and state["customer_phone"] and state["customer_name"]:
            for name in ("pickup", "destination"):
                address = state["booking_slots"][name]
                if address["status"] != "confirmed" or not address.get("coords"):
                    continue
                key = search_key(address["raw"] or "")
                label = "home" if re.search(r"\bnha\b", key) else "work" if "cong ty" in key else name
                self.crm.remember(state["customer_phone"], label, address)

    async def _cancel(self, state):
        result = state["transaction"].get("booking_result")
        if not result:
            state["booking_status"] = "canceled"
            state["transaction"]["active_operation"] = None
            return self._respond(state, "general_reply", "Đã hủy yêu cầu đặt xe; chưa có cuốc xe nào được tạo.")
        key = "cancel:" + result["booking_id"]
        state["transaction"]["active_operation"] = {"type": "cancel", "idempotency_key": key, "booking_id": result["booking_id"], "status": "dispatched"}
        try:
            receipt = await self.booking.cancel(result["booking_id"], key)
        except TimeoutError:
            state["transaction"]["active_operation"]["status"] = "unknown"
            return self._respond(state, "general_reply", "Đang đối soát yêu cầu hủy; chưa xác định kết quả cuối cùng.")
        self._integrate_cancel(state, receipt)
        return self._respond(state, "inform_success" if state["booking_status"] == "canceled" else "general_reply", self._terminal_text(state))

    @staticmethod
    def _integrate_cancel(state, result):
        state["transaction"]["active_operation"]["status"] = result["status"]
        if result["status"] == "succeeded":
            state["booking_status"] = "canceled"
            state["transaction"]["booking_result"]["provider_status"] = "cancelled"
        else:
            state["booking_status"] = "booked"

    async def _recover(self, state):
        operation = state["transaction"].get("active_operation")
        if not operation and not state["transaction"].get("booking_result"):
            receipt = await self.booking.find_by_draft(state["control"]["draft_id"])
            if receipt:
                state["transaction"]["active_operation"] = {"type": "create", "status": "succeeded", "idempotency_key": "recovered:" + state["control"]["draft_id"]}
                self._integrate_create(state, {**receipt, "status": "succeeded"}, receipt["payload"])
                if receipt.get("provider_status") == "cancelled":
                    state["booking_status"] = "canceled"
            return
        if not operation or operation["status"] not in {"dispatched", "unknown"}:
            return
        receipt = await self.booking.lookup(operation["idempotency_key"])
        if not receipt and operation["type"] == "create":
            receipt = await self.booking.find_by_draft(state["control"]["draft_id"])
            if receipt:
                receipt["status"] = "succeeded"
        if receipt:
            if operation["type"] == "create":
                self._integrate_create(state, receipt, operation["payload"])
            else:
                self._integrate_cancel(state, receipt)

    def _booking_text(self, state):
        return self._terminal_text(state)

    async def reconcile(self, state, event_id=None):
        state = self.migrate_state(state)
        await self._recover(state)
        state["control"]["last_event_id"] = event_id
        return self._respond(state, "general_reply", "Giao dịch còn đang đối soát." if self._unknown(state) else self._terminal_text(state))

    @staticmethod
    def _terminal_text(state):
        if state["booking_status"] == "operator_required":
            return "Mình cần nhân viên hỗ trợ xác định vị trí đón. Yêu cầu đã được giữ để hỗ trợ; chưa tạo cuốc xe."
        if state["booking_status"] == "canceled":
            return "Yêu cầu đặt xe đã hủy. Bạn có thể mở phiên mới để đặt chuyến khác."
        booking = state["transaction"].get("booking_result")
        if booking:
            schedule = (state.get("schedule") or {}).get("label")
            return f"Đã tạo cuốc xe thử nghiệm {booking['booking_id']}. " + (f"Giờ đón: {schedule}. " if schedule else "") + METER_NOTE
        return "Chưa tạo cuốc xe. Bạn kiểm tra và xác nhận thông tin nhé."

    async def process(self, state, text, *, action=None, delivered_response_ids=None,
                      reply_to_response_id=None, event_id=None, ingress_guard=None, occurred_at=None, **_):
        if event_id and state["control"].get("last_event_id") == event_id:
            return state
        event = {"event_id": event_id or str(uuid.uuid4()), "text": text, "action": action,
                 "delivered_response_ids": delivered_response_ids or [],
                 "reply_to_response_id": reply_to_response_id,
                 "occurred_at": occurred_at if occurred_at is not None else self.clock()}
        interpreted = await self.interpret(state, event)
        prepared = await self.prepare(state, event, interpreted)
        return await self.finalize(prepared, event, ingress_guard)


def slots_have_coordinates(state):
    slots = state["booking_slots"]
    return (slots["vehicle_type"]["value"] in {"xe_may", "oto_4_cho", "oto_7_cho"}
            and all(address.get("coords") for address in [slots["pickup"], slots["destination"],
                    *(row["address"] for row in slots["stopovers"])]))
