"""
One-shot seeders for data.taipei static metadata that can run in lifespan.

Currently:
  - seed_parking_lots: pulls the parking-lot static dataset and inserts into
    `parking_lot`. No-op if the table already has rows. Network errors are
    logged and swallowed — service still boots.

VD static metadata (`vd_static`) is intentionally NOT here; it's seeded by the
offline CLI `scripts/seed_vd_static.py` (see design D9) so that lifespan
doesn't depend on data.taipei being reachable at boot.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ParkingLot

logger = logging.getLogger(__name__)

PARKING_STATIC_DATASET_ID = "f6d196d7-c123-4561-a7ca-aef0d8aaae09"
PARKING_STATIC_URL = (
    f"https://data.taipei/api/v1/dataset/{PARKING_STATIC_DATASET_ID}?scope=resourceAquire"
)


def _safe_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


async def fetch_parking_lots() -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(PARKING_STATIC_URL)
        response.raise_for_status()
        payload = response.json()
    results = (
        payload.get("result", {}).get("results")
        or payload.get("results")
        or []
    )
    return results if isinstance(results, list) else []


async def seed_parking_lots(session: AsyncSession) -> None:
    """Insert parking lot static metadata if the table is empty.

    Each row gets PostGIS `geom` populated via ST_MakePoint(lng, lat).
    """
    count = (await session.execute(select(func.count()).select_from(ParkingLot))).scalar_one()
    if count > 0:
        logger.info("parking_lot already has %d rows, skipping seed", count)
        return

    try:
        lots = await fetch_parking_lots()
    except Exception as exc:
        logger.warning("parking_lot seed failed (network): %s — service continues", exc)
        return

    rows: list[dict] = []
    for lot in lots:
        if not isinstance(lot, dict):
            continue
        try:
            lot_id = int(str(lot.get("id") or lot.get("ID") or lot.get("ParkID")).strip())
        except (TypeError, ValueError):
            continue
        lat = _safe_float(lot.get("latitude") or lot.get("lat"))
        lng = _safe_float(lot.get("longitude") or lot.get("lng") or lot.get("lon"))
        if lat is None or lng is None:
            continue
        rows.append(
            {
                "id": lot_id,
                "name": (lot.get("name") or lot.get("ParkName") or "").strip() or None,
                "address": (lot.get("address") or lot.get("Address") or "").strip() or None,
                "total_car": _safe_int(lot.get("totalcar") or lot.get("TotalCar")),
                "total_motor": _safe_int(lot.get("totalmot") or lot.get("TotalMot")),
                "latitude": lat,
                "longitude": lng,
            }
        )

    if not rows:
        logger.warning("parking_lot seed: no rows parsed from upstream")
        return

    insert_sql = text(
        """
        INSERT INTO parking_lot (id, name, address, total_car, total_motor, latitude, longitude, geom)
        VALUES (:id, :name, :address, :total_car, :total_motor, :latitude, :longitude,
                ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326))
        ON CONFLICT (id) DO NOTHING
        """
    )
    for row in rows:
        await session.execute(insert_sql, row)
    await session.commit()
    logger.info("parking_lot seed: inserted %d rows", len(rows))
