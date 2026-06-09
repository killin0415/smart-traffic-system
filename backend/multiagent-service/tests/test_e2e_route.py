"""End-to-end integration tests for the route-planning pipeline.

Scope (Level A — direct function E2E):
- Spin up `timescale/timescaledb-ha:pg14-all` via testcontainers.
- Apply the project's `infra/init-db/*.sql` schema.
- Build a tiny synthetic Taipei-shaped road graph directly via INSERTs
  (no osm2pgsql), modelling 台北車站 → 101 as a chain of nodes.
- Seed `vd_static` + `vd_reading` so `TaipeiWeightProvider` exercises
  Tier-1 (spatial) for inner edges and falls back to Tier-2 / Tier-3
  for outer edges; seed `parking_lot` + `parking_availability` and a
  `speed_camera` row attached to a specific edge.
- Call `plan_optimal_route(...)` directly and assert the response dict
  contains the four required fields:
    routes (≥ 1), estimated_time_min, speed_cameras, parking_suggestions
  (the OpenSpec acceptance criterion calls this "estimated_minutes" in
  prose; the actual `RouteResponse` schema names it `estimated_time_min`
  — see `src/mcp_servers/routing_tool.py:RouteItem`).

Level B (Kafka round-trip via `route.request` / `route.response`) is
intentionally deferred — it requires a Kafka testcontainer + lifespan
plumbing (~30-45s container startup, brittle on Windows Docker). Level A
already covers the acceptance criterion ("response 含 routes (≥1),
estimated_minutes, speed_cameras, parking_suggestions 欄位"). Level B
follow-up can call `handle_route_request` directly once the Kafka
infrastructure stabilises.

To run only this file:
    uv run pytest tests/test_e2e_route.py -m integration -v

To skip integration suite in fast loops:
    uv run pytest -m "not integration"
"""

from __future__ import annotations

import asyncio
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

# All tests in this module require docker + are slow.
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Paths to the canonical init-db SQL the project ships with.
# tests/ -> multiagent-service/ -> backend/ -> repo root
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_DB_DIR = _REPO_ROOT / "infra" / "init-db"

_INIT_SQL_FILES = [
    _INIT_DB_DIR / "00-extensions.sql",
    _INIT_DB_DIR / "02-road-network-tables.sql",
    _INIT_DB_DIR / "03-vd-tables.sql",
    _INIT_DB_DIR / "04-parking-tables.sql",
    _INIT_DB_DIR / "05-speed-limit-exception.sql",
    _INIT_DB_DIR / "06-default-maxspeed-fn.sql",
]


# ---------------------------------------------------------------------------
# Synthetic Taipei-shaped graph fixture.
#
# Build a small chain of nodes between 台北車站 (~25.0478, 121.5170) and
# 台北 101 (~25.0337, 121.5645) with two parallel routes so find_top_k_routes
# has alternatives. Coordinates are deliberately on a short straight bearing
# so haversine distances stay sane.
# ---------------------------------------------------------------------------

# Anchor points.
TAIPEI_STATION = (25.0478, 121.5170)
TAIPEI_101 = (25.0337, 121.5645)

# A handful of intermediate nodes along the main route, plus a parallel
# branch sharing endpoints (so top-K returns ≥ 2 routes).
_NODES: list[dict] = [
    # id, lat, lng, has_signal
    {"id": 1, "lat": 25.0478, "lng": 121.5170, "has_signal": True},   # 台北車站
    {"id": 2, "lat": 25.0450, "lng": 121.5230, "has_signal": True},
    {"id": 3, "lat": 25.0420, "lng": 121.5300, "has_signal": False},
    {"id": 4, "lat": 25.0390, "lng": 121.5380, "has_signal": True},
    {"id": 5, "lat": 25.0360, "lng": 121.5460, "has_signal": False},
    {"id": 6, "lat": 25.0337, "lng": 121.5645, "has_signal": True},   # 101
    # Parallel branch: 2 -> 7 -> 8 -> 5 (slightly longer)
    {"id": 7, "lat": 25.0500, "lng": 121.5300, "has_signal": False},
    {"id": 8, "lat": 25.0480, "lng": 121.5460, "has_signal": False},
]

