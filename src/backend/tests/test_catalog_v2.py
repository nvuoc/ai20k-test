"""Electric motorbike remains disabled until its price/profile/policy are supplied."""

import asyncio
import json
from pathlib import Path

import pytest
from test_conversation_v2 import say

from app.adapters.booking_sandbox import SandboxBookingProvider
from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.quote_fixture import FIXTURE_DIR, QuoteAdapter
from app.adapters.turn_fixture import extract_turn_fixture
from app.domain.conversation import ConversationEngine


@pytest.fixture
def electric(tmp_path):
    catalog = json.loads((FIXTURE_DIR / "vehicle_catalog.json").read_text(encoding="utf-8"))
    pricing = json.loads((FIXTURE_DIR / "pricing.json").read_text(encoding="utf-8"))
    catalog["vehicles"]["xe_may_dien"].update(
        bookable=True, quote_available=True, advertised=True, allow_adult_with_child=True
    )
    # Test-only tariff, deliberately supplied rather than invented by runtime.
    pricing["tariffs"]["xe_may_dien"] = {"base_fee": 5000, "per_km": 4000}
    catalog_path, pricing_path = tmp_path / "catalog.json", tmp_path / "pricing.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    pricing_path.write_text(json.dumps(pricing), encoding="utf-8")
    quote = QuoteAdapter(catalog_path=catalog_path, pricing_path=pricing_path)

    class Maps(FixtureMapAdapter):
        supported_profiles = frozenset({"car", "motorcycle"})

        def __init__(self):
            super().__init__()
            self.supported_profiles = frozenset({"car", "motorcycle"})
            self._routes[("hn_opera", "hn_station", "motorcycle")] = {
                "pickup_id": "hn_opera",
                "destination_id": "hn_station",
                "distance_m": 2800,
                "duration_s": 540,
                "vehicle_profile": "motorcycle",
            }

    async def extractor(data):
        return extract_turn_fixture(data)

    provider = SandboxBookingProvider(tmp_path / "provider.sqlite")
    engine = ConversationEngine(extractor, Maps(), provider, quote)
    yield engine, provider
    provider.close()


@pytest.mark.parametrize(
    "people,expected",
    [
        ("1 người lớn", True),
        ("2 người lớn", False),
        ("1 người lớn và 1 trẻ em", True),
        ("2 người", False),
    ],
)
def test_electric_party_capacity(electric, people, expected):
    engine, provider = electric

    async def run():
        state = await say(
            engine,
            engine.new_state("session"),
            f"Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, đi ngay, {people}, xe máy điện, số 0901234567.",
        )
        if expected:
            assert state["last_response"]["action"] == "confirm_booking", state["last_response"][
                "text"
            ]
            assert state["last_response"]["summary"]["vehicle_type"] == "xe_may_dien"
            state = await say(engine, state, "Đồng ý đặt")
            assert provider.booking_count() == 1
            assert (
                state["transaction"]["committed_snapshot"]["route"]["vehicle_profile"]
                == "motorcycle"
            )
        else:
            assert state["last_response"]["action"] == "ask_clarification"
            assert provider.booking_count() == 0

    asyncio.run(run())


def test_default_catalog_does_not_claim_electric_is_bookable():
    quote = QuoteAdapter()
    assert not quote.get_vehicle("xe_may_dien")["bookable"]
    assert "xe_may_dien" not in quote.pricing["tariffs"]


def test_catalog_cannot_enable_motorbike_without_tariff(tmp_path):
    catalog = json.loads((FIXTURE_DIR / "vehicle_catalog.json").read_text(encoding="utf-8"))
    catalog["vehicles"]["xe_may_dien"]["bookable"] = True
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(ValueError, match="tariff"):
        QuoteAdapter(catalog_path=Path(path))
