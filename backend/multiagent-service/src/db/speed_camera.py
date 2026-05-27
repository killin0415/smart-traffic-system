"""
Speed camera static data: CSV parsing + PostGIS-snap-to-edge + DB seeding.

Input CSV: `data/taipei_speed_cameras.csv` — data.taipei
"臺北市固定測速照相地點表".

Schema (fixed columns, single-source):
  緯度 (latitude), 經度 (longitude), 速限 (speed limit km/h),
  拍攝方向 (direction), 設置地點 (address)

Snap-to-edge uses PostGIS `ST_Distance + ORDER BY LIMIT 1` against
`traffic_edge.geom` rather than a Python O(n) endpoint scan.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import SpeedCamera

logger = logging.getLogger(__name__)

_PARENTS = Path(__file__).resolve().parents
DEFAULT_CSV_PATH = (
    _PARENTS[4] / "data" / "taipei_speed_cameras.csv"
    if len(_PARENTS) > 4
    else Path("data/taipei_speed_cameras.csv")
)

# data.taipei urban default if 速限 column is missing/blank.
DEFAULT_SPEED_LIMIT_KMH = 50

# Single-flavour fixed-column mapping (data.taipei).
_LAT_KEYS = ("緯度",)
_LNG_KEYS = ("經度",)
_SPEED_LIMIT_KEYS = ("速限", "速限-速度限制")
_DIRECTION_KEYS = ("拍攝方向",)
_ADDRESS_KEYS = ("設置地點",)


@dataclass
class ParsedCamera:
    latitude: float
    longitude: float
    direction: str
    speed_limit: int
    address: str


def _pick(row: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return str(row[k]).strip()
    return ""


def parse_speed_cameras(csv_path: Path | None = None) -> list[ParsedCamera]:
    """Load all rows from the data.taipei speed-camera CSV.

    Skips rows whose lat/lng can't be parsed; everything else passes through.
    """
    path = csv_path or DEFAULT_CSV_PATH
    if not path.exists():
        logger.warning("taipei_speed_cameras.csv not found at %s", path)
        return []

    cameras: list[ParsedCamera] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lat_s = _pick(row, _LAT_KEYS)
            lng_s = _pick(row, _LNG_KEYS)
            try:
                lat = float(lat_s)
                lng = float(lng_s)
            except (TypeError, ValueError):
                continue

            try:
                speed_limit = int(float(_pick(row, _SPEED_LIMIT_KEYS) or "0"))
            except (TypeError, ValueError):
                speed_limit = 0
            if speed_limit <= 0:
                speed_limit = DEFAULT_SPEED_LIMIT_KMH

            cameras.append(
                ParsedCamera(
                    latitude=lat,
                    longitude=lng,
                    direction=_pick(row, _DIRECTION_KEYS),
                    speed_limit=speed_limit,
                    address=_pick(row, _ADDRESS_KEYS),
                )
            )
    logger.info("parse_speed_cameras: %d cameras from %s", len(cameras), path)
    return cameras


_NEAREST_EDGE_SQL = text(
    """
    SELECT id
    FROM traffic_edge
    ORDER BY ST_Distance(
        geom::geography,
        ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography
    )
    LIMIT 1
    """
)


async def snap_camera_to_edge(
    session: AsyncSession,
    cam: ParsedCamera,
) -> int | None:
    """Return the edge_id of the closest traffic_edge to this camera (PostGIS)."""
    row = (await session.execute(_NEAREST_EDGE_SQL, {"lat": cam.latitude, "lng": cam.longitude})).first()
    return int(row.id) if row else None


async def seed_speed_cameras(session: AsyncSession, csv_path: Path | None = None) -> None:
    """Seed the speed_camera table from the CSV if empty.

    Graceful no-op when CSV is missing or the road network hasn't been built yet.
    """
    count = (await session.execute(select(func.count()).select_from(SpeedCamera))).scalar_one()
    if count > 0:
        logger.info("speed_camera 已有 %d 筆資料，跳過 seed", count)
        return

    cameras = parse_speed_cameras(csv_path)
    if not cameras:
        return

    edge_count = (
        await session.execute(text("SELECT COUNT(*) FROM traffic_edge"))
    ).scalar_one()
    if not edge_count:
        logger.warning("traffic_edge is empty — skipping speed camera seed")
        return

    objs = []
    for cam in cameras:
        nearest = await snap_camera_to_edge(session, cam)
        objs.append(
            SpeedCamera(
                latitude=cam.latitude,
                longitude=cam.longitude,
                direction=cam.direction or None,
                speed_limit=cam.speed_limit,
                address=cam.address or None,
                nearest_edge_id=nearest,
            )
        )
    session.add_all(objs)
    await session.commit()
    logger.info("speed_camera seed 完成：%d 筆", len(objs))