_EDGES: list[dict] = [
    # id, source, target, road_name, length_km, road_class, max_speed_kmh
    {"id": 101, "src": 1, "tgt": 2, "name": "忠孝西路",  "len": 0.75, "cls": "primary",   "max": 50},
    {"id": 102, "src": 2, "tgt": 3, "name": "忠孝東路",  "len": 0.80, "cls": "primary",   "max": 50},
    {"id": 103, "src": 3, "tgt": 4, "name": "忠孝東路",  "len": 0.95, "cls": "primary",   "max": 50},
    {"id": 104, "src": 4, "tgt": 5, "name": "信義路",    "len": 0.90, "cls": "secondary", "max": 50},
    {"id": 105, "src": 5, "tgt": 6, "name": "信義路",    "len": 2.00, "cls": "secondary", "max": 50},
    # Parallel branch:
    {"id": 201, "src": 2, "tgt": 7, "name": "市民大道",  "len": 0.90, "cls": "tertiary",  "max": 40},
    {"id": 202, "src": 7, "tgt": 8, "name": "市民大道",  "len": 1.70, "cls": "tertiary",  "max": 40},
    {"id": 203, "src": 8, "tgt": 5, "name": "基隆路",    "len": 1.50, "cls": "secondary", "max": 50},
]

# Speed camera attached to a specific edge so the assertion is deterministic.
# Edge 103 (3 -> 4) on 忠孝東路.
_SPEED_CAMERAS: list[dict] = [
    {
        "id": 9001,
        "lat": 25.0405,
        "lng": 121.5340,
        "direction": "東向",
        "speed_limit": 50,
        "address": "忠孝東路 fixture",
        "nearest_edge_id": 103,
    },
]

# VD coverage: one VD near edge 102 (mid-route) so Tier-1 fires for inner
# edges and Tier-2 / Tier-3 fallback covers the outer ones.
_VD_STATIC: list[dict] = [
    {
        "vdid": "VD-FIX-001",
        "link_id": "L-001",
        "road_name": "忠孝東路",
        "road_class": "primary",
        "bidirectional": False,
        "bearing": "E",
        "lat": 25.0435,
        "lng": 121.5265,
        "snapped_road_class": "primary",
    },
    {
        "vdid": "VD-FIX-002",
        "link_id": "L-002",
        "road_name": "市民大道",
        "road_class": "tertiary",
        "bidirectional": False,
        "bearing": "E",
        "lat": 25.0490,
        "lng": 121.5380,
        "snapped_road_class": "tertiary",
    },
]

# Two recent readings per VD, well within the 10-minute window.
def _vd_readings_now() -> list[dict]:
    now = datetime.now(timezone.utc)
    rows = []
    for offset_min in (1, 3):
        ts = now - timedelta(minutes=offset_min)
        rows.append({"ts": ts, "vdid": "VD-FIX-001", "lane_no": 1, "speed": 28.0})
        rows.append({"ts": ts, "vdid": "VD-FIX-001", "lane_no": 2, "speed": 30.0})
        rows.append({"ts": ts, "vdid": "VD-FIX-002", "lane_no": 1, "speed": 22.0})
    return rows


# Parking lot near 101 destination, with availability >= 10 so it is
# picked up by query_parking_near_destination (min_available=10 default).
_PARKING_LOTS: list[dict] = [
    {
        "id": 7001,
        "name": "Fixture Parking 101",
        "address": "信義區 fixture",
        "total_car": 200,
        "total_motor": 0,
        "lat": 25.0340,
        "lng": 121.5650,  # ~50m from 101
    },
    {
        "id": 7002,
        "name": "Fixture Parking Empty",
        "address": "Far away",
        "total_car": 100,
        "total_motor": 0,
        "lat": 25.0341,
        "lng": 121.5648,
    },
]

# Parking availability rows: lot 7001 has plenty, lot 7002 has < 10 (filtered).
def _parking_availability_now() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"ts": now - timedelta(minutes=1), "lot_id": 7001, "available_car": 42, "available_motor": 0},
        {"ts": now - timedelta(minutes=1), "lot_id": 7002, "available_car": 3,  "available_motor": 0},
    ]


# ---------------------------------------------------------------------------
# Container + schema + fixture-data setup.
# ---------------------------------------------------------------------------


# The image timescaledb-ha:pg14-all carries PostGIS + TimescaleDB. The
# alternative timescale/timescaledb:latest-pg14 image does NOT bundle
# PostGIS, which our schema requires.
_IMAGE = "timescale/timescaledb-ha:pg14-all"


@pytest.fixture(scope="module")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Module-scoped event loop so the testcontainer is reused across tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
def _pg_container() -> Iterator[PostgresContainer]:
    """Boot timescaledb-ha and yield the running container."""
    container = (
        PostgresContainer(
            image=_IMAGE,
            username="admin",
            password="secret",
            dbname="traffic_data",
            driver=None,  # we'll attach asyncpg ourselves
        )
    )
    container.start()
    try:
        yield container
    finally:
        container.stop()


