"""
VD (Vehicle Detector) static data: TDX fetch, snap-to-edge, and DB seeding.

Mirrors `speed_camera.py` flow — runs once at lifespan startup if `vd_sensor` is
empty, persists `vdid → nearest_edge_id` mapping for the live refresher to use.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import TrafficEdge, TrafficNode, VDSensor
from src.db.road_network import Coord, haversine_m

logger = logging.getLogger(__name__)

TDX_AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TDX_VD_STATIC_URL = "https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/VD/City/Kaohsiung"

PAGE_SIZE = 100
REQUEST_DELAY_SEC = 2.0


@dataclass
class ParsedVD:
    vdid: str
    latitude: float
    longitude: float
    link_id: str | None
    road_section_id: str | None


async def _get_access_token(client: httpx.AsyncClient) -> str:
    client_id = os.getenv("TDX_CLIENT_ID") or os.getenv("TDX-CLIENT-ID")
    client_secret = os.getenv("TDX_CLIENT_SECRET") or os.getenv("TDX-CLIENT-SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("TDX_CLIENT_ID / TDX_CLIENT_SECRET not set")

    response = await client.post(
        TDX_AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _extract_link_id(raw: dict) -> str | None:
    """TDX VD records expose links inside `DetectionLinks: [{LinkID, ...}]` or directly."""
    links = raw.get("DetectionLinks") or raw.get("LinkIDs") or []
    if isinstance(links, list) and links:
        first = links[0]
        if isinstance(first, dict):
            lid = first.get("LinkID")
            if lid:
                return str(lid)
        elif isinstance(first, str):
            return first
    if raw.get("LinkID"):
        return str(raw["LinkID"])
    return None


def _extract_road_section_id(raw: dict) -> str | None:
    rs = raw.get("RoadSection")
    if isinstance(rs, dict):
        sid = rs.get("Start") or rs.get("End") or rs.get("RoadSectionID")
        if sid:
            return str(sid)
    if raw.get("RoadSectionID"):
        return str(raw["RoadSectionID"])
    return None


def _parse_vd_record(raw: dict) -> ParsedVD | None:
    vdid = raw.get("VDID")
    lat = raw.get("PositionLat")
    lon = raw.get("PositionLon")
    if not vdid or lat is None or lon is None:
        return None
    try:
        latf = float(lat)
        lonf = float(lon)
    except (TypeError, ValueError):
        return None
    return ParsedVD(
        vdid=str(vdid),
        latitude=latf,
        longitude=lonf,
        link_id=_extract_link_id(raw),
        road_section_id=_extract_road_section_id(raw),
    )


async def fetch_vd_static(token: str) -> list[ParsedVD]:
    """Page through TDX VD City/Kaohsiung and return all VD records."""
    parsed: list[ParsedVD] = []
    skip = 0
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            await asyncio.sleep(REQUEST_DELAY_SEC)
            response = await client.get(
                TDX_VD_STATIC_URL,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"$top": str(PAGE_SIZE), "$skip": str(skip), "$format": "JSON"},
            )
            if response.status_code == 429:
                logger.warning("TDX VD static rate limited, waiting 10s...")
                await asyncio.sleep(10)
                continue
            response.raise_for_status()
            payload = response.json()

            if isinstance(payload, dict):
                items = payload.get("VDs") or payload.get("VDInfos") or []
            elif isinstance(payload, list):
                items = payload
            else:
                items = []

            if not items:
                break

            for raw in items:
                vd = _parse_vd_record(raw)
                if vd is not None:
                    parsed.append(vd)

            if len(items) < PAGE_SIZE:
                break
            skip += PAGE_SIZE

    return parsed


def snap_vd_to_edge(
    vd: ParsedVD,
    edges: list[TrafficEdge],
    node_coords: dict[int, tuple[float, float]],
) -> int | None:
    """Return the edge_id of the nearest TrafficEdge (haversine to nearer endpoint)."""
    best_id: int | None = None
    best_dist = float("inf")
    vd_coord = Coord(latitude=vd.latitude, longitude=vd.longitude)
    for edge in edges:
        src = node_coords.get(edge.source_node_id)
        tgt = node_coords.get(edge.target_node_id)
        if src is None or tgt is None:
            continue
        d = min(
            haversine_m(vd_coord, Coord(latitude=src[0], longitude=src[1])),
            haversine_m(vd_coord, Coord(latitude=tgt[0], longitude=tgt[1])),
        )
        if d < best_dist:
            best_dist = d
            best_id = edge.id
    return best_id


async def seed_vd_sensors(session: AsyncSession) -> None:
    """Seed `vd_sensor` from TDX VD City/Kaohsiung when the table is empty.

    No-op (with WARNING) if TDX credentials are missing — keeps lifespan resilient.
    """
    count = (await session.execute(select(func.count()).select_from(VDSensor))).scalar_one()
    if count > 0:
        logger.info("vd_sensor 已有 %d 筆資料，跳過 seed", count)
        return

    if not (os.getenv("TDX_CLIENT_ID") or os.getenv("TDX-CLIENT-ID")) or not (
        os.getenv("TDX_CLIENT_SECRET") or os.getenv("TDX-CLIENT-SECRET")
    ):
        logger.warning("TDX credentials missing — skipping vd_sensor seed")
        return

    edges = (await session.execute(select(TrafficEdge))).scalars().all()
    node_rows = (await session.execute(select(TrafficNode))).scalars().all()
    node_coords = {n.id: (n.latitude, n.longitude) for n in node_rows}

    if not edges or not node_coords:
        logger.warning("road network not yet seeded — skipping vd_sensor seed")
        return

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await _get_access_token(client)
        vds = await fetch_vd_static(token)
    except Exception as e:
        logger.warning("VD static fetch failed: %s — skipping vd_sensor seed", e)
        return

    if not vds:
        logger.warning("TDX VD static returned 0 records — nothing to seed")
        return

    objs: list[VDSensor] = []
    snapped = 0
    for vd in vds:
        nearest = snap_vd_to_edge(vd, edges, node_coords)
        if nearest is not None:
            snapped += 1
        objs.append(
            VDSensor(
                vdid=vd.vdid,
                latitude=vd.latitude,
                longitude=vd.longitude,
                link_id=vd.link_id,
                road_section_id=vd.road_section_id,
                nearest_edge_id=nearest,
            )
        )

    session.add_all(objs)
    await session.commit()
    logger.info("vd_sensor seed 完成：%d 筆（%d 已 snap 到 edge）", len(objs), snapped)
