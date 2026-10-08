"""Archived V2/V3 orchestration; export the current architecture-fixed engine.

LegacyConversationEngine is only used by historical compatibility tests.
HTTP, TextBot and CLI run domain.booking_engine.ConversationEngine.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from copy import deepcopy
from datetime import UTC, datetime

from app.adapters.extractor import ExtractorError
from app.adapters.turn_fixture import faq_turn
from app.contracts.nlu import NluInput, NluResult, validate_candidate_references
from app.contracts.turn import QuestionIntent, TurnInput, TurnResult
from app.domain.assistance_policy import AssistancePolicy
from app.domain.engine import ChatEngine, acknowledge, new_state, normalized
from app.domain.inquiries import MAP_QUESTIONS, InquiryService
from app.domain.location_confirmation import (
    LOCATION_PURPOSES,
    LocationWorkflowMixin,
    fee_reply_matches,
    upgrade_state,
)


def migrate_state(state: dict) -> dict:
    """Migrate at a safe turn boundary; retain legacy in-flight transactions."""
    state = deepcopy(state)
    if state["control"].get("schema_version", 4) < 5:
        if state["booking_status"] in {
            "booking_in_progress",
            "booking_unknown",
            "cancel_pending",
            "cancel_unknown",
        }:
            return state
        state["control"].update(
            schema_version=5,
            graph_version="chat-graph-2",
            policy_version="mvp-chat-policy-2",
            turn_version="parrotgo-turn-2",
        )
        state["confirmation"]["pending_prompt"] = None
        state["confirmation"]["accepted_snapshot"] = None
    state.setdefault("inquiries", {})
    state.setdefault("active_inquiry_id", None)
    state.setdefault(
        "dialogue",
        {
            "pending_prompt": None,
            "suspended_booking_prompt": None,
            "last_discussed_route_ref": None,
            "pending_questions": [],
            "booking_started": any(s["value"] is not None for s in state["booking_state"].values()),
        },
    )
    state.setdefault("travel_party", {"adults": None, "children": None})
    state.setdefault("read_requests", {})
    state.setdefault("read_facts", {})
    return state


def new_conversation_state(session_id: str, draft_id: str | None = None) -> dict:
    state = migrate_state(new_state(session_id, draft_id))
    state["last_response"]["text"] = (
        "Mình là trợ lý đặt xe ParrotGo. Bạn có thể hỏi vị trí địa điểm, giá, tuyến đường, thời tiết hoặc đặt một chuyến thử nghiệm. Bạn cần mình giúp gì?"
    )
    state["last_response"]["presentation"]["contract_version"] = "chat-presentation-2"
    state["last_response"]["inquiry"] = None
    return state


class LegacyConversationEngine(LocationWorkflowMixin, ChatEngine):
    def __init__(
        self,
        *args,
        weather=None,
        brand_name="ParrotGo",
        inquiry_ttl=600,
        read_deadline=15,
        location_confirmation=False,
        assistance_policy_path=None,
        area_assistance_enabled=False,
        service_area_path=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.brand_name = brand_name
        self.location_confirmation = location_confirmation
        self.area_assistance_enabled = area_assistance_enabled
        self.assistance_policy = AssistancePolicy.load(assistance_policy_path)
        self.vehicle_catalog = deepcopy(self.quote_adapter.vehicle_catalog)
        for code, row in self.vehicle_catalog.items():
            row.setdefault("route_profile", "car")
            row.setdefault("bookable", True)
            row.setdefault("quote_available", True)
            if row.get("bookable") and row["route_profile"] not in getattr(
                self.maps, "supported_profiles", {"car"}
            ):
                raise ValueError(f"Bookable vehicle {code} needs a verified route profile")
        self.inquiry_service = InquiryService(
            self, weather=weather, ttl=inquiry_ttl, read_deadline=read_deadline
        )

    def new_state(self, session_id, draft_id=None):
        state = new_conversation_state(session_id, draft_id)
        upgrade_state(state, self.location_confirmation)
        state["last_response"]["text"] = state["last_response"]["text"].replace(
            "ParrotGo", self.brand_name
        )
        return state

    def projection(self, state, text, occurred_at):
        state = migrate_state(state)
        upgrade_state(state, self.location_confirmation)
        base = {
            "utterance": {"text": text, "asr_confidence": None},
            "conversation_context": state["conversation_context"],
            "booking_state": state["booking_state"],
            "booking_status": state["booking_status"],
            "candidates": [
                {
                    k: v
                    for k, v in c.items()
                    if k
                    in {
                        "candidate_id",
                        "candidate_set_id",
                        "target",
                        "ordinal",
                        "label",
                        "stop_ref",
                    }
                }
                for c in state["candidates"]
            ],
        }
        if state["control"]["schema_version"] < 5:
            return NluInput.model_validate(base)
        prompt = state["dialogue"].get("pending_prompt")
        if prompt and prompt.get("scope_kind") == "inquiry":
            base["conversation_context"] = {
                "last_bot_message": state["conversation_context"].get("last_bot_message"),
                "last_bot_action": state["conversation_context"].get("last_bot_action"),
                "current_focus": None,
            }
        return TurnInput.model_validate(
            {
                **base,
                "contract_version": state["control"].get("turn_version", "parrotgo-turn-2"),
                "inquiries": [
                    {
                        k: i.get(k)
                        for k in (
                            "inquiry_id",
                            "revision",
                            "origin",
                            "destination",
                            "vehicle",
                            "departure_time",
                            "status",
                        )
                    }
                    for i in state["inquiries"].values()
                ],
                "active_inquiry_id": state["active_inquiry_id"],
                "pending_prompt": prompt,
                "capabilities": {
                    "inquiry_v2": True,
                    "weather": self.inquiry_service.weather is not None,
                    "electric_motorbike": bool(
                        self.vehicle_catalog.get("xe_may_dien", {}).get("bookable")
                    ),
                    "scheduled": True,
                },
                "occurred_at": datetime.fromtimestamp(occurred_at, UTC).isoformat(),
            }
        )

    async def interpret(self, state, event):
        when = event.get("occurred_at", self.clock())
        if event.get("action"):
            return {"kind": "action", "occurred_at": when}
        state = acknowledge(deepcopy(state), event.get("delivered_response_ids"))
        data = self.projection(state, event["text"], when)
        shortcut = faq_turn(data) if isinstance(data, TurnInput) else None
        try:
            result = shortcut or await self.extractor(data)
            if isinstance(result, TurnResult):
                result.validate_evidence(data)
                return {
                    "kind": "turn",
                    "result": result.model_dump(mode="json"),
                    "occurred_at": when,
                }
            if isinstance(result, dict) and result.get("contract_version") in {"parrotgo-turn-2", "parrotgo-turn-3"}:
                result = TurnResult.model_validate(result)
                result.validate_evidence(data)
                return {
                    "kind": "turn",
                    "result": result.model_dump(mode="json"),
                    "occurred_at": when,
                }
            result = NluResult.model_validate(result)
            validate_candidate_references(result, data)
            return {"kind": "legacy", "result": result.model_dump(mode="json"), "occurred_at": when}
        except ExtractorError as exc:
            if exc.retryable or exc.code in {"RATE_LIMITED", "RATE_LIMIT", "QUOTA_EXCEEDED"}:
                raise
            return {"kind": "error", "code": exc.code, "occurred_at": when}
        except (ValueError, TypeError):
            return {"kind": "error", "code": "OUTPUT_INVALID", "occurred_at": when}

    async def prepare(self, state, event, interpretation):
        # An interpretation already checkpointed under V2 resumes under V2.
        # A fresh interpret node advertises V3 before a safe draft migration.
        incoming_version = interpretation.get("result", {}).get("contract_version")
        state = upgrade_state(migrate_state(state), self.location_confirmation and incoming_version != "parrotgo-turn-2")
        acknowledge(state, event.get("delivered_response_ids"))
        if self._location_v3(state) and interpretation["kind"] == "turn":
            result = TurnResult.model_validate(interpretation["result"])
            prompt = state["dialogue"].get("pending_prompt") or {}
            if prompt.get("purpose") in LOCATION_PURPOSES and result.speech_status == "clear" and result.location_decisions:
                # Corrections and questions take precedence over location consent.
                changes = [act for act in result.booking_acts if act.intent != "confirm"]
                if not changes and not result.travel_party and not result.questions and not result.inquiry_actions and len(result.location_decisions) == 1:
                    decision = result.location_decisions[0].decision
                    folded = normalized(event["text"])
                    conflict = decision in {"confirm", "request_assistance", "accept_fee"} and (
                        re.search(r"\b(neu|mien la|chi khi|nhung|doi|sua|chua|khong phai|khong dong y)\b", folded)
                        or (prompt.get("field") in {"destination"} and re.search(r"\b(diem don|pickup)\b", folded) and not re.search(r"\b(diem den|destination)\b", folded))
                        or (prompt.get("field") in {"pickup", "origin"} and re.search(r"\b(diem den|destination)\b", folded) and not re.search(r"\b(diem don|pickup)\b", folded))
                    )
                    if prompt.get("purpose") == "confirm_assistance" and decision in {"confirm", "accept_fee"} and not fee_reply_matches(event["text"], prompt.get("fee_amount")):
                        conflict = True
                    if conflict:
                        state["control"]["last_event_id"] = event["event_id"]
                        return self._respond(state, "ask_clarification", "Mình chưa xác nhận vì phản hồi có điều kiện hoặc nói về địa điểm khác. Bạn nêu rõ địa chỉ cần sửa, hoặc xác nhận đúng địa điểm đang được hỏi nhé.", reason="LOCATION_CONSENT_SCOPE_UNCLEAR")
                    if prompt.get("scope_kind") == "inquiry":
                        return await self.inquiry_service.apply_location_decision(state, decision, event)
                    return await self._apply_location_decision(state, decision, event)
                result.booking_acts = [act for act in result.booking_acts if act.intent != "confirm"]
                interpretation = {**interpretation, "result": result.model_dump(mode="json")}
        return await self._prepare_base(state, event, interpretation)

    async def _prepare_base(self, state, event, interpretation):
        state = migrate_state(state)
        acknowledge(state, event.get("delivered_response_ids"))
        text, event_id = event["text"], event["event_id"]
        if interpretation["kind"] == "error":
            state["control"]["last_event_id"] = event_id
            state["turn"] = {"issues": [], "questions": [], "changed_slots": []}
            return self._respond(
                state,
                "ask_clarification",
                ("Dịch vụ AI đang gặp lỗi cấu hình. Thông tin chuyến vẫn được giữ; bạn có thể gửi tiếp hoặc thử lại sau."
                 if interpretation["code"] in {"PROVIDER_AUTH_ERROR", "PROVIDER_MODEL_UNAVAILABLE", "PROVIDER_CONFIG_ERROR"}
                 else "Mình chưa hiểu chắc tin nhắn này. Bạn viết rõ hơn giúp mình nhé; thông tin chuyến vẫn được giữ."),
                reason=interpretation["code"],
            )
        if state["control"]["schema_version"] < 5 or interpretation["kind"] == "legacy":
            return await super().process(
                state,
                text,
                action=event.get("action"),
                interpretation=interpretation.get("result"),
                event_id=event_id,
                delivered_response_ids=event.get("delivered_response_ids"),
                reply_to_response_id=event.get("reply_to_response_id"),
                allow_dispatch=False,
                occurred_at=interpretation["occurred_at"],
            )
        result = (
            TurnResult.model_validate(interpretation["result"])
            if interpretation["kind"] == "turn"
            else None
        )
        if result and result.speech_status != "clear":
            state["control"]["last_event_id"] = event_id
            state["turn"] = {"issues": [], "questions": [], "changed_slots": []}
            return self._respond(
                state,
                "ask_clarification",
                "Mình chưa hiểu chắc lời bạn nói. Bạn nói hoặc viết lại giúp mình nhé.",
            )
        acts = (
            [a.model_dump(exclude={"evidence_span"}, mode="json") for a in result.booking_acts]
            if result
            else []
        )
        questions = list(result.questions) if result else []
        actions = list(result.inquiry_actions) if result else []
        action = event.get("action")
        kind = action.get("type") if action else None
        state["turn"] = {
            "issues": [],
            "questions": [],
            "changed_slots": [],
            "occurred_at": interpretation["occurred_at"],
        }
        service = self.inquiry_service
        try:
            if kind in {
                "use_inquiry_route",
                "choose_inquiry_vehicle",
                "dismiss_inquiry",
                "resume_booking",
            }:
                item = (
                    service.validate_action(state, action, event.get("reply_to_response_id"))
                    if kind not in {"resume_booking", "dismiss_inquiry"}
                    else service.active(state)
                )
                if kind == "use_inquiry_route":
                    if (state.get("last_response") or {}).get("delivery_status") != "rendered":
                        raise ValueError("UNSHOWN_INQUIRY")
                    acts.extend(service.promote(state, item, action))
                    action = None
                elif kind == "choose_inquiry_vehicle":
                    if action["vehicle_type"] not in self.vehicle_catalog:
                        raise ValueError("UNSUPPORTED_VEHICLE")
                    service.update(item, "vehicle", action["vehicle_type"])
                    questions = [
                        QuestionIntent.model_validate(q).model_copy(
                            update={
                                "route_scope": "active_inquiry",
                                "origin": None,
                                "destination": None,
                                "vehicle_ref": action["vehicle_type"],
                            }
                        )
                        for q in item["questions"]
                    ]
                    action = None
                else:
                    if item and kind == "dismiss_inquiry":
                        item["status"] = "abandoned"
                    state["active_inquiry_id"] = None
                    state["dialogue"]["pending_prompt"] = None
                    action = None
                    acts.append({"intent": "chit_chat", "target": None, "value": None})
            elif kind == "select_candidate":
                item = service.active(state)
                if item and any(
                    batch["candidate_set_id"] == action["candidate_set_id"]
                    for batch in item["candidate_sets"].values()
                ):
                    service.validate_action(
                        state, {"inquiry_id": item["inquiry_id"]}, event.get("reply_to_response_id")
                    )
                    service.select(state, item, action["candidate_id"], action["candidate_set_id"])
                    questions = [
                        QuestionIntent.model_validate(q).model_copy(
                            update={
                                "route_scope": "active_inquiry",
                                "origin": None,
                                "destination": None,
                            }
                        )
                        for q in item["questions"]
                    ]
                    action = None
            for command in actions:
                if command.type in {"resume_booking", "dismiss"}:
                    state["active_inquiry_id"] = None
                    state["dialogue"]["pending_prompt"] = None
                    acts.append({"intent": "chit_chat", "target": None, "value": None})
                    continue
                if (
                    command.type == "promote"
                    and len(state["inquiries"]) > 1
                    and "vua hoi" not in normalized(text)
                    and not command.inquiry_id
                ):
                    raise ValueError("AMBIGUOUS_INQUIRY")
                item = service.validate_action(
                    state, {"inquiry_id": command.inquiry_id}, event.get("reply_to_response_id")
                )
                if command.type == "promote":
                    acts.extend(service.promote(state, item))
                elif command.type == "update":
                    service.update(item, command.field, command.value)
                elif command.type == "reverse":
                    origin, destination = item["origin"], item["destination"]
                    service.update(item, "origin", destination)
                    service.update(item, "destination", origin)
                elif command.type == "select_candidate":
                    service.select(state, item, command.value)
                elif command.type == "reject_candidate":
                    item["candidate_sets"] = {}
                    item["locations"].pop(command.field, None)
                    service._prompt(state, item, command.field)
                if command.type not in {"promote", "reject_candidate"}:
                    questions = [
                        QuestionIntent.model_validate(q).model_copy(
                            update={
                                "route_scope": "active_inquiry",
                                "origin": None,
                                "destination": None,
                                "departure_time_ref": item.get("departure_time"),
                            }
                        )
                        for q in item["questions"]
                    ]
        except (ValueError, KeyError) as exc:
            state["control"]["last_event_id"] = event_id
            message = "Tuyến hỏi thử hoặc lựa chọn đã cũ. Bạn hỏi lại tuyến hoặc tiếp tục chuyến đang đặt nhé."
            if str(exc) == "NEW_SESSION_REQUIRED":
                message = "Đơn hiện tại không được mở lại hoặc sửa. Bạn hãy mở chuyến mới để dùng tuyến hỏi thử này."
            return self._respond(state, "ask_clarification", message, reason=str(exc))
        if result:
            # A read-only question cannot supply booking mutations from its span.
            protected = [
                q.evidence_span
                for q in result.questions
                if q.relation_to_booking != "explicit_update"
            ]
            acts = [
                a.model_dump(exclude={"evidence_span"}, mode="json")
                for a in result.booking_acts
                if not (
                    a.intent in {"provide_info", "change_info"}
                    and any(
                        q.start <= a.evidence_span.start and a.evidence_span.end <= q.end
                        for q in protected
                    )
                )
            ] + [
                a
                for a in acts
                if a
                not in [
                    b.model_dump(exclude={"evidence_span"}, mode="json")
                    for b in result.booking_acts
                ]
            ]
            if result.travel_party and state["booking_status"] not in {
                "booked",
                "cancelled",
                "booking_in_progress",
                "booking_unknown",
                "cancel_pending",
                "cancel_unknown",
                "cancel_failed",
            }:
                party = {
                    "adults": result.travel_party.adults,
                    "children": result.travel_party.children,
                }
                if party != state["travel_party"]:
                    state["travel_party"] = party
                    state["confirmation"]["accepted_snapshot"] = None
                    state["confirmation"]["pending_prompt"] = None
                    state["control"]["booking_revision"] += 1
            if "repeat" in result.conversational_acts:
                acts.append({"intent": "request_repeat", "target": None, "value": None})
        if any(a["intent"] in {"provide_info", "change_info"} for a in acts):
            # A changed trip always needs a new summary and separate consent.
            acts = [a for a in acts if not (a["intent"] == "confirm" and a["target"] is None)]
        booking_work = bool(acts or action)
        if booking_work:
            state["dialogue"]["booking_started"] = state["dialogue"].get("booking_started") or any(
                a["intent"] in {"provide_info", "change_info"} for a in acts
            )
            # Attach questions only as blockers for the existing consent guards.
            legacy_acts = acts + [
                {"intent": "ask_question", "target": None, "value": q.raw_text} for q in questions
            ]
            if not legacy_acts and action:
                try:
                    legacy_acts = self._action_acts(
                        state, action, event.get("reply_to_response_id")
                    )
                except (ValueError, TypeError):
                    state["control"]["last_event_id"] = event_id
                    return self._respond(
                        state,
                        "ask_clarification",
                        "Lựa chọn hoặc phản hồi này đã cũ. Hãy trả lời câu hỏi mới nhất nhé.",
                        reason="INVALID_INTERPRETATION_OR_ACTION",
                    )
            elif action:
                try:
                    legacy_acts.extend(
                        self._action_acts(state, action, event.get("reply_to_response_id"))
                    )
                except (ValueError, TypeError):
                    state["control"]["last_event_id"] = event_id
                    return self._respond(
                        state,
                        "ask_clarification",
                        "Lựa chọn hoặc phản hồi này đã cũ. Hãy trả lời câu hỏi mới nhất nhé.",
                        reason="INVALID_INTERPRETATION_OR_ACTION",
                    )
            legacy = NluResult.model_validate(
                {"speech_status": "clear", "dialogue_acts": legacy_acts}
            )
            booking_text = (
                " ; ".join(
                    dict.fromkeys(a.evidence_span.extract(text) for a in result.booking_acts)
                )
                if result and result.booking_acts
                else text
            )
            state = await super().process(
                state,
                booking_text,
                interpretation=legacy,
                action=action,
                event_id=event_id,
                delivered_response_ids=event.get("delivered_response_ids"),
                reply_to_response_id=event.get("reply_to_response_id"),
                allow_dispatch=False,
                occurred_at=interpretation["occurred_at"],
            )
            state["turn"]["occurred_at"] = interpretation["occurred_at"]
        else:
            state["control"]["last_event_id"] = event_id
            await self._recover_committed(state)
        if questions:
            state["confirmation"]["accepted_snapshot"] = None
            state["dialogue"]["suspended_booking_prompt"] = {
                "focus": state["conversation_context"].get("current_focus"),
                "revision": state["control"]["booking_revision"],
            }
            try:
                async with asyncio.timeout(service.read_deadline):
                    answer = await service.answer(state, questions)
            except TimeoutError:
                answer = "Một phần dữ liệu đang phản hồi chậm. Thông tin chuyến vẫn được giữ; bạn có thể hỏi lại phần chưa có."
            has_inquiry = any(q.type in MAP_QUESTIONS for q in questions)
            if has_inquiry:
                self._respond(state, "answer_question", answer)
                service.attach_presentation(state)
                item = service.active(state)
                if item and not state["dialogue"].get("pending_prompt"):
                    service._prompt(state, item, None, "inquiry_discussion")
            elif state["dialogue"].get("booking_started") and state["booking_status"] not in {
                "booked",
                "cancelled",
                "booking_unknown",
                "cancel_unknown",
            }:
                self._decide_response(state, prefix=answer + "\n")
            else:
                self._respond(state, "answer_question", answer)
        elif not booking_work:
            conversational = result.conversational_acts if result else []
            message = (
                "Cảm ơn bạn. Mình sẵn sàng giúp bạn xem tuyến hoặc đặt xe."
                if "thanks" in conversational
                else f"Xin chào! Mình là trợ lý đặt xe {self.brand_name}. Bạn muốn hỏi tuyến, giá, thời tiết hay đặt xe?"
                if "greeting" in conversational
                else "Bạn muốn hỏi tuyến đường, giá hay tiếp tục đặt xe? Bạn nói rõ giúp mình nhé."
            )
            if (
                state["dialogue"].get("booking_started")
                and "unclear" not in conversational
                and state["booking_status"]
                not in {"booked", "cancelled", "booking_unknown", "cancel_unknown"}
            ):
                self._decide_response(state, prefix=message + "\n")
            else:
                self._respond(
                    state,
                    "ask_clarification" if "unclear" in conversational else "answer_question",
                    message,
                )
        state["dialogue"]["pending_questions"] = (
            [q.model_dump() for q in questions] if state["dialogue"].get("pending_prompt") else []
        )
        return state

    async def finalize(self, state, event, ingress_guard=None):
        recovered_before = state["transaction"].get("booking_result")
        await self._recover_committed(state)
        if not recovered_before and state["transaction"].get("booking_result"):
            return self._respond(state, "inform_success", self._booking_text(state))
        if state["turn"].get("pending_cancel_requested"):
            return await self._cancel(state, event["event_id"])
        if state["turn"].get("ready_to_dispatch") and not self._pickup_time_valid(state):
            state["confirmation"]["accepted_snapshot"] = None
            self._validate_requirements(state)
            self._decide_response(state)
            state["last_response"]["reason"] = "PICKUP_TIME_ELAPSED"
            return state
        if state["turn"].get("ready_to_dispatch") and not self._quote_valid(state):
            state["confirmation"]["accepted_snapshot"] = None
            state["confirmation"]["pending_prompt"] = None
            await self._resolve(state, set())
            self._validate_requirements(state)
            self._decide_response(
                state,
                prefix="Giá đã hết hạn trước khi đặt. Bạn kiểm tra lại tóm tắt mới để xác nhận nhé.\n",
            )
            state["last_response"]["reason"] = "QUOTE_EXPIRED_BEFORE_DISPATCH"
            return state
        if state["turn"].get("ready_to_dispatch") and self._can_create(state):
            allowed = ingress_guard() if ingress_guard else True
            allowed = await allowed if inspect.isawaitable(allowed) else allowed
            if not allowed:
                state["confirmation"]["accepted_snapshot"] = None
                return self._respond(
                    state,
                    "inform_pending",
                    "Mình đã nhận thêm tin nhắn và sẽ xử lý thông tin mới trước khi đặt.",
                    reason="SUPERSEDED_BEFORE_DISPATCH",
                )
            return await self._create(state)
        return state

    async def process(
        self,
        state,
        text,
        *,
        action=None,
        delivered_response_ids=None,
        reply_to_response_id=None,
        event_id=None,
        ingress_guard=None,
        **kwargs,
    ):
        if event_id and state["control"].get("last_event_id") == event_id:
            return state
        event = {
            "text": text,
            "event_id": event_id,
            "action": action,
            "delivered_response_ids": delivered_response_ids or [],
            "reply_to_response_id": reply_to_response_id,
            "occurred_at": self.clock(),
        }
        interpretation = await self.interpret(state, event)
        prepared = await self.prepare(state, event, interpretation)
        return await self.finalize(prepared, event, ingress_guard)

    def _decide_response(self, state, prefix=""):
        questions = state["turn"].get("questions", [])
        state["turn"]["questions"] = []
        result = super()._decide_response(state)
        state["turn"]["questions"] = questions
        if prefix:
            state["last_response"]["text"] = prefix + state["last_response"]["text"]
        if state["last_response"]["action"] != "confirm_booking":
            recognized = []
            for field in state["turn"].get("changed_slots", []):
                place = state["resolution"]["locations"].get(field, {}).get("place")
                if field in {"pickup", "destination"} and place:
                    recognized.append(
                        f"Mình đã nhận {self._slot_label(field)} tại {place['label']}."
                    )
            if recognized:
                state["last_response"]["text"] = (
                    " ".join(recognized) + "\n" + state["last_response"]["text"]
                )
        if state.get("dialogue") and not state["last_response"].get("inquiry"):
            if state["last_response"]["action"] in LOCATION_PURPOSES:
                return result
            focus = state["last_response"].get("focus")
            state["dialogue"]["pending_prompt"] = {
                "purpose": state["last_response"]["action"],
                "scope_kind": "booking",
                "scope_id": state["control"]["draft_id"],
                "field": focus,
                "revision": state["control"]["booking_revision"],
            }
        return result

    def _respond(self, state, *args, **kwargs):
        result = super()._respond(state, *args, **kwargs)
        if state["control"].get("schema_version", 4) >= 5:
            result["last_response"]["presentation"]["contract_version"] = "chat-presentation-3" if self._location_v3(state) else "chat-presentation-2"
            result["last_response"]["inquiry"] = None
        return result

    def _validate_requirements(self, state):
        super()._validate_requirements(state)
        if "travel_party" not in state:
            return
        party = state["travel_party"]
        adults, children = party.get("adults"), party.get("children")
        passengers = state["booking_state"]["passengers"]["value"]
        if (
            adults is not None
            and children is not None
            and passengers is not None
            and adults + children != passengers
        ):
            state["issues"]["validation_party"] = {
                "target": "passengers",
                "text": "Tổng người lớn và trẻ em chưa khớp số khách. Bạn xác định lại thành phần hành khách nhé.",
            }
        vehicle = state["booking_state"]["vehicle_type"]["value"]
        if vehicle != "xe_may_dien" or vehicle not in self.vehicles:
            return
        row = self.vehicle_catalog[vehicle]
        if passengers == 2 and (adults is None or children is None):
            message = "Hai khách đi xe máy điện gồm bao nhiêu người lớn và trẻ em? Xe hỗ trợ tối đa một người lớn và một trẻ em theo quy định dịch vụ."
        elif adults is not None and adults > row.get("max_adults", 1):
            message = "Xe máy điện không chở hai người lớn. Bạn có thể chọn ô tô 4 chỗ hoặc 7 chỗ."
        elif children and (
            adults != 1
            or children > row.get("max_children", 1)
            or not row.get("allow_adult_with_child")
        ):
            message = "Thành phần hành khách chưa đáp ứng quy định xe máy điện. Bạn chọn ô tô hoặc xác định lại người đi nhé."
        elif passengers == 1 and children == 1:
            message = "Xe máy điện chưa hỗ trợ trẻ em đi một mình. Bạn chọn phương án có người lớn đi cùng nhé."
        else:
            message = None
        if message:
            state["issues"]["validation_motorbike_party"] = {
                "target": "passengers",
                "text": message,
            }

    def _quote_fingerprint(self, state):
        from app.domain.engine import fingerprint

        base = super()._quote_fingerprint(state)
        if state["control"].get("schema_version", 4) < 5:
            return base
        return fingerprint(
            [
                base,
                state.get("travel_party"),
                self.quote_adapter.version,
                self.quote_adapter.catalog["version"],
            ]
        )

    def _snapshot_payload(self, state):
        payload = super()._snapshot_payload(state)
        if state["control"].get("schema_version", 4) >= 5:
            payload["travel_party"] = deepcopy(state.get("travel_party"))
        return payload

    def _summary(self, state, prefix=""):
        result = super()._summary(state, prefix)
        if state["booking_state"]["vehicle_type"]["value"] == "xe_may_dien":
            result["last_response"]["summary"]["travel_party"] = deepcopy(state["travel_party"])
            party = state["travel_party"]
            result["last_response"]["text"] += (
                f"\nThành phần khách: {party['adults'] if party['adults'] is not None else 'chưa rõ'} người lớn, {party['children'] if party['children'] is not None else 'chưa rõ'} trẻ em."
            )
        return result


# Old policy is retained only for checkpoint compatibility/regression fixtures.
# All new callers use the architecture_fixed.md implementation.
from app.domain.booking_engine import ConversationEngine  # noqa: E402,F401