async def _exec_sql_file(session_factory, sql_path: Path) -> None:
    """Run a .sql file by splitting on plain semicolons.

    Sufficient for the project's init-db scripts which don't use
    dollar-quoted bodies *except* `06-default-maxspeed-fn.sql`. For that
    one, we route the whole file through a single `execute(text(...))`
    call inside a transaction so the $$...$$ body is preserved.
    """
    from sqlalchemy import text as _sql_text

    raw = sql_path.read_text(encoding="utf-8")

    async with session_factory() as session:
        if "$$" in raw:
            # Execute as a single block; asyncpg handles multiple statements.
            await session.execute(_sql_text(raw))
            await session.commit()
            return

        # Naive split — every statement in our files ends with `;` on its own.
        # Strip line comments first.
        cleaned_lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines)

        for stmt in cleaned.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            await session.execute(_sql_text(stmt))
        await session.commit()


async def _seed_fixture_data(session_factory) -> None:
    """Insert synthetic graph + VD + parking + speed camera rows."""
    from sqlalchemy import text as _sql_text

    async with session_factory() as session:
        # ---- traffic_node ----
        for n in _NODES:
            await session.execute(
                _sql_text(
                    "INSERT INTO traffic_node (id, latitude, longitude, geom, has_signal) "
                    "VALUES (:id, :lat, :lng, "
                    "        ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), :sig)"
                ),
                {"id": n["id"], "lat": n["lat"], "lng": n["lng"], "sig": n["has_signal"]},
            )
        # Reset the SERIAL sequence above any manual IDs we just inserted.
        await session.execute(
            _sql_text(
                "SELECT setval(pg_get_serial_sequence('traffic_node','id'), "
                "(SELECT MAX(id) FROM traffic_node))"
            )
        )

        # ---- traffic_edge ----
        for e in _EDGES:
            src = next(n for n in _NODES if n["id"] == e["src"])
            tgt = next(n for n in _NODES if n["id"] == e["tgt"])
            await session.execute(
                _sql_text(
                    "INSERT INTO traffic_edge "
                    "(id, source_node_id, target_node_id, road_name, length_km, "
                    " road_class, max_speed_kmh, oneway, geom) VALUES "
                    "(:id, :src, :tgt, :name, :len, :cls, :max, FALSE, "
                    " ST_SetSRID(ST_MakeLine("
                    "    ST_MakePoint(:slng, :slat), "
                    "    ST_MakePoint(:tlng, :tlat)), 4326))"
                ),
                {
                    "id": e["id"], "src": e["src"], "tgt": e["tgt"],
                    "name": e["name"], "len": e["len"],
                    "cls": e["cls"], "max": e["max"],
                    "slat": src["lat"], "slng": src["lng"],
                    "tlat": tgt["lat"], "tlng": tgt["lng"],
                },
            )
        await session.execute(
            _sql_text(
                "SELECT setval(pg_get_serial_sequence('traffic_edge','id'), "
                "(SELECT MAX(id) FROM traffic_edge))"
            )
        )

        # ---- speed_camera ----
        for c in _SPEED_CAMERAS:
            await session.execute(
                _sql_text(
                    "INSERT INTO speed_camera "
                    "(id, latitude, longitude, direction, speed_limit, address, "
                    " nearest_edge_id) VALUES "
                    "(:id, :lat, :lng, :dir, :sl, :addr, :eid)"
                ),
                {
                    "id": c["id"], "lat": c["lat"], "lng": c["lng"],
                    "dir": c["direction"], "sl": c["speed_limit"],
                    "addr": c["address"], "eid": c["nearest_edge_id"],
                },
            )

        # ---- vd_static ----
        for v in _VD_STATIC:
            await session.execute(
                _sql_text(
                    "INSERT INTO vd_static "
                    "(vdid, link_id, road_name, road_class, bidirectional, "
                    " bearing, latitude, longitude, geom, snapped_road_class) "
                    "VALUES (:vdid, :link, :name, :cls, :bi, :br, :lat, :lng, "
                    "        ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), "
                    "        :snap)"
                ),
                {
                    "vdid": v["vdid"], "link": v["link_id"], "name": v["road_name"],
                    "cls": v["road_class"], "bi": v["bidirectional"],
                    "br": v["bearing"], "lat": v["lat"], "lng": v["lng"],
                    "snap": v["snapped_road_class"],
                },
            )

        # ---- vd_reading ----
        for r in _vd_readings_now():
            await session.execute(
                _sql_text(
                    "INSERT INTO vd_reading (ts, vdid, lane_no, avg_speed, "
                    "                        volume, occupancy) "
                    "VALUES (:ts, :vdid, :lane, :spd, 100, 10.0)"
                ),
                {"ts": r["ts"], "vdid": r["vdid"], "lane": r["lane_no"],
                 "spd": r["speed"]},
            )

        # ---- parking_lot ----
        for p in _PARKING_LOTS:
            await session.execute(
                _sql_text(
                    "INSERT INTO parking_lot "
                    "(id, name, address, total_car, total_motor, latitude, "
                    " longitude, geom) VALUES "
                    "(:id, :name, :addr, :tc, :tm, :lat, :lng, "
                    " ST_SetSRID(ST_MakePoint(:lng, :lat), 4326))"
                ),
                {
                    "id": p["id"], "name": p["name"], "addr": p["address"],
                    "tc": p["total_car"], "tm": p["total_motor"],
                    "lat": p["lat"], "lng": p["lng"],
                },
            )

        # ---- parking_availability ----
        for a in _parking_availability_now():
            await session.execute(
                _sql_text(
                    "INSERT INTO parking_availability "
                    "(ts, lot_id, available_car, available_motor) "
                    "VALUES (:ts, :lot, :ac, :am)"
                ),
                {"ts": a["ts"], "lot": a["lot_id"],
                 "ac": a["available_car"], "am": a["available_motor"]},
            )

        await session.commit()


