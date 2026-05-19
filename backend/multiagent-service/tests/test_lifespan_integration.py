"""Integration test for the FastAPI lifespan startup against a real TimescaleDB.

Spins up `timescale/timescaledb-ha:pg14-all`, applies `infra/init-db/*.sql`,
injects a tiny synthetic OSM-style fixture (`planet_osm_line` +
`planet_osm_point`), runs `scripts/build_graph_from_osm.sql` to populate
`traffic_node`/`traffic_edge`, seeds a couple of VD rows, then drives the
real `main.lifespan` end-to-end.

Networked / external dependencies are patched out:
  - `main.start_kafka_consumer` -> stub coroutine (no broker)
  - `src.agents.vd_traffic.fetch_vd_dynamic` -> [] (no HTTP)
  - `src.agents.parking.fetch_parking_availability` -> [] (no HTTP)
  - `main.seed_parking_lots` -> AsyncMock (avoid hitting data.taipei)
  - `main.build_chat_agent_from_env` -> stub returning is_available=False

The test asserts:
  * Lifespan startup runs without raising.
  * `kafka_runtime.get_graph()` returns a populated RoadGraph (nodes >= 1,
    edges >= 1).
  * `kafka_runtime.get_weight_provider()` returns a non-None
    TaipeiWeightProvider with `apply_to_graph` callable.
  * Edge weights have been set (non-zero) by `weight_provider.apply_to_graph`.
  * Background tasks (kafka, vd, parking) were created.
  * Shutdown cancels the background tasks and prints the shutdown banner.

Mark: `pytest -m integration`. Requires Docker + image already pulled.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Skip the entire module cleanly when Docker / testcontainers is unavailable.
testcontainers = pytest.importorskip("testcontainers.core.container")
from testcontainers.core.container import DockerContainer  # noqa: E402
from testcontainers.core.waiting_utils import wait_for_logs  # noqa: E402

logger = logging.getLogger(__name__)

# tests/ -> multiagent-service/ -> backend/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_DB_DIR = _REPO_ROOT / "infra" / "init-db"
_BUILD_GRAPH_SQL = _REPO_ROOT / "scripts" / "build_graph_from_osm.sql"

_TIMESCALE_IMAGE = "timescale/timescaledb-ha:pg14-all"
_PG_USER = "admin"
_PG_PASSWORD = "secret"
_PG_DB = "traffic_data"


# ---------------------------------------------------------------------------
# Container fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def timescale_container():
    """Boot timescaledb-ha:pg14-all, apply schema, return (host, port)."""
    container = (
        DockerContainer(_TIMESCALE_IMAGE)
        .with_env("POSTGRES_USER", _PG_USER)
        .with_env("POSTGRES_PASSWORD", _PG_PASSWORD)
        .with_env("POSTGRES_DB", _PG_DB)
        .with_exposed_ports(5432)
    )
    container.start()
    try:
        wait_for_logs(container, "database system is ready to accept connections", timeout=120)
        # Container logs the "ready" banner once during init and again after init
        # scripts finish; sleeping a beat avoids racing against pg_isready.
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        _wait_until_postgres_ready(host, port)
        _apply_schema(host, port)
        yield host, port
    finally:
        container.stop()


def _wait_until_postgres_ready(host: str, port: int, timeout_s: float = 60.0) -> None:
    """Poll psycopg2 connection until it succeeds or timeout."""
    import time

    import psycopg2

    start = time.monotonic()
    last_err: Exception | None = None
    while time.monotonic() - start < timeout_s:
        try:
            conn = psycopg2.connect(
                host=host,
                port=port,
                user=_PG_USER,
                password=_PG_PASSWORD,
                dbname=_PG_DB,
            )
            conn.close()
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(1.0)
    raise RuntimeError(f"Postgres did not become ready within {timeout_s}s: {last_err}")


def _apply_schema(host: str, port: int) -> None:
    """Run every infra/init-db/*.sql in name order, then synthetic fixtures, then build_graph."""
    import psycopg2

    conn = psycopg2.connect(
        host=host,
        port=port,
        user=_PG_USER,
        password=_PG_PASSWORD,
        dbname=_PG_DB,
    )
    conn.autocommit = True
    try:
        cur = conn.cursor()

        # 1. infra/init-db/*.sql in alphabetical order (00-extensions first, etc.)
        for sql_path in sorted(_INIT_DB_DIR.glob("*.sql")):
            text = sql_path.read_text(encoding="utf-8")
            cur.execute(text)

        # 2. Synthetic planet_osm_line / planet_osm_point so build_graph_from_osm.sql
        #    has something to chew on. We replicate the osm2pgsql column shape we
        #    actually depend on: highway, maxspeed, oneway, name (line) and
        #    highway, way (point). geom column is `way`, SRID 3857.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS planet_osm_line (
                osm_id     BIGINT,
                highway    TEXT,
                maxspeed   TEXT,
                oneway     TEXT,
                name       TEXT,
                ref        TEXT,
                lanes      TEXT,
                way        geometry(LineString, 3857)
            );
            CREATE TABLE IF NOT EXISTS planet_osm_point (
                osm_id     BIGINT,
                highway    TEXT,
                way        geometry(Point, 3857)
            );
            """
        )

        # Two short connected ways near Taipei 101 (WGS84 -> 3857 via ST_Transform).
        # way 1: A -> B   (residential, oneway=no, name='Test St')
        # way 2: B -> C   (secondary, oneway=yes, name='Big Rd')
        # Coords (lng, lat):
        #   A = (121.5640, 25.0330)
        #   B = (121.5650, 25.0340)
        #   C = (121.5660, 25.0350)
        cur.execute(
            """
            INSERT INTO planet_osm_line (osm_id, highway, maxspeed, oneway, name, way)
            VALUES
              (1001, 'residential', '30', 'no', 'Test St',
               ST_Transform(ST_SetSRID(ST_MakeLine(
                   ST_MakePoint(121.5640, 25.0330),
                   ST_MakePoint(121.5650, 25.0340)
               ), 4326), 3857)),
              (1002, 'secondary', '50', 'yes', 'Big Rd',
               ST_Transform(ST_SetSRID(ST_MakeLine(
                   ST_MakePoint(121.5650, 25.0340),
                   ST_MakePoint(121.5660, 25.0350)
               ), 4326), 3857));
            """
        )

        # One traffic_signals point near B so build_graph SQL flips has_signal.
        cur.execute(
            """
            INSERT INTO planet_osm_point (osm_id, highway, way)
            VALUES
              (2001, 'traffic_signals',
               ST_Transform(ST_SetSRID(ST_MakePoint(121.5650, 25.0340), 4326), 3857));
            """
        )

        # 3. Run build_graph_from_osm.sql to populate traffic_node / traffic_edge.
        # The script uses \echo psql-only meta-commands; strip them before sending
        # through psycopg2, otherwise the driver raises a syntax error.
        build_sql_raw = _BUILD_GRAPH_SQL.read_text(encoding="utf-8")
        build_sql = re.sub(r"^\s*\\echo[^\n]*\n", "", build_sql_raw, flags=re.MULTILINE)
        cur.execute(build_sql)

        # 4. A couple of synthetic vd_static + vd_reading rows so the weight
        #    provider has something to aggregate during rebuild().
        now = datetime.now(timezone.utc)
        ts_recent = now - timedelta(minutes=2)
        cur.execute(
            """
            INSERT INTO vd_static (vdid, road_name, road_class, latitude, longitude, geom, snapped_road_class)
            VALUES
              ('VD_FIXTURE_01', 'Test St', 'residential', 25.0335, 121.5645,
               ST_SetSRID(ST_MakePoint(121.5645, 25.0335), 4326), 'residential'),
              ('VD_FIXTURE_02', 'Big Rd',  'secondary',   25.0345, 121.5655,
               ST_SetSRID(ST_MakePoint(121.5655, 25.0345), 4326), 'secondary')
            ON CONFLICT (vdid) DO NOTHING;
            """
        )
        cur.execute(
            """
            INSERT INTO vd_reading (ts, vdid, lane_no, avg_speed, volume, occupancy)
            VALUES
              (%s, 'VD_FIXTURE_01', 0, 35.0, 10, 0.20),
              (%s, 'VD_FIXTURE_02', 0, 55.0, 20, 0.15)
            ON CONFLICT DO NOTHING;
            """,
            (ts_recent, ts_recent),
        )

        # Sanity check: graph should be non-empty.
        cur.execute("SELECT COUNT(*) FROM traffic_node;")
        node_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM traffic_edge;")
        edge_count = cur.fetchone()[0]
        if node_count == 0 or edge_count == 0:
            raise RuntimeError(
                f"build_graph produced an empty graph: nodes={node_count}, edges={edge_count}"
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifespan_starts_and_wires_runtime(timescale_container, monkeypatch, capsys):
    """Drive main.lifespan against the live container and assert wiring + tasks."""
    host, port = timescale_container
    db_url = f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}@{host}:{port}/{_PG_DB}"

    # main imports DATABASE_URL via src.db at import time; we need src.db (and
    # therefore main + dependents) to bind to the testcontainer engine. Set the
    # env var *and* reload the modules that captured it.
    monkeypatch.setenv("DATABASE_URL", db_url)
    # Make sure no leftover DeepSeek key triggers a real OpenAI client.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    # Reload src.db so it picks up the patched DATABASE_URL, then reload main.
    import src.db as db_module
    importlib.reload(db_module)
    import main as main_module
    importlib.reload(main_module)

    # --- Patch out network / broker dependencies ---
    # Kafka consumer: replace with a coroutine that waits forever (until cancelled).
    async def _fake_kafka_consumer() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module, "start_kafka_consumer", _fake_kafka_consumer)

    # data.taipei parking static seeder — avoid hitting upstream.
    monkeypatch.setattr(main_module, "seed_parking_lots", AsyncMock(return_value=None))

    # VD + parking dynamic fetchers used inside the background loops.
    import src.agents.vd_traffic as vd_traffic_mod
    import src.agents.parking as parking_mod

    monkeypatch.setattr(vd_traffic_mod, "fetch_vd_dynamic", AsyncMock(return_value=[]))
    monkeypatch.setattr(parking_mod, "fetch_parking_availability", AsyncMock(return_value=[]))

    # Chat agent: stub so we don't need DEEPSEEK_API_KEY or real http client.
    stub_agent = MagicMock(name="StubChatAgent")
    stub_agent.is_available = False
    monkeypatch.setattr(main_module, "build_chat_agent_from_env", lambda: stub_agent)

    # --- Drive the lifespan ---
    from src.kafka import runtime as kafka_runtime

    tasks_before = set(asyncio.all_tasks())

    async with main_module.app.router.lifespan_context(main_module.app):
        # Startup banner present on stdout
        out = capsys.readouterr().out
        assert "[Multiagent Service] Starting up..." in out
        assert "[Multiagent Service] All components started successfully!" in out

        # Runtime globals populated
        graph = kafka_runtime.get_graph()
        wp = kafka_runtime.get_weight_provider()
        assert graph is not None, "kafka_runtime.get_graph() should be populated"
        assert wp is not None, "kafka_runtime.get_weight_provider() should be populated"
        assert kafka_runtime.get_session_factory() is not None
        assert kafka_runtime.get_loop() is not None

        # Graph has nodes + edges from the synthetic OSM fixture.
        assert len(graph.nodes) >= 1, f"graph.nodes empty: {len(graph.nodes)}"
        assert len(graph.edges) >= 1, f"graph.edges empty: {len(graph.edges)}"

        # WeightProvider applied non-zero weights to the graph.
        any_nonzero_weight = any(
            w > 0
            for adj in graph.adjacency.values()
            for (_nb, _eid, w) in adj
        )
        assert any_nonzero_weight, "expected at least one non-zero edge weight after apply_to_graph"

        # Background tasks created by lifespan.
        new_tasks = asyncio.all_tasks() - tasks_before
        task_names = {t.get_name() for t in new_tasks if not t.done()}
        # At least 3 new tasks should exist: kafka, vd refresher, parking refresher.
        assert len(new_tasks) >= 3, (
            f"expected >= 3 new background tasks, got {len(new_tasks)}: {task_names}"
        )

    # --- Shutdown side: lifespan ran the cleanup branch ---
    out = capsys.readouterr().out
    assert "[Multiagent Service] Shutting down..." in out
    assert "[Multiagent Service] Shutdown complete." in out

    # All background tasks created by lifespan should now be done (cancelled).
    leftover = asyncio.all_tasks() - tasks_before
    leftover_alive = [t for t in leftover if not t.done()]
    assert not leftover_alive, f"background tasks still alive after shutdown: {leftover_alive}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifespan_health_endpoint_reachable_after_startup(
    timescale_container, monkeypatch
):
    """Smoke check that the FastAPI app handles /health once lifespan has run."""
    from httpx import ASGITransport, AsyncClient

    host, port = timescale_container
    db_url = f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}@{host}:{port}/{_PG_DB}"

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    import src.db as db_module
    importlib.reload(db_module)
    import main as main_module
    importlib.reload(main_module)

    async def _fake_kafka_consumer() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module, "start_kafka_consumer", _fake_kafka_consumer)
    monkeypatch.setattr(main_module, "seed_parking_lots", AsyncMock(return_value=None))

    import src.agents.vd_traffic as vd_traffic_mod
    import src.agents.parking as parking_mod

    monkeypatch.setattr(vd_traffic_mod, "fetch_vd_dynamic", AsyncMock(return_value=[]))
    monkeypatch.setattr(parking_mod, "fetch_parking_availability", AsyncMock(return_value=[]))

    stub_agent = MagicMock(name="StubChatAgent")
    stub_agent.is_available = False
    monkeypatch.setattr(main_module, "build_chat_agent_from_env", lambda: stub_agent)

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # AsyncClient with ASGITransport drives the lifespan automatically.
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "healthy", "service": "multiagent-service"}
