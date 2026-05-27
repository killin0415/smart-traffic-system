"""Integration tests verifying that the production database image
(`timescale/timescaledb-ha:pg14-all`) ships with the PostgreSQL extensions
this project depends on (TimescaleDB, PostGIS, postgis_topology, hstore --
the same list as `infra/init-db/00-extensions.sql`) and that the core
spatial / hypertable functions actually run.

These tests are marked `@pytest.mark.integration` and require Docker.

Dev-machine setup
-----------------
Before running for the first time you must pull the image once (~700 MB),
otherwise the first test invocation will spend several minutes pulling on
its own and may exceed the testcontainers default startup timeout::

    docker pull timescale/timescaledb-ha:pg14-all

Run the tests via uv (project rule -- never call .venv python directly)::

    uv run pytest tests/test_infra_extensions.py -m integration -v

To skip these in a unit-only run::

    uv run pytest -m 'not integration'
"""
from __future__ import annotations

import psycopg2
import pytest
from testcontainers.postgres import PostgresContainer

# All tests in this module are integration tests -- they spawn a real Docker
# container via testcontainers, so they're behind `-m integration`.
pytestmark = pytest.mark.integration


# The exact image listed in infra/docker-compose.yml / OpenSpec change
# `taipei-opendata-rebuild`. TimescaleDB-HA bundles PostGIS, postgis_topology
# and hstore alongside timescaledb on top of Postgres 14.
_IMAGE = "timescale/timescaledb-ha:pg14-all"


@pytest.fixture(scope="module")
def pg_container():
    """Spawn a fresh `timescaledb-ha:pg14-all` container for the whole module.

    Module-scoped so the ~30-60s PostGIS init only happens once for all
    tests in this file. The `PostgresContainer` waits on the
    "database system is ready to accept connections" log line before
    yielding, so by the time tests run psql is reachable.
    """
    container = PostgresContainer(
        image=_IMAGE,
        username="test_user",
        password="test_password",
        dbname="test_db",
        driver=None,  # we connect with raw psycopg2, no SQLAlchemy URL needed
    )
    # Give the heavy image extra headroom for cold-start initdb.
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def pg_conn(pg_container):
    """A psycopg2 connection in autocommit mode, reused across tests.

    Pulls host/port/user/password/db out of the testcontainer mapping so
    the tests don't hard-code anything.
    """
    conn = psycopg2.connect(
        host=pg_container.get_container_host_ip(),
        port=int(pg_container.get_exposed_port(5432)),
        user=pg_container.username,
        password=pg_container.password,
        dbname=pg_container.dbname,
    )
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Extension creation -- mirrors infra/init-db/00-extensions.sql exactly.
# ---------------------------------------------------------------------------


def test_create_extension_timescaledb(pg_conn):
    """`CREATE EXTENSION timescaledb` must succeed and register the extension."""
    with pg_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
        cur.execute("SELECT extname FROM pg_extension WHERE extname = 'timescaledb';")
        row = cur.fetchone()
    assert row is not None, "timescaledb extension was not registered after CREATE EXTENSION"
    assert row[0] == "timescaledb"


def test_create_extension_postgis(pg_conn):
    """`CREATE EXTENSION postgis` must succeed (prereq for postgis_topology)."""
    with pg_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        cur.execute("SELECT extname FROM pg_extension WHERE extname = 'postgis';")
        row = cur.fetchone()
    assert row is not None, "postgis extension was not registered after CREATE EXTENSION"
    assert row[0] == "postgis"


def test_create_extension_postgis_topology(pg_conn):
    """`CREATE EXTENSION postgis_topology` must succeed once postgis is loaded."""
    with pg_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")  # idempotent prereq
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis_topology;")
        cur.execute(
            "SELECT extname FROM pg_extension WHERE extname = 'postgis_topology';"
        )
        row = cur.fetchone()
    assert row is not None, "postgis_topology was not registered after CREATE EXTENSION"
    assert row[0] == "postgis_topology"


def test_create_extension_hstore(pg_conn):
    """`CREATE EXTENSION hstore` -- required by osm2pgsql --hstore."""
    with pg_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS hstore;")
        cur.execute("SELECT extname FROM pg_extension WHERE extname = 'hstore';")
        row = cur.fetchone()
    assert row is not None, "hstore extension was not registered after CREATE EXTENSION"
    assert row[0] == "hstore"


# ---------------------------------------------------------------------------
# Version probe -- PostGIS_Version() must return a parseable major >= 3.
# ---------------------------------------------------------------------------


