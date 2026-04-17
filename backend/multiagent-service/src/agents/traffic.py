"""
TDX Live Traffic integration.

Periodically fetches TDX Live Section API data, then:
  - Writes each section to Redis under `traffic:section:{tdx_section_id}` (TTL 10 min).
  - Inserts a time-series row into `traffic_history` hypertable.
  - Recomputes congestion_factor and updates the in-memory RoadGraph's edge weights.

Note: TDX's `basic/v2/Road/Traffic/Live/City/{City}` endpoint does NOT list
Kaohsiung as a supported city (it returns HTTP 400 "is not accepted"). When
that happens we log once and the refresher degrades to a silent no-op — A*
routing still works using the base edge weights. VD-based live integration
for Kaohsiung is tracked as a separate OpenSpec change.
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
TDX_LIVE_SECTION_URL = "https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/Live/City/Kaohsiung"

REDIS_KEY_PREFIX = "traffic:section:"
REDIS_TTL_SECONDS = 600  # 10 minutes

DEFAULT_REFRESH_INTERVAL_SECONDS = int(os.getenv("TDX_LIVE_REFRESH_SECONDS", "300"))

_unsupported_city_logged = False


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


async def fetch_live_section_data() -> list[dict]:
    """Call TDX Live Section API for Kaohsiung and return a normalised list.

    Each item: {"tdx_section_id": str, "travel_speed": float|None, "travel_time": float|None}

    Returns [] if TDX doesn't support this city (HTTP 400 "is not accepted"),
    logging the reason once.
    """
    global _unsupported_city_logged
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_access_token(client)
        response = await client.get(
            TDX_LIVE_SECTION_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"$format": "JSON"},
        )
        if response.status_code == 400 and "is not accepted" in response.text:
            if not _unsupported_city_logged:
                logger.warning(
                    "TDX Live City endpoint does not support Kaohsiung — refresher will no-op. "
                    "Details: %s",
                    response.text.strip(),
                )
                _unsupported_city_logged = True
            return []
        response.raise_for_status()
        payload = response.json()

    # TDX payloads have historically shifted between top-level list and {"LiveTraffics":[...]}; handle both.
    if isinstance(payload, dict):
        rows = (
            payload.get("LiveTraffics")
            or payload.get("Sections")
            or payload.get("LiveTrafficSections")
            or payload.get("RoadLiveTraffics")
            or []
        )
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    normalised: list[dict] = []
    for row in rows:
        sid = row.get("SectionID") or row.get("RoadSectionID")
        if not sid:
            continue
        speed = row.get("TravelSpeed")
        travel_time = row.get("TravelTime")
        normalised.append(
            {
                "tdx_section_id": str(sid),
                "travel_speed": float(speed) if speed is not None else None,
                "travel_time": float(travel_time) if travel_time is not None else None,
            }
        )
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
    """Read live traffic info for a set of edges via their tdx_section_id.

    Returns {edge_id: {travel_speed, travel_time, updated_at}} for edges that have data.
    """
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
    """congestion_factor = min(speed_limit / current_speed, MAX).

    current_speed <= 0 → MAX; None → 1.0 (no data ⇒ assume free-flow).
    """
    if current_speed is None:
        return 1.0
    if current_speed <= 0:
        return MAX_CONGESTION_FACTOR
    if speed_limit <= 0:
        return 1.0
    return min(speed_limit / current_speed, MAX_CONGESTION_FACTOR)


def update_graph_weights(graph: RoadGraph, section_data: list[dict]) -> int:
    """Recompute congestion_factor per section and patch the in-memory graph weights.

    Returns number of edges updated.
    """
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
    if section_data:
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
    """Background loop: refresh_traffic_data every `interval_seconds`.

    Designed to be started via `asyncio.create_task()` in lifespan.
    """
    logger.info("TDX Live refresher starting — interval=%ds", interval_seconds)
    while True:
        try:
            async with session_factory() as session:
                await refresh_traffic_data(session, graph)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("refresh_traffic_data unexpected error: %s", e)
        await asyncio.sleep(interval_seconds)
