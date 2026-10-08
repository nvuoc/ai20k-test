"""Limited offline interpretation for v2 regression and demonstration only."""

from __future__ import annotations

import re

from app.adapters.nlu_fixture import _fold, extract_fixture
from app.contracts.nlu import NluInput
from app.contracts.turn import TurnInput, TurnResult
from app.domain.pickup_time import time_expression

QUESTION_PATTERNS = {
    "static_faq": r"(?:hanh ly|thu cung|cong kenh|ghe tre em|hut thuoc).*\b(?:khong|duoc|quy dinh|chinh sach|bao nhieu)\b",
    "session_question": r"(?:da chon xe|chon xe gi|dat di dau|nay toi dat|doc lai|nhac lai)",
    "route_membership": r"(?:tren|gan) (?:lo trinh|tuyen duong)|co (?:di |nam )?qua",
    "identity": r"ban la ai|ban ten gi|parrotgo|co phai tai xe|ban co phai tai xe",
    "vehicle_catalog": r"co (?:nhung )?(?:loai )?xe (?:gi|nao)|nhung loai xe|cac loai xe",
    "place_location": r"\b(?:o|tai) (?:dau|cho nao|vi tri nao)\b|\bthuoc (?:tinh|thanh pho|quan|huyen) nao\b|\bdia chi .+ (?:la gi|the nao)\b|^(?:dia chi|(?:cho (?:toi|minh|em) biet|toi (?:muon|can) biet) dia chi)\b",
    "weather_forecast": r"thoi tiet|co mua|troi mua|co nong|nhiet do|du bao|khong mua",
    "route_distance": r"\b(?:km|kilomet|met|khoang cach|co xa|bao xa)\b",
    "pickup_availability": r"xe (?:toi|den) don|tai xe|bao gio (?:co xe|don)|don (?:nhanh|ngay).*khong|khi nao.*don",
    "price_objection": r"\b(?:dat vay|sao dat|gia dat|re hon|gia cao|giam gia|gia co giam)\b|^dat (?:qua|the)",
    "fare_estimate": r"\b(?:gia|bao nhieu tien|het bao nhieu|cuoc|bao gia|phi)\b",
    "travel_duration": r"\b(?:bao lau|may phut|thoi gian di|mat bao nhieu phut|toi noi luc)\b",
    "travel_duration_explanation": r"\b(?:(?:sao|tai sao|vi sao).*\b(?:nhanh|lau)|(?:nhanh|lau)\s+(?:the|vay|qua))\b",
}


def question_types(text: str) -> list[str]:
    key = _fold(text)
    found = [kind for kind, pattern in QUESTION_PATTERNS.items() if re.search(pattern, key)]
    if "price_objection" in found and not re.search(r"\btu\b.+\b(?:den|toi)\b", key):
        found = [kind for kind in found if kind != "fare_estimate"]
    if "pickup_availability" in found:
        found = [kind for kind in found if kind != "travel_duration"]
    if "travel_duration_explanation" in found:
        found = [kind for kind in found if kind != "travel_duration"]
    if found:
        return found
    return ["other_booking_question"] if "?" in text or "cho hoi" in key else []


