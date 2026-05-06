"""
data.taipei VD (Vehicle Detector) live integration.

Source: https://tcgbusfs.blob.core.windows.net/blobtisv/GetVDDATA.xml
Format: plain XML (NOT gzipped), 5-min update cycle, no auth, no API key.
Each cycle:
  1. fetch_vd_dynamic() -> list[VDReading]
  2. INSERT into vd_reading hypertable with ON CONFLICT DO NOTHING
  3. await weight_provider.rebuild()
  4. weight_provider.apply_to_graph(graph)
"""

from __future__ import annotations

import asyncio
import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db.models import VDReading

if TYPE_CHECKING:
    from src.agents.routing import RoadGraph
    from src.agents.weight_provider import WeightProvider

logger = logging.getLogger(__name__)

VD_DYNAMIC_URL = "https://tcgbusfs.blob.core.windows.net/blobtisv/GetVDDATA.xml"

DEFAULT_REFRESH_INTERVAL_SECONDS = int(os.getenv("VD_REFRESH_SECONDS", "300"))


@dataclass
class VDLaneReading:
    """One lane's reading inside a VD device snapshot."""

    ts: datetime
    vdid: str
    lane_no: int
    avg_speed: float | None
    volume: int | None
    occupancy: float | None


# ---------- XML parser ----------


def _strip_ns(tag: str) -> str:
    """Strip XML namespace prefix from a tag (e.g. '{ns}VDLive' -> 'VDLive')."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_text(elem: ET.Element, name: str) -> str | None:
    for child in elem.iter():
        if _strip_ns(child.tag) == name:
            return (child.text or "").strip() or None
    return None


def _to_float(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    # data.taipei often uses -99 / -1 sentinels for "no data".
    if v < 0:
        return None
    return v


def _to_int(s: str | None) -> int | None:
    if s is None:
        return None
    try:
        v = int(float(s))
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return v


def parse_vd_dynamic_xml(xml_text: str) -> list[VDLaneReading]:
    """Parse the GetVDDATA.xml body into a list of per-lane readings.

    Schema (relevant nodes):
        <VDLiveList>
          <VDLive>
            <VDID>VLRJL00</VDID>
            <DataCollectTime>2026-05-07T13:55:00+08:00</DataCollectTime>
            <LinkFlows>
              <LinkFlow>
                <Lanes>
                  <Lane>
                    <LaneID>0</LaneID>
                    <Speed>42.0</Speed>
                    <Occupancy>14</Occupancy>
                    <Vehicles>...</Vehicles> (optional aggregate)
                  </Lane>
                </Lanes>
              </LinkFlow>
            </LinkFlows>
          </VDLive>
        </VDLiveList>

    The parser is namespace-tolerant (strips XML namespaces) and resilient to
    missing inner fields; rows without VDID or DataCollectTime are skipped.
    """
    if not xml_text:
        return []

    root = ET.fromstring(xml_text)
    readings: list[VDLaneReading] = []

    for vd_elem in root.iter():
        if _strip_ns(vd_elem.tag) != "VDLive":
            continue

        vdid = _find_text(vd_elem, "VDID")
        ts_raw = _find_text(vd_elem, "DataCollectTime")
        if not vdid or not ts_raw:
            continue

        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            # Non-ISO format; fallback to "now" so we don't lose the row.
            ts = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        for lane_elem in vd_elem.iter():
            if _strip_ns(lane_elem.tag) != "Lane":
                continue

            lane_id_raw = _find_text(lane_elem, "LaneID")
            try:
                lane_no = int(lane_id_raw) if lane_id_raw is not None else 0
            except ValueError:
                lane_no = 0

            speed = _to_float(_find_text(lane_elem, "Speed"))
            occupancy = _to_float(_find_text(lane_elem, "Occupancy"))

            # Volume can show up as <Volume> directly or summed from <Vehicles><Vehicle><Volume>.
            volume = _to_int(_find_text(lane_elem, "Volume"))
            if volume is None:
                vol_sum = 0
                got = False
                for veh in lane_elem.iter():
                    if _strip_ns(veh.tag) == "Volume":
                        v = _to_int(veh.text)
                        if v is not None:
                            vol_sum += v
                            got = True
                if got:
                    volume = vol_sum

            readings.append(
                VDLaneReading(
                    ts=ts,
                    vdid=vdid,
                    lane_no=lane_no,
                    avg_speed=speed,
                    volume=volume,
                    occupancy=occupancy,
                )
            )

    return readings


# ---------- HTTP fetch ----------


async def fetch_vd_dynamic(
    client: httpx.AsyncClient | None = None,
    url: str = VD_DYNAMIC_URL,
) -> list[VDLaneReading]:
    """GET the dynamic VD XML and parse it into per-lane readings."""
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30)
    try:
        response = await client.get(url)
        response.raise_for_status()
        return parse_vd_dynamic_xml(response.text)
    finally:
        if owns_client:
            await client.aclose()


# ---------- DB write ----------


async def insert_vd_readings(session, readings: Iterable[VDLaneReading]) -> int:
    """Bulk-insert readings into vd_reading with ON CONFLICT DO NOTHING.

    Returns the number of rows attempted (Postgres' rowcount post-conflict
    is not reliably reported by asyncpg, so we just log the input size).
    """
    rows = [
        {
            "ts": r.ts,
            "vdid": r.vdid,
            "lane_no": r.lane_no,
            "avg_speed": r.avg_speed,
            "volume": r.volume,
            "occupancy": r.occupancy,
        }
        for r in readings
    ]
    if not rows:
        return 0
    stmt = pg_insert(VDReading).values(rows).on_conflict_do_nothing(
        index_elements=["ts", "vdid", "lane_no"],
    )
    await session.execute(stmt)
    await session.commit()
    return len(rows)


# ---------- Refresh cycle ----------


async def refresh_vd_cycle(
    graph: "RoadGraph",
    weight_provider: "WeightProvider",
    session_factory,
) -> dict:
    """Run one fetch -> insert -> rebuild -> apply cycle."""
    try:
        readings = await fetch_vd_dynamic()
    except Exception as exc:
        logger.error("VD fetch failed: %s — keeping previous weights", exc)
        return {"fetched": 0, "error": str(exc)}

    inserted = 0
    if readings:
        async with session_factory() as session:
            inserted = await insert_vd_readings(session, readings)

    await weight_provider.rebuild(session_factory)
    weight_provider.apply_to_graph(graph)

    logger.info(
        "refresh_vd_cycle: %d lane readings fetched, %d inserted, weights re-applied",
        len(readings),
        inserted,
    )
    return {"fetched": len(readings), "inserted": inserted}


async def run_periodic_vd_refresh(
    graph: "RoadGraph",
    weight_provider: "WeightProvider",
    session_factory,
    interval_seconds: int = DEFAULT_REFRESH_INTERVAL_SECONDS,
) -> None:
    """Background loop: refresh_vd_cycle every `interval_seconds`.

    Network or DB errors are logged but never crash the loop — A* keeps
    running with whatever weights are already loaded.
    """
    logger.info("VD live refresher starting — interval=%ds", interval_seconds)
    while True:
        try:
            await refresh_vd_cycle(graph, weight_provider, session_factory)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("refresh_vd_cycle unexpected error: %s", exc)
        await asyncio.sleep(interval_seconds)
