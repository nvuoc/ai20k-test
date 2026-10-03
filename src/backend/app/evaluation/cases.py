"""Independent annotated turns covering demo capabilities and negative controls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.contracts.nlu import NluInput, NluResult, empty_booking_state


def act(intent: str, target: str | None = None, value: Any = None) -> dict:
    return {"intent": intent, "target": target, "value": value}


def provide(target: str, value: Any) -> dict:
    return act("provide_info", target, value)


@dataclass(frozen=True)
class ExtractionCase:
    case_id: str
    text: str
    expected: list[dict]
    slots: dict[str, Any] = field(default_factory=dict)
    focus: str | None = None
    bot: str | None = None
    action: str | None = None
    candidates: list[dict] = field(default_factory=list)
    status: str = "collecting_info"

    def projection(self) -> NluInput:
        state = empty_booking_state().model_dump()
        for target, value in self.slots.items():
            state[target]["value"] = value
        return NluInput.model_validate(
            {
                "utterance": {"text": self.text, "asr_confidence": None},
                "conversation_context": {
                    "last_bot_message": self.bot,
                    "last_bot_action": self.action,
                    "current_focus": self.focus,
                },
                "booking_state": state,
                "candidates": self.candidates,
                "booking_status": self.status,
            }
        )

    def expected_result(self) -> NluResult:
        return NluResult.model_validate({"speech_status": "clear", "dialogue_acts": self.expected})


CANDIDATES = [
    {
        "candidate_id": "d1",
        "candidate_set_id": "destination-r1",
        "target": "destination",
        "ordinal": 1,
        "label": "Vincom Bà Triệu",
    },
    {
        "candidate_id": "d2",
        "candidate_set_id": "destination-r1",
        "target": "destination",
        "ordinal": 2,
        "label": "Vincom Trần Duy Hưng",
    },
]

CASES = [
    ExtractionCase(
        "F069",
        "Một vali lớn, một vali xách tay",
        [provide("luggage", {"count": 2, "size": "mixed"})],
    ),
    ExtractionCase(
        "F070",
        "Đúng xe 4 chỗ, nhưng đổi điểm đón sang B",
        [act("confirm", "vehicle_type"), act("change_info", "pickup", "B")],
        slots={"vehicle_type": "oto_4_cho", "pickup": "A"},
        action="confirm_booking_info",
        focus="vehicle_type",
    ),
    ExtractionCase(
        "F071",
        "Không dùng tiền mặt",
        [act("deny", "payment_method")],
        slots={"payment_method": "cash"},
    ),
    ExtractionCase("F072", "Không dùng tiền mặt", [act("no_understanding")]),
    ExtractionCase(
        "F067",
        "Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, đi ngay, 2 người, xe 4 chỗ, số 0901234567.",
        [
            provide("pickup", "Nhà hát Lớn Hà Nội"),
            provide("destination", "Ga Hà Nội"),
            provide("pickup_time", "ngay bây giờ"),
            provide("passengers", 2),
            provide("vehicle_type", "oto_4_cho"),
            provide("contact_phone", "0901234567"),
        ],
    ),
    ExtractionCase(
        "F068",
        "Đón ở Nhà hát Lớn Hà Nội, đến Trường Sao Mai, đi ngay, 2 người, xe 4 chỗ, số 0901234567",
        [
            provide("pickup", "Nhà hát Lớn Hà Nội"),
            provide("destination", "Trường Sao Mai"),
            provide("pickup_time", "ngay bây giờ"),
            provide("passengers", 2),
            provide("vehicle_type", "oto_4_cho"),
            provide("contact_phone", "0901234567"),
        ],
    ),
    ExtractionCase("F001", "Đón ở 36 Hoàng Cầu", [provide("pickup", "36 Hoàng Cầu")]),
    ExtractionCase("F002", "Đi Vincom", [provide("destination", "Vincom")]),
    ExtractionCase("F003", "Đón ở A, đến B", [provide("pickup", "A"), provide("destination", "B")]),
    ExtractionCase(
        "F004",
        "Từ A qua B rồi đến C",
        [provide("pickup", "A"), provide("stops", ["B"]), provide("destination", "C")],
    ),
    ExtractionCase(
        "F005",
        "Đổi điểm đón sang B",
        [act("change_info", "pickup", "B")],
        slots={"pickup": "A"},
        focus="pickup_time",
    ),
    ExtractionCase("F006", "Đón ở A", [provide("pickup", "A")], slots={"pickup": "A"}),
    ExtractionCase("F007", "Đón ở đây", [act("no_understanding")]),
    ExtractionCase("F008", "Hai người", [provide("passengers", 2)]),
    ExtractionCase("F009", "Hai người lớn, một bé", [provide("passengers", 3)]),
    ExtractionCase("F010", "Hai người, gồm một bé", [provide("passengers", 2)]),
    ExtractionCase("F011", "Tôi và hai bạn đi", [provide("passengers", 3)]),
    ExtractionCase("F012", "Ba đến bốn người", [act("no_understanding")]),
    ExtractionCase("F013", "Xe bảy chỗ nhé", [provide("vehicle_type", "oto_7_cho")]),
    ExtractionCase("F014", "Xe 4 chỗ", [provide("vehicle_type", "oto_4_cho")]),
    ExtractionCase("F015", "Xe máy", [provide("vehicle_type", "xe_may")]),
    ExtractionCase(
        "F016",
        "Đổi sang xe 7 chỗ nhé",
        [act("change_info", "vehicle_type", "oto_7_cho")],
        slots={"vehicle_type": "oto_4_cho"},
    ),
    ExtractionCase(
        "F017",
        "Xe 7 chỗ thì bao nhiêu?",
        [act("ask_question", value="Xe 7 chỗ thì bao nhiêu?")],
        slots={"vehicle_type": "oto_4_cho"},
    ),
    ExtractionCase("F018", "Đón ở A, xe 16 chỗ", [provide("pickup", "A"), act("no_understanding")]),
    ExtractionCase("F019", "Đón ngay", [provide("pickup_time", "ngay bây giờ")]),
    ExtractionCase("F020", "Ngày mai 8 giờ sáng", [provide("pickup_time", "Ngày mai 8 giờ sáng")]),
    ExtractionCase("F021", "15 phút nữa", [provide("pickup_time", "15 phút nữa")]),
    ExtractionCase("F022", "4 giờ", [provide("pickup_time", "4 giờ")]),
    ExtractionCase(
        "F023",
        "3 giờ, à 4 giờ chiều",
        [act("change_info", "pickup_time", "4 giờ chiều")],
        slots={"pickup_time": "15:00"},
    ),
    ExtractionCase(
        "F024", "3 hoặc 4 giờ", [act("no_understanding")], slots={"pickup_time": "15:00"}
    ),
    ExtractionCase(
        "F025",
        "Không hủy, đổi thành 5 giờ chiều",
        [act("change_info", "pickup_time", "5 giờ chiều")],
        slots={"pickup_time": "15:00"},
    ),
    ExtractionCase("F026", "Hai vali lớn", [provide("luggage", {"count": 2, "size": "large"})]),
    ExtractionCase("F027", "Có vali lớn", [provide("luggage", {"count": None, "size": "large"})]),
    ExtractionCase("F028", "Hai vali", [provide("luggage", {"count": 2, "size": "unknown"})]),
    ExtractionCase("F029", "Không hành lý", [provide("luggage", {"count": 0, "size": "none"})]),
    ExtractionCase(
        "F030", "Một vali xách tay", [provide("luggage", {"count": 1, "size": "cabin"})]
    ),
    ExtractionCase(
        "F031",
        "Hai cái",
        [act("change_info", "luggage", {"count": 2, "size": "large"})],
        slots={"luggage": {"count": None, "size": "large"}},
        focus="luggage",
        bot="Mình có mấy vali lớn?",
        action="ask_slot",
    ),
    ExtractionCase("F032", "Tôi trả tiền mặt", [provide("payment_method", "cash")]),
    ExtractionCase(
        "F033", "Nhận tiền mặt không?", [act("ask_question", value="Nhận tiền mặt không?")]
    ),
    ExtractionCase(
        "F034",
        "Đổi sang thẻ đã liên kết",
        [act("change_info", "payment_method", "linked_card")],
        slots={"payment_method": "cash"},
    ),
    ExtractionCase("F035", "Dùng số đăng ký", [provide("contact_phone", "profile:primary")]),
    ExtractionCase("F036", "Gọi số 0900000000", [provide("contact_phone", "0900000000")]),
    ExtractionCase("F037", "Gọi số đuôi 1234", [act("no_understanding")]),
    ExtractionCase("F038", "Tôi tên Lan", [provide("contact_name", "Lan")]),
    ExtractionCase(
        "F039",
        "Tôi đứng cổng chính áo xanh",
        [provide("pickup_note", "Tôi đứng cổng chính áo xanh")],
    ),
    ExtractionCase("F040", "Không có điểm dừng", [provide("stops", [])]),
    ExtractionCase(
        "F041",
        "Đảo hai điểm ghé",
        [act("change_info", "stops", ["B", "A"])],
        slots={"stops": ["A", "B"]},
    ),
    ExtractionCase(
        "F042",
        "Ghé thêm B sau A",
        [act("change_info", "stops", ["A", "B"])],
        slots={"stops": ["A"]},
    ),
    ExtractionCase("F043", "Ghé thêm C", [act("no_understanding")], slots={"stops": ["A", "B"]}),
    ExtractionCase(
        "F044", "Cần xe hỗ trợ xe lăn", [provide("special_requests", ["wheelchair_access"])]
    ),
    ExtractionCase(
        "F045",
        "Cần hai ghế trẻ em",
        [provide("special_requests", ["child_seat"]), act("no_understanding")],
    ),
    ExtractionCase("F046", "Mang thú cưng", [provide("special_requests", ["pet"])]),
    ExtractionCase("F047", "Không có yêu cầu thêm", [provide("special_requests", [])]),
    ExtractionCase("F048", "Hủy chuyến giúp tôi", [act("cancel")], status="booked"),
    ExtractionCase(
        "F049",
        "Hủy có mất phí không?",
        [act("ask_question", value="Hủy có mất phí không?")],
        status="booked",
    ),
    ExtractionCase(
        "F050", "Nếu dưới 200 nghìn thì đặt", [act("no_understanding")], action="confirm_booking"
    ),
    ExtractionCase("F051", "Chờ chút, đừng đặt vội", [act("deny")], action="confirm_booking"),
    ExtractionCase(
        "F052",
        "Đúng rồi đặt giúp tôi",
        [act("confirm")],
        action="confirm_booking",
        status="awaiting_confirmation",
    ),
    ExtractionCase(
        "F053",
        "Đúng điểm đón thôi",
        [act("confirm", "pickup")],
        slots={"pickup": "A"},
        action="confirm_booking",
    ),
    ExtractionCase(
        "F054",
        "Đúng rồi",
        [act("confirm", "pickup")],
        slots={"pickup": "A"},
        focus="pickup",
        action="confirm_booking_info",
        bot="Đón A đúng không?",
    ),
    ExtractionCase(
        "F055",
        "Ừ",
        [act("no_understanding")],
        focus="destination",
        action="ask_slot",
        bot="Mình đến đâu?",
    ),
    ExtractionCase(
        "F056",
        "Hai",
        [provide("passengers", 2)],
        focus="passengers",
        action="ask_slot",
        bot="Mình đi mấy người?",
    ),
    ExtractionCase(
        "F057",
        "Hai",
        [act("no_understanding")],
        focus="passengers",
        action="ask_slot",
        bot="Mấy người và mấy vali?",
    ),
    ExtractionCase(
        "F058", "Cái thứ hai", [act("select_candidate", "destination", "d2")], candidates=CANDIDATES
    ),
    ExtractionCase(
        "F059",
        "Không phải cái nào cả",
        [act("reject_candidate", "destination")],
        candidates=CANDIDATES,
    ),
    ExtractionCase("F060", "Cái thứ hai", [act("no_understanding")]),
    ExtractionCase("F061", "Nhắc lại giúp tôi", [act("request_repeat")]),
    ExtractionCase("F062", "Xin chào", [act("chit_chat", value="Xin chào")]),
    ExtractionCase("F063", "Đặt pizza", [act("out_of_scope", value="Đặt pizza")]),
    ExtractionCase(
        "F064",
        "Đón ở A, hai người, có vali lớn",
        [
            provide("pickup", "A"),
            provide("passengers", 2),
            provide("luggage", {"count": None, "size": "large"}),
        ],
    ),
    ExtractionCase(
        "F065",
        "Địa chỉ đúng, đổi thành 4 giờ chiều",
        [act("confirm", "pickup"), act("change_info", "pickup_time", "4 giờ chiều")],
        slots={"pickup": "A", "pickup_time": "15:00"},
        action="confirm_booking_info",
        focus="pickup_time",
    ),
    ExtractionCase(
        "F066",
        "Đúng rồi, tổng bao nhiêu?",
        [act("confirm"), act("ask_question", value="tổng bao nhiêu?")],
        action="confirm_booking",
    ),
]
