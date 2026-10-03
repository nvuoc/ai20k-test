"""Deterministic offline Vietnamese examples for the runnable demo.

This deliberately limited parser is a fixture provider, not a replacement for
Gemini language understanding. Unknown or ambiguous phrasing asks for clarity.
It still goes through the exact same typed extractor and domain guards.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from app.contracts.nlu import NluInput, NluResult

from .extractor import ModelReply

_NUMBERS = {
    "mot": 1,
    "hai": 2,
    "ba": 3,
    "bon": 4,
    "tu": 4,
    "nam": 5,
    "sau": 6,
    "bay": 7,
    "tam": 8,
    "chin": 9,
    "muoi": 10,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
}
_NUMBER = r"(?:\d{1,2}|mot|hai|ba|bon|tu|nam|sau|bay|tam|chin|muoi|one|two|three|four)"
_QUESTION = r"(?:\?|bao nhieu|gia sao|gia the nao|mat phi|co nhan|nhan .+ khong|duoc khong|bao gia|cho hoi|may phut|bao lau|khi nao)"
_AFFIRM = r"^(?:dung(?: roi)?|vang|da|u|uh|ok|okay|dong y|xac nhan|chot|dat(?: xe| chuyen)?(?: giup toi| giup minh| di| nhe)?)(?:[ .!]|$)"
_LOCATION_END = (
    r"(?=\s*(?:,|;|$|\b(?:roi\s+)?(?:den|di den|toi|drop[- ]?off)\s+|"
    r"\b(?:luc|vao luc|ngay mai|mai(?=\s+(?:luc\s+)?\d{1,2}\s*(?:gio|:))|hom nay|\d{1,2}\s*(?:gio|nguoi)|"
    r"hai nguoi|ba nguoi|mot nguoi|xe [47] cho|doi xe|tra tien|so dien thoai)\b))"
)


def _fold(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
        if not unicodedata.combining(char)
    )


def _number(text: str) -> int | None:
    normalized = _fold(text.strip())
    return int(normalized) if normalized.isdigit() else _NUMBERS.get(normalized)


def _clean(text: str) -> str:
    return re.sub(
        r"\s+(?:nhé|nhe|ạ|a|giúp tôi|giup toi|giúp mình|giup minh)$",
        "",
        text.strip(" ,.;!"),
        flags=re.I,
    ).strip()


class FixtureExtractorClient:
    async def close(self) -> None:
        return None

    async def generate(
        self,
        *,
        system_prompt: str,
        input_json: str,
        response_schema: dict[str, Any],
        model: str,
        timeout_seconds: float,
    ) -> ModelReply:
        raw = json.loads(input_json)
        if raw.get("contract_version") in {"parrotgo-turn-2", "parrotgo-turn-3"}:
            from app.adapters.turn_fixture import extract_turn_fixture
            from app.contracts.turn import TurnInput
            result = extract_turn_fixture(TurnInput.model_validate(raw))
        else:
            data = NluInput.model_validate(raw)
            result = extract_fixture(data)
        return ModelReply(text=result.model_dump_json())


def extract_fixture(data: NluInput) -> NluResult:
    text = (data.utterance.text or "").strip()
    folded = _fold(text)
    context = data.conversation_context
    focus = context.current_focus
    acts: list[dict[str, Any]] = []
    values: dict[str, Any] = {}
    uncertain = False

    def add(intent: str, target: str | None = None, value: Any = None) -> None:
        act = {"intent": intent, "target": target, "value": value}
        if act not in acts:
            acts.append(act)

    def put(target: str, value: Any) -> None:
        if isinstance(value, str):
            value = _clean(value)
            if not value:
                return
        values[target] = value

    # Candidate ordinals refer to the active IDs, never indexes or invented IDs.
    ordinal = re.search(rf"(?:cai\s+)?(?:thu|so|chon)\s+({_NUMBER})\b", folded)
    bare = re.fullmatch(rf"({_NUMBER})(?:\s+(?:cai|nhe))?[.!]?", folded)
    if data.candidates:
        if re.search(r"khong (?:phai )?(?:cai nao|chon cai nao)|khong dung cai nao", folded):
            add("reject_candidate", data.candidates[0].target)
        elif ordinal or bare:
            selected = _number((ordinal or bare).group(1))
            candidate = next((item for item in data.candidates if item.ordinal == selected), None)
            if candidate:
                add("select_candidate", candidate.target, candidate.candidate_id)
            else:
                uncertain = True
        else:
            matching = [
                item for item in data.candidates if _fold(item.label) == folded.strip(" .!")
            ]
            if len(matching) == 1:
                add("select_candidate", matching[0].target, matching[0].candidate_id)
    elif ordinal:
        uncertain = True

    if re.search(r"(?:nhac|noi|doc) lai|lap lai", folded):
        add("request_repeat")
    if re.search(r"(?:cho chut|dung dat|chua dat|khoan dat|dung voi dat)", folded):
        add("deny")

    # A question or conditional clause cannot quietly become a slot mutation.
    clauses = re.split(r"[,;\n]+", text)
    for clause in clauses:
        clause = clause.strip()
        lower = _fold(clause).strip()
        if not lower:
            continue
        question = bool(re.search(_QUESTION, lower))
        if question:
            add("ask_question", value=clause.strip())
            continue
        if re.search(r"\bneu\b|\bmien la\b|\bchi khi\b", lower):
            uncertain = True
            continue
        if re.search(r"\bhuy\b", lower):
            if re.search(r"(?:a thoi|giu chuyen|khong phai la khong)", lower):
                uncertain = True
            elif not re.search(r"(?:khong|dung|chua)\s+huy", lower):
                add("cancel")

        route = re.search(r"\btu\s+(.+?)\s+qua\s+(.+?)\s+(?:roi\s+)?(?:den|toi)\s+(.+)$", lower)
        if route:
            put("pickup", clause[route.start(1) : route.end(1)])
            put("stops", [clause[route.start(2) : route.end(2)].strip()])
            put("destination", clause[route.start(3) : route.end(3)])
        else:
            pickup = re.search(
                r"(?:\b(?:doi\s+)?diem don\s+(?:sang|thanh|la)|(?<!diem )\bdon(?: toi| minh)?(?:\s+o|\s+tai)?|\bpickup(?:\s+o)?|\btu)\s+(.+?)"
                + _LOCATION_END,
                lower,
            )
            if pickup:
                location = clause[pickup.start(1) : pickup.end(1)]
                location_lower = _fold(location)
                if location_lower in {"day", "o day", "nha", "cho cu"} or re.search(
                    r"\b(?:hay|hoac)\b", location_lower
                ):
                    uncertain = True
                elif not re.match(r"(?:ngay|bay gio|luc|\d{1,2}\s*gio)", location_lower):
                    put("pickup", location)
            destination_matches = re.finditer(
                r"(?:\b(?:doi\s+)?diem den\s+(?:sang|thanh|la)|\b(?:di den|den|toi|drop[- ]?off|di))\s+(.+?)"
                + _LOCATION_END,
                lower,
            )
            destination = next(
                (
                    item
                    for item in destination_matches
                    if not clause[item.start() : item.start() + 3].casefold() == "tôi"
                ),
                None,
            )
            if destination:
                location = clause[destination.start(1) : destination.end(1)]
                if _fold(_clean(location)) in {"ngay", "bay gio", "ngay bay gio", "asap"}:
                    pass
                elif re.match(
                    r"(?:[0-9]|mot|hai|ba|bon)\s*(?:nguoi|people)|thang\b", _fold(location)
                ):
                    pass
                elif _fold(location) in {"nha", "day", "cho cu"}:
                    uncertain = True
                else:
                    put("destination", location)

        for pattern, code in (
            (r"\b(?:xe\s*(?:o to\s*)?)?(?:4|bon)\s*cho\b", "oto_4_cho"),
            (r"\b(?:xe\s*(?:o to\s*)?)?(?:7|bay)\s*cho\b", "oto_7_cho"),
            (r"\bxe may\b", "xe_may"),
        ):
            if re.search(pattern, lower) and not re.search(
                r"(?:khong|dung)\s+(?:can\s+)?" + pattern.replace(r"\b", ""), lower
            ):
                if lower.startswith("dung xe") and data.booking_state.vehicle_type.value == code:
                    add("confirm", "vehicle_type")
                else:
                    put("vehicle_type", code)
        if re.search(r"\b(?:16|muoi sau|29|45)\s*cho\b|\blimousine\b", lower):
            uncertain = True

        if re.search(
            r"(?:khong|dung)\s+(?:tra|thanh toan|chon|dung)\s+(?:bang\s+)?tien mat", lower
        ):
            if data.booking_state.payment_method.value == "cash":
                add("deny", "payment_method")
            else:
                uncertain = True
        elif re.search(r"(?:tra|thanh toan|chon|dung)\s+(?:bang\s+)?tien mat|^tien mat", lower):
            put("payment_method", "cash")
        elif re.search(r"the (?:da )?lien ket|the cu", lower):
            put("payment_method", "linked_card")
        elif re.search(r"cong ty tra|tai khoan cong ty", lower):
            put("payment_method", "corporate")
        elif re.search(r"(?:tra|thanh toan).*(?:momo|zalopay|chuyen khoan|vi dien tu)", lower):
            uncertain = True

        if re.search(r"\b(?:ngay bay gio|bay gio|don ngay|di ngay)\b", lower):
            put("pickup_time", "ngay bây giờ")
        else:
            time_matches = list(
                re.finditer(
                    rf"(?:(?:ngay mai|mai|hom nay|chieu nay|toi nay|sang mai|ngay\s+\d{{1,2}}/\d{{1,2}}(?:/\d{{4}})?)\s+(?:luc\s+)?)?(?:\d{{1,2}}:\d{{2}}|{_NUMBER}\s*gio(?:\s*\d{{1,2}}(?:\s*phut)?)?)(?:\s*(?:sang|chieu|toi|trua)(?:\s+nay|\s+mai)?)?",
                    lower,
                )
            )
            relative = re.search(r"(?:\d+\s*phut nua|som hon nua tieng|muon hon nua tieng)", lower)
            if re.search(rf"{_NUMBER}\s*(?:gio\s*)?(?:hoac|hay)\s*{_NUMBER}\s*gio", lower):
                uncertain = True
            elif time_matches:
                match = time_matches[-1]
                put("pickup_time", clause[match.start() : match.end()])
            elif relative:
                if "hon" not in relative.group() or data.booking_state.pickup_time.value:
                    put("pickup_time", clause[relative.start() : relative.end()])
                else:
                    uncertain = True

        if re.search(r"(?:dung|su dung) so (?:dang ky|ho so)|so da dang ky", lower):
            put("contact_phone", "profile:primary")
        phone = re.search(r"(?<!\d)(?:\+?84|0)(?:[ .-]?\d){8,10}(?!\d)", lower)
        if phone:
            put("contact_phone", re.sub(r"[ .-]", "", clause[phone.start() : phone.end()]))
        elif re.search(r"so duoi\s+\d+", lower):
            uncertain = True
        elif re.search(r"(?:dien thoai|goi .+ so|goi so)\s+", lower):
            spoken = re.search(
                r"\bso\s+((?:(?:khong|mot|hai|ba|bon|nam|sau|bay|tam|chin)\s*){9,12})\b", lower
            )
            if spoken:
                digits = {"khong": 0, **_NUMBERS}
                put("contact_phone", "".join(str(digits[word]) for word in spoken.group(1).split()))

        recipient = re.search(
            r"(?:dat (?:ho|cho))\s+(?:chi|anh|co|chu|ban)\s+([a-z ]+?)(?=\s+(?:goi|tai xe|so)|$)",
            lower,
        )
        name = recipient or re.search(
            r"(?:toi(?:\s+ten(?:\s+la)?)?|ten(?:\s+la)?|goi toi la)\s+([a-z ]+)$", lower
        )
        if name and (recipient or "ten" in lower or focus == "contact_name"):
            put("contact_name", clause[name.start(1) : name.end(1)])

        if re.search(r"(?:khong (?:co |mang )?(?:vali|hanh ly)|khong hanh ly)", lower):
            put("luggage", {"count": 0, "size": "none"})
        elif re.search(r"\b(?:vali|hanh ly)\b", lower):
            luggage_matches = list(re.finditer(rf"({_NUMBER})\s+vali\b", lower))
            count = sum(_number(item.group(1)) or 0 for item in luggage_matches) or None
            size = (
                "large"
                if "lon" in lower
                else "cabin"
                if "xach tay" in lower or "cabin" in lower
                else "unknown"
            )
            if "lon" in lower and ("xach tay" in lower or "cabin" in lower):
                size = "mixed"
            put("luggage", {"count": count, "size": size})

        if re.search(r"bo het diem (?:ghe|dung)|khong (?:co )?diem (?:ghe|dung)", lower):
            put("stops", [])
        elif re.search(r"dao hai diem ghe|dao thu tu", lower):
            old_stops = data.booking_state.stops.value
            if old_stops and len(old_stops) == 2:
                put("stops", list(reversed(old_stops)))
            else:
                uncertain = True
        else:
            removed_stop = re.search(r"\bbo\s+(.+)$", lower)
            existing_stops = values.get("stops", data.booking_state.stops.value)
            if removed_stop and existing_stops:
                removed = _fold(_clean(clause[removed_stop.start(1) : removed_stop.end(1)]))
                matches = [item for item in existing_stops if _fold(item) == removed]
                if len(matches) == 1:
                    put("stops", [item for item in existing_stops if item != matches[0]])
            stop = re.search(r"\bghe(?: them)?\s+(.+?)(?:\s+sau\s+(.+))?$", lower)
            if stop and clause[stop.start() : stop.start() + 3].casefold() == "ghế":
                stop = None
            if stop:
                place = _clean(clause[stop.start(1) : stop.end(1)])
                old_stops = values.get("stops", data.booking_state.stops.value)
                if "them" in stop.group() and old_stops is None:
                    uncertain = True
                elif "them" in stop.group() and old_stops and not stop.group(2):
                    uncertain = True
                elif stop.group(2) and old_stops:
                    previous = _fold(_clean(clause[stop.start(2) : stop.end(2)]))
                    matching = [i for i, item in enumerate(old_stops) if _fold(item) == previous]
                    if len(matching) == 1:
                        updated = list(old_stops)
                        updated.insert(matching[0] + 1, place)
                        put("stops", updated)
                    else:
                        uncertain = True
                else:
                    put("stops", [place])

        requests = list(
            values.get("special_requests", data.booking_state.special_requests.value or [])
        )
        requests_changed = False
        for marker, code in (
            ("ghe tre em", "child_seat"),
            ("xe lan", "wheelchair_access"),
            ("thu cung", "pet"),
        ):
            if marker in lower:
                requests_changed = True
                if re.search(r"(?:khong can|bo|khong mang).+" + marker, lower):
                    requests = [item for item in requests if item != code]
                elif code not in requests:
                    requests.append(code)
                if marker == "ghe tre em" and re.search(rf"(?:hai|ba|[2-9])\s+{marker}", lower):
                    uncertain = True
        if requests_changed:
            put("special_requests", requests)
        elif re.search(r"khong (?:co |can )?yeu cau(?: gi)?(?: them)?", lower):
            put("special_requests", [])

        if re.search(r"(?:toi (?:dung|mac)|ao (?:xanh|do|vang)|goi khi den|cho o cong)", lower):
            put("pickup_note", clause.strip())

    # Independent luggage counts in separate clauses describe the combined load.
    luggage_texts = [
        _fold(clause)
        for clause in clauses
        if "vali" in _fold(clause) and not re.search(_QUESTION + r"|\bneu\b", _fold(clause))
    ]
    luggage_text = ", ".join(luggage_texts)
    luggage_counts = list(re.finditer(rf"({_NUMBER})\s+vali\b", luggage_text))
    if len(luggage_counts) > 1 and not re.search(r"\b(?:khong|doi|nham|thay|a)\b", luggage_text):
        has_large = "lon" in luggage_text
        has_cabin = "xach tay" in luggage_text or "cabin" in luggage_text
        size = (
            "mixed"
            if has_large and has_cabin
            else "large"
            if has_large
            else "cabin"
            if has_cabin
            else "unknown"
        )
        put(
            "luggage",
            {"count": sum(_number(item.group(1)) or 0 for item in luggage_counts), "size": size},
        )

    # Count the people together across clauses, but distinguish added/included children.
    passenger_matches = list(
        re.finditer(rf"({_NUMBER})\s*(?:nguoi(?: lon)?|hanh khach|people)\b", folded)
    )
    passenger_matches = [
        item
        for item in passenger_matches
        if not re.search(
            _QUESTION + r"|\bneu\b",
            folded[
                max(folded.rfind(",", 0, item.start()), folded.rfind(";", 0, item.start()))
                + 1 : min(
                    [
                        index
                        for index in (
                            folded.find(",", item.end()),
                            folded.find(";", item.end()),
                            len(folded),
                        )
                        if index >= 0
                    ]
                )
            ],
        )
    ]
    babies = list(re.finditer(rf"({_NUMBER})\s*(?:be|tre em|con)\b", folded))
    if re.search(rf"{_NUMBER}\s*(?:den|hoac|hay)\s*{_NUMBER}\s*nguoi", folded):
        uncertain = True
    elif passenger_matches:
        # A question about capacity does not supply a chosen passenger total.
        first = passenger_matches[0]
        if not re.search(r"\bneu\b", folded[: first.start()]):
            count = _number(first.group(1)) or 1
            if babies and not re.search(r"(?:gom|trong do|co ca|bao gom)", folded):
                count += sum(_number(item.group(1)) or 0 for item in babies)
            put("passengers", count)
    elif re.search(rf"toi va\s+({_NUMBER})\s*(?:ban|nguoi ban|con)\b", folded):
        match = re.search(rf"toi va\s+({_NUMBER})\s*(?:ban|nguoi ban|con)\b", folded)
        put("passengers", 1 + (_number(match.group(1)) or 0))

    # Short responses are interpreted only with the question the app supplied.
    if re.search(r"(?:pizza|thoi tiet|chung khoan|viet code)", folded):
        add("out_of_scope", value=text)
    if not values and not acts and not uncertain:
        count_match = re.fullmatch(rf"({_NUMBER})(?:\s+(?:cai|nguoi|nhe))?[.!]?", folded)
        short_count = _number(count_match.group(1)) if count_match else None
        bot = _fold(context.last_bot_message or "")
        if short_count and focus == "passengers" and not ("vali" in bot or "hanh ly" in bot):
            put("passengers", short_count)
        elif short_count and focus == "luggage":
            old = data.booking_state.luggage.value
            put(
                "luggage",
                {
                    "count": short_count,
                    "size": old.size if old and old.size != "none" else "unknown",
                },
            )
        elif short_count and focus == "pickup_time":
            put("pickup_time", f"{short_count} giờ")
        elif focus == "pickup_time" and folded in {
            "sang",
            "buoi sang",
            "chieu",
            "buoi chieu",
            "toi",
        }:
            old = data.booking_state.pickup_time.value
            if old:
                put("pickup_time", f"{old} {text}")
            else:
                uncertain = True
        elif re.match(_AFFIRM, folded) and not re.search(
            r"diem don|diem den|dia chi|dung xe", folded
        ):
            confirmation_actions = {
                "confirm_booking_info",
                "confirm_booking",
                "confirm_amendment",
                "confirm_cancellation",
            }
            if context.last_bot_action in confirmation_actions:
                target = focus if context.last_bot_action == "confirm_booking_info" else None
                if target is None or getattr(data.booking_state, target).value is not None:
                    add("confirm", target)
                else:
                    uncertain = True
            else:
                uncertain = True
        elif re.fullmatch(r"(?:khong|chua dung|sai roi|khong phai)(?:[.!])?", folded):
            add("deny", focus)
        elif focus in {"pickup", "destination", "contact_name", "pickup_note"} and not re.search(
            r"\b(?:huy|dung|neu|doi)\b", folded
        ):
            put(focus, text)

    # Partial confirmation and uncertainty never become blanket consent.
    for marker, target in (
        (r"(?:dung\s+(?:diem don|dia chi)|(?:diem don|dia chi)\s+dung)", "pickup"),
        (r"(?:dung\s+diem den|diem den\s+dung)", "destination"),
        (r"(?:dung\s+xe|xe\s+.+\s+dung)", "vehicle_type"),
    ):
        if (
            re.search(marker, folded)
            and target not in values
            and getattr(data.booking_state, target).value is not None
        ):
            add("confirm", target)
    if (
        re.search(r"(?:gio.+chua chac|chua chac.+gio)", folded)
        and data.booking_state.pickup_time.value
    ):
        add("deny", "pickup_time")
    if re.search(r"khong can ghi chu", folded):
        add("deny", "pickup_note")
    if (
        re.match(_AFFIRM, folded)
        and context.last_bot_action
        in {"confirm_booking_info", "confirm_booking", "confirm_amendment", "confirm_cancellation"}
        and not re.search(
            r"diem don|diem den|dia chi|dung xe|\bneu\b|chi khi|dung dat|chua dat", folded
        )
    ):
        target = focus if context.last_bot_action == "confirm_booking_info" else None
        if target not in values and (target is not None or not values):
            add("confirm", target)
    if re.search(r"\b(?:neu|mien la|chi khi)\b", folded) and not any(
        item["intent"] == "ask_question" for item in acts
    ):
        uncertain = True
    if re.search(r"(?:gap|noi chuyen voi).*(?:nhan vien|tong dai)|dat ho me|dat cho me", folded):
        uncertain = True

    for target, value in values.items():
        previous = getattr(data.booking_state, target).value
        if hasattr(previous, "model_dump"):
            previous = previous.model_dump()
        intent = "provide_info" if previous is None or previous == value else "change_info"
        add(intent, target, value)
    if uncertain:
        add("no_understanding")
    if not acts:
        if re.search(r"^(?:xin chao|chao|hello|cam on|thanks)\b", folded):
            add("chit_chat", value=text)
        elif re.search(r"(?:pizza|thoi tiet|chung khoan|viet code)", folded):
            add("out_of_scope", value=text)
        else:
            add("no_understanding")
    return NluResult.model_validate({"speech_status": "clear", "dialogue_acts": acts})
