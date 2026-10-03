"""Location consent is distinct from booking consent and from assistance fees."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from app.domain.engine import fingerprint, normalized
from app.domain.location_policy import representative_point

LOCATION_PURPOSES = {
    "confirm_location",
    "ask_area_detail",
    "choose_area_service",
    "confirm_assistance",
}
IN_FLIGHT = {
    "booking_in_progress",
    "booking_unknown",
    "cancel_pending",
    "cancel_unknown",
    "amendment_pending",
    "amendment_unknown",
    "booked",
    "cancelled",
}


def fee_reply_matches(text: str, amount: int | None) -> bool:
    """Do not turn a smaller price/cap into consent for the displayed fee."""
    key = normalized(text)
    if re.search(r"\b(toi da|duoi|khong qua|chi tra|mien phi|giam|it hon|voi dieu kien)\b", key):
        return False
    matches = list(re.finditer(r"(\d+(?:[.,]\d+)*)(?:\s*(nghin|ngan|k|trieu|dong|vnd|d)\b)?", key))
    currency_text = re.sub(r"\bdong y\b", "", key)
    if not matches and re.search(r"\b(nghin|ngan|trieu|dong|vnd)\b", currency_text):
        return False
    for match in matches:
        raw, unit = match.groups()
        raw = (
            re.sub(r"[.,]", "", raw)
            if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw)
            else raw.replace(",", ".")
        )
        try:
            value = float(raw) * (
                1000 if unit in {"nghin", "ngan", "k"} else 1000000 if unit == "trieu" else 1
            )
        except ValueError:
            return False
        if value != amount:
            return False
    return True


def upgrade_state(state: dict, enabled: bool) -> dict:
    if (
        enabled
        and state["control"].get("schema_version", 4) == 5
        and state["booking_status"] not in IN_FLIGHT
        and not state["transaction"].get("booking_result")
        and not state["transaction"].get("active_operation")
    ):
        state["control"].update(
            schema_version=6,
            graph_version="chat-graph-3",
            policy_version="mvp-chat-policy-3",
            turn_version="parrotgo-turn-3",
        )
        state["confirmation"].update(pending_prompt=None, accepted_snapshot=None)
        state["dialogue"]["pending_prompt"] = None
        state["resolution"]["quote"] = None
        # Existing map evidence is retained; a new rendered proposal is required.
        for target in ("pickup", "destination"):
            state["booking_state"][target]["confirmed"] = False
        state["last_response"]["presentation"]["contract_version"] = "chat-presentation-3"
    if state["control"].get("schema_version", 4) >= 6:
        state.setdefault("location_workflow", {"proposals": {}, "areas": {}, "assistance": None})
    return state


class LocationWorkflowMixin:
    def _location_v3(self, state):
        return state["control"].get("schema_version", 4) >= 6

    def _location_binding(self, state, target):
        return fingerprint(
            {
                "draft": state["control"]["draft_id"],
                "revision": state["control"]["booking_revision"],
                "slot_revision": state["control"]["slot_revisions"][target],
                "place": state["resolution"]["locations"].get(target),
                "vehicle": state["booking_state"]["vehicle_type"]["value"],
                "assistance_policy": self.assistance_policy.model_dump(),
            }
        )

    def _area_anchor(self, state, area):
        vehicle = state["booking_state"]["vehicle_type"]["value"] or "oto_4_cho"
        profile = self.vehicle_catalog.get(vehicle, {}).get("route_profile", "car")
        return representative_point(area["fact"], vehicle_profile=profile)

    def _location_prompt(self, state, purpose, target, text, *, place=None, fee=None):
        workflow = state["location_workflow"]
        proposal = {
            "proposal_id": uuid4().hex,
            "purpose": purpose,
            "target": target,
            "binding": self._location_binding(state, target),
            "expires_at": self.clock() + 300,
            "booking_revision": state["control"]["booking_revision"],
            "place": deepcopy(place),
        }
        self._respond(state, purpose, text, focus=target)
        proposal["response_id"] = state["last_response"]["response_id"]
        workflow["proposals"][target] = proposal
        state["dialogue"]["pending_prompt"] = {
            "purpose": purpose,
            "scope_kind": "booking",
            "scope_id": state["control"]["draft_id"],
            "field": target,
            "revision": state["control"]["booking_revision"],
            "proposal_id": proposal["proposal_id"],
            "proposed_label": (place or {}).get("label"),
            "fee_amount": fee,
            "policy_version": self.assistance_policy.version if fee is not None else None,
        }
        state["last_response"]["presentation"].update(
            prompt_id=proposal["proposal_id"],
            booking_revision=proposal["booking_revision"],
            valid_until=proposal["expires_at"],
        )
        return state

    def _proposal_valid(self, state, proposal, event):
        response = state.get("last_response", {})
        return bool(
            proposal
            and response.get("delivery_status") == "rendered"
            and response.get("response_id") == proposal["response_id"]
            and response.get("action") == proposal["purpose"]
            and (
                not event.get("reply_to_response_id")
                or event["reply_to_response_id"] == proposal["response_id"]
            )
            and proposal["expires_at"] > self.clock()
            and proposal["binding"] == self._location_binding(state, proposal["target"])
        )

    def _assistance_valid(self, state):
        assistance = state.get("location_workflow", {}).get("assistance")
        if not assistance:
            return True
        area = state["location_workflow"]["areas"].get("destination", {})
        vehicle = state["booking_state"]["vehicle_type"]["value"]
        rule = self.assistance_policy.rule(assistance["area_id"], vehicle)
        return bool(
            self.area_assistance_enabled
            and rule
            and assistance.get("consent")
            and assistance["policy_version"] == self.assistance_policy.version
            and assistance["vehicle"] == vehicle
            and assistance["fee_amount"] == rule.fee_amount
            and assistance["max_extra_distance_m"] == rule.max_extra_distance_m
            and assistance["max_wait_minutes"] == rule.max_wait_minutes
            and assistance["consent_expires_at"] > self.clock()
            and assistance["area_id"] == area.get("fact", {}).get("area_id")
            and assistance["anchor_id"]
            == state["resolution"]["locations"].get("destination", {}).get("place", {}).get("id")
        )

    def _replace_slot(self, state, target, value, changed):
        previous = state["booking_state"][target]["value"]
        super()._replace_slot(state, target, value, changed)
        if self._location_v3(state) and previous != value:
            workflow = state["location_workflow"]
            workflow["proposals"].clear()
            if target in {"pickup", "destination"}:
                workflow["areas"].pop(target, None)
            if target == "destination":
                workflow["assistance"] = None
            if target == "pickup_note":
                state["booking_state"]["pickup"]["confirmed"] = False
            if target in {
                "pickup",
                "vehicle_type",
                "passengers",
                "luggage",
                "pickup_time",
                "payment_method",
            }:
                for area in workflow["areas"].values():
                    area.pop("provisional_base_amount", None)

    async def _resolve(self, state, changed):
        # Refresh stale route facts before they enter a fresh booking quote.
        route = state["resolution"].get("route")
        if self._location_v3(state) and route and route.get("valid_until"):
            expiry = route["valid_until"]
            expiry = (
                datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
                if isinstance(expiry, str)
                else expiry
            )
            if expiry <= self.clock():
                state["resolution"].update(route=None, quote=None)
                state["confirmation"]["accepted_snapshot"] = None
        await super()._resolve(state, changed)
        if not self._location_v3(state):
            return
        workflow = state["location_workflow"]
        for target, location in state["resolution"]["locations"].items():
            fact = location.get("area")
            if fact and target not in workflow["areas"]:
                workflow["areas"][target] = {"phase": "ask_detail", "fact": deepcopy(fact)}
        quote = state["resolution"].get("quote")
        route = state["resolution"].get("route")
        if quote and route and route.get("valid_until"):
            expiry = route["valid_until"]
            expiry = (
                datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
                if isinstance(expiry, str)
                else expiry
            )
            quote["expires_at"] = min(quote["expires_at"], expiry)
        assistance = workflow.get("assistance")
        if quote and assistance and self._assistance_valid(state):
            quote.setdefault("base_amount", quote["amount"])
            quote["assistance_fee"] = assistance["fee_amount"]
            quote["amount"] = quote["base_amount"] + quote["assistance_fee"]
            quote["kind"] = "provisional_area_estimate"
            quote["expires_at"] = min(quote["expires_at"], assistance["consent_expires_at"])

    def _decide_response(self, state):
        if (
            self._location_v3(state)
            and state["booking_status"] not in IN_FLIGHT
            and state["location_workflow"].get("assistance")
            and not self._assistance_valid(state)
        ):
            state["confirmation"]["accepted_snapshot"] = None
            area = state["location_workflow"]["areas"].get("destination")
            if area:
                rule = self.assistance_policy.rule(
                    area["fact"]["area_id"], state["booking_state"]["vehicle_type"]["value"]
                )
                anchor = self._area_anchor(state, area)
                if rule and self.area_assistance_enabled and anchor:
                    return self._fee_prompt(state, area, anchor, rule)
                state["location_workflow"]["assistance"] = None
                area["phase"] = "fixed"
                state["booking_state"]["destination"]["confirmed"] = False
                state["resolution"]["quote"] = None
                state["control"]["booking_revision"] += 1
                if anchor:
                    return self._location_prompt(
                        state,
                        "confirm_location",
                        "destination",
                        f"Dịch vụ hỗ trợ không còn áp dụng. Bạn có muốn đến điểm cố định {anchor['label']} không? Trả lời ‘đúng’ hoặc nêu địa chỉ khác.",
                        place=anchor,
                    )
                state["resolution"]["locations"].pop("destination", None)
                return self._respond(
                    state,
                    "ask_clarification",
                    "Dịch vụ hỗ trợ không áp dụng cho loại xe hoặc chính sách hiện tại. Bạn chọn điểm trả cố định hoặc địa chỉ cụ thể giúp mình nhé.",
                    focus="destination",
                )
        if (
            not self._location_v3(state)
            or state["issues"]
            or state["turn"].get("issues")
            or state["booking_status"] in IN_FLIGHT
        ):
            return super()._decide_response(state)
        workflow = state["location_workflow"]
        for target in ("destination", "pickup"):
            location = state["resolution"]["locations"].get(target, {})
            area = workflow["areas"].get(target)
            if area and location.get("status") != "valid":
                fact = area["fact"]
                if target == "pickup":
                    return self._respond(
                        state,
                        "ask_clarification",
                        f"{fact['label']} là khu vực rộng. Để đón bạn, mình cần cổng, tòa nhà hoặc địa chỉ cụ thể nơi bạn đang đứng.",
                        focus=target,
                    )
                anchor = self._area_anchor(state, area)
                if area["phase"] == "ask_detail":
                    return self._location_prompt(
                        state,
                        "ask_area_detail",
                        target,
                        f"{fact['label']} là khu vực rộng. Bạn có biết địa chỉ, tòa nhà hoặc cổng chính xác bên trong không? Nếu không biết, hãy trả lời ‘không biết địa chỉ’. Mình chưa chốt nơi trả khách.",
                    )
                if not anchor:
                    return self._respond(
                        state,
                        "ask_clarification",
                        f"Mình chưa có điểm đại diện có nguồn cho {fact['label']}. Bạn cung cấp cổng hoặc địa chỉ cụ thể giúp mình nhé.",
                        focus=target,
                    )
                rule = self.assistance_policy.rule(
                    fact["area_id"], state["booking_state"]["vehicle_type"]["value"]
                )
                if (
                    self.area_assistance_enabled
                    and not state["booking_state"]["vehicle_type"]["value"]
                ):
                    return self._respond(
                        state,
                        "ask_slot",
                        "Để kiểm tra gói hỗ trợ trong khu vực, bạn muốn đi xe 4 chỗ hay 7 chỗ?",
                        focus="vehicle_type",
                    )
                if area["phase"] == "fee" and rule and self.area_assistance_enabled:
                    return self._fee_prompt(state, area, anchor, rule)
                if area["phase"] == "fixed" or not (self.area_assistance_enabled and rule):
                    return self._location_prompt(
                        state,
                        "confirm_location",
                        target,
                        f"Hiện chưa có dịch vụ hỗ trợ tìm địa chỉ trong khu vực này cho loại xe đang chọn. Bạn có muốn đến điểm cố định {anchor['label']} không? Trả lời ‘đúng’ hoặc nêu địa chỉ khác.",
                        place=anchor,
                    )
                return self._location_prompt(
                    state,
                    "choose_area_service",
                    target,
                    f"Bạn muốn hỗ trợ tìm điểm đến cụ thể bên trong {fact['label']}, hay đến điểm cố định {anchor['label']}? Hãy trả lời ‘hỗ trợ’ hoặc ‘điểm cố định’. Phụ phí hỗ trợ thử nghiệm là {rule.fee_amount:,} đ; mình sẽ hỏi xác nhận phí riêng trước khi thêm vào tổng tiền.",
                    place=anchor,
                    fee=rule.fee_amount,
                )
            if (
                location.get("status") == "valid"
                and not state["booking_state"][target]["confirmed"]
            ):
                place = location["place"]
                return self._location_prompt(
                    state,
                    "confirm_location",
                    target,
                    f"Mình tìm được {place['label']}. Bạn có muốn {'được đón' if target == 'pickup' else 'đi đến'} tại địa điểm này không? Trả lời ‘đúng’ để xác nhận địa điểm, hoặc nêu địa chỉ khác.",
                    place=place,
                )
        return super()._decide_response(state)

    def _fee_prompt(self, state, area, anchor, rule):
        provisional = area.get("provisional_base_amount")
        estimate = (
            f" Giá tuyến tạm tính đến điểm đại diện: {provisional:,} đ, chưa cộng phụ phí."
            if provisional is not None
            else " Giá tuyến sẽ được tính tạm thời đến điểm đại diện khi có đủ nơi đón và loại xe."
        )
        return self._location_prompt(
            state,
            "confirm_assistance",
            "destination",
            f"Hỗ trợ thử nghiệm bên trong {area['fact']['label']}: phụ phí {rule.fee_amount:,} đ, tối đa {rule.max_extra_distance_m / 1000:g} km bổ sung và {rule.max_wait_minutes} phút hỗ trợ.{estimate} {anchor['label']} chỉ là điểm tính quãng đường tạm thời, chưa phải nơi trả khách chính xác. Bạn đồng ý mức phí và giới hạn này không? Trả lời ‘đồng ý phí’ hoặc ‘không hỗ trợ’.",
            place=anchor,
            fee=rule.fee_amount,
        )

    async def _apply_location_decision(self, state, decision, event):
        prompt = state["dialogue"].get("pending_prompt") or {}
        target = prompt.get("field")
        proposal = state["location_workflow"]["proposals"].get(target)
        state["control"]["last_event_id"] = event["event_id"]
        state["turn"] = {"issues": [], "questions": [], "changed_slots": []}
        if not self._proposal_valid(state, proposal, event):
            self._decide_response(state)
            state["last_response"]["reason"] = "STALE_LOCATION_PROPOSAL"
            return state
        purpose = proposal["purpose"]
        state["confirmation"].update(pending_prompt=None, accepted_snapshot=None)
        area = state["location_workflow"]["areas"].get(target)
        place = proposal.get("place")
        if purpose == "ask_area_detail" and decision == "unknown_detail":
            area["phase"] = "choose_service"
        elif purpose == "ask_area_detail" and decision == "confirm":
            state["location_workflow"]["proposals"].pop(target, None)
            self._respond(
                state,
                "ask_slot",
                f"Bạn gửi địa chỉ, tòa nhà hoặc cổng cụ thể bên trong {area['fact']['label']} giúp mình nhé.",
                focus=target,
            )
            state["dialogue"]["pending_prompt"] = {
                "purpose": "ask_slot",
                "scope_kind": "booking",
                "scope_id": state["control"]["draft_id"],
                "field": target,
                "revision": state["control"]["booking_revision"],
            }
            return state
        elif purpose == "choose_area_service" and decision in {"request_assistance", "confirm"}:
            area["phase"] = "fee"
            # This is a preview only; no assistance fee is added before consent.
            pickup = state["resolution"]["locations"].get("pickup", {}).get("place")
            vehicle = state["booking_state"]["vehicle_type"]["value"]
            if pickup and vehicle in self.vehicles:
                try:
                    route = await self.maps.route(pickup, place, vehicle_type=vehicle)
                    quote = await self.quote_adapter.quote(route, vehicle)
                    area["provisional_base_amount"] = quote["amount"]
                except Exception:
                    area.pop("provisional_base_amount", None)
        elif purpose in {"choose_area_service", "confirm_assistance"} and decision in {
            "decline_assistance",
            "reject",
        }:
            area["phase"] = "fixed"
            state["location_workflow"]["assistance"] = None
            return self._location_prompt(
                state,
                "confirm_location",
                target,
                f"Bạn có muốn đến điểm cố định {place['label']} không? Trả lời ‘đúng’ hoặc nêu địa chỉ khác.",
                place=place,
            )
        elif purpose == "confirm_assistance" and decision in {"accept_fee", "confirm"}:
            rule = self.assistance_policy.rule(
                area["fact"]["area_id"], state["booking_state"]["vehicle_type"]["value"]
            )
            if not rule or not self.area_assistance_enabled:
                self._decide_response(state)
                return state
            state["location_workflow"]["assistance"] = {
                "area_id": area["fact"]["area_id"],
                "anchor_id": place["id"],
                "vehicle": state["booking_state"]["vehicle_type"]["value"],
                "policy_version": self.assistance_policy.version,
                "fee_amount": rule.fee_amount,
                "max_extra_distance_m": rule.max_extra_distance_m,
                "max_wait_minutes": rule.max_wait_minutes,
                "consent": {
                    "response_id": proposal["response_id"],
                    "proposal_id": proposal["proposal_id"],
                    "event_id": event["event_id"],
                },
                "consent_expires_at": self.clock() + self.assistance_policy.consent_ttl_seconds,
            }
            self._accept_location(state, target, place, proposal, area_mode="assistance")
        elif purpose == "confirm_location" and decision == "confirm":
            self._accept_location(
                state, target, place, proposal, area_mode="fixed" if area else None
            )
        elif purpose == "confirm_location" and decision == "reject":
            state["location_workflow"]["proposals"].pop(target, None)
            state["resolution"]["locations"].pop(target, None)
            state["resolution"].update(route=None, quote=None)
            state["confirmation"]["accepted_snapshot"] = None
            state["issues"][f"uncertain_{target}"] = {
                "target": target,
                "text": "Bạn cung cấp địa chỉ hoặc địa điểm khác giúp mình nhé.",
            }
            self._decide_response(state)
            return state
        else:
            self._decide_response(state)
            return state
        state["confirmation"].update(pending_prompt=None, accepted_snapshot=None)
        state["resolution"]["quote"] = None
        await self._resolve(state, set())
        self._validate_requirements(state)
        self._decide_response(state)
        return state

    def _accept_location(self, state, target, place, proposal, area_mode=None):
        state["booking_state"][target]["confirmed"] = True
        state["resolution"]["locations"][target] = {
            "status": "valid",
            "place": deepcopy(place),
            "source_slot_revision": state["control"]["slot_revisions"][target],
        }
        state["confirmation"]["slot_evidence"][target] = {
            "proposal_id": proposal["proposal_id"],
            "response_id": proposal["response_id"],
            "revision": state["control"]["slot_revisions"][target],
        }
        if area_mode:
            state["location_workflow"]["areas"][target]["phase"] = area_mode
        state["control"]["booking_revision"] += 1
        state["booking_status"] = "collecting_info"
        state["location_workflow"]["proposals"].clear()
        state["resolution"].update(route=None, quote=None)

    def _quote_fingerprint(self, state):
        base = super()._quote_fingerprint(state)
        if not self._location_v3(state):
            return base
        return fingerprint(
            [base, state["location_workflow"].get("assistance"), self.assistance_policy.version]
        )

    def _snapshot_payload(self, state):
        payload = super()._snapshot_payload(state)
        if self._location_v3(state):
            payload["area_service"] = deepcopy(state["location_workflow"].get("assistance"))
            payload["area_destinations"] = deepcopy(state["location_workflow"]["areas"])
        return payload

    def _can_create(self, state):
        return bool(
            super()._can_create(state)
            and (not self._location_v3(state) or self._assistance_valid(state))
        )

    def _quote_valid(self, state):
        if not super()._quote_valid(state):
            return False
        if not self._location_v3(state):
            return True
        route = state["resolution"].get("route") or {}
        expiry = route.get("valid_until")
        if expiry:
            expiry = (
                datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
                if isinstance(expiry, str)
                else expiry
            )
            if expiry <= self.clock():
                return False
        return self._assistance_valid(state)

    def _summary(self, state, prefix=""):
        result = super()._summary(state, prefix)
        if self._location_v3(state):
            assistance = state["location_workflow"].get("assistance")
            if assistance:
                quote = state["resolution"]["quote"]
                text = result["last_response"]["text"]
                text += f"\nGiá tuyến tạm tính {quote['base_amount']:,} đ + hỗ trợ {assistance['fee_amount']:,} đ = {quote['amount']:,} đ. Điểm đại diện chỉ dùng tính quãng đường; nơi trả chính xác bên trong khu vực chưa được chốt. Vượt giới hạn hỗ trợ cần thỏa thuận lại trước khi phát sinh phí."
                result["last_response"]["text"] = text
                result["last_response"]["summary"].update(
                    base_fare=quote["base_amount"],
                    assistance_fee=assistance["fee_amount"],
                    provisional=True,
                )
        return result
