"""Scoped, read-only route inquiries and answers rendered from provider facts."""

from __future__ import annotations

import asyncio
import inspect
import math
import re
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.contracts.maps import MapResolution, RouteResult
from app.contracts.turn import QuestionIntent
from app.contracts.weather import WeatherFact, WeatherRequest
from app.domain.engine import candidate_ref, fingerprint, normalized

ZONE = ZoneInfo("Asia/Ho_Chi_Minh")
ROUTE_QUESTIONS = {"fare_estimate", "route_distance", "travel_duration", "travel_duration_explanation", "weather_forecast"}


def fact_expiry(value):
    if isinstance(value, datetime):
        return value.timestamp() if value.tzinfo else 0
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo else 0
        except ValueError:
            return 0
    return value if isinstance(value, (int, float)) else 0


def duration_explanation(route, origin, destination, vehicle_label, now):
    """No model-generated claim about traffic or the cause of an ETA."""
    distance = route["distance_m"]
    length = (f"{round(distance)} m" if distance < 1000 else
              f"{distance / 1000:.2f}".rstrip("0").rstrip(".") + " km")
    fixture = route["source"].startswith("fixture:")
    source_name = "dữ liệu tuyến thử nghiệm" if fixture else "VietMap" if route["source"].startswith("vietmap:") else "nhà cung cấp bản đồ"
    text = (f"Từ {origin} đến {destination} bằng {vehicle_label.lower()}, tuyến dài khoảng {length}. "
            f"Khoảng {max(1, math.ceil(route['duration_s'] / 60))} phút là thời gian {source_name} ước tính cho tuyến này. ")
    traffic = route.get("traffic") or {}
    fetched_at = fact_expiry(traffic.get("fetched_at"))
    fresh = bool(fetched_at and fetched_at <= now + 5 and fact_expiry(traffic.get("valid_until")) > now)
    if traffic.get("status") == "available" and fresh:
        levels = set(traffic.get("congestion") or [])
        descriptions = [("low", "ít ùn tắc"), ("moderate", "ùn tắc vừa"),
                        ("heavy", "ùn tắc nhiều"), ("severe", "ùn tắc nghiêm trọng")]
        found = [label for level, label in descriptions if level in levels]
        if found:
            text += ("Dữ liệu giao thông mẫu có đoạn " if fixture else "Dữ liệu giao thông nhận từ VietMap có đoạn ") + ", ".join(found) + ". "
            if "unknown" in levels:
                text += "Một số đoạn chưa có thông tin giao thông. "
        if route.get("eta_basis") == "traffic_adjusted" and not fixture:
            text += "Nhà cung cấp xác nhận ước tính thời gian này có tính dữ liệu giao thông. "
        else:
            text += "Dữ liệu ùn tắc chưa đủ để khẳng định ước tính thời gian đã tính giao thông hiện tại. "
    elif traffic.get("status") == "stale" or traffic.get("status") == "available" and not fresh:
        text += "Thông tin giao thông đã cũ, nên mình chưa có căn cứ về giao thông hiện tại. "
    else:
        text += "Mình chưa có dữ liệu giao thông hiện tại đủ để giải thích nguyên nhân nhanh hay lâu. "
    text += f"Thời gian thực tế có thể thay đổi theo giao thông và điều kiện di chuyển. Nguồn: {route['source']}."
    return text


def arrival_scenario(question):
    return question.weather_target == "both" or (
        question.weather_target == "destination"
        and (
            not question.departure_time_ref
            or re.search(r"toi noi|xuong xe|khi den noi|luc den noi", normalized(question.raw_text))
        )
    )


WEATHER_LABELS = {
    0: "trời quang",
    1: "ít mây",
    2: "có mây",
    3: "nhiều mây",
    45: "sương mù",
    48: "sương mù",
    51: "mưa phùn nhẹ",
    53: "mưa phùn",
    55: "mưa phùn mạnh",
    61: "mưa nhẹ",
    63: "mưa",
    65: "mưa to",
    80: "mưa rào nhẹ",
    81: "mưa rào",
    82: "mưa rào mạnh",
    95: "dông",
    96: "dông có mưa đá",
    99: "dông có mưa đá",
}


def parse_forecast_time(raw: str | None, now: float) -> tuple[datetime | None, str]:
    """Interpret only unambiguous local times; pin relative dates to the event."""
    if not raw:
        return None, "Bạn muốn xem dự báo vào ngày và giờ nào?"
    text = normalized(raw).strip()
    local = datetime.fromtimestamp(now, ZONE)
    if text in {"bay gio", "hien tai", "ngay", "ngay bay gio", "di ngay", "asap", "now"}:
        return local, "nếu xuất phát khoảng hiện tại, chưa có giờ đón thực tế"
    try:
        iso = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if iso.tzinfo:
            return iso.astimezone(ZONE), "thời điểm bạn yêu cầu"
    except ValueError:
        pass
    day = local.date() + timedelta(days=1 if re.search(r"\bmai\b", text) else 0)
    date = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", text)
    if date:
        try:
            day = day.replace(year=int(date[3] or local.year), month=int(date[2]), day=int(date[1]))
        except ValueError:
            return None, "Ngày bạn nói chưa hợp lệ. Bạn cho mình ngày và giờ cụ thể nhé."
    clock = re.search(r"\b(\d{1,2})(?::(\d{2})|\s*gio(?:\s*(\d{1,2})(?:\s*phut)?)?)", text)
    if not clock:
        return None, "Bạn muốn xem dự báo lúc mấy giờ vào ngày đó?"
    hour, minute = int(clock[1]), int(clock[2] or clock[3] or 0)
    if hour > 23 or minute > 59:
        return None, "Giờ bạn nói chưa hợp lệ. Bạn cho mình giờ theo định dạng HH:MM nhé."
    if hour < 12 and not clock[2] and not re.search(r"\b(sang|chieu|toi|dem|trua)\b", text):
        return None, f"Bạn muốn xem dự báo lúc {hour} giờ sáng hay tối?"
    if hour < 12 and re.search(r"\b(chieu|toi|trua)\b", text):
        hour += 12
    return datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=ZONE
    ), "thời điểm bạn yêu cầu"


