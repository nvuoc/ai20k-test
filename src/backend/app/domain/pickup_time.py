"""Resolve a requested pickup time once, using the customer's turn as its anchor."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Ho_Chi_Minh")
ASAP = {"ngay", "ngay bay gio", "bay gio", "di ngay", "don ngay", "asap", "now", "hien tai", "lap tuc"}
NUMBERS = {"mot": 1, "hai": 2, "ba": 3, "bon": 4, "nam": 5, "sau": 6,
           "bay": 7, "tam": 8, "chin": 9, "muoi": 10, "muoi mot": 11,
           "muoi hai": 12, "muoi ba": 13, "muoi bon": 14, "muoi lam": 15,
           "muoi sau": 16, "muoi bay": 17, "muoi tam": 18, "muoi chin": 19,
           "hai muoi": 20, "hai muoi mot": 21, "hai muoi hai": 22, "hai muoi ba": 23}
for tens_word, tens in (("hai", 20), ("ba", 30), ("bon", 40), ("nam", 50)):
    NUMBERS[tens_word + " muoi"] = tens
    for unit_word, unit in (("mot", 1), ("hai", 2), ("ba", 3), ("bon", 4), ("lam", 5),
                            ("sau", 6), ("bay", 7), ("tam", 8), ("chin", 9)):
        NUMBERS[tens_word + " muoi " + unit_word] = tens + unit
NUMBER = r"(?:\d+|" + "|".join(sorted(NUMBERS, key=len, reverse=True)) + ")"
CLOCK = rf"\b(?:(\d+)(:|h)(\d{{1,2}})?|({NUMBER})\s*gio(?:\s*({NUMBER})(?:\s*phut)?|\s*(ruoi))?)\b"
DATE = (
    r"\b(?:ngay mai|ngay kia|hom nay|hom qua|(?:sang|chieu|toi|trua) (?:mai|nay)|"
    r"(?:ngay\s+)?\d{1,2}[/-]\d{1,2}(?:[/-]\d{4})?|\d{4}-\d{2}-\d{2}|"
    r"ngay \d{1,2} thang \d{1,2}(?: nam \d{4})?|"
    r"thu (?:hai|ba|tu|nam|sau|bay|[2-7])(?: tuan (?:nay|sau))?|chu nhat(?: tuan (?:nay|sau))?)\b"
)
UNRESOLVED_DATE = r"\b(?:tuan (?:sau|toi)|thang (?:sau|toi)|ngay \d{1,2}|mung \d{1,2}|hom khac)\b"
DURATION = (
    rf"(?:(?:sau|som hon|muon hon)\s+)?(?:{NUMBER}\s*(?:gio|tieng)(?:\s*{NUMBER}\s*phut|\s*ruoi)?|"
    rf"{NUMBER}\s*phut|nua (?:gio|tieng))(?:\s*nua)?"
)


def fold(text):
    return "".join(c for c in unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
                   if not unicodedata.combining(c))


def number(text):
    return int(text) if text.isdigit() else NUMBERS[text]


def time_expression(text: str) -> str | None:
    """Bounded fixture extraction; the live interpreter still supplies raw slot text."""
    key = fold(text)
    # "Bây giờ" is an immediate request, distinct from the spoken number "bảy giờ".
    def mask_now(match):
        literal = text[match.start():match.end()].lower()
        if "bảy" in literal or re.match(r"\s+(?:sang|chieu|toi|trua)\b", key[match.end():]):
            return match.group()
        return " " * len(match.group())

    key = re.sub(r"\bbay gio\b", mask_now, key)
    relative = re.search(DURATION, key)
    if relative and re.search(r"\b(?:sau|nua|som hon|muon hon)\b", relative.group()):
        return text[relative.start():relative.end()]
    iso = re.search(r"\d{4}-\d{2}-\d{2}[tT ]\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2}|[zZ])?", text)
    if iso:
        return iso.group()
    clock = re.search(CLOCK, key)
    dates = []
    for match in re.finditer(DATE, key):
        prefix = key[:match.start()]
        if re.search(r"\b(?:cai|chon|duong|pho|ngo|hem|nha)\s*$", prefix):
            continue
        if match.group()[0].isdigit() and (prefix.endswith("/") or key[match.end():].startswith("/")):
            continue
        if not clock and match.group()[0].isdigit() and key.strip(" .!?") != match.group():
            continue
        dates.append(match)
    if not dates:
        dates = list(re.finditer(UNRESOLVED_DATE, key))
    if not clock:
        return text[dates[0].start():dates[-1].end()] if dates else None
    start, end = clock.span()
    period = re.match(r"\s*(?:sang|chieu|toi|trua|dem|am|pm)(?:\s*(?:nay|mai))?\b", key[end:])
    if period:
        end += period.end()
    if dates:
        start, end = min(start, dates[0].start()), max(end, dates[-1].end())
    return text[start:end]


def parse_pickup_time(raw: str, reference_at: float, *, previous_pickup_at=None) -> dict:
    result = {"raw": raw, "reference_at": reference_at, "timezone": ZONE.key,
              "status": "needs_clarification", "mode": None, "pickup_at": None,
              "label": None, "reason": None, "message": None}
    key = fold(raw).strip(" .!?")
    local = datetime.fromtimestamp(reference_at, ZONE)

    def unclear(reason, message):
        result.update(reason=reason, message=message)
        return result

    def resolved(target):
        if target.timestamp() <= reference_at:
            return unclear("PAST_TIME", "Thời điểm đón này đã qua. Bạn cho mình ngày và giờ đón trong tương lai nhé.")
        result.update(status="valid", mode="scheduled", pickup_at=target.isoformat(),
                      label=target.strftime("%H:%M ngày %d/%m/%Y") + " (giờ Việt Nam)")
        return result

    if key in ASAP and not re.search(r"bảy\s+giờ", raw.lower()):
        result.update(status="valid", mode="asap", label="Ngay bây giờ")
        return result
    if re.search(r"\b(?:hoac|hay|khoang)\b|\btam\s+(?!(?:gio|tieng)\b)|\d+\s*[-–]\s*\d+\s*gio", key):
        return unclear("AMBIGUOUS_TIME", "Bạn muốn đón chính xác vào ngày nào, lúc mấy giờ?")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[tT ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[+-]\d{2}:\d{2}|[zZ])?", raw.strip()):
            target = datetime.fromisoformat(raw.strip().replace("Z", "+00:00").replace("z", "+00:00"))
            return resolved(target.replace(tzinfo=ZONE) if not target.tzinfo else target.astimezone(ZONE))
    except ValueError:
        return unclear("INVALID_TIME", "Ngày hoặc giờ chưa hợp lệ. Bạn cho mình ngày và giờ cụ thể nhé.")
    relative = re.fullmatch(DURATION, key)
    if relative and re.search(r"\b(?:sau|nua|som hon|muon hon)\b", key):
        if re.search(DATE, key):
            return unclear("AMBIGUOUS_TIME", "Bạn muốn đón sau bao lâu kể từ bây giờ, hay vào một ngày và giờ cụ thể?")
        hours = re.search(rf"({NUMBER})\s*(?:gio|tieng)\b", key)
        minutes = re.search(rf"({NUMBER})\s*phut\b", key)
        delay = (number(hours[1]) * 60 if hours else 0) + (number(minutes[1]) if minutes else 0)
        if "nua gio" in key or "nua tieng" in key or "ruoi" in key:
            delay += 30
        if delay <= 0:
            return unclear("INVALID_DURATION", "Khoảng chờ phải lớn hơn 0. Bạn muốn đi ngay hay đón sau bao lâu?")
        anchor = local
        if "som hon" in key or "muon hon" in key:
            if not previous_pickup_at:
                return unclear("MISSING_REFERENCE", "Bạn cho mình ngày và giờ đón cụ thể trước khi điều chỉnh sớm hoặc muộn hơn nhé.")
            anchor = datetime.fromisoformat(previous_pickup_at).astimezone(ZONE)
        try:
            return resolved(anchor + timedelta(minutes=-delay if "som hon" in key else delay))
        except OverflowError:
            return unclear("INVALID_DURATION", "Khoảng chờ quá lớn. Bạn cho mình ngày và giờ đón cụ thể nhé.")

    if re.search(r"\bnua\b", key) and re.search(DATE, key):
        return unclear("AMBIGUOUS_TIME", "Bạn muốn đón sau bao lâu kể từ bây giờ, hay vào một ngày và giờ cụ thể?")
    day = local.date()
    explicit = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?\b", key)
    written = re.search(r"\bngay (\d{1,2}) thang (\d{1,2})(?: nam (\d{4}))?\b", key)
    iso_date = re.search(r"\b\d{4}-\d{2}-\d{2}\b", key)
    weekday = re.search(r"\bthu (hai|ba|tu|nam|sau|bay|[2-7])\b|\bchu nhat\b", key)
    if re.search(UNRESOLVED_DATE, key) and not (explicit or written or iso_date or weekday):
        return unclear("MISSING_DATE", "Bạn cho mình ngày đón cụ thể theo dạng DD/MM/YYYY và giờ đón nhé.")
    try:
        if iso_date:
            day = datetime.strptime(iso_date.group(), "%Y-%m-%d").date()
        elif explicit or written:
            match = explicit or written
            day = day.replace(year=int(match[3] or day.year), month=int(match[2]), day=int(match[1]))
        elif "ngay kia" in key:
            day += timedelta(days=2)
        elif re.search(r"\bmai\b", key):
            day += timedelta(days=1)
        elif "hom qua" in key:
            day -= timedelta(days=1)
        else:
            if weekday:
                names = {"hai": 0, "ba": 1, "tu": 2, "nam": 3, "sau": 4, "bay": 5}
                code = weekday[1]
                wanted = 6 if not code else int(code) - 2 if code.isdigit() else names[code]
                if "tuan sau" in key:
                    day += timedelta(days=7 - day.weekday() + wanted)
                elif "tuan nay" in key:
                    day += timedelta(days=wanted - day.weekday())
                else:
                    day += timedelta(days=(wanted - day.weekday()) % 7)
    except (ValueError, OverflowError):
        return unclear("INVALID_DATE", "Ngày đón chưa hợp lệ. Bạn cho mình ngày theo dạng DD/MM/YYYY nhé.")
    result["date"] = day.isoformat()
    clocks = list(re.finditer(CLOCK, key))
    if len(clocks) != 1:
        return unclear("MISSING_TIME" if not clocks else "AMBIGUOUS_TIME",
                       f"Bạn muốn đón ngày {day.strftime('%d/%m/%Y')} lúc mấy giờ?" if not clocks
                       else "Bạn muốn đón vào giờ nào? Hãy chọn một thời điểm cụ thể nhé.")
    match = clocks[0]
    hour = number(match[1] or match[4])
    minute = int(match[3] or 0) if match[2] else number(match[5]) if match[5] else 30 if match[6] else 0
    if hour > 23 or minute > 59:
        return unclear("INVALID_TIME", "Giờ đón chưa hợp lệ. Bạn cho mình giờ theo dạng HH:MM nhé.")
    period = re.search(r"\b(sang|chieu|toi|trua|dem|am|pm)\b", key)
    if period:
        if period[1] in {"sang", "am"}:
            if hour > 12:
                return unclear("INVALID_TIME", "Giờ đón và buổi sáng chưa khớp. Bạn cho mình giờ cụ thể nhé.")
            hour = hour % 12
        elif period[1] in {"chieu", "toi", "trua", "pm"} and hour < 12:
            hour += 12
    elif not match[2] and 1 <= hour <= 12:
        return unclear("MISSING_PERIOD", f"Bạn muốn đón lúc {hour} giờ sáng hay chiều/tối? Bạn có thể ghi giờ theo dạng HH:MM.")
    return resolved(datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE))


def merge_time_clarification(previous: dict | None, value: str) -> tuple[str, float | None]:
    """Keep the original day when a later turn supplies only the missing hour/buổi."""
    if not previous or previous.get("status") == "valid":
        return value, None
    old = previous["raw"]
    key = fold(value)
    if fold(value).startswith(fold(old)):
        return value, previous["reference_at"]
    if key in ASAP or re.search(DATE, key) or re.search(r"\b(?:sau|nua|som hon|muon hon)\b", key):
        return value, None
    if previous.get("reason") == "MISSING_TIME" and re.search(CLOCK, key):
        return old + " " + value, previous["reference_at"]
    if previous.get("reason") == "MISSING_PERIOD":
        if key in {"sang", "buoi sang", "chieu", "buoi chieu", "toi", "buoi toi"}:
            return old + " " + value, previous["reference_at"]
        if re.search(CLOCK, key) and previous.get("date"):
            return value + " ngay " + datetime.fromisoformat(previous["date"]).strftime("%d/%m/%Y"), previous["reference_at"]
    return value, None