def _parameters(text: str) -> dict:
    folded = _fold(text)
    origin = destination = None
    route = re.search(r"\btu\s+(.+?)\s*(?:den|toi|→|->)\s+(.+)", folded)
    if route:
        origin = text[route.start(1) : route.end(1)].strip(" ,")
        tail = route.group(2)
        ending = re.search(
            r"\s+(?:bang xe|xe [47]|het bao|gia|bao nhieu|mat bao|di bao|bao lau|co xa|cach nhau|luc|vao|ngay mai|mai\s+\d|co mua|thoi tiet|khoang|thi|la bao|khong mua)|[?;]",
            tail,
        )
        end = route.start(2) + (ending.start() if ending else len(tail))
        destination = text[route.start(2) : end].strip(" ,.!?")
    elif re.search(r"thoi tiet|co mua|nhiet do|du bao|co nong", folded):
        location = re.search(
            r"\b(?:o|tai)\s+(.+?)(?=\s+(?:luc|vao|hom nay|ngay mai|mai\b|bay gio|hien tai|co mua|co nong)|[?;]|$)",
            folded,
        )
        if location:
            origin = text[location.start(1) : location.end(1)].strip(" ,.!?")
    else:
        one_end = re.search(r"\bden\s+(.+?)(?=\s+(?:gia|bao nhieu|mat bao|bao lau)|[?]|$)", folded)
        if one_end:
            destination = text[one_end.start(1) : one_end.end(1)].strip(" ,.!?")
    vehicle = next(
        (
            code
            for pattern, code in (
                (r"xe may dien", "xe_may_dien"),
                (r"xe may", "xe_may"),
                (r"7 cho", "oto_7_cho"),
                (r"4 cho", "oto_4_cho"),
            )
            if re.search(pattern, folded)
        ),
        None,
    )
    when = time_expression(text)
    if not when:
        when = next(
            (
                s
                for s in ("ngày mai", "bây giờ", "hiện tại", "đi ngay", "hôm nay")
                if _fold(s) in folded
            ),
            None,
        )
    scope = (
        "explicit_pair"
        if origin or destination
        else "current_booking"
        if re.search(
            r"chuyen nay|chuyen dang dat|chuyen da dat|diem don|diem den|len xe|xuong xe|toi noi",
            folded,
        )
        else "active_inquiry"
        if re.search(r"tuyen (?:do|vua hoi|nay)|hai cho", folded)
        else "unresolved"
    )
    target = (
        "both"
        if re.search(r"(?:len xe|don).*(?:xuong xe|toi noi|den)|ca hai", folded)
        else "destination"
        if re.search(r"xuong xe|toi noi|diem den", folded)
        else "explicit_location"
        if origin and not destination
        else "pickup"
    )
    return dict(
        origin=origin,
        destination=destination,
        vehicle_ref=vehicle,
        departure_time_ref=when,
        route_scope=scope,
        weather_target=target,
    )


def _place_parameters(text: str) -> dict:
    folded = _fold(text)
    prefix = re.match(
        r"(?:xin hoi|cho (?:toi|minh|em) hoi|cho hoi|ban (?:co )?biet|"
        r"toi (?:muon|can) biet|cho (?:toi|minh|em) biet)\s+", folded,
    )
    start = prefix.end() if prefix else 0
    body = folded[start:]
    ending = r"(?:\s+(?:khong|nhi|nhe|a|vay|the))*[ .!?]*$"
    patterns = (
        r"(?:dia chi(?:\s+cua)?\s+)?(.+?)(?:\s+(?:la|thi))?"
        r"(?:\s+(?:nam|toa lac))?\s+(?:o|tai)\s+(?:dau|cho nao|vi tri nao)" + ending,
        r"dia chi(?:\s+cua)?\s+(.+?)(?:\s+la\s+gi|\s+the nao)?" + ending,
        r"(.+?)\s+thuoc\s+(?:tinh|thanh pho|quan|huyen)\s+nao" + ending,
    )
    match = next((m for pattern in patterns if (m := re.fullmatch(pattern, body))), None)
    query = text[start + match.start(1):start + match.end(1)].strip(" ,.!?") if match else None
    key = _fold(query or "")
    booking_ref = bool(re.search(r"\b(?:diem|noi) (?:don|den)\b", key))
    if booking_ref or key in {"dia diem nay", "dia diem do", "dia danh nay", "dia danh do", "cho nay", "cho do", "do"}:
        query = None
    return dict(
        origin=query,
        destination=None,
        vehicle_ref=None,
        departure_time_ref=None,
        route_scope="explicit_pair" if query else "current_booking" if booking_ref else "unresolved",
        weather_target=None,
    )


