"""
TDX Live Traffic integration (Section path, Taipei).

Periodically fetches TDX `Live/City/Taipei` Section live data, drops `-99`
sentinel rows (TDX uses negative TravelSpeed/TravelTime/CongestionLevel for
"no data"), then:
  - Writes each section's live values to Redis under
    `traffic:section:{tdx_section_id}` (TTL 10 min).
  - Inserts a time-series row into the `traffic_history` hypertable.
  - Recomputes congestion_factor and patches the in-memory RoadGraph's edge
    weights (only for sections that map to a known edge).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.routing import MAX_CONGESTION_FACTOR, RoadGraph
from src.cache import redis_client
from src.db.models import TrafficHistory

logger = logging.getLogger(__name__)

TDX_AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TDX_LIVE_SECTION_URL = "https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/Live/City/Taipei"

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


# ---------- Live data fetch ----------


def _is_sentinel(row: dict) -> bool:
    """Return True if any of TravelSpeed/TravelTime/CongestionLevel is a TDX `-99` sentinel."""
    speed = row.get("TravelSpeed")
    travel_time = row.get("TravelTime")
    congestion = row.get("CongestionLevel")

    try:
        if speed is not None and float(speed) <= 0:
            return True
    except (TypeError, ValueError):
        return True
    try:
        if travel_time is not None and float(travel_time) <= 0:
            return True
    except (TypeError, ValueError):
        return True
    if congestion is not None and str(congestion).strip() == "-99":
        return True
    return False


async def fetch_live_section_data() -> list[dict]:
    """Call TDX Live City/Taipei Section API and return a normalised, filtered list.

    Each item: `{"tdx_section_id": str, "travel_speed": float, "travel_time": float | None}`.
    Rows with `-99` sentinels in TravelSpeed/TravelTime/CongestionLevel are dropped.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_access_token(client)
        response = await client.get(
            TDX_LIVE_SECTION_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"$format": "JSON"},
        )
        response.raise_for_status()
        payload = response.json()

    if isinstance(payload, dict):
        rows = (
            payload.get("LiveTraffics")
            or payload.get("Sections")
            or payload.get("LiveTrafficSections")
            or []
        )
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    normalised: list[dict] = []
    dropped = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("SectionID") or row.get("RoadSectionID")
        if not sid:
            continue
        if _is_sentinel(row):
            dropped += 1
            continue
        normalised.append(
            {
                "tdx_section_id": str(sid),
                "travel_speed": float(row["TravelSpeed"]),
                "travel_time": float(row["TravelTime"]) if row.get("TravelTime") is not None else None,
            }
        )
    if dropped:
        logger.debug("Filtered %d sentinel (-99) rows from TDX Live Taipei", dropped)
    return normalised


# ---------- Redis cache ----------


async def update_redis_cache(section_data: list[dict]) -> None:
    """Write each section's live data to Redis with a 10-min TTL."""
    if not section_data:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    pipe = redis_client.pipeline()
    for item in section_data:
        key = f"{REDIS_KEY_PREFIX}{item['tdx_section_id']}"
        pipe.set(
            key,
            json.dumps(
                {
                    "travel_speed": item["travel_speed"],
                    "travel_time": item["travel_time"],
                    "updated_at": now_iso,
                },
                ensure_ascii=False,
            ),
            ex=REDIS_TTL_SECONDS,
        )
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
    """Insert a snapshot row per section into traffic_history hypertable."""
    if not section_data:
        return
    now = datetime.now(timezone.utc)
    rows = [
        {
            "time": now,
            "tdx_section_id": item["tdx_section_id"],
            "travel_speed": item["travel_speed"],
            "travel_time": item["travel_time"],
        }
        for item in section_data
    ]
    await session.execute(insert(TrafficHistory), rows)
    await session.commit()


# ---------- Congestion factor + graph weight update ----------


def _congestion_factor(speed_limit: int, current_speed: float | None) -> float:
    """`min(speed_limit / current_speed, MAX)`, clamped to `[1.0, MAX]`.

    `current_speed = None` (no live data for this edge) → 1.0 (free flow).
    The `-99` sentinel filter upstream guarantees that any value reaching here
    is positive, so this function does not handle `current_speed <= 0`.
    """
    if current_speed is None or speed_limit <= 0:
        return 1.0
    return max(1.0, min(speed_limit / current_speed, MAX_CONGESTION_FACTOR))


def update_graph_weights(graph: RoadGraph, section_data: list[dict]) -> int:
    """Recompute congestion_factor per section and patch in-memory graph weights."""
    updated = 0
    for item in section_data:
        eid = graph.section_to_edge.get(item["tdx_section_id"])
        if eid is None:
            logger.debug("no edge mapping for tdx_section_id=%s", item["tdx_section_id"])
            continue
        edge = graph.edges[eid]
        factor = _congestion_factor(edge.speed_limit_kmh, item["travel_speed"])
        graph.update_weight(eid, factor)
        updated += 1
    return updated


# ---------- Orchestration ----------


async def refresh_traffic_data(session: AsyncSession, graph: RoadGraph) -> dict:
    """Fetch live TDX data and propagate it to Redis, TimescaleDB, and graph weights."""
    try:
        section_data = await fetch_live_section_data()
    except Exception as e:
        logger.error("TDX Live fetch failed: %s — retaining previous Redis cache", e)
        return {"fetched": 0, "updated_edges": 0, "error": str(e)}

    await update_redis_cache(section_data)

    try:
        await update_timescaledb(session, section_data)
    except Exception as e:
        logger.error("traffic_history insert failed: %s", e)

    updated = update_graph_weights(graph, section_data)
    logger.info(
        "refresh_traffic_data: %d sections fetched, %d edges updated",
        len(section_data),
        updated,
    )
    return {"fetched": len(section_data), "updated_edges": updated}


async def run_periodic_refresh(
    graph: RoadGraph,
    session_factory,
    interval_seconds: int = DEFAULT_REFRESH_INTERVAL_SECONDS,
) -> None:
    """Background loop: refresh_traffic_data every `interval_seconds`."""
    logger.info("TDX Live refresher (Taipei Section) starting — interval=%ds", interval_seconds)
    while True:
        try:
            async with session_factory() as session:
                await refresh_traffic_data(session, graph)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("refresh_traffic_data unexpected error: %s", e)
        await asyncio.sleep(interval_seconds)
