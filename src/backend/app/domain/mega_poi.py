"""Local Mega POI names and configured defaults; maps supply the coordinates."""

from __future__ import annotations

import json
from pathlib import Path

from app.contracts.maps import search_key


class MegaPOIRegistry:
    def __init__(self, path: Path | None = None):
        self.data = json.loads((path or Path(__file__).resolve().parents[1] /
                               "fixtures/mega_pois.json").read_text(encoding="utf-8"))

    def lookup(self, query):
        key = search_key(query)
        matches = [row for row in self.data["entries"]
                   if any(key == search_key(alias) or key.startswith(search_key(alias) + " ")
                          for alias in row["aliases"])]
        return max(matches, key=lambda row: max(map(len, row["aliases"]))) if matches else None

    @staticmethod
    def specific(query, entry):
        key = search_key(query)
        if any(key == search_key(alias) for alias in entry["aliases"]):
            return False
        import re
        return bool(re.search(r"\b(?:cong|sanh|toa|cua|ga t\d|quan|so \d)\b", key))