class InquiryService:
    def __init__(self, engine, *, weather=None, ttl=600, read_deadline=15):
        self.engine = engine
        self.weather = weather
        self.ttl = ttl
        self.read_deadline = read_deadline

    def active(self, state):
        return state["inquiries"].get(state.get("active_inquiry_id"))

    def _new(self, state, origin=None, destination=None, vehicle=None, departure_time=None):
        iid = (
            "inq_"
            + fingerprint(
                [
                    state["control"]["session_id"],
                    state["control"]["last_event_id"],
                    origin,
                    destination,
                    len(state["inquiries"]),
                ]
            )[:24]
        )
        item = {
            "inquiry_id": iid,
            "revision": 0,
            "origin": origin,
            "destination": destination,
            "vehicle": vehicle,
            "departure_time": departure_time,
            "locations": {},
            "candidate_sets": {},
            "routes": {},
            "quotes": {},
            "status": "new",
            "expires_at": self.engine.clock() + self.ttl,
            "created_at": self.engine.clock(),
            "time_reference_at": state["turn"].get("occurred_at", self.engine.clock()),
            "questions": [],
            "route_fingerprint": None,
        }
        state["inquiries"][iid] = item
        while len(state["inquiries"]) > 5:
            del state["inquiries"][next(iter(state["inquiries"]))]
        state["active_inquiry_id"] = iid
        return item

    def update(self, item, field, value):
        if field not in {"origin", "destination", "vehicle", "departure_time"} or not value:
            raise ValueError("INVALID_INQUIRY_UPDATE")
        if item[field] == value:
            return
        if (
            field == "departure_time"
            and item[field]
            and normalized(value) in {"sang", "chieu", "toi", "trua"}
        ):
            value = item[field] + " " + value
        item[field] = value
        item["revision"] += 1
        item["routes"], item["quotes"] = {}, {}
        item["route_fingerprint"] = None
        item["expires_at"] = self.engine.clock() + self.ttl
        item["candidate_sets"] = {}
        if field in {"origin", "destination"}:
            item["locations"].pop(field, None)

    def validate_action(self, state, action, reply_to=None):
        item = state["inquiries"].get(action.get("inquiry_id") or state.get("active_inquiry_id"))
        if not item or item["expires_at"] <= self.engine.clock():
            raise ValueError("INQUIRY_EXPIRED")
        if action.get("inquiry_revision", item["revision"]) != item["revision"]:
            raise ValueError("STALE_INQUIRY")
        response = state.get("last_response") or {}
        if reply_to is not None and response.get("response_id") != reply_to:
            raise ValueError("STALE_INQUIRY_PRESENTATION")
        return item

    def select(self, state, item, candidate_id, set_id=None):
        response = state.get("last_response") or {}
        if response.get("delivery_status") != "rendered":
            raise ValueError("UNSHOWN_INQUIRY_CANDIDATE")
        for field, batch in item["candidate_sets"].items():
            if set_id and batch["candidate_set_id"] != set_id:
                continue
            if (
                batch["expires_at"] <= self.engine.clock()
                or batch["revision"] != item["revision"]
                or batch.get("presented_response_id") != response.get("response_id")
            ):
                continue
            place = next(
                (
                    p
                    for p in batch["places"]
                    if candidate_ref(batch["candidate_set_id"], p["id"]) == candidate_id
                ),
                None,
            )
            if place:
                self.update(item, field, place["label"])
                item["locations"][field] = {"status": "valid", "place": deepcopy(place), "confirmed": True}
                return
        raise ValueError("STALE_INQUIRY_CANDIDATE")

    def promote(self, state, item, action=None):
        if state["booking_status"] in {
            "booked",
            "cancelled",
            "booking_unknown",
            "cancel_unknown",
            "cancel_failed",
        }:
            raise ValueError("NEW_SESSION_REQUIRED")
        if action:
            if (
                action.get("booking_revision", state["control"]["booking_revision"])
                != state["control"]["booking_revision"]
            ):
                raise ValueError("STALE_BOOKING_REVISION")
            if (
                action.get("route_fingerprint", item["route_fingerprint"])
                != item["route_fingerprint"]
            ):
                raise ValueError("STALE_ROUTE")
        if item["expires_at"] <= self.engine.clock() or not all(
            item["locations"].get(f, {}).get("status") == "valid" for f in ("origin", "destination")
        ):
            raise ValueError("INQUIRY_NEEDS_RESOLUTION")
        if any(location.get("area_preview") for location in item["locations"].values()):
            raise ValueError("AREA_PREVIEW_NEEDS_EXACT_ENDPOINT")
        acts = [
            {
                "intent": "change_info",
                "target": target,
                "value": item["locations"][field]["place"]["label"],
            }
            for field, target in (("origin", "pickup"), ("destination", "destination"))
        ]
        if item["vehicle"]:
            acts.append(
                {"intent": "change_info", "target": "vehicle_type", "value": item["vehicle"]}
            )
        item["status"] = "promoted"
        state["dialogue"]["pending_prompt"] = None
        state["active_inquiry_id"] = None
        state["confirmation"]["accepted_snapshot"] = None
        state["confirmation"]["pending_prompt"] = None
        return acts

    def _scope(self, state, question):
        active = self.active(state)
        if question.route_scope == "current_booking":
            committed = state["transaction"].get("committed_snapshot")
            slots = (
                committed["slots"]
                if committed
                else {k: v["value"] for k, v in state["booking_state"].items()}
            )
            locations = (
                {
                    field: {"status": "valid", "place": committed[target]}
                    for field, target in (("origin", "pickup"), ("destination", "destination"))
                    if committed and committed.get(target)
                }
                if committed
                else deepcopy(
                    {
                        "origin": state["resolution"]["locations"].get("pickup", {}),
                        "destination": state["resolution"]["locations"].get("destination", {}),
                    }
                )
            )
            return {
                "inquiry_id": state["control"]["draft_id"],
                "revision": state["control"]["booking_revision"],
                "origin": slots.get("pickup"),
                "destination": slots.get("destination"),
                "vehicle": question.vehicle_ref or slots.get("vehicle_type"),
                "departure_time": question.departure_time_ref or slots.get("pickup_time"),
                "locations": locations,
                "candidate_sets": {},
                "routes": {},
                "quotes": {},
                "scope_kind": "booking",
                "status": "new",
                "expires_at": self.engine.clock() + self.ttl,
                "committed_route": committed.get("route")
                if committed
                else state["resolution"].get("route"),
                "committed_quote": committed.get("quote")
                if committed
                else state["resolution"].get("quote"),
                "quote_committed": bool(committed),
            }
        if question.route_scope == "explicit_pair":
            item = next(
                (
                    i
                    for i in reversed(list(state["inquiries"].values()))
                    if i["origin"] == question.origin
                    and i["destination"] == question.destination
                    and i["expires_at"] > self.engine.clock()
                ),
                None,
            )
            if item is None:
                item = self._new(
                    state,
                    question.origin,
                    question.destination,
                    question.vehicle_ref,
                    question.departure_time_ref,
                )
            else:
                for field, value in (
                    ("vehicle", question.vehicle_ref),
                    ("departure_time", question.departure_time_ref),
                ):
                    if value:
                        self.update(item, field, value)
            state["active_inquiry_id"] = item["inquiry_id"]
            return item
        if active and active["expires_at"] > self.engine.clock():
            if question.vehicle_ref:
                self.update(active, "vehicle", question.vehicle_ref)
            if question.departure_time_ref:
                self.update(active, "departure_time", question.departure_time_ref)
            return active
        if (
            question.route_scope == "unresolved"
            and not state["inquiries"]
            and all(state["booking_state"][s]["value"] for s in ("pickup", "destination"))
        ):
            return self._scope(
                state, question.model_copy(update={"route_scope": "current_booking"})
            )
        return self._new(state)

    def _explanation_scope(self, state, question):
        if question.route_scope in {"explicit_pair", "current_booking"}:
            return self._scope(state, question)
        explicit_travel = bool(re.search(r"\b(di|di chuyen|toi noi|tuyen|hanh trinh)\b", normalized(question.raw_text)))
        if not explicit_travel:
            last = state.get("last_response") or {}
            reason = str(last.get("reason") or "")
            if state["dialogue"].get("last_discussed_topic") in {"pickup_availability", "provider_wait"} or any(
                marker in reason for marker in ("RATE_LIMIT", "QUOTA", "PROVIDER_WAIT", "EXTRACTION_DEFERRED")
            ):
                return None
        ref = state["dialogue"].get("last_discussed_route_ref")
        context = state["dialogue"].get("last_discussed_route") or {}
        if ref == state["control"]["draft_id"]:
            item = self._scope(state, question.model_copy(update={"route_scope": "current_booking"}))
        else:
            item = state["inquiries"].get(ref)
        if not item or item.get("expires_at", 0) <= self.engine.clock():
            return None
        if context and (context.get("scope_id") != item["inquiry_id"] or
                        context.get("revision") != item["revision"] or
                        context.get("vehicle") != (item.get("vehicle") or "oto_4_cho")):
            return None
        if question.vehicle_ref and question.vehicle_ref != (item.get("vehicle") or "oto_4_cho"):
            return None
        return item

    def _remember_route(self, state, item, vehicle, topic):
        state["dialogue"]["last_discussed_route_ref"] = item["inquiry_id"]
        state["dialogue"]["last_discussed_route"] = {
            "scope_kind": item.get("scope_kind", "inquiry"), "scope_id": item["inquiry_id"],
            "revision": item["revision"], "vehicle": vehicle,
            "route_fingerprint": item.get("route_fingerprint"), "topic": topic,
        }
        state["dialogue"]["last_discussed_topic"] = topic

    def _prompt(self, state, item, field, purpose="inquiry_endpoint"):
        state["dialogue"]["pending_prompt"] = {
            "purpose": purpose,
            "scope_kind": item.get("scope_kind", "inquiry"),
            "scope_id": item["inquiry_id"],
            "field": field,
            "revision": item["revision"],
        }

    async def _resolve(self, state, item, fields):
        missing = [field for field in fields if not item.get(field)]
        if missing:
            self._prompt(state, item, missing[0])
            item["status"] = "awaiting_endpoint"
            return "Bạn muốn tính từ đâu?" if missing[0] == "origin" else "Bạn muốn tính đến đâu?"

        async def lookup(field):
            cached = item["locations"].get(field)
            if cached and cached.get("status") == "valid":
                return field, cached
            dependency = fingerprint([item["inquiry_id"], item["revision"], field, item[field]])
            binding = {
                "scope_kind": item.get("scope_kind", "inquiry"),
                "scope_id": item["inquiry_id"],
                "revision": item["revision"],
                "dependency_fingerprint": dependency,
                "request_id": "map_" + dependency[:24],
            }
            try:
                result = await self.engine.maps.resolve(
                    item[field],
                    target="pickup" if field == "origin" else "destination",
                    context=binding,
                )
                result = MapResolution.model_validate(result).model_dump(mode="json")
                if any(
                    result["binding"].get(key) != value for key, value in binding.items()
                ) or result["target"] != ("pickup" if field == "origin" else "destination"):
                    return field, {
                        "status": "unavailable",
                        "message": "Dữ liệu địa điểm không đúng yêu cầu hiện tại. Bạn hỏi lại giúp mình nhé.",
                    }
                state["read_requests"][binding["request_id"]] = {**binding, "status": "completed"}
                state["read_facts"][binding["request_id"]] = {
                    "value": deepcopy(result),
                    "valid_until": datetime.fromisoformat(result["expires_at"]).timestamp(),
                    "source": (result.get("place") or {}).get("source"),
                }
                status = (
                    "valid"
                    if result.get("status") in {"valid", "resolved", "unique"}
                    and result.get("place")
                    else result.get("status", "unavailable")
                )
                return field, {
                    "status": status,
                    "place": result.get("place"),
                    "candidates": result.get("candidates", []),
                    "message": result.get("clarification") or result.get("message"),
                    "reason_codes": result.get("reason_codes", []),
                    "area": result.get("area"),
                }
            except Exception:
                return field, {"status": "unavailable"}

        for field, location in await asyncio.gather(*(lookup(field) for field in fields)):
            item["locations"][field] = location
        for field in fields:
            location = item["locations"][field]
            if location["status"] == "valid":
                if state["control"].get("schema_version", 4) >= 6 and item.get("scope_kind", "inquiry") == "inquiry" and not location.get("confirmed"):
                    return self._propose_location(state, item, field, location["place"])
                continue
            if state["control"].get("schema_version", 4) >= 6 and location.get("area"):
                from app.domain.location_policy import representative_point

                profile = self.engine.vehicle_catalog.get(item.get("vehicle") or "oto_4_cho", {}).get("route_profile", "car")
                place = representative_point(location["area"], vehicle_profile=profile)
                if place:
                    location["area_preview"] = True
                    return self._propose_location(state, item, field, place, area_preview=True)
            self._prompt(state, item, field)
            item["status"] = "awaiting_candidate"
            # Relation candidates are anchors, not operational points to select.
            if location.get("candidates") and "RELATION_UNRESOLVED" not in location.get(
                "reason_codes", []
            ):
                old = item["candidate_sets"].get(field)
                item["candidate_sets"][field] = old or {
                    "candidate_set_id": "set_"
                    + fingerprint([item["inquiry_id"], item["revision"], field])[:24],
                    "revision": item["revision"],
                    "expires_at": min(item["expires_at"], self.engine.clock() + 300),
                    "places": location["candidates"][:3],
                    "presented_response_id": None,
                }
                return "Có nhiều địa điểm phù hợp. Bạn chọn địa điểm cho tuyến hỏi thử nhé."
            return location.get("message") or (
                "Dịch vụ bản đồ chưa trả được dữ liệu cho tuyến hỏi thử. Bạn có thể thử lại sau."
                if location["status"] == "unavailable"
                else f"Mình chưa xác định được {item[field]}. Bạn bổ sung địa chỉ hoặc khu vực nhé."
            )
        return None

    def _propose_location(self, state, item, field, place, area_preview=False):
        proposal = {"proposal_id": "loc_" + fingerprint([item["inquiry_id"], item["revision"], field, place])[:24], "field": field, "place": deepcopy(place), "revision": item["revision"], "expires_at": min(item["expires_at"], self.engine.clock() + 300), "area_preview": area_preview, "presented_response_id": None}
        item.setdefault("location_proposals", {})[field] = proposal
        self._prompt(state, item, field, "confirm_location")
        state["dialogue"]["pending_prompt"].update(proposal_id=proposal["proposal_id"], proposed_label=place["label"])
        item["status"] = "awaiting_location_confirmation"
        if area_preview:
            return f"{item[field]} là khu vực rộng. Bạn có muốn tính thử đến điểm đại diện {place['label']} không? Điểm này chỉ dùng ước tính; khi đặt xe vẫn cần chốt nơi đón/đến và dịch vụ hỗ trợ riêng. Trả lời ‘đúng’ hoặc cung cấp địa chỉ cụ thể."
        return f"Mình tìm được {place['label']} cho {'nơi đi' if field == 'origin' else 'nơi đến'} của tuyến hỏi thử. Bạn có muốn dùng địa điểm này để tính tuyến không? Trả lời ‘đúng’ hoặc cung cấp địa chỉ khác."

    async def apply_location_decision(self, state, decision, event):
        prompt = state["dialogue"].get("pending_prompt") or {}
        item = state["inquiries"].get(prompt.get("scope_id"))
        field = prompt.get("field")
        proposal = (item or {}).get("location_proposals", {}).get(field)
        response = state["last_response"]
        valid = bool(item and proposal and response.get("delivery_status") == "rendered" and proposal["presented_response_id"] == response["response_id"] and (not event.get("reply_to_response_id") or event["reply_to_response_id"] == response["response_id"]) and proposal["revision"] == item["revision"] and proposal["expires_at"] > self.engine.clock())
        state["control"]["last_event_id"] = event["event_id"]
        state["turn"] = {"issues": [], "questions": [], "changed_slots": [], "occurred_at": event.get("occurred_at", self.engine.clock())}
        if valid and decision == "confirm":
            item["locations"][field] = {"status": "valid", "place": deepcopy(proposal["place"]), "confirmed": True, "area_preview": proposal["area_preview"]}
            item["location_proposals"].pop(field, None)
            item["status"] = "collecting"
        elif valid and decision == "reject":
            item[field] = None
            item["revision"] += 1
            item["expires_at"] = self.engine.clock() + self.ttl
            item["locations"].pop(field, None)
            item["location_proposals"] = {}
            item["candidate_sets"] = {}
            item["routes"], item["quotes"] = {}, {}
            item["route_fingerprint"] = None
            item["status"] = "awaiting_endpoint"
        questions = [QuestionIntent.model_validate(q).model_copy(update={"route_scope": "active_inquiry", "origin": None, "destination": None}) for q in state["dialogue"].get("pending_questions", [])]
        if not questions:
            # Never attach an old location proposal to a generic unnamed reply.
            state["dialogue"]["pending_prompt"] = None
        answer = await self.answer(state, questions) if questions else "Bạn muốn hỏi giá, quãng đường hay thời gian cho tuyến này?"
        self.engine._respond(state, "answer_question", answer, reason=None if valid else "STALE_LOCATION_PROPOSAL")
        self.attach_presentation(state)
        return state

    async def _route(self, state, item, vehicle, *, include_traffic=False):
        profile = self.engine.vehicle_catalog.get(vehicle, {}).get("route_profile", "car")
        origin, destination = (
            item["locations"][field]["place"] for field in ("origin", "destination")
        )
        # Route-only and traffic reads have different caches and freshness.
        departure = None
        raw_departure = item.get("departure_time")
        if raw_departure:
            departure, _ = parse_forecast_time(raw_departure, item.get("time_reference_at", self.engine.clock()))
        dep = fingerprint([origin, destination, profile, "traffic" if include_traffic else "route",
                           departure.isoformat() if departure else None])
        request_id = "route_" + dep[:24]
        cached = state["read_facts"].get(request_id)
        if cached and cached["valid_until"] > self.engine.clock():
            route = deepcopy(cached["value"])
        else:
            committed = item.get("committed_route")
            prior_quote = item.get("committed_quote")
            prior_expires = (
                datetime.fromisoformat(prior_quote["expires_at"]).timestamp()
                if prior_quote and isinstance(prior_quote.get("expires_at"), str)
                else (prior_quote or {}).get("expires_at", 0)
            )
            if (
                committed and not include_traffic
                and committed.get("vehicle_profile", "car") == profile
                and (item.get("quote_committed") or prior_expires > self.engine.clock())
            ):
                route = {k: v for k, v in committed.items() if k != "dependency_fingerprint"}
            else:
                try:
                    parameters = inspect.signature(self.engine.maps.route).parameters
                    kwargs = {"vehicle_type": vehicle}
                    supports_kwargs = any(value.kind == inspect.Parameter.VAR_KEYWORD for value in parameters.values())
                    if "include_traffic" in parameters or supports_kwargs:
                        kwargs["include_traffic"] = include_traffic
                    if departure and ("departure_time" in parameters or supports_kwargs):
                        kwargs["departure_time"] = departure
                    route = await self.engine.maps.route(origin, destination, **kwargs)
                except Exception:
                    return None
            if not route or route.get("vehicle_profile", "car") != profile:
                return None
            try:
                route = RouteResult.model_validate(
                    {k: v for k, v in route.items() if k != "dependency_fingerprint"}
                ).model_dump(mode="json")
            except ValueError:
                return None
            if route["pickup_id"] != origin["id"] or route["destination_id"] != destination["id"]:
                return None
            state["read_facts"][request_id] = {
                "dependency_fingerprint": dep,
                "value": deepcopy(route),
                "valid_until": min(
                    self.engine.clock() + (60 if include_traffic else 120),
                    fact_expiry(route.get("valid_until")) or self.engine.clock() + 120,
                    (fact_expiry(route.get("traffic", {}).get("valid_until")) or self.engine.clock() + 60)
                    if include_traffic else self.engine.clock() + 120,
                ),
                "source": route.get("source"),
            }
        state["read_requests"][request_id] = {
            "scope_kind": item.get("scope_kind", "inquiry"),
            "scope_id": item["inquiry_id"],
            "revision": item["revision"],
            "dependency_fingerprint": dep,
            "status": "completed",
        }
        item["routes"][profile] = route
        item["route_fingerprint"] = fingerprint(
            [item["inquiry_id"], item["revision"], origin, destination, vehicle]
        )
        item["status"] = "answered"
        return route

    async def _quote(self, item, vehicle, route):
        prior = item.get("committed_quote")
        if item.get("scope_kind") == "booking" and prior and vehicle == prior.get("vehicle_type"):
            expires = (
                datetime.fromisoformat(prior["expires_at"]).timestamp()
                if isinstance(prior.get("expires_at"), str)
                else prior.get("expires_at", 0)
            )
            if item.get("quote_committed") or expires > self.engine.clock():
                return prior
        quote = item["quotes"].get(vehicle)
        if quote:
            expires = quote.get("expires_at", 0)
            expires = (
                datetime.fromisoformat(expires).timestamp() if isinstance(expires, str) else expires
            )
            if expires > self.engine.clock():
                return quote
        try:
            quote = self.engine.quote_adapter.quote(
                {k: v for k, v in route.items() if k != "dependency_fingerprint"}, vehicle
            )
            quote = await quote if inspect.isawaitable(quote) else quote
        except Exception:
            return None
        item["quotes"][vehicle] = quote
        return quote

    async def answer(self, state, questions: list[QuestionIntent]):
        answers = []
        state["dialogue"]["pending_prompt"] = None
        for question in questions:
            kind = question.type
            if kind == "identity":
                answers.append(
                    f"Mình là trợ lý đặt xe của {self.engine.brand_name}. Hiện đây là bản thử nghiệm, chưa gọi tài xế thật."
                )
            elif kind == "vehicle_catalog":
                labels = [
                    f"{row['label']} (tối đa {row['max_passengers']} khách)"
                    for row in self.engine.vehicle_catalog.values()
                    if row.get("bookable")
                ]
                text = f"{self.engine.brand_name} đang hỗ trợ thử nghiệm {', '.join(labels)}, tính cả trẻ em."
                if self.engine.vehicle_catalog.get("xe_may_dien", {}).get("bookable"):
                    text += " Xe máy điện tối đa một người lớn; người lớn đi cùng trẻ em cần đáp ứng quy định dịch vụ."
                answers.append(text)
            elif kind == "pickup_availability":
                state["dialogue"]["last_discussed_topic"] = kind
                text = "Đây là bản thử nghiệm nên mình chưa điều phối tài xế hoặc xác nhận giờ xe tới đón."
                if (
                    state["dialogue"].get("booking_started")
                    and state["booking_state"]["pickup_time"]["value"] is None
                ):
                    text += " Bạn muốn đón khi nào?"
                answers.append(text)
            elif kind == "price_objection":
                item = (
                    self._scope(state, question)
                    if question.route_scope == "current_booking"
                    else self.active(state)
                )
                quote = (
                    (item.get("committed_quote") or next(iter(item["quotes"].values()), None))
                    if item
                    else state["resolution"].get("quote")
                )
                committed = (
                    state["transaction"].get("committed_snapshot")
                    if item and item.get("scope_kind") == "booking"
                    else None
                )
                if quote and not committed:
                    expires = quote.get("expires_at", 0)
                    expires = (
                        datetime.fromisoformat(expires).timestamp()
                        if isinstance(expires, str)
                        else expires
                    )
                    if expires <= self.engine.clock():
                        if item and all(
                            item["locations"].get(f, {}).get("status") == "valid"
                            for f in ("origin", "destination")
                        ):
                            vehicle = quote["vehicle_type"]
                            route = await self._route(state, item, vehicle)
                            quote = await self._quote(item, vehicle, route) if route else None
                        else:
                            quote = None
                if not quote:
                    answers.append("Bạn đang cân nhắc mức giá của tuyến nào?")
                else:
                    tariff = quote.get("breakdown") or self.engine.quote_adapter.pricing[
                        "tariffs"
                    ].get(quote.get("vehicle_type"), {})
                    detail = (
                        f"phí nền {tariff['base_fee']:,.0f} đồng và {tariff['per_km']:,.0f} đồng/km"
                        if tariff
                        else "loại xe và quãng đường có nguồn"
                    )
                    answers.append(
                        f"Mình hiểu bạn đang cân nhắc chi phí. Đây là giá thử nghiệm, tính theo {detail}. Bạn có thể hỏi giá loại xe khác để so sánh."
                    )
            elif kind in ROUTE_QUESTIONS:
                item = self._explanation_scope(state, question) if kind == "travel_duration_explanation" else self._scope(state, question)
                if not item:
                    answers.append("Bạn muốn mình giải thích thời gian di chuyển của tuyến nào, thời gian xe đến đón hay thời gian chờ phản hồi?")
                    continue
                if item.get("scope_kind") != "booking":
                    item["questions"] = [
                        q.model_dump() for q in questions if q.type in ROUTE_QUESTIONS
                    ]
                fields = (
                    ("origin",)
                    if kind == "weather_forecast"
                    and question.weather_target in {"pickup", "explicit_location"}
                    else ("destination",)
                    if kind == "weather_forecast"
                    and question.weather_target == "destination"
                    and not arrival_scenario(question)
                    else ("origin", "destination")
                )
                message = await self._resolve(state, item, fields)
                if message:
                    if message not in answers:
                        answers.append(message)
                    continue
                if (
                    fields == ("origin", "destination")
                    and item["locations"]["origin"]["place"]["id"]
                    == item["locations"]["destination"]["place"]["id"]
                ):
                    answers.append(
                        "Hai địa điểm đang trùng nhau. Bạn muốn tính đến một cổng hay địa điểm khác?"
                    )
                    continue
                vehicle = item.get("vehicle") or "oto_4_cho"
                if vehicle not in self.engine.vehicle_catalog or not self.engine.vehicle_catalog[
                    vehicle
                ].get("quote_available"):
                    answers.append(
                        "Xe máy điện chưa có đủ giá và cấu hình tuyến đường để báo giá hoặc đặt xe. Bạn có thể chọn ô tô 4 chỗ hoặc 7 chỗ."
                    )
                    continue
                if kind == "weather_forecast":
                    answers.extend(await self._weather(state, item, question, vehicle))
                    continue
                route = await self._route(state, item, vehicle, include_traffic=kind == "travel_duration_explanation")
                if not route:
                    answers.append(
                        "Mình chưa lấy được tuyến đường có nguồn cho hai địa điểm này, nên chưa thể tính giá hoặc thời gian."
                    )
                    continue
                a, b = (item["locations"][f]["place"]["label"] for f in ("origin", "destination"))
                label = self.engine.vehicle_catalog[vehicle]["label"]
                self._remember_route(state, item, vehicle, kind)
                if kind == "route_distance":
                    distance = route["distance_m"]
                    formatted = (
                        f"{round(distance)} m"
                        if distance < 1000
                        else f"{distance / 1000:.2f}".rstrip("0").rstrip(".") + " km"
                    )
                    answers.append(
                        f"Từ {a} đến {b}, quãng đường theo tuyến {label.lower()} khoảng {formatted}. Nguồn: {route['source']}."
                    )
                elif kind == "travel_duration":
                    answers.append(
                        f"Từ {a} đến {b} bằng {label.lower()}, thời gian di chuyển ước tính khoảng {max(1, math.ceil(route['duration_s'] / 60))} phút. Thực tế có thể thay đổi theo giao thông. Nguồn: {route['source']}."
                    )
                elif kind == "travel_duration_explanation":
                    answers.append(duration_explanation(route, a, b, label, self.engine.clock()))
                else:
                    vehicles = (
                        [vehicle]
                        if item.get("vehicle")
                        else [
                            code
                            for code, row in self.engine.vehicle_catalog.items()
                            if row.get("quote_available")
                        ]
                    )
                    prices = []
                    for code in vehicles:
                        option = (
                            route
                            if self.engine.vehicle_catalog[code]["route_profile"]
                            == self.engine.vehicle_catalog[vehicle]["route_profile"]
                            else await self._route(state, item, code)
                        )
                        quote = await self._quote(item, code, option) if option else None
                        if quote:
                            prices.append(
                                f"{self.engine.vehicle_catalog[code]['label']}: {quote['amount']:,.0f} đồng"
                            )
                    answers.append(
                        f"Tuyến {a} → {b}. Giá thử nghiệm tham khảo: {'; '.join(prices)}. Giá có thời hạn, sẽ kiểm tra lại trước khi đặt."
                        if prices
                        else "Chưa có giá có nguồn cho tuyến này."
                    )
            elif kind == "chit_chat":
                answers.append("Mình sẵn sàng giúp bạn xem tuyến, giá hoặc tiếp tục đặt xe.")
            else:
                answers.append(
                    f"Mình là trợ lý đặt xe của {self.engine.brand_name}. Bản thử nghiệm hỗ trợ xem tuyến, giá, thời tiết và tạo/hủy đơn đi ngay, một chiều."
                )
        for name in ("read_requests", "read_facts"):
            while len(state[name]) > 64:
                del state[name][next(iter(state[name]))]
        return "\n".join(dict.fromkeys(answers))

    async def _weather(self, state, item, question, vehicle):
        now = item.get("time_reference_at", state["turn"].get("occurred_at", self.engine.clock()))
        when, basis = parse_forecast_time(
            question.departure_time_ref or item.get("departure_time"), now
        )
        if when is None:
            self._prompt(state, item, "departure_time", "weather_time")
            return [basis]
        if self.weather is None:
            return ["Dịch vụ thời tiết chưa được cấu hình. Mình chưa có dự báo để trả lời."]
        targets = (
            ["origin", "destination"]
            if question.weather_target == "both"
            else ["destination"]
            if question.weather_target == "destination"
            else ["origin"]
        )
        route = None
        if "destination" in targets and arrival_scenario(question):
            route = await self._route(state, item, vehicle)
            if not route:
                return [
                    "Mình cần tuyến đường và thời gian xuất phát có nguồn để ước tính thời điểm tới nơi."
                ]
        replies = []
        for field in targets:
            if field == "destination" and question.weather_target == "both" and not route:
                replies.append("Chưa lấy được thời gian di chuyển để xem dự báo lúc tới nơi.")
                continue
            target = (
                when + timedelta(seconds=route["duration_s"])
                if field == "destination" and route
                else when
            )
            location = item["locations"][field]["place"]
            dep = fingerprint(
                [item["inquiry_id"], item["revision"], location, target.isoformat(), vehicle]
            )
            request = WeatherRequest(
                request_id="weather_" + dep[:24],
                scope_kind=item.get("scope_kind", "inquiry"),
                scope_id=item["inquiry_id"],
                location_ref=location["id"],
                latitude=location["lat"],
                longitude=location["lon"],
                target_time=target,
                dependency_fingerprint=dep,
            )
            try:
                fact = await self.weather.forecast(request)
                fact = WeatherFact.model_validate(fact)
            except Exception:
                replies.append(
                    f"Chưa lấy được dự báo tại {location['label']}; mình sẽ không đoán thời tiết."
                )
                continue
            if (
                fact.request_id != request.request_id
                or fact.dependency_fingerprint != dep
                or fact.scope_id != request.scope_id
                or fact.scope_kind != request.scope_kind
            ):
                replies.append(
                    "Dữ liệu thời tiết trả về đã cũ hoặc không đúng yêu cầu. Bạn có thể hỏi lại."
                )
                continue
            state["read_facts"][request.request_id] = {
                "value": fact.model_dump(mode="json"),
                "valid_until": fact.valid_until.timestamp(),
                "source": fact.source_ref,
            }
            if fact.status != "available" or not fact.sample:
                replies.append(
                    f"Chưa có dự báo tại {location['label']} cho thời điểm này ({fact.unavailable_reason})."
                )
                continue
            if (
                fact.valid_until.timestamp() <= self.engine.clock()
                or abs(fact.sample.timestamp.timestamp() - target.timestamp()) > 3600
            ):
                replies.append(
                    f"Dự báo tại {location['label']} đã hết hạn hoặc không bao phủ đúng giờ bạn hỏi."
                )
                continue
            sample = fact.sample
            details = []
            if sample.weather_code is not None:
                details.append(
                    WEATHER_LABELS.get(
                        sample.weather_code, f"mã thời tiết WMO {sample.weather_code}"
                    )
                )
            if sample.temperature_c is not None:
                details.append(f"nhiệt độ khoảng {sample.temperature_c:g}°C")
            if sample.precipitation_probability is not None:
                details.append(
                    f"khả năng mưa {sample.precipitation_probability:g}% trong khoảng giờ dự báo"
                )
            label = target.astimezone(ZONE).strftime("%H:%M ngày %d/%m")
            replies.append(
                f"Tại {location['label']} khoảng {label} ({basis}), dự báo: {', '.join(details)}. Dữ liệu theo giờ, không phải đo tại đúng phút lên xe. Nguồn: {fact.attribution}."
            )
        return replies

    def attach_presentation(self, state):
        item = self.active(state)
        if not item:
            return
        response = state["last_response"]
        prompt = state["dialogue"].get("pending_prompt")
        if prompt and prompt["scope_kind"] == "inquiry":
            field = prompt.get("field")
            proposal = item.get("location_proposals", {}).get(field)
            if proposal and prompt.get("purpose") == "confirm_location":
                response["action"] = "confirm_location"
                response["focus"] = "pickup" if field == "origin" else "destination"
                proposal["presented_response_id"] = response["response_id"]
                response["presentation"].update(prompt_id=proposal["proposal_id"], valid_until=proposal["expires_at"])
            batch = item["candidate_sets"].get(field)
            if batch:
                candidates = [
                    {
                        "candidate_id": candidate_ref(batch["candidate_set_id"], place["id"]),
                        "candidate_set_id": batch["candidate_set_id"],
                        "target": "pickup" if field == "origin" else "destination",
                        "ordinal": i,
                        "label": place["label"],
                        "scope_kind": "inquiry",
                        "scope_id": item["inquiry_id"],
                        "revision": item["revision"],
                    }
                    for i, place in enumerate(batch["places"], 1)
                ]
                response["candidates"] = candidates
                response["text"] += "\n" + "\n".join(
                    f"{c['ordinal']}. {c['label']}" for c in candidates
                )
                response["action"] = "offer_candidates"
                batch["presented_response_id"] = response["response_id"]
                state["candidates"] = candidates
        response["inquiry"] = {
            k: item.get(k)
            for k in (
                "inquiry_id",
                "revision",
                "origin",
                "destination",
                "vehicle",
                "status",
                "expires_at",
                "route_fingerprint",
            )
        }
        response["inquiry"]["can_use_route"] = bool(
            item.get("route_fingerprint")
            and all(
                item["locations"].get(f, {}).get("status") == "valid"
                for f in ("origin", "destination")
            )
            and state["booking_status"]
            not in {"booked", "cancelled", "booking_unknown", "cancel_unknown", "cancel_failed"}
            and not any(location.get("area_preview") for location in item["locations"].values())
        )
        response["inquiry"]["booking_revision"] = state["control"]["booking_revision"]
        if response["inquiry"]["can_use_route"]:
            response["text"] += "\nBạn có thể nhắn ‘dùng tuyến vừa hỏi để đặt’, ‘tuyến vừa hỏi đi xe 7 chỗ giá bao nhiêu?’ hoặc ‘tiếp tục chuyến đang đặt’."
        if any(location.get("area_preview") for location in item["locations"].values()) and item.get("route_fingerprint"):
            response["text"] += "\nĐây là ước tính đến điểm đại diện có nguồn, chưa gồm phí hỗ trợ và chưa chốt điểm trả bên trong khu vực."
        response["presentation"].update(
            scope_kind="inquiry",
            scope_id=item["inquiry_id"],
            inquiry_revision=item["revision"],
            valid_until=item["expires_at"],
        )
