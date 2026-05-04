"""
TDX Live Traffic integration (VD path).

Periodically fetches TDX `Live/VD/City/Kaohsiung`, filters out faulty lane
readings, aggregates per-edge speed via the persisted `vd_sensor` snap, then:
  - Writes each edge's section data to Redis under `traffic:section:{tdx_section_id}` (TTL 10 min).
  - Inserts a time-series row into `traffic_history` hypertable.
  - Recomputes congestion_factor and updates the in-memory RoadGraph's edge weights.

The City Live Section endpoint does not support Kaohsiung, so this service
relies entirely on the per-VD lane-level live API. VD → edge mapping is
loaded once from `vd_sensor` and cached for the lifetime of the refresher.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.routing import MAX_CONGESTION_FACTOR, RoadGraph
from src.cache import redis_client
from src.db.models import TrafficEdge, TrafficHistory, VDSensor

logger = logging.getLogger(__name__)

TDX_AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TDX_LIVE_VD_URL = "https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/Live/VD/City/Kaohsiung"

REDIS_KEY_PREFIX = "traffic:section:"
REDIS_TTL_SECONDS = 600  # 10 minutes

DEFAULT_REFRESH_INTERVAL_SECONDS = int(os.getenv("TDX_LIVE_REFRESH_SECONDS", "300"))


# ---------- OAuth token ----------


_token_cache: dict[str, object] = {"token": None, "expires_at": 0.0}


async def get_access_token(client: httpx.AsyncClient | None = None) -> str:
    """Fetch (and cache) a TDX OAuth2 access token using client credentials."""
    now = time.time()
    if _token_cache["token"] and now < float(_token_cache["expires_at"]) - 30:
        return str(_token_cache["token"])

    client_id = os.getenv("TDX_CLIENT_ID") or os.getenv("TDX-CLIENT-ID")
    client_secret = os.getenv("TDX_CLIENT_SECRET") or os.getenv("TDX-CLIENT-SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("TDX_CLIENT_ID / TDX_CLIENT_SECRET not set")

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30)
    try:
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
        payload = response.json()
        token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + expires_in
        return token
    finally:
        if owns_client:
            await client.aclose()


# ---------- VD Live data fetch ----------


def _filter_healthy(lanes: list[dict]) -> list[float]:
    """Drop lane readings with `Speed <= 0` or non-empty `ErrorType`."""
    healthy: list[float] = []
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        err = lane.get("ErrorType")
        if err:
            continue
        speed = lane.get("Speed")
        if speed is None:
            continue
        try:
            sval = float(speed)
        except (TypeError, ValueError):
            continue
        if sval <= 0:
            continue
        healthy.append(sval)
    return healthy


async def fetch_live_vd_data() -> dict[str, list[float]]:
    """Call TDX VD Live City/Kaohsiung and return `{vdid: [healthy_lane_speeds]}`."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_access_token(client)
        response = await client.get(
            TDX_LIVE_VD_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"$format": "JSON"},
        )
        response.raise_for_status()
        payload = response.json()

    if isinstance(payload, dict):
        records = payload.get("VDLives") or []
    elif isinstance(payload, list):
        records = payload
    else:
        records = []

    out: dict[str, list[float]] = {}
    for record in records:
        vdid = record.get("VDID")
        if not vdid:
            continue
        speeds: list[float] = []
        for link_flow in record.get("LinkFlows", []) or []:
            speeds.extend(_filter_healthy(link_flow.get("Lanes", []) or []))
        out[str(vdid)] = speeds
    return out


# ---------- Edge-level aggregation ----------


async def load_vd_edge_map(session: AsyncSession) -> dict[str, tuple[int, str | None, int]]:
    """Build `{vdid: (edge_id, tdx_section_id, speed_limit_kmh)}` from `vd_sensor` JOIN `traffic_edge`."""
    rows = (
        await session.execute(
            select(
                VDSensor.vdid,
                VDSensor.nearest_edge_id,
                TrafficEdge.tdx_section_id,
                TrafficEdge.speed_limit_kmh,
            )
            .join(TrafficEdge, VDSensor.nearest_edge_id == TrafficEdge.id)
        )
    ).all()
    return {
        str(vdid): (int(eid), tsid, int(speed_limit))
        for vdid, eid, tsid, speed_limit in rows
        if eid is not None
    }


def aggregate_edge_speeds(
    vd_data: dict[str, list[float]],
    edge_vd_map: dict[str, tuple[int, str | None, int]],
) -> tuple[list[dict], dict[str, int]]:
    """Average healthy lane speeds across all VDs on the same edge.

    Returns `(section_data, stats)` where:
      - `section_data` items = `{edge_id, tdx_section_id, speed_limit_kmh, travel_speed}`
        (only edges with at least one healthy reading)
      - `stats` = `{"vds_total", "vds_healthy", "edges_updated"}`
    """
    per_edge: dict[int, dict] = {}
    healthy_vd_count = 0

    for vdid, speeds in vd_data.items():
        mapping = edge_vd_map.get(vdid)
        if mapping is None or not speeds:
            continue
        healthy_vd_count += 1
        edge_id, tsid, speed_limit = mapping
        bucket = per_edge.setdefault(
            edge_id,
            {
                "edge_id": edge_id,
                "tdx_section_id": tsid,
                "speed_limit_kmh": speed_limit,
                "_speeds": [],
            },
        )
        bucket["_speeds"].extend(speeds)

    section_data: list[dict] = []
    for entry in per_edge.values():
        speeds = entry.pop("_speeds")
        if not speeds:
            continue
        avg = sum(speeds) / len(speeds)
        entry["travel_speed"] = avg
        entry["travel_time"] = None
        section_data.append(entry)

    stats = {
        "vds_total": len(vd_data),
        "vds_healthy": healthy_vd_count,
        "edges_updated": len(section_data),
    }
    return section_data, stats


