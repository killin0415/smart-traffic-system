"""Sanity-check Tainan TDX coverage: static sections + live count + sample IDs."""

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

CITY = os.getenv("CITY", "Taipei")
SECTION_URL = f"https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/Section/City/{CITY}"
LIVE_URL = f"https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/Live/City/{CITY}"

# Default: Taipei main station ~25.0478, 121.5170
STATION_LAT = float(os.getenv("STATION_LAT", "25.0478"))
STATION_LNG = float(os.getenv("STATION_LNG", "121.5170"))
RADIUS_DEG = float(os.getenv("RADIUS_DEG", "0.02"))  # ~2.2km box


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_access_token(client)
        hdr = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        # --- Static Section count ---
        all_sections = []
        skip = 0
        page = 100
        while True:
            await asyncio.sleep(2)
            r = await client.get(SECTION_URL, headers=hdr, params={"$top": str(page), "$skip": str(skip), "$format": "JSON"})
            if r.status_code == 429:
                print(f"  rate-limited at skip={skip}, waiting 15s...")
                await asyncio.sleep(15)
                continue
            r.raise_for_status()
            chunk = r.json().get("Sections", [])
            if not chunk:
                break
            all_sections.extend(chunk)
            if len(chunk) < page:
                break
            skip += page
        print(f"[Section/City/{CITY}] static total: {len(all_sections)} sections")

        # Filter to bbox around train station.
        in_bbox = []
        for s in all_sections:
            start = s.get("SectionStart", {})
            end = s.get("SectionEnd", {})
            lats = [start.get("PositionLat", 0), end.get("PositionLat", 0)]
            lngs = [start.get("PositionLon", 0), end.get("PositionLon", 0)]
            if any(abs(la - STATION_LAT) <= RADIUS_DEG and abs(ln - STATION_LNG) <= RADIUS_DEG for la, ln in zip(lats, lngs)):
                in_bbox.append(s)
        print(f"[Section/City/{CITY}] within ~2.2km of station: {len(in_bbox)}")
        if in_bbox:
            print("  sample sections in bbox:")
            for s in in_bbox[:5]:
                print(f"    {s.get('SectionID')}  {s.get('RoadName')}  RoadClass={s.get('RoadClass')}")

        # --- Live count ---
        r = await client.get(LIVE_URL, headers=hdr, params={"$format": "JSON"})
        r.raise_for_status()
        live = r.json().get("LiveTraffics", [])
        live_ids = {row.get("SectionID") for row in live}
        print(f"\n[Live/City/{CITY}] total live records: {len(live)}")
        print(f"  sample IDs: {list(live_ids)[:5]}")

        # Overlap: live sections that are also in our bbox
        bbox_ids = {s.get("SectionID") for s in in_bbox}
        overlap = bbox_ids & live_ids
        print(f"\n  live ∩ bbox: {len(overlap)} sections have BOTH static metadata and live data near the station")
        for sid in list(overlap)[:5]:
            print(f"    {sid}")


if __name__ == "__main__":
    asyncio.run(main())