@pytest_asyncio.fixture(scope="module")
async def session_factory(_pg_container):
    """Async session factory pointed at the test container.

    Also applies the project's init-db schema and seeds fixture data once
    per module. Yields a `sessionmaker` callable identical in shape to
    `src.db.async_session` so SUT code can be invoked unchanged.
    """
    # Build asyncpg URL from the testcontainer.
    raw_url = _pg_container.get_connection_url(driver=None)
    # raw_url looks like: postgresql://admin:secret@localhost:NNNN/traffic_data
    asyncpg_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(asyncpg_url, echo=False, poolclass=NullPool)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Apply schema files in dependency order.
    for sql_path in _INIT_SQL_FILES:
        await _exec_sql_file(factory, sql_path)

    # Seed synthetic Taipei-shaped fixture data.
    await _seed_fixture_data(factory)

    yield factory

    await engine.dispose()


@pytest_asyncio.fixture(scope="module")
async def loaded_graph(session_factory):
    """RoadGraph loaded from the fixture DB + WeightProvider applied."""
    from src.agents.routing import RoadGraph
    from src.agents.weight_provider import TaipeiWeightProvider

    async with session_factory() as session:
        graph = await RoadGraph.from_db(session)

    weight_provider = TaipeiWeightProvider()
    await weight_provider.rebuild(session_factory)
    weight_provider.apply_to_graph(graph)
    return graph, weight_provider


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_plan_optimal_route_e2e_returns_routes_with_required_fields(
    session_factory, loaded_graph
):
    """Happy path: 台北車站 → 101 returns ≥1 route with all 4 required fields."""
    from src.agents.routing import plan_optimal_route

    graph, weight_provider = loaded_graph
    o_lat, o_lng = TAIPEI_STATION
    d_lat, d_lng = TAIPEI_101

    async with session_factory() as session:
        out = await plan_optimal_route(
            session, graph, weight_provider,
            o_lat, o_lng, d_lat, d_lng,
            k=3,
        )

    # Top-level shape.
    assert set(out.keys()) == {"routes", "error"}, out
    assert out["error"] is None, out["error"]

    # Acceptance criterion: ≥ 1 route.
    assert isinstance(out["routes"], list)
    assert len(out["routes"]) >= 1

    # Each route carries the four required fields.
    # (`estimated_minutes` in the OpenSpec prose is `estimated_time_min` in the
    # actual `RouteItem` schema; we assert the real field name.)
    for route in out["routes"]:
        assert "estimated_time_min" in route
        assert "speed_cameras" in route
        assert "parking_suggestions" in route
        assert isinstance(route["estimated_time_min"], (int, float))
        assert route["estimated_time_min"] > 0
        assert math.isfinite(route["estimated_time_min"])
        assert isinstance(route["speed_cameras"], list)
        assert isinstance(route["parking_suggestions"], list)
        # Route metadata sanity.
        assert isinstance(route["path"], list) and len(route["path"]) >= 2
        assert isinstance(route["edges"], list) and len(route["edges"]) >= 1
        assert isinstance(route["distance_km"], (int, float))
        assert route["distance_km"] > 0

    # Top-K must be sorted by estimated_time_min ascending (best first).
    times = [r["estimated_time_min"] for r in out["routes"]]
    assert times == sorted(times), (
        f"routes not sorted by estimated_time_min: {times}"
    )


