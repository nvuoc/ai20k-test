"""Validate packaged fixtures; never resets runtime databases."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
from app.adapters.map_fixture import FixtureMapAdapter


async def main():
    maps = FixtureMapAdapter()
    pickup = await maps.resolve('Nhà hát Lớn Hà Nội')
    destination = await maps.resolve('Ga Hà Nội', target='destination')
    route = await maps.route(pickup['place'], destination['place'])
    print(f"Fixtures OK: {len(maps.places)} địa điểm; tuyến mẫu {route['distance_m']} m.")

asyncio.run(main())
