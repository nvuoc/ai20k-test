"""Address replay checks for identity, components, anchors and route profile."""

import asyncio
import json

import httpx
import pytest

from app.adapters.map_fixture import FixtureMapAdapter
from app.adapters.map_vietmap import VietMapAdapter
from app.domain.local_aliases import LocalAliasRegistry
from app.domain.location_parser import parse_location


@pytest.mark.parametrize(
    "query,house",
    [
        ("65 phố Nhổn Hà Nội", "65"),
        ("65A phố Nhổn Hà Nội", "65a"),
        ("65/2 ngõ 65 phố Nhổn Hà Nội", "65/2"),
        ("Nhà 09 đường X", "09"),
    ],
)
def test_address_components_preserve_strings(query, house):
    parsed = parse_location(query)
    assert parsed.house_number == house
    assert parsed.raw_text == query


def test_exact_address_can_accept_one_verified_entity_among_different_houses():
    def handler(req):
        if "/search/" in req.url.path:
            return httpx.Response(
                200,
                json=[
                    {"ref_id": f"house:{num}", "display": f"{num} phố Nhổn Hà Nội"}
                    for num in ("65", "650", "65A")
                ],
            )
        num = req.url.params["refid"].split(":")[-1]
        return httpx.Response(
            200,
            json={
                "lat": 21.01,
                "lng": 105.75,
                "hs_num": num,
                "street": "phố Nhổn",
                "city": "Hà Nội",
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = VietMapAdapter("test-key", client=client)
            result = await adapter.resolve("65 phố Nhổn Hà Nội")
            assert result["status"] == "resolved"
            assert result["place"]["id"] == "house:65"

    asyncio.run(run())


def test_uninspected_competitor_prevents_auto_accept():
    def handler(req):
        if "/search/" in req.url.path:
            return httpx.Response(
                200,
                json=[
                    {"ref_id": f"house:{num}", "display": f"{num} phố Nhổn Hà Nội"}
                    for num in ("65", "650", "65A", "65B")
                ],
            )
        num = req.url.params["refid"].split(":")[-1]
        return httpx.Response(
            200,
            json={
                "lat": 21.01,
                "lng": 105.75,
                "hs_num": num,
                "street": "phố Nhổn",
                "city": "Hà Nội",
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await VietMapAdapter("test-key", client=client).resolve("65 phố Nhổn Hà Nội")
            assert result["status"] == "ambiguous"

    asyncio.run(run())


def test_motorbike_route_passes_motorcycle_profile_and_converts_milliseconds():
    def handler(req):
        assert req.url.path == "/api/route/v4"
        assert req.url.params["vehicle"] == "motorcycle"
        return httpx.Response(
            200, json={"code": "OK", "paths": [{"distance": 1700, "time": 240000}]}
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            route = await VietMapAdapter("test-key", client=client).route(
                {"id": "a", "label": "A", "lat": 21, "lon": 105, "source": "replay"},
                {"id": "b", "label": "B", "lat": 21.01, "lon": 105.01, "source": "replay"},
                "xe_may_dien",
            )
            assert route["vehicle_profile"] == "motorcycle" and route["duration_s"] == 240

    asyncio.run(run())


def test_local_alias_requires_area_and_records_source(tmp_path):
    entries = [
        {
            "alias": "nhà hát cũ",
            "area": area,
            "canonical_query": query,
            "entity_id": identity,
            "source_ref": "fixture:locations-sandbox-1",
            "source_version": "review-fixture-1",
            "kind": "fixture",
        }
        for area, query, identity in [
            ("Hà Nội", "Nhà hát Lớn Hà Nội", "hn_opera"),
            ("Hồ Chí Minh", "VietMap Hồ Chí Minh", "hcm_vietmap"),
        ]
    ]
    path = tmp_path / "aliases.json"
    path.write_text(
        json.dumps({"version": "test-aliases-1", "notice": "synthetic tests", "entries": entries}),
        encoding="utf-8",
    )
    assert LocalAliasRegistry(path).lookup("nhà hát cũ", "Hà Nội")[0] == "none"

    async def run():
        maps = FixtureMapAdapter(alias_path=path)
        ambiguous = await maps.resolve("nhà hát cũ")
        assert ambiguous["status"] == "ambiguous" and not ambiguous["candidates"]
        resolved = await maps.resolve("nhà hát cũ", context={"area": "Hà Nội"})
        assert resolved["place"]["id"] == "hn_opera"
        assert resolved["place"]["metadata"]["local_alias"]["dataset_version"] == "test-aliases-1"
        assert resolved["place"]["metadata"]["local_alias"]["source_version"] == "review-fixture-1"

    asyncio.run(run())