async def test_plan_optimal_route_e2e_unreachable_origin_returns_empty_routes(
    session_factory, loaded_graph
):
    """Origin far outside the synthetic graph -> no usable route.

    The graph spans roughly (25.034..25.050, 121.517..121.565). Picking an
    origin in the South China Sea forces `snap_to_graph` to bind to some
    node, but `find_top_k_routes` with the computed bbox will not return
    any meaningful path connecting that far origin to 101 in our tiny
    graph — we accept either an empty list or an `error` populated.
    """
    from src.agents.routing import plan_optimal_route

    graph, weight_provider = loaded_graph
    # Way out of the bbox — Singapore-ish.
    o_lat, o_lng = 1.3521, 103.8198
    d_lat, d_lng = TAIPEI_101

    async with session_factory() as session:
        out = await plan_optimal_route(
            session, graph, weight_provider,
            o_lat, o_lng, d_lat, d_lng,
            k=3,
        )

    assert set(out.keys()) == {"routes", "error"}
    # Sensible empty payload: either no routes at all (the contract this
    # test cares about) OR an error string populated.
    if out["routes"]:
        # Some routes — must still be well-formed.
        for r in out["routes"]:
            assert "estimated_time_min" in r
        # In that case, error must be None.
        assert out["error"] is None
    else:
        # Empty routes: error must be set so caller can distinguish from
        # "no routes found".
        assert out["error"], (
            "When routes is empty, an `error` string must be populated."
        )


async def test_plan_optimal_route_e2e_includes_parking_suggestions(
    session_factory, loaded_graph
):
    """Destination near a parking_lot with availability ≥ 10 -> non-empty
    `parking_suggestions` on the best route."""
    from src.agents.routing import plan_optimal_route

    graph, weight_provider = loaded_graph
    o_lat, o_lng = TAIPEI_STATION
    d_lat, d_lng = TAIPEI_101

    async with session_factory() as session:
        out = await plan_optimal_route(
            session, graph, weight_provider,
            o_lat, o_lng, d_lat, d_lng,
            k=3,
        )

    assert out["error"] is None
    assert out["routes"], "expected at least one route"

    best = out["routes"][0]
    suggestions = best["parking_suggestions"]
    assert isinstance(suggestions, list)
    assert len(suggestions) >= 1, (
        "parking_lot 7001 (42 spaces, ~50m from destination) should appear"
    )
    # The fixture lot 7001 has 42 spaces; lot 7002 has 3 (filtered out by
    # the min_available=10 threshold inside query_parking_near_destination).
    names = {s["name"] for s in suggestions}
    assert "Fixture Parking 101" in names
    assert "Fixture Parking Empty" not in names

    # Each suggestion carries the documented fields.
    for s in suggestions:
        for key in ("id", "name", "available_car", "distance_m",
                    "latitude", "longitude"):
            assert key in s, f"parking suggestion missing key {key!r}: {s}"
        assert s["available_car"] >= 10

    # Per implementation: parking_suggestions are attached only to the best
    # route (idx == 0). All others must be [].
    for r in out["routes"][1:]:
        assert r["parking_suggestions"] == []


async def test_plan_optimal_route_e2e_includes_speed_cameras(
    session_factory, loaded_graph
):
    """Route 台北車站 -> 101 passes through edge 103, which has a speed
    camera attached -> at least one route has non-empty `speed_cameras`."""
    from src.agents.routing import plan_optimal_route

    graph, weight_provider = loaded_graph
    o_lat, o_lng = TAIPEI_STATION
    d_lat, d_lng = TAIPEI_101

    async with session_factory() as session:
        out = await plan_optimal_route(
            session, graph, weight_provider,
            o_lat, o_lng, d_lat, d_lng,
            k=3,
        )

    assert out["error"] is None
    assert out["routes"]

    # Find any route that traverses edge 103.
    routes_through_103 = [r for r in out["routes"] if 103 in r["edges"]]
    assert routes_through_103, (
        f"expected at least one route through edge 103; got edges="
        f"{[r['edges'] for r in out['routes']]}"
    )

    # Every such route must include the camera attached to edge 103.
    for r in routes_through_103:
        cams = r["speed_cameras"]
        assert isinstance(cams, list)
        assert len(cams) >= 1, (
            f"route through edge 103 should expose its speed camera; "
            f"speed_cameras={cams}"
        )
        # Check field shape on the first camera.
        c0 = cams[0]
        for key in ("latitude", "longitude", "speed_limit", "address",
                    "direction"):
            assert key in c0, f"speed camera missing key {key!r}: {c0}"
        assert c0["speed_limit"] == 50
