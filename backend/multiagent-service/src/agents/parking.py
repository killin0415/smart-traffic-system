"""
data.taipei parking-lot integration.

Static metadata (`parking_lot`): seeded once via `db.seed_taipei.seed_parking_lots`.
Dynamic availability (`parking_availability`): polled every 5 min by
`run_periodic_parking_refresh` and inserted with ON CONFLICT DO NOTHING.

API doc: https://data.taipei/dataset/detail?id=d5c0656b-5250-4179-a491-c94daa56ef2c
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db.models import ParkingAvailability

logger = logging.getLogger(__name__)

PARKING_DATASET_ID = "d5c0656b-5250-4179-a491-c94daa56ef2c"
PARKING_API_URL = (
    f"https://data.taipei/api/v1/dataset/{PARKING_DATASET_ID}?scope=resourceAquire"
)
DEFAULT_REFRESH_INTERVAL_SECONDS = int(os.getenv("PARKING_REFRESH_SECONDS", "300"))


@dataclass
class ParkingReading:
    ts: datetime
    lot_id: int
    available_car: int | None
    available_motor: int | None


def _safe_int(v) -> int | None:
    if v is None:
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def parse_parking_payload(payload: dict, ts: datetime | None = None) -> list[ParkingReading]:
    """Parse a data.taipei parking dataset response.

    The dataset returns roughly:
      {"result": {"limit": ..., "offset": ..., "count": ..., "results": [
          {"id": "...", "name": "...", "totalcar": "200", "totalmot": "10",
           "availablecar": "47", "availablemot": "3", ...},
          ...
      ]}}

    `id` is sometimes numeric, sometimes prefixed (e.g. "001"); we use raw int().
    """
    ts = ts or datetime.now(timezone.utc)
    out: list[ParkingReading] = []
    if not isinstance(payload, dict):
        return out

    results = (
        payload.get("result", {}).get("results")
        or payload.get("results")
        or payload.get("data")
        or []
    )
    if not isinstance(results, list):
        return out

    for row in results:
        if not isinstance(row, dict):
            continue
        rid = row.get("id") or row.get("ID") or row.get("ParkID")
        try:
            lot_id = int(str(rid).strip())
        except (TypeError, ValueError):
            continue

        avail_car = _safe_int(
            row.get("availablecar")
            or row.get("AvailableCar")
            or (row.get("FareInfo", {}).get("availablecar") if isinstance(row.get("FareInfo"), dict) else None)
        )
        avail_mot = _safe_int(
            row.get("availablemot") or row.get("AvailableMot")
        )

        out.append(
            ParkingReading(
                ts=ts,
                lot_id=lot_id,
                available_car=avail_car,
                available_motor=avail_mot,
            )
        )

    return out


async def fetch_parking_availability(
    client: httpx.AsyncClient | None = None,
    url: str = PARKING_API_URL,
) -> list[ParkingReading]:
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30)
    try:
        response = await client.get(url)
        response.raise_for_status()
        return parse_parking_payload(response.json())
    finally:
        if owns_client:
            await client.aclose()


async def insert_parking_readings(session, readings: Iterable[ParkingReading]) -> int:
    rows = [
        {
            "ts": r.ts,
            "lot_id": r.lot_id,
            "available_car": r.available_car,
            "available_motor": r.available_motor,
        }
        for r in readings
    ]
    if not rows:
        return 0
    stmt = pg_insert(ParkingAvailability).values(rows).on_conflict_do_nothing(
        index_elements=["ts", "lot_id"],
    )
    await session.execute(stmt)
    await session.commit()
    return len(rows)


async def run_periodic_parking_refresh(
    session_factory,
    interval_seconds: int = DEFAULT_REFRESH_INTERVAL_SECONDS,
) -> None:
    """Background loop: parking availability refresh every `interval_seconds`."""
    logger.info("Parking refresher starting — interval=%ds", interval_seconds)
    while True:
        try:
            readings = await fetch_parking_availability()
            if readings:
                async with session_factory() as session:
                    await insert_parking_readings(session, readings)
                logger.info(
                    "parking refresh: %d availability rows inserted",
                    len(readings),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("parking refresh unexpected error: %s", exc)
        await asyncio.sleep(interval_seconds)
