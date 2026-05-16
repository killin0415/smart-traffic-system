"""TDX VD (Vehicle Detector) live integration.

Source: https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/Live/VD/City/Taipei
Format: JSON, 5-min update cycle, OAuth2 client_credentials.
Each cycle:
  1. fetch_vd_dynamic() -> list[VDLaneReading]
  2. INSERT into vd_reading hypertable with ON CONFLICT DO NOTHING
  3. await weight_provider.rebuild()
  4. weight_provider.apply_to_graph(graph)

Historical note: an earlier implementation pointed at the data.taipei blob
`tcgbusfs.blob.core.windows.net/blobtisv/GetVDDATA.xml`, which has been stale
since 2024-11. TDX Live VD/City/Taipei now provides ~85% live coverage of
the same VDID space.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.agents.tdx_client import get_access_token
from src.db.models import VDReading

if TYPE_CHECKING:
    from src.agents.routing import RoadGraph
    from src.agents.weight_provider import WeightProvider

logger = logging.getLogger(__name__)

VD_LIVE_URL = (
    "https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/Live/VD/City/Taipei"
    "?$format=JSON"
)

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


# ---------- JSON parser ----------


def _non_negative_float(v: Any) -> float | None:
    """TDX uses -99 / -1 sentinels for "no data"; collapse them to None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f < 0:
        return None
    return f


def _non_negative_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        i = int(float(v))
    except (TypeError, ValueError):
        return None
    if i < 0:
        return None
    return i


def parse_vd_live_json(payload: dict) -> list[VDLaneReading]:
    """Parse TDX VDLive JSON into a list of per-lane readings.

    Schema (relevant nodes):
        {
          "UpdateTime": "...",
          "VDLives": [
            {
              "VDID": "V0111C0",
              "DataCollectTime": "2026-05-17T00:05:00+08:00",
              "Status": 0,
              "LinkFlows": [
                {
                  "LinkID": "...",
                  "Lanes": [
                    {
                      "LaneID": 0,
                      "Speed": 80.0,
                      "Occupancy": 3.0,
                      "Vehicles": [{"VehicleType": "S", "Volume": 10, ...}, ...]
                    }
                  ]
                }
              ]
            }
          ]
        }

    Rows missing VDID or DataCollectTime are skipped. Volume is summed across
    vehicle types per lane.
    """
    if not isinstance(payload, dict):
        return []

    readings: list[VDLaneReading] = []

    for vd in payload.get("VDLives") or []:
        if not isinstance(vd, dict):
            continue
        vdid = vd.get("VDID")
        ts_raw = vd.get("DataCollectTime")
        if not vdid or not ts_raw:
            continue

        try:
            ts = datetime.fromisoformat(ts_raw)
        except (TypeError, ValueError):
            ts = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        for lf in vd.get("LinkFlows") or []:
            if not isinstance(lf, dict):
                continue
            for lane in lf.get("Lanes") or []:
                if not isinstance(lane, dict):
                    continue

                lane_id_raw = lane.get("LaneID")
                try:
                    lane_no = int(lane_id_raw) if lane_id_raw is not None else 0
                except (TypeError, ValueError):
                    lane_no = 0

                speed = _non_negative_float(lane.get("Speed"))
                occupancy = _non_negative_float(lane.get("Occupancy"))

                # Volume: prefer summed Vehicles[].Volume; fall back to a flat
                # Volume field if the upstream schema is degraded.
                volume: int | None = None
                vehicles = lane.get("Vehicles")
                if isinstance(vehicles, list) and vehicles:
                    total = 0
                    got = False
                    for veh in vehicles:
                        if not isinstance(veh, dict):
                            continue
                        v = _non_negative_int(veh.get("Volume"))
                        if v is not None:
                            total += v
                            got = True
                    if got:
                        volume = total
                else:
                    volume = _non_negative_int(lane.get("Volume"))

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
    url: str = VD_LIVE_URL,
) -> list[VDLaneReading]:
    """GET the TDX Live VD JSON and parse it into per-lane readings."""
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30)
    try:
        token = await get_access_token(client)
        response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return parse_vd_live_json(response.json())
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
