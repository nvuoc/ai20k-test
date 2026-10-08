"""Conservative Vietnamese speech matching against the two offered places."""
import re
from difflib import SequenceMatcher

from app.contracts.maps import search_key


def match_candidate(text: str, candidates: list[dict]) -> dict | None:
    if not candidates or len(candidates) > 2:
        return None
    spoken = search_key(text).strip(" .,!?")
    # Negations, corrections, questions and compound requests go to the extractor.
    if re.search(r"\b(khong|dung|doi|thay|huy|hay|hoac|den|don|gia|bao nhieu)\b", spoken):
        return None
    ordinal = re.fullmatch(
        r"(?:(?:chon|lay|cai|dia diem|ket qua|phuong an|so|thu)\s+)*"
        r"(1|2|mot|hai|nhat|dau tien|dau)(?:\s+(?:nhe|a|di))?", spoken)
    if ordinal:
        index = 1 if ordinal[1] in {"2", "hai"} else 0
        return candidates[index] if index < len(candidates) else None
    spoken = re.sub(r"^(?:toi |minh |em )?(?:chon|lay)\s+", "", spoken)
    exact = [candidate for candidate in candidates if search_key(candidate["label"]) == spoken]
    if len(exact) == 1:
        return exact[0]
    spoken = re.sub(r"\s+nhe$", "", spoken)
    if len(spoken) < 4:
        return None
    # A distinctive suffix (cơ sở B, cổng 2) matters more than a shared POI name.
    words = set(spoken.split())
    subsets = [candidate for candidate in candidates
               if words <= set(search_key(candidate["label"]).split())]
    if len(words) >= 2 and len(subsets) == 1:
        return subsets[0]
    scores = []
    for candidate in candidates:
        label = search_key(candidate["label"])
        words = set(spoken.split())
        coverage = len(words & set(label.split())) / max(len(words), 1)
        score = max(SequenceMatcher(None, spoken, label).ratio(), coverage * 0.9)
        scores.append((score, candidate))
    scores.sort(key=lambda row: row[0], reverse=True)
    if scores[0][0] >= 0.78 and (len(scores) == 1 or scores[0][0] - scores[1][0] >= 0.15):
        return scores[0][1]
    return None


def ambiguous_choice(text: str, candidates: list[dict]) -> bool:
    """Keep a shared place name/out-of-range ordinal from becoming guessed consent."""
    spoken = search_key(text)
    if re.fullmatch(r"(?:(?:chon|lay|cai|so|thu)\s+)*(?:3|4|5|ba|bon|nam)", spoken):
        return True
    if re.search(r"\b(khong|doi|thay|huy|den|don|gia|bao nhieu)\b", spoken):
        return False
    words = set(re.sub(r"^(?:toi |minh |em )?(?:chon|lay)\s+", "", spoken).split())
    return len(words) >= 2 and len(candidates) == 2 and all(
        words <= set(search_key(candidate["label"]).split()) for candidate in candidates)