# ---------- Redis cache ----------


async def update_redis_cache(section_data: list[dict]) -> None:
    """Write each edge's live data to Redis (key = `traffic:section:{tdx_section_id}`)."""
    if not section_data:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    pipe = redis_client.pipeline()
    wrote = 0
    for item in section_data:
        sid = item.get("tdx_section_id")
        if not sid:
            continue
        key = f"{REDIS_KEY_PREFIX}{sid}"
        pipe.set(
            key,
            json.dumps(
                {
                    "travel_speed": item.get("travel_speed"),
                    "travel_time": item.get("travel_time"),
                    "updated_at": now_iso,
                },
                ensure_ascii=False,
            ),
            ex=REDIS_TTL_SECONDS,
        )
        wrote += 1
    if wrote:
        await pipe.execute()


async def get_current_traffic(edge_ids: list[int], graph: RoadGraph) -> dict[int, dict]:
    """Read live traffic info for a set of edges via their tdx_section_id."""
    out: dict[int, dict] = {}
    if not edge_ids:
        return out

    keys: list[str] = []
    edge_for_key: list[int] = []
    for eid in edge_ids:
        edge = graph.edges.get(eid)
        if edge and edge.tdx_section_id:
            keys.append(f"{REDIS_KEY_PREFIX}{edge.tdx_section_id}")
            edge_for_key.append(eid)

    if not keys:
        return out

    values = await redis_client.mget(keys)
    for eid, raw in zip(edge_for_key, values):
        if raw is None:
            continue
        try:
            out[eid] = json.loads(raw)
        except json.JSONDecodeError:
            continue
    return out


# ---------- TimescaleDB write ----------


async def update_timescaledb(session: AsyncSession, section_data: list[dict]) -> None:
    """Insert a snapshot row per edge (with non-null tdx_section_id) into traffic_history."""
    rows = [
        {
            "time": datetime.now(timezone.utc),
            "tdx_section_id": item["tdx_section_id"],
            "travel_speed": item.get("travel_speed"),
            "travel_time": item.get("travel_time"),
        }
        for item in section_data
        if item.get("tdx_section_id")
    ]
    if not rows:
        return
    await session.execute(insert(TrafficHistory), rows)
    await session.commit()


# ---------- Congestion factor + graph weight update ----------


def _congestion_factor(speed_limit: int, current_speed: float | None) -> float:
    """congestion_factor = min(speed_limit / current_speed, MAX), clamped to [1.0, MAX]."""
    if current_speed is None:
        return 1.0
    if current_speed <= 0:
        return MAX_CONGESTION_FACTOR
    if speed_limit <= 0:
        return 1.0
    return max(1.0, min(speed_limit / current_speed, MAX_CONGESTION_FACTOR))


def update_graph_weights(graph: RoadGraph, section_data: list[dict]) -> int:
    """Recompute congestion_factor per edge and patch in-memory graph weights."""
    updated = 0
    for item in section_data:
        eid = item.get("edge_id")
        if eid is None or eid not in graph.edges:
            continue
        edge = graph.edges[eid]
        factor = _congestion_factor(edge.speed_limit_kmh, item.get("travel_speed"))
        graph.update_weight(eid, factor)
        updated += 1
    return updated


# ---------- Orchestration ----------


_edge_map_cache: dict[str, tuple[int, str | None, int]] | None = None


async def _get_edge_map(session: AsyncSession) -> dict[str, tuple[int, str | None, int]]:
    global _edge_map_cache
    if _edge_map_cache is None:
        _edge_map_cache = await load_vd_edge_map(session)
        logger.info("VD edge map loaded: %d VD→edge entries", len(_edge_map_cache))
    return _edge_map_cache


async def refresh_traffic_data(session: AsyncSession, graph: RoadGraph) -> dict:
    """Fetch live VD data and propagate to Redis, TimescaleDB, and graph weights."""
    try:
        vd_data = await fetch_live_vd_data()
    except Exception as e:
        logger.error("TDX VD Live fetch failed: %s — retaining previous Redis cache", e)
        return {"fetched": 0, "updated_edges": 0, "error": str(e)}

    edge_map = await _get_edge_map(session)
    section_data, stats = aggregate_edge_speeds(vd_data, edge_map)

    await update_redis_cache(section_data)

    try:
        await update_timescaledb(session, section_data)
    except Exception as e:
        logger.error("traffic_history insert failed: %s", e)

    updated = update_graph_weights(graph, section_data)
    logger.info(
        "refresh_traffic_data: %d VDs fetched, %d healthy, %d edges updated",
        stats["vds_total"],
        stats["vds_healthy"],
        updated,
    )
    return {
        "fetched": stats["vds_total"],
        "healthy": stats["vds_healthy"],
        "updated_edges": updated,
    }


async def run_periodic_refresh(
    graph: RoadGraph,
    session_factory,
    interval_seconds: int = DEFAULT_REFRESH_INTERVAL_SECONDS,
) -> None:
    """Background loop: refresh_traffic_data every `interval_seconds`."""
    logger.info("TDX Live refresher (VD path) starting — interval=%ds", interval_seconds)
    while True:
        try:
            async with session_factory() as session:
                await refresh_traffic_data(session, graph)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("refresh_traffic_data unexpected error: %s", e)
        await asyncio.sleep(interval_seconds)