def extract_turn_fixture(data: TurnInput) -> TurnResult:
    text = data.utterance.text or ""
    whole = {"start": 0, "end": max(1, len(text))}
    key = _fold(text).strip()
    acts, questions, actions, conversational = [], [], [], []
    base = NluInput.model_validate(
        {k: v for k, v in data.model_dump().items() if k in NluInput.model_fields}
    )
    active = data.active_inquiry_id
    prompt = data.pending_prompt
    if re.fullmatch(r"(?:cam on(?: ban)?|xin chao|chao|hello|thanks)[ .!?]*", key):
        return TurnResult.model_validate(dict(contract_version=data.contract_version, speech_status="clear", booking_acts=[], questions=[], inquiry_actions=[], conversational_acts=["thanks" if re.search(r"cam on|thanks", key) else "greeting"], travel_party=None))
    if prompt and prompt.purpose in {"confirm_location", "ask_area_detail", "choose_area_service", "confirm_assistance"}:
        clean = key.strip(" .!?")
        decision = None
        if re.fullmatch(r"(?:khong biet(?: dia chi(?: chinh xac)?)?|khong nho|khong ro|chua biet|khong)(?: dau)?", clean) and prompt.purpose == "ask_area_detail":
            decision = "unknown_detail" if prompt.purpose == "ask_area_detail" else None
        elif re.fullmatch(r"(?:ho tro|can ho tro|muon ho tro|dong y ho tro|tim dia chi)", clean):
            decision = "request_assistance"
        elif re.fullmatch(r"(?:diem co dinh|khong ho tro|khong can ho tro|khong dong y phi|khong dong y|khong|thoi)", clean):
            decision = "decline_assistance" if prompt.purpose in {"choose_area_service", "confirm_assistance"} else "reject"
        elif re.fullmatch(r"(?:dong y phi|dong y muc phi|chap nhan phi)", clean):
            decision = "accept_fee"
        elif re.fullmatch(r"(?:dung|dung roi|dung dia diem|dong y|xac nhan|ok|oke|vang|u|co)(?: nhe)?", clean):
            decision = "confirm"
        if decision:
            return TurnResult.model_validate(dict(contract_version=data.contract_version, speech_status="clear", booking_acts=[], questions=[], inquiry_actions=[], conversational_acts=[], travel_party=None, location_decisions=[dict(decision=decision, evidence_span=whole)]))
    route_context = None
    has_question = bool(question_types(text))
    explicit_booking = bool(
        re.search(
            r"\b(?:dat xe|dat chuyen|don toi|don minh|doi diem don|doi diem den|doi sang|toi muon dat|dat ho|don o)\b",
            key,
        )
    )

    def inquiry_action(kind, field=None, value=None):
        actions.append(
            dict(type=kind, inquiry_id=active, field=field, value=value, evidence_span=whole)
        )

    if re.search(
        r"(?:dung|lay|dat) (?:tuyen|hai cho|hai diem).*(?:vua hoi|nay|do)|dat tuyen nay", key
    ):
        inquiry_action("promote")
    elif re.search(r"dao chieu|doi chieu|di nguoc", key):
        inquiry_action("reverse")
    elif re.fullmatch(
        r"(?:tiep tuc dat xe|quay lai dat xe|bo qua tuyen nay|tiep tuc chuyen cu)[ .!]*", key
    ):
        inquiry_action("resume_booking")
    elif (
        prompt
        and prompt.scope_kind == "inquiry"
        and not question_types(text)
        and not re.search(
            r"\b(?:huy|dien thoai|sdt|so 0|diem don|diem den|dong y|xac nhan)\b|^0\d{8,}", key
        )
    ):
        selection = extract_fixture(base)
        candidate_acts = [
            a
            for a in selection.dialogue_acts
            if a.intent in {"select_candidate", "reject_candidate"}
        ]
        if candidate_acts:
            for item in candidate_acts:
                inquiry_action(
                    item.intent, "origin" if item.target == "pickup" else "destination", item.value
                )
        elif prompt.field in {"origin", "destination", "vehicle", "departure_time"}:
            value = (
                _parameters(text)["vehicle_ref"] if prompt.field == "vehicle" else text.strip(" .!")
            )
            inquiry_action("update", prompt.field, value)
    else:
        place_query = _place_parameters(text)["origin"] if question_types(text) == ["place_location"] and not explicit_booking else None
        clause_pattern = r"[^;\n]+" if place_query and not question_types(place_query) else r"[^,;\n]+"
        for match in re.finditer(clause_pattern, text):
            raw = match.group().strip()
            if not raw:
                continue
            offset = match.start() + len(match.group()) - len(match.group().lstrip())
            parts = re.split(r"\s+(?:nhưng|nhung|còn|con)\s+", raw, flags=re.I)
            cursor = offset
            for part in parts:
                start = text.find(part, cursor)
                cursor = start + len(part)
                kinds = question_types(part)
                evidence = {"start": start, "end": cursor}
                parameters = _parameters(part)
                if (
                    not kinds
                    and has_question
                    and parameters["origin"]
                    and parameters["destination"]
                    and not explicit_booking
                ):
                    route_context = parameters
                    continue
                if kinds:
                    if route_context and not parameters["origin"] and not parameters["destination"]:
                        parameters.update(
                            origin=route_context["origin"],
                            destination=route_context["destination"],
                            route_scope="explicit_pair",
                        )
                    for kind in kinds:
                        question_parameters = _place_parameters(part) if kind == "place_location" else parameters
                        questions.append(
                            dict(
                                question_id=f"q{len(questions) + 1}",
                                type=kind,
                                raw_text=part,
                                evidence_span=evidence,
                                relation_to_booking="hypothetical"
                                if question_parameters["route_scope"] == "explicit_pair" and kind != "place_location"
                                else "read_only",
                                **question_parameters,
                            )
                        )
                else:
                    projection = base.model_copy(
                        update={"utterance": base.utterance.model_copy(update={"text": part})}
                    )
                    for item in extract_fixture(projection).dialogue_acts:
                        if item.intent == "chit_chat":
                            if re.search(r"cam on", _fold(part)):
                                conversational.append("thanks")
                            elif re.search(r"chao|hello|hi\b", _fold(part)):
                                conversational.append("greeting")
                        elif item.intent == "ask_question":
                            questions.append(
                                dict(
                                    question_id=f"q{len(questions) + 1}",
                                    type="other_booking_question",
                                    raw_text=part,
                                    evidence_span=evidence,
                                    relation_to_booking="read_only",
                                    **_parameters(part),
                                )
                            )
                        else:
                            act = item.model_dump()
                            if act["target"] == "vehicle_type" and "xe may dien" in _fold(part):
                                act["value"] = "xe_may_dien"
                            acts.append({**act, "evidence_span": evidence})
    if not (acts or questions or actions or conversational):
        conversational = ["unclear"]
    if (
        questions
        and not explicit_booking
        and (route_context or any(q["origin"] or q["destination"] for q in questions))
    ):
        hypothetical_fields = {
            "pickup",
            "destination",
            "vehicle_type",
            "pickup_time",
            "passengers",
            "luggage",
            "stops",
            "special_requests",
        }
        acts = [
            a
            for a in acts
            if not (
                a["intent"] in {"provide_info", "change_info"}
                and a["target"] in hypothetical_fields
            )
        ]
    party = None
    numbers = {"mot": 1, "hai": 2, "ba": 3, "bon": 4}
    adult = re.search(r"(\d+|mot|hai|ba|bon)\s+nguoi lon", key)
    child = re.search(r"(\d+|mot|hai|ba|bon)\s+(?:tre em|be|tre)", key)
    if (adult or child) and not questions:

        def count(match):
            return (
                int(match.group(1))
                if match and match.group(1).isdigit()
                else numbers.get(match.group(1))
                if match
                else None
            )

        party = dict(adults=count(adult), children=count(child), evidence_span=whole)
        if adult and child:
            acts = [a for a in acts if a["target"] != "passengers"]
            acts.append(
                dict(
                    intent="provide_info",
                    target="passengers",
                    value=party["adults"] + party["children"],
                    evidence_span=whole,
                )
            )
    if len(questions) > 8:
        remainder = questions[7].copy()
        remainder.update(type="unclear", route_scope="unresolved", origin=None, destination=None)
        questions = questions[:7] + [remainder]
    return TurnResult.model_validate(
        dict(
            contract_version=data.contract_version,
            speech_status="clear",
            booking_acts=acts,
            questions=questions,
            inquiry_actions=actions,
            conversational_acts=conversational,
            travel_party=party,
        )
    )


def faq_turn(data: TurnInput) -> TurnResult | None:
    key = _fold(data.utterance.text or "").strip(" .!?")
    if re.fullmatch(
        r"(?:ban la ai|ban ten gi|co nhung loai xe nao|co xe gi|co loai xe nao|xin chao|chao|cam on|cam on ban)",
        key,
    ):
        return extract_turn_fixture(data)
    return None
