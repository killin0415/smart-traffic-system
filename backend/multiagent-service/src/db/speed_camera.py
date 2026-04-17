"""
Speed camera static data: CSV parsing, snap-to-edge, and DB seeding.

Input CSV: `data/speed_cameras.csv` (government open data, e.g. dataset 6489).
Column names in Taiwan open-data CSVs vary; this module accepts several common variants.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import SpeedCamera, TrafficEdge, TrafficNode
from src.db.road_network import Coord, haversine_m

logger = logging.getLogger(__name__)

DEFAULT_CSV_PATH = Path(__file__).resolve().parents[4] / "data" / "speed_cameras.csv"
KAOHSIUNG_KEYWORDS = ("高雄市", "高雄")

# Taiwan's Kaohsiung urban default when the CSV lacks a SpeedLimit column.
DEFAULT_SPEED_LIMIT_KMH = 50

# When the CSV has a 測照型式 column, keep only rows whose value includes any of
# these tokens — i.e. speed enforcement (超速, 闖紅燈兼超速). Rows with no such
# column pass through unfiltered.
SPEED_ENFORCEMENT_TOKENS = ("超速",)


@dataclass
class ParsedCamera:
    latitude: float
    longitude: float
    direction: str
    speed_limit: int
    address: str


# ---- CSV column aliases (Taiwan open-data flavoured) ----

_CITY_KEYS = ("CityName", "縣市", "縣市別", "city")
_LAT_KEYS = ("Latitude", "緯度", "座標緯度", "PositionLat", "lat", "Y")
_LNG_KEYS = ("Longitude", "經度", "座標經度", "PositionLon", "lng", "lon", "X")
_DIR_KEYS = ("Direction", "方向", "測照方向", "偵測方向", "車道方向")
_LIMIT_KEYS = ("SpeedLimit", "限速", "速限", "速限(公里)", "限速值")
_ADDRESS_KEYS = ("Address", "地點", "測照地點", "地址", "設置地點", "位置", "路段")
_ENFORCEMENT_TYPE_KEYS = ("測照型式", "取締項目", "enforcement_type")


def _pick(row: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return str(row[k]).strip()
    return ""


def parse_speed_cameras(csv_path: Path | None = None) -> list[ParsedCamera]:
    """Load and filter speed cameras from the government open-data CSV.

    Keeps only rows whose city column contains 高雄. Rows lacking lat/lng are skipped.
    """
    path = csv_path or DEFAULT_CSV_PATH
    if not path.exists():
        logger.warning("speed_cameras.csv not found at %s", path)
        return []

    cameras: list[ParsedCamera] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            city = _pick(row, _CITY_KEYS)
            if city and not any(k in city for k in KAOHSIUNG_KEYWORDS):
                # If a city column is present, require it to name Kaohsiung.
                continue

            # If the CSV marks enforcement type, keep only speed-related rows
            # (e.g. 超速, 闖紅燈兼超速 — skip 闖紅燈/違左-only rows).
            enforcement = _pick(row, _ENFORCEMENT_TYPE_KEYS)
            if enforcement and not any(t in enforcement for t in SPEED_ENFORCEMENT_TOKENS):
                continue

            lat_s = _pick(row, _LAT_KEYS)
            lng_s = _pick(row, _LNG_KEYS)
            try:
                lat = float(lat_s)
                lng = float(lng_s)
            except (TypeError, ValueError):
                continue

            try:
                speed_limit = int(float(_pick(row, _LIMIT_KEYS) or "0"))
            except (TypeError, ValueError):
                speed_limit = 0
            if speed_limit <= 0:
                speed_limit = DEFAULT_SPEED_LIMIT_KMH

            cameras.append(
                ParsedCamera(
                    latitude=lat,
                    longitude=lng,
                    direction=_pick(row, _DIR_KEYS),
                    speed_limit=speed_limit,
                    address=_pick(row, _ADDRESS_KEYS),
                )
            )
    logger.info("parse_speed_cameras: %d Kaohsiung cameras from %s", len(cameras), path)
    return cameras


def snap_camera_to_edge(
    cam: ParsedCamera,
    edges: list[TrafficEdge],
    node_coords: dict[int, tuple[float, float]],
) -> int | None:
    """Return the edge_id of the closest TrafficEdge to this camera.

    Distance is measured to the nearer of the edge's two endpoints (sufficient for
    short urban edges; full point-to-segment is overkill here).
    """
    best_id: int | None = None
    best_dist = float("inf")
    cam_coord = Coord(latitude=cam.latitude, longitude=cam.longitude)
    for edge in edges:
        src = node_coords.get(edge.source_node_id)
        tgt = node_coords.get(edge.target_node_id)
        if src is None or tgt is None:
            continue
        d = min(
            haversine_m(cam_coord, Coord(latitude=src[0], longitude=src[1])),
            haversine_m(cam_coord, Coord(latitude=tgt[0], longitude=tgt[1])),
        )
        if d < best_dist:
            best_dist = d
            best_id = edge.id
    return best_id


async def seed_speed_cameras(session: AsyncSession, csv_path: Path | None = None) -> None:
    """Seed the speed_camera table from the CSV if the table is empty.

    Graceful no-op when CSV is missing (e.g. capstone demo before user downloads data).
    """
    count = (await session.execute(select(func.count()).select_from(SpeedCamera))).scalar_one()
    if count > 0:
        logger.info("speed_camera 已有 %d 筆資料，跳過 seed", count)
        return

    cameras = parse_speed_cameras(csv_path)
    if not cameras:
        return

    edges = (await session.execute(select(TrafficEdge))).scalars().all()
    node_rows = (await session.execute(select(TrafficNode))).scalars().all()
    node_coords = {n.id: (n.latitude, n.longitude) for n in node_rows}

    if not edges or not node_coords:
        logger.warning("road network not yet seeded — skipping speed camera seed")
        return

    objs = []
    for cam in cameras:
        nearest = snap_camera_to_edge(cam, edges, node_coords)
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