def test_postgis_version_ge_3(pg_conn):
    """`SELECT PostGIS_Version();` returns e.g. '3.4 USE_GEOS=1 USE_PROJ=1 USE_STATS=1';
    we assert the leading major version is >= 3 (PostGIS 3.x is what
    timescaledb-ha:pg14-all ships and what the project targets).
    """
    with pg_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        cur.execute("SELECT PostGIS_Version();")
        version_str = cur.fetchone()[0]

    assert version_str, "PostGIS_Version() returned an empty value"
    # The output looks like '3.4 USE_GEOS=1 USE_PROJ=1 USE_STATS=1'; the
    # first whitespace-separated token is the dotted version.
    head = version_str.strip().split()[0]
    major_str = head.split(".")[0]
    assert major_str.isdigit(), (
        f"Could not parse a major version from PostGIS_Version() output: "
        f"{version_str!r}"
    )
    assert int(major_str) >= 3, (
        f"Expected PostGIS major version >= 3, got {version_str!r}"
    )


# ---------------------------------------------------------------------------
# Spatial functions used by the routing build pipeline must be callable.
# ---------------------------------------------------------------------------


def test_st_dumppoints_callable(pg_conn):
    """ST_DumpPoints unrolls a LineString's vertices -- used to build nodes
    from raw OSM ways in `02-build-graph.sql`. Three vertices in, three rows out.
    """
    with pg_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        cur.execute(
            """
            SELECT COUNT(*)
            FROM ST_DumpPoints(
                ST_GeomFromText('LINESTRING(0 0, 1 1, 2 2)', 4326)
            );
            """
        )
        count = cur.fetchone()[0]
    assert count is not None, "ST_DumpPoints returned NULL"
    assert count == 3, f"ST_DumpPoints should yield 3 points, got {count}"


def test_st_snaptogrid_callable(pg_conn):
    """ST_SnapToGrid quantises coordinates -- used to dedupe intersection
    nodes that share a position within ~1e-7 deg. Returns a non-null geometry.
    """
    with pg_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        cur.execute(
            """
            SELECT ST_AsText(
                ST_SnapToGrid(
                    ST_GeomFromText('POINT(121.5654321 25.0334567)', 4326),
                    0.0000001
                )
            );
            """
        )
        wkt = cur.fetchone()[0]
    assert wkt is not None, "ST_SnapToGrid returned NULL"
    assert wkt.startswith("POINT("), f"Expected a POINT WKT, got {wkt!r}"


def test_st_dwithin_callable(pg_conn):
    """ST_DWithin is the spatial join predicate used everywhere in the
    pipeline (snap VDs to nodes, parking to nodes, signals to intersections).
    Two points 0 deg apart must be within 1 deg; assert TRUE.
    """
    with pg_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        cur.execute(
            """
            SELECT ST_DWithin(
                ST_GeomFromText('POINT(121.5 25.0)', 4326),
                ST_GeomFromText('POINT(121.5 25.0)', 4326),
                1.0
            );
            """
        )
        within = cur.fetchone()[0]
    assert within is not None, "ST_DWithin returned NULL"
    assert within is True, "Identical points should report ST_DWithin = TRUE"


# ---------------------------------------------------------------------------
# TimescaleDB-specific smoke test: create_hypertable on a toy table.
# ---------------------------------------------------------------------------


def test_create_hypertable_callable(pg_conn):
    """`create_hypertable` is what powers vd_reading / parking_availability
    in `03-vd-tables.sql` and `04-parking-tables.sql`. Build a toy time-series
    table and convert it; the call must succeed and the table must appear in
    `timescaledb_information.hypertables`.
    """
    with pg_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
        # Fresh table per test run -- the connection / DB is throwaway.
        cur.execute("DROP TABLE IF EXISTS _smoke_ts;")
        cur.execute(
            """
            CREATE TABLE _smoke_ts (
                ts TIMESTAMPTZ NOT NULL,
                value DOUBLE PRECISION
            );
            """
        )
        cur.execute("SELECT create_hypertable('_smoke_ts', 'ts');")
        result = cur.fetchone()
        assert result is not None, "create_hypertable returned no row"

        cur.execute(
            """
            SELECT hypertable_name
            FROM timescaledb_information.hypertables
            WHERE hypertable_name = '_smoke_ts';
            """
        )
        ht_row = cur.fetchone()
    assert ht_row is not None, (
        "_smoke_ts was not registered as a hypertable after create_hypertable()"
    )
    assert ht_row[0] == "_smoke_ts"
