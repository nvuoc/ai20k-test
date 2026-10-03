"""Bounded Vietnamese address normalization with raw wording retained."""

import re

from app.contracts.location import ParsedLocation
from app.contracts.maps import search_key


def parse_location(raw: str) -> ParsedLocation:
    query = re.sub(r"\bđối điện\b", "đối diện", raw, flags=re.I)
    query = re.sub(r"\buỷ bạn\b|\bủy bạn\b", "ủy ban", query, flags=re.I)
    query = re.sub(r"\bUBND\b", "ủy ban nhân dân", query, flags=re.I)
    query = re.sub(r"\bP\.\s*(?=\d)", "phường ", query, flags=re.I)
    query = re.sub(r"\bQ\.\s*(?=\d)", "quận ", query, flags=re.I)
    key = search_key(query)
    house = re.match(r"^(?:(?:nha|so)\s+)?(\d+[a-z]?(?:/\d+[a-z]?)*)(?=\s|$)", key)
    alleys = re.findall(r"\b(?:ngo|ngach|hem)\s+\d+[a-z]?(?:/\d+[a-z]?)*", key)
    relation = "none"
    anchor = None
    for pattern, kind in (
        (r"^\s*(?:ở |tại )?gần\s+(.+)", "near"),
        (r"^\s*(?:ở |tại )?đối diện\s+(.+)", "opposite"),
        (r"^\s*(?:ở |tại )?cạnh\s+(.+)", "adjacent"),
        (r"^\s*(?:ở |tại )?(?:phía sau|đằng sau|sau)\s+(.+)", "behind"),
        (r"^\s*bên trái\s+(.+)", "left_of"),
        (r"^\s*bên phải\s+(.+)", "right_of"),
        (r"^\s*giữa\s+(.+)", "between"),
    ):
        match = re.match(pattern, query, re.I)
        if match:
            relation, anchor = kind, match[1].strip()
            break
    # Also support unaccented speech transcripts, without losing the raw form.
    if relation == "none":
        match = re.match(
            r"^(gan|doi dien|canh|phia sau|dang sau|ben trai|ben phai|giua)\s+(.+)$", key
        )
        if match:
            relation = {
                "gan": "near",
                "doi dien": "opposite",
                "canh": "adjacent",
                "phia sau": "behind",
                "dang sau": "behind",
                "ben trai": "left_of",
                "ben phai": "right_of",
                "giua": "between",
            }[match[1]]
            anchor = match[2]
    qualifier = re.search(r"\b(?:cổng|cửa|sảnh|ga)\s+[^,;]+", query, re.I)
    uncertainty = [
        match[0]
        for match in re.finditer(r"\b(?:hinh nhu|co le|khong chac|khong nho|hoac|hay)\b", key)
    ]
    return ParsedLocation(
        raw_text=raw,
        normalized_query=query,
        house_number=house[1] if house else None,
        alley_path=alleys,
        anchor_name=anchor,
        relation=relation,
        qualifier=qualifier[0] if qualifier else None,
        uncertainties=uncertainty,
    )


async def resolve_relation(adapter, parsed: ParsedLocation, target, context):
    """Look up anchors first, while keeping the operational point unresolved."""
    from app.contracts.maps import resolution

    names = (
        re.split(r"\s+(?:và|va)\s+", parsed.anchor_name or "", maxsplit=1)
        if parsed.relation == "between"
        else [parsed.anchor_name]
    )
    anchors = []
    for name in names:
        if not name:
            continue
        result = await adapter.resolve(name, target, context)
        if result.get("place"):
            anchors.append(result["place"])
        else:
            anchors.extend(result.get("candidates", [])[:3])
    labels = "; ".join(a["label"] for a in anchors)
    message = (
        f"Mình tìm được mốc {labels}. " if labels else "Mình chưa xác định chắc địa danh mốc. "
    )
    message += "Bạn cho mình địa chỉ hoặc tên cổng và phía đường nơi bạn muốn hẹn đón nhé."
    return resolution(
        "ambiguous",
        parsed.raw_text,
        target,
        reason="RELATION_UNRESOLVED",
        clarification=message,
        context=context,
        parsed_location=parsed.model_dump(),
        anchors=anchors,
    )
