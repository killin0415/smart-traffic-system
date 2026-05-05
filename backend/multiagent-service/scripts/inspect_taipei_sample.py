"""One-shot: dump 3 Live/City/Taipei records to confirm data quality."""

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


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as c:
        tok = await get_access_token(c)
        r = await c.get(
            "https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/Live/City/Taipei",
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
            params={"$format": "JSON"},
        )
        rows = r.json().get("LiveTraffics", [])

    healthy = [row for row in rows if isinstance(row.get("TravelSpeed"), (int, float)) and row["TravelSpeed"] > 0]
    print(f"Total records: {len(rows)}")
    print(f"Healthy (TravelSpeed > 0): {len(healthy)} ({100 * len(healthy) / max(len(rows), 1):.1f}%)")
    if healthy:
        speeds = [row["TravelSpeed"] for row in healthy]
        print(f"  speed range: min={min(speeds):.1f}, max={max(speeds):.1f}, mean={sum(speeds)/len(speeds):.1f} km/h")
        print(f"  sample: {[(row['SectionID'], row['TravelSpeed']) for row in healthy[:5]]}")


if __name__ == "__main__":
    asyncio.run(main())
