"""One-shot: dump a few raw records from TDX Live/City/Tainan to verify data quality."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_env_path = Path(__file__).resolve().parents[3] / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

import httpx
from src.agents.traffic import get_access_token

URL = "https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/Live/City/Tainan"


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_access_token(client)
        response = await client.get(
            URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"$format": "JSON"},
        )
        print(f"HTTP {response.status_code}")
        print(f"Content-Type: {response.headers.get('Content-Type')}")
        payload = response.json()

    if isinstance(payload, dict):
        rows = (
            payload.get("LiveTraffics")
            or payload.get("Sections")
            or payload.get("LiveTrafficSections")
            or payload.get("RoadLiveTraffics")
            or []
        )
        print(f"Top-level keys: {list(payload.keys())}")
    else:
        rows = payload if isinstance(payload, list) else []

    print(f"Total records returned: {len(rows)}")
    print()

    for i, row in enumerate(rows[:3], 1):
        print(f"=== Record {i} ===")
        print(json.dumps(row, ensure_ascii=False, indent=2))
        print()

    # Quick stats: how many have non-null TravelSpeed / TravelTime?
    speeds = [r.get("TravelSpeed") for r in rows if r.get("TravelSpeed") is not None]
    times = [r.get("TravelTime") for r in rows if r.get("TravelTime") is not None]
    print(f"TravelSpeed populated in {len(speeds)}/{len(rows)} records  (min={min(speeds, default='?')}, max={max(speeds, default='?')})")
    print(f"TravelTime populated in {len(times)}/{len(rows)} records")


if __name__ == "__main__":
    asyncio.run(main())
