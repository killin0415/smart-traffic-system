# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "httpx>=0.28",
#   "asyncpg>=0.30",
#   "sqlalchemy>=2.0",
#   "geoalchemy2>=0.19",
# ]
# ///
"""
seed_vd_static.py — one-shot CLI to import VD static metadata into the DB.

Source: https://tcgbusfs.blob.core.windows.net/blobtisv/VD.xml (plain XML, no auth).
Target: vd_static table.

This is part of the OFFLINE migration flow (run after the OSM graph build,
before post_build_snap_vd.sql) — NOT inside the FastAPI lifespan, so the
service doesn't depend on data.taipei being reachable at boot.

Usage:
  uv run --script scripts/seed_vd_static.py
  uv run --script scripts/seed_vd_static.py --db-url postgresql+asyncpg://admin:secret@localhost:5432/traffic_data

The script uses INSERT ... ON CONFLICT (vdid) DO UPDATE so it's safe to re-run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

VD_STATIC_URL = "https://tcgbusfs.blob.core.windows.net/blobtisv/VD.xml"

logger = logging.getLogger("seed_vd_static")


@dataclass
class VDRecord:
    vdid: str
    link_id: str | None
    road_name: str | None
    road_class: str | None
    bidirectional: bool
    bearing: str | None
    latitude: float
    longitude: float


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _find_text(elem: ET.Element, name: str) -> str | None:
    for child in elem.iter():
        if _strip_ns(child.tag) == name:
            return (child.text or "").strip() or None
    return None


def parse_vd_static_xml(xml_text: str) -> list[VDRecord]:
    """Parse VD.xml. Tolerant of namespace + missing fields."""
    if not xml_text:
        return []

    root = ET.fromstring(xml_text)
    out: list[VDRecord] = []

    for vd_elem in root.iter():
        if _strip_ns(vd_elem.tag) != "VD":
            continue

        vdid = _find_text(vd_elem, "VDID")
        if not vdid:
            continue

        # Coordinates
        lat_raw = _find_text(vd_elem, "PositionLat") or _find_text(vd_elem, "Latitude")
        lng_raw = _find_text(vd_elem, "PositionLon") or _find_text(vd_elem, "Longitude")
        if lat_raw is None or lng_raw is None:
            continue
        try:
            lat = float(lat_raw)
            lng = float(lng_raw)
        except ValueError:
            continue

        link_id = _find_text(vd_elem, "LinkID")
        road_name = _find_text(vd_elem, "RoadName") or _find_text(vd_elem, "Road")
        road_class = _find_text(vd_elem, "RoadClass")
        bearing = _find_text(vd_elem, "RoadDirection") or _find_text(vd_elem, "Bearing")

        bidir_raw = _find_text(vd_elem, "BiDirectional")
        bidirectional = bidir_raw is not None and bidir_raw.strip().lower() in (
            "1", "true", "yes", "y",
        )

        out.append(
            VDRecord(
                vdid=vdid,
                link_id=link_id,
                road_name=road_name,
                road_class=road_class,
                bidirectional=bidirectional,
                bearing=bearing,
                latitude=lat,
                longitude=lng,
            )
        )

    return out


async def fetch_vd_static() -> list[VDRecord]:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(VD_STATIC_URL)
        response.raise_for_status()
        return parse_vd_static_xml(response.text)


_UPSERT_SQL = text(
    """
    INSERT INTO vd_static (
        vdid, link_id, road_name, road_class, bidirectional,
        bearing, latitude, longitude, geom
    )
    VALUES (
        :vdid, :link_id, :road_name, :road_class, :bidirectional,
        :bearing, :latitude, :longitude,
        ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)
    )
    ON CONFLICT (vdid) DO UPDATE SET
        link_id       = EXCLUDED.link_id,
        road_name     = EXCLUDED.road_name,
        road_class    = EXCLUDED.road_class,
        bidirectional = EXCLUDED.bidirectional,
        bearing       = EXCLUDED.bearing,
        latitude      = EXCLUDED.latitude,
        longitude     = EXCLUDED.longitude,
        geom          = EXCLUDED.geom
    """
)


async def upsert_vd_records(db_url: str, records: list[VDRecord]) -> int:
    if not records:
        return 0
    engine = create_async_engine(db_url, echo=False)
    try:
        async with engine.begin() as conn:
            for r in records:
                await conn.execute(
                    _UPSERT_SQL,
                    {
                        "vdid": r.vdid,
                        "link_id": r.link_id,
                        "road_name": r.road_name,
                        "road_class": r.road_class,
                        "bidirectional": r.bidirectional,
                        "bearing": r.bearing,
                        "latitude": r.latitude,
                        "longitude": r.longitude,
                    },
                )
        return len(records)
    finally:
        await engine.dispose()


async def _amain(db_url: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.info("fetching VD static metadata from %s", VD_STATIC_URL)
    records = await fetch_vd_static()
    if not records:
        logger.warning("no <VD> records returned — graceful exit")
        return 0
    logger.info("parsed %d VD records, upserting into vd_static", len(records))
    n = await upsert_vd_records(db_url, records)
    logger.info("seeded %d rows into vd_static", n)
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed vd_static from data.taipei VD.xml")
    parser.add_argument(
        "--db-url",
        default=os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://admin:secret@localhost:5432/traffic_data",
        ),
        help="SQLAlchemy async URL (default: $DATABASE_URL)",
    )
    args = parser.parse_args(argv)

    n = asyncio.run(_amain(args.db_url))
    return 0 if n >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
