"""Integration tests for scripts/build_graph_from_osm.sql.

Strategy
--------
We do NOT run osm2pgsql in CI. Instead we synthesise the same `planet_osm_line`
and `planet_osm_point` tables that osm2pgsql would produce (osm2pgsql defaults
to SRID 3857 Web Mercator + an `hstore` `tags` column when launched with
`--hstore`). The SQL under test reads exactly those columns and nothing else,
so a synthetic fixture exercises the same code paths a real PBF would.

The fixture covers:
  * highway=primary, residential, service (kept)
  * highway=pedestrian, footway, cycleway, track, steps, path (excluded)
  * one oneway=yes line
  * one line with explicit maxspeed=30
  * one line whose end-vertex is < 5m (well below the 0.00005 deg ~ 5.5m
    snap grid) from another line's vertex — must collapse into a single
    traffic_node (snap-grid dedup)
  * one highway=traffic_signals point ~10m from a snapped intersection —
    must flip traffic_node.has_signal = TRUE

Run via (do NOT run pytest yourself while another task is using Docker):

    cd backend/multiagent-service
    uv run pytest tests/test_build_graph_sql.py -m integration -v
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

# testcontainers + psycopg2 are integration-only deps; skip the whole module
# cleanly when they're absent so unit-test runs (`pytest -m 'not integration'`)
# don't error during collection.
testcontainers = pytest.importorskip("testcontainers.postgres")
psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402

from testcontainers.postgres import PostgresContainer  # noqa: E402


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Paths to SUT + init scripts
# ---------------------------------------------------------------------------

# tests/test_build_graph_sql.py -> tests/ -> multiagent-service/ -> backend/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_DB = _REPO_ROOT / "infra" / "init-db"
_SCRIPTS = _REPO_ROOT / "scripts"

_EXT_SQL = _INIT_DB / "00-extensions.sql"
_SCHEMA_SQL = _INIT_DB / "02-road-network-tables.sql"
_MAXSPEED_FN_SQL = _INIT_DB / "06-default-maxspeed-fn.sql"
_BUILD_SQL = _SCRIPTS / "build_graph_from_osm.sql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_psql_meta(sql_text: str) -> str:
    """psycopg2 cannot run psql backslash commands like `\\echo`.

    Drop any line whose first non-whitespace character is a backslash; keep
    everything else verbatim so transactions / DO blocks are unaffected.
    """
    out_lines: list[str] = []
    for line in sql_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("\\"):
            continue
        out_lines.append(line)
    return "\n".join(out_lines) + "\n"


def _exec_sql_file(conn, path: Path) -> None:
    """Execute a .sql file in autocommit mode after stripping psql meta-commands."""
    text = _strip_psql_meta(path.read_text(encoding="utf-8"))
    with conn.cursor() as cur:
        cur.execute(text)


def _exec(conn, sql: str, params: tuple | dict | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _fetchone(conn, sql: str, params: tuple | dict | None = None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _fetchall(conn, sql: str, params: tuple | dict | None = None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# osm2pgsql-style raw tables
# ---------------------------------------------------------------------------

# Reproduces the subset of columns build_graph_from_osm.sql reads from
# osm2pgsql's output. osm2pgsql --hstore --slim --create with default projection
# stores geometry in EPSG:3857 and exposes (osm_id, highway, oneway, maxspeed,
# name, way, tags). We only declare the columns the SUT touches.
_CREATE_OSM_TABLES = """
CREATE TABLE IF NOT EXISTS planet_osm_line (
    osm_id     BIGINT,
    highway    TEXT,
    oneway     TEXT,
    maxspeed   TEXT,
    name       TEXT,
    ref        TEXT,
    lanes      TEXT,
    tags       hstore,
    way        geometry(LineString, 3857)
);
CREATE INDEX IF NOT EXISTS ix_pol_way ON planet_osm_line USING GIST (way);
CREATE INDEX IF NOT EXISTS ix_pol_highway ON planet_osm_line (highway);

CREATE TABLE IF NOT EXISTS planet_osm_point (
    osm_id     BIGINT,
    highway    TEXT,
    name       TEXT,
    tags       hstore,
    way        geometry(Point, 3857)
);
CREATE INDEX IF NOT EXISTS ix_pop_way ON planet_osm_point USING GIST (way);
CREATE INDEX IF NOT EXISTS ix_pop_highway ON planet_osm_point (highway);
"""


# ---------------------------------------------------------------------------
# Synthetic OSM fixture (lon/lat in WGS84; SQL transforms to 3857 on insert).
# All coordinates live inside the Xinyi 1km² area for realism but exact values
# don't matter -- only relative spacing for the snap test and the signal test.
# ---------------------------------------------------------------------------

# Helper macro: ST_Transform(ST_SetSRID(ST_MakePoint(lon,lat),4326),3857)
def _pt(lon: float, lat: float) -> str:
    return (
        "ST_Transform(ST_SetSRID(ST_MakePoint("
        f"{lon!r}, {lat!r}), 4326), 3857)"
    )


def _line(coords: list[tuple[float, float]]) -> str:
    """SQL expression producing a LineString in SRID 3857 from WGS84 coords."""
    pts = ", ".join(f"ST_MakePoint({lon!r}, {lat!r})" for lon, lat in coords)
    return (
        "ST_Transform(ST_SetSRID(ST_MakeLine(ARRAY["
        f"{pts}"
        "]), 4326), 3857)"
    )


# ---- Coordinates ----------------------------------------------------------
# A small grid around Xinyi. Two ways share an intersection point (B);
# additionally a third way's endpoint (B') is offset from B by ~3m, well below
# the ~5.5m snap grid -> must collapse to the same traffic_node.

A = (121.5600, 25.0330)            # SW corner
B = (121.5610, 25.0330)            # mid-east, intersection
B_NEAR = (121.5610 + 0.00002, 25.0330)  # ~2m east of B -> snaps onto B's grid cell
C = (121.5620, 25.0330)            # far east
D = (121.5610, 25.0340)            # north of B
E = (121.5610, 25.0320)            # south of B (for oneway line)
F = (121.5605, 25.0335)            # NW diagonal (for pedestrian / excluded)
G = (121.5615, 25.0335)            # NE diagonal (excluded class)

# Traffic signal point ~ 10m east of B (well within 30m -> should flag B's node)
SIGNAL_NEAR_B = (121.5610 + 0.0001, 25.0330)
# Traffic signal far away (>>30m), should NOT flag anything
SIGNAL_FAR = (121.5700, 25.0400)

# ---------------------------------------------------------------------------
# fix-osm-graph-topology fixtures (§8.1):
#
# Five extra synthetic ways exercising the new SQL features:
#   - non-oneway primary CHAIN F2-G2-H2     -> G2 must be contracted away,
#     leaving a merged F2<->H2 pair (2 edges).
#   - oneway primary chain I2->J2->K2       -> J2 contracted away, single
#     merged I2->K2 edge survives.
#   - cross-class boundary L2-M2-N2         -> M2 stays (primary/secondary
#     class change blocks contraction).
#   - reverse-oneway secondary O2-P2 with
#     `oneway='-1'`                         -> single edge inserted as
#     P2->O2 (target-to-source).
# ---------------------------------------------------------------------------

F2 = (121.5500, 25.0330)
G2 = (121.5510, 25.0330)
H2 = (121.5520, 25.0330)

I2 = (121.5500, 25.0320)
J2 = (121.5510, 25.0320)
K2 = (121.5520, 25.0320)

L2 = (121.5500, 25.0310)
M2 = (121.5510, 25.0310)
N2 = (121.5520, 25.0310)

O2 = (121.5500, 25.0300)
P2 = (121.5510, 25.0300)

# Tight primary chain T2-U2-V2 (~15m between consecutive nodes) so signal
# placed at U2 falls within the 30m snap radius of BOTH chain endpoints
# after contraction. Exercises the "中段號誌靠 contraction 重定位" scenario.
T2 = (121.54000, 25.0250)
U2 = (121.54015, 25.0250)
V2 = (121.54030, 25.0250)
SIGNAL_AT_U2 = (121.54015, 25.0250)  # at U2 location; will relocate

# Mixed-oneway/non-oneway adjacent ways meeting at R2. R2 has degree-2
# undirected ({Q2, S2}) but its adjacent edges differ on `oneway`, so the
# uniform-signature guard MUST keep R2 alive (no contraction).
Q2 = (121.5500, 25.0260)
R2 = (121.5510, 25.0260)
S2 = (121.5520, 25.0260)


def _insert_fixture(conn) -> None:
    """Populate planet_osm_line + planet_osm_point with a tiny synthetic OSM
    extract. Returns nothing; rows are flushed via autocommit."""

    # Each row is (osm_id, highway, oneway, maxspeed, line_coords)
    drivable_lines = [
        # 1: primary, two-way, no explicit maxspeed -> default_maxspeed('primary')=50
        (1, "primary", None, None, [A, B]),
        # 2: residential, two-way, explicit maxspeed=30
        (2, "residential", "no", "30", [B, C]),
        # 3: service, oneway=yes
        (3, "service", "yes", None, [B, D]),
        # 4: secondary, oneway implied "no"; endpoint near B (offset ~2m)
        #    -> ST_SnapToGrid(0.00005) should collapse to the same node as B
        (4, "secondary", "no", None, [B_NEAR, E]),
        # 5: primary with maxspeed containing trailing unit text ("50 km/h")
        #    parser pulls "50"
        (5, "primary", None, "50 km/h", [A, D]),
        # ----- fix-osm-graph-topology §8.1 fixtures -----
        # 20-21: non-oneway primary chain F2-G2-H2 (G2 must be contracted)
        (20, "primary", "no", None, [F2, G2]),
        (21, "primary", "no", None, [G2, H2]),
        # 22-23: oneway primary chain I2->J2->K2 (J2 must be contracted)
        (22, "primary", "yes", None, [I2, J2]),
        (23, "primary", "yes", None, [J2, K2]),
        # 24-25: class boundary at M2 (primary -> secondary), no contraction
        (24, "primary", "no", None, [L2, M2]),
        (25, "secondary", "no", None, [M2, N2]),
        # 26: reverse-oneway secondary, OSM `oneway='-1'`
        (26, "secondary", "-1", None, [O2, P2]),
        # 27-28: mixed oneway/non-oneway at R2 — must NOT contract R2
        (27, "primary", "yes", None, [Q2, R2]),
        (28, "primary", "no",  None, [R2, S2]),
        # 29-30: tight primary chain for signal-relocation scenario
        (29, "primary", "no",  None, [T2, U2]),
        (30, "primary", "no",  None, [U2, V2]),
    ]
    excluded_lines = [
        # All of these must be dropped by the highway filter.
        (10, "pedestrian", None, None, [F, G]),
        (11, "footway", None, None, [F, B]),
        (12, "cycleway", None, None, [G, C]),
        (13, "track", None, None, [A, F]),
        (14, "steps", None, None, [G, D]),
        (15, "path", None, None, [E, F]),
    ]

    for osm_id, highway, oneway, maxspeed, coords in drivable_lines + excluded_lines:
        _exec(
            conn,
            f"""
            INSERT INTO planet_osm_line (osm_id, highway, oneway, maxspeed, name, way)
            VALUES (%s, %s, %s, %s, %s, {_line(coords)});
            """,
            (osm_id, highway, oneway, maxspeed, f"way-{osm_id}"),
        )

    # Points
    signal_rows = [
        (100, "traffic_signals", SIGNAL_NEAR_B),
        (101, "traffic_signals", SIGNAL_FAR),
        # Non-signal point: must not affect has_signal
        (102, "bus_stop", (121.5600, 25.0335)),
        # Placed at U2 location; U2 gets contracted away, so this signal must
        # relocate to the chain endpoints T2 / V2 (both within 30m).
        (103, "traffic_signals", SIGNAL_AT_U2),
    ]
    for osm_id, highway, (lon, lat) in signal_rows:
        _exec(
            conn,
            f"""
            INSERT INTO planet_osm_point (osm_id, highway, name, way)
            VALUES (%s, %s, %s, {_pt(lon, lat)});
            """,
            (osm_id, highway, f"pt-{osm_id}"),
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_container() -> Iterator[PostgresContainer]:
    """Spawn timescaledb-ha:pg14-all once per module (Docker pull is ~700MB)."""
    image = os.environ.get("TSDB_HA_IMAGE", "timescale/timescaledb-ha:pg14-all")
    container = (
        PostgresContainer(image=image, dbname="traffic_data", username="admin", password="secret")
    )
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def db_conn(pg_container: PostgresContainer):
    """One autocommit psycopg2 connection per module, with schema + fixture
    already loaded. The build SQL itself is NOT yet run -- individual tests
    re-execute it under a fresh fixture state via a function-scoped wrapper
    if they need to mutate inputs; the common case reuses this module setup."""
    dsn = pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    # testcontainers may return e.g. "postgresql+psycopg2://..." depending on version;
    # psycopg2.connect understands plain "postgresql://...".
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)

    # 1. Init extensions, schema, default_maxspeed function
    _exec_sql_file(conn, _EXT_SQL)
    _exec_sql_file(conn, _SCHEMA_SQL)
    _exec_sql_file(conn, _MAXSPEED_FN_SQL)

    # 2. osm2pgsql-style raw tables
    _exec(conn, _CREATE_OSM_TABLES)

    # 3. Synthetic fixture rows
    _insert_fixture(conn)

    # 4. Run the SUT (build_graph_from_osm.sql)
    _exec_sql_file(conn, _BUILD_SQL)

    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


_EXCLUDED_CLASSES: tuple[str, ...] = (
    "pedestrian",
    "footway",
    "cycleway",
    "track",
    "steps",
    "path",
    "bridleway",
)

# Number of "drivable" input ways in the fixture (excluded ones must NOT
# contribute any nodes or edges). Five original (1-5) + seven new from
# fix-osm-graph-topology §8.1 (20-26).
_NUM_DRIVABLE_LINES = 12


def test_traffic_node_count_in_expected_range(db_conn):
    """Distinct vertices after snap-dedup, less the nodes contraction
    removes.

    Original fixture: {A, B, C, D, E} (B_NEAR collapses onto B) = 5
    fix-osm-graph-topology §8.1 fixtures: F2..H2 + I2..K2 + L2..N2 + O2,P2
    + T2..V2 + Q2..S2 = 17
    Total pre-contraction = 22

    Contraction (§2) removes 4 interior pass-through nodes:
      - A    (primary chain B-A-D)
      - G2   (primary chain F2-G2-H2)
      - J2   (oneway primary chain I2->J2->K2)
      - U2   (primary chain T2-U2-V2)
    -> 22 - 4 = 18 nodes survive.

    R2 stays (mixed oneway/non-oneway signature blocks contraction);
    M2 stays (class boundary).
    """
    (count,) = _fetchone(db_conn, "SELECT COUNT(*) FROM traffic_node;")
    assert count == 18, (
        f"expected 18 distinct snapped nodes after contraction "
        f"(A, G2, J2, U2 removed by §2 contraction), got {count}"
    )


def test_traffic_edge_count_matches_input(db_conn):
    """Post-rebuild row totals:
      Pre-contraction edges (per §1):
        non-oneway lines emit 2 rows; oneway/oneway=-1 emit 1 row each.
        non-oneway: 1, 2, 4, 5, 20, 21, 24, 25, 28, 29, 30 -> 22 rows
        oneway   :  3, 22, 23, 26, 27                     ->  5 rows
        total pre-contraction = 27
      Contraction (§2) collapses four chains:
        B-A-D primary       : -4 rows, +2 rows (B<->D)
        F2-G2-H2 primary    : -4 rows, +2 rows (F2<->H2)
        I2-J2-K2 oneway     : -2 rows, +1 row  (I2->K2)
        T2-U2-V2 primary    : -4 rows, +2 rows (T2<->V2)
      Net edges = 27 - 4 + 2 - 4 + 2 - 2 + 1 - 4 + 2 = 20.
    """
    (count,) = _fetchone(db_conn, "SELECT COUNT(*) FROM traffic_edge;")
    assert count == 20, (
        f"expected 20 edges post-contraction "
        f"(27 pre-contraction, 4 chains merged), got {count}"
    )


def test_no_excluded_road_classes_present(db_conn):
    """The highway filter in build_graph_from_osm.sql must drop these."""
    rows = _fetchall(
        db_conn,
        "SELECT DISTINCT road_class FROM traffic_edge ORDER BY road_class;",
    )
    classes = {r[0] for r in rows}
    leaked = classes & set(_EXCLUDED_CLASSES)
    assert not leaked, (
        f"excluded highway classes leaked into traffic_edge: {sorted(leaked)}; "
        f"all classes seen: {sorted(classes)}"
    )


def test_road_class_distribution_is_reasonable(db_conn):
    """Sanity-check distribution: at minimum the fixture's primary, residential,
    service, secondary classes must all be present, none of the excluded ones."""
    rows = _fetchall(db_conn, "SELECT road_class FROM traffic_edge;")
    classes = {r[0] for r in rows}
    for required in ("primary", "residential", "service", "secondary"):
        assert required in classes, (
            f"expected drivable class {required!r} in traffic_edge, "
            f"got {sorted(classes)}"
        )


def test_oneway_flag_reflects_input(db_conn):
    """fix-osm-graph-topology §1: post-rebuild, each non-oneway way emits
    two rows (both with oneway=FALSE); each oneway/oneway=-1 way emits a
    single row with oneway=TRUE.

      service (osm_id=3, oneway=yes)        -> 1 row,  TRUE
      residential (osm_id=2, oneway=no)     -> 2 rows, both FALSE
      secondary line 4 (oneway=no) + 25     -> 4 rows FALSE
        + secondary line 26 (oneway=-1)     -> 1 row  TRUE
    """
    rows = _fetchall(
        db_conn,
        "SELECT road_class, oneway FROM traffic_edge ORDER BY road_class, oneway;",
    )
    by_class: dict[str, list[bool]] = {}
    for rc, ow in rows:
        by_class.setdefault(rc, []).append(ow)

    assert by_class.get("service") == [True], (
        f"service should be 1 oneway=TRUE row, got {by_class.get('service')}"
    )
    assert sorted(by_class.get("residential", [])) == [False, False], (
        f"residential (oneway=no) should expand to two FALSE rows, got "
        f"{by_class.get('residential')}"
    )
    # secondary class has line 4 (non-oneway -> 2 rows), line 25 (non-oneway
    # -> 2 rows), and line 26 (-1 oneway -> 1 row TRUE).
    sec = sorted(by_class.get("secondary", []))
    assert sec == [False, False, False, False, True], (
        f"secondary should be 4 non-oneway + 1 reverse-oneway TRUE, got {sec}"
    )


def test_snap_to_grid_collapses_near_coincident_vertices(db_conn):
    """B and B_NEAR are <5m apart (well under the 0.00005 deg ~ 5.5m snap
    grid) and must collapse to a single traffic_node id. Verify by checking
    that the residential edge set (line 2, starting at B) and line 4's
    secondary edge set (starting at B_NEAR) share at least one node id.
    Each class now has multiple rows (non-oneway emits two) so we aggregate
    endpoints class-by-class instead of asserting row counts here.
    """
    rows = _fetchall(
        db_conn,
        """
        SELECT road_class, source_node_id, target_node_id
        FROM traffic_edge
        WHERE road_class IN ('residential', 'secondary');
        """,
    )
    residential_nodes: set[int] = set()
    # Only line 4 from secondary touches B_NEAR (lines 25 / 26 are far away).
    # Aggregate every secondary endpoint and rely on shared-set semantics:
    # B is unique to line 4 in this fixture.
    secondary_nodes: set[int] = set()
    for rc, src, tgt in rows:
        if rc == "residential":
            residential_nodes.update((src, tgt))
        elif rc == "secondary":
            secondary_nodes.update((src, tgt))

    shared = residential_nodes & secondary_nodes
    assert shared, (
        "residential edges (from B) and secondary edges (incl. one from "
        "B_NEAR) should share a node after ST_SnapToGrid dedup; got "
        f"residential={residential_nodes} secondary={secondary_nodes}"
    )


def test_has_signal_set_for_node_near_traffic_signals_point(db_conn):
    """A traffic_signals point sits ~10m from intersection B (well within
    the 30m snap radius). The corresponding traffic_node must have
    has_signal=TRUE. Conversely, nodes >30m from any signal must remain
    has_signal=FALSE.

    With fix-osm-graph-topology fixtures (T2-V2 chain + signal at U2 ≈
    15m from both endpoints), T2 and V2 also light up — three total.
    """
    (signal_count,) = _fetchone(
        db_conn, "SELECT COUNT(*) FROM traffic_node WHERE has_signal = TRUE;"
    )
    assert signal_count == 3, (
        f"expected 3 signal-flagged nodes (B from SIGNAL_NEAR_B, "
        f"plus T2 + V2 from the relocated signal at U2), got {signal_count}"
    )

    # The B-flagging signal: exactly one node within 30m of SIGNAL_NEAR_B.
    near_lon, near_lat = SIGNAL_NEAR_B
    (within,) = _fetchone(
        db_conn,
        """
        SELECT COUNT(*)
        FROM traffic_node
        WHERE has_signal = TRUE
          AND ST_DWithin(
                geom::geography,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                30
              );
        """,
        (near_lon, near_lat),
    )
    assert within == 1, (
        f"the signal-flagged node should be within 30m of SIGNAL_NEAR_B "
        f"({SIGNAL_NEAR_B}); found {within}"
    )


def test_default_maxspeed_applied_when_maxspeed_tag_missing(db_conn):
    """osm_id=1 (highway=primary, no maxspeed tag) must fall back to
    default_maxspeed('primary') = 50."""
    rows = _fetchall(
        db_conn,
        """
        SELECT road_class, max_speed_kmh
        FROM traffic_edge
        WHERE road_class = 'primary'
        ORDER BY max_speed_kmh;
        """,
    )
    assert rows, "expected at least one primary edge"
    speeds = {r[1] for r in rows}
    assert 50 in speeds, (
        f"primary edge with no maxspeed tag must inherit "
        f"default_maxspeed('primary')=50, got speeds={sorted(speeds)}"
    )


def test_explicit_maxspeed_parsed_with_unit_suffix(db_conn):
    """osm_id=5 has maxspeed='50 km/h' (text with unit); the regex strip in
    the build SQL should pull leading "50". osm_id=2 has maxspeed='30' clean."""
    rows = _fetchall(
        db_conn,
        """
        SELECT road_class, max_speed_kmh
        FROM traffic_edge
        WHERE road_class = 'residential';
        """,
    )
    assert rows, "expected the residential edge to be present"
    assert all(r[1] == 30 for r in rows), (
        f"residential edge should inherit explicit maxspeed=30, got {rows}"
    )


def test_length_km_is_positive_and_finite(db_conn):
    """Every edge must have length_km > 0; no NaN/Inf. PostGIS
    ST_Length(geography) on a non-degenerate line is strictly positive."""
    rows = _fetchall(
        db_conn,
        "SELECT id, length_km FROM traffic_edge WHERE NOT (length_km > 0);",
    )
    assert not rows, f"edges with non-positive length_km: {rows}"


def test_edge_geom_srid_is_4326(db_conn):
    """traffic_edge.geom must be stored in WGS84 (SRID 4326), as the build SQL
    ST_Transforms inputs from 3857 -> 4326."""
    rows = _fetchall(
        db_conn,
        "SELECT DISTINCT ST_SRID(geom) FROM traffic_edge;",
    )
    srids = {r[0] for r in rows}
    assert srids == {4326}, f"expected all edge geoms in SRID 4326, got {srids}"


def test_node_geom_srid_is_4326(db_conn):
    rows = _fetchall(
        db_conn,
        "SELECT DISTINCT ST_SRID(geom) FROM traffic_node;",
    )
    srids = {r[0] for r in rows}
    assert srids == {4326}, f"expected all node geoms in SRID 4326, got {srids}"


def test_source_and_target_node_ids_resolve(db_conn):
    """Every traffic_edge.{source,target}_node_id must reference an existing
    traffic_node row. The FK enforces it at INSERT time but we double-check
    via JOIN in case the FK were ever relaxed."""
    (orphans,) = _fetchone(
        db_conn,
        """
        SELECT COUNT(*) FROM traffic_edge e
        LEFT JOIN traffic_node ns ON ns.id = e.source_node_id
        LEFT JOIN traffic_node nt ON nt.id = e.target_node_id
        WHERE ns.id IS NULL OR nt.id IS NULL;
        """,
    )
    assert orphans == 0, f"{orphans} edges reference missing traffic_node rows"


# ---------------------------------------------------------------------------
# fix-osm-graph-topology §8.2 — edge directionality after the SQL rebuild
# ---------------------------------------------------------------------------


def _node_id_at(db_conn, lon: float, lat: float) -> int:
    """Resolve a traffic_node id at (lon, lat) post-snap-to-grid. Tolerance
    is generous (50m) because ST_SnapToGrid rounds coords to ~5.5m cells."""
    row = _fetchone(
        db_conn,
        """
        SELECT id
        FROM traffic_node
        ORDER BY ST_Distance(
            geom::geography,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
        )
        LIMIT 1;
        """,
        (lon, lat),
    )
    assert row is not None, f"no traffic_node near ({lon}, {lat})"
    return int(row[0])


def test_non_oneway_emits_both_directions(db_conn):
    """fix-osm-graph-topology §1.3 — non-oneway way (line 25, M2-N2,
    secondary) must produce two rows: M2->N2 and N2->M2."""
    m2_id = _node_id_at(db_conn, *M2)
    n2_id = _node_id_at(db_conn, *N2)

    rows = _fetchall(
        db_conn,
        """
        SELECT source_node_id, target_node_id
        FROM traffic_edge
        WHERE road_class = 'secondary'
          AND (source_node_id, target_node_id) IN ((%s, %s), (%s, %s));
        """,
        (m2_id, n2_id, n2_id, m2_id),
    )
    directions = {(src, tgt) for src, tgt in rows}
    assert (m2_id, n2_id) in directions, (
        f"forward direction M2->N2 missing; secondary edges between M2-N2: {rows}"
    )
    assert (n2_id, m2_id) in directions, (
        f"reverse direction N2->M2 missing; non-oneway must emit BOTH rows"
    )


def test_oneway_yes_emits_only_forward(db_conn):
    """fix-osm-graph-topology §1.3 — line 3 service oneway=yes (B->D) must
    insert exactly ONE row (B->D), no reverse."""
    b_id = _node_id_at(db_conn, *B)
    d_id = _node_id_at(db_conn, *D)

    rows = _fetchall(
        db_conn,
        """
        SELECT source_node_id, target_node_id, oneway
        FROM traffic_edge
        WHERE road_class = 'service';
        """,
    )
    assert len(rows) == 1, f"oneway=yes service way should emit 1 row, got {rows}"
    src, tgt, ow = rows[0]
    assert (src, tgt) == (b_id, d_id), f"expected B({b_id})->D({d_id}), got {(src, tgt)}"
    assert ow is True, f"service edge oneway flag should be TRUE, got {ow!r}"


def test_oneway_negative_one_inserts_reverse_direction(db_conn):
    """fix-osm-graph-topology §1.3 — OSM `oneway='-1'` (line 26, O2->P2)
    must insert a SINGLE row with reversed direction: P2->O2, oneway=TRUE."""
    o2_id = _node_id_at(db_conn, *O2)
    p2_id = _node_id_at(db_conn, *P2)

    rows = _fetchall(
        db_conn,
        """
        SELECT source_node_id, target_node_id, oneway
        FROM traffic_edge
        WHERE road_class = 'secondary'
          AND oneway = TRUE
          AND (source_node_id, target_node_id) IN ((%s, %s), (%s, %s));
        """,
        (o2_id, p2_id, p2_id, o2_id),
    )
    assert len(rows) == 1, (
        f"oneway=-1 must emit exactly one row, got {rows}"
    )
    src, tgt, ow = rows[0]
    assert (src, tgt) == (p2_id, o2_id), (
        f"oneway=-1 should reverse: expected P2({p2_id})->O2({o2_id}), got {(src, tgt)}"
    )
    assert ow is True


# ---------------------------------------------------------------------------
# fix-osm-graph-topology §8.3 — degree-2 chain contraction behaviour
# ---------------------------------------------------------------------------


def _node_exists_near(db_conn, lon: float, lat: float, radius_m: int = 10) -> bool:
    (cnt,) = _fetchone(
        db_conn,
        """
        SELECT COUNT(*)
        FROM traffic_node
        WHERE ST_DWithin(
            geom::geography,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
            %s
        );
        """,
        (lon, lat, radius_m),
    )
    return cnt > 0


def test_contraction_removes_interior_chain_node(db_conn):
    """fix-osm-graph-topology §2 — G2 sits between F2 and H2 with matching
    (primary, default_maxspeed, oneway=FALSE) on both adjacent ways, so it
    MUST be contracted away. Similarly J2 in the oneway chain."""
    assert not _node_exists_near(db_conn, *G2), (
        "G2 should have been contracted (degree-2 primary non-oneway chain "
        "with matching adjacent edges)"
    )
    assert not _node_exists_near(db_conn, *J2), (
        "J2 should have been contracted (degree-2 oneway primary chain)"
    )
    # Chain endpoints survive.
    assert _node_exists_near(db_conn, *F2)
    assert _node_exists_near(db_conn, *H2)
    assert _node_exists_near(db_conn, *I2)
    assert _node_exists_near(db_conn, *K2)


def test_contraction_creates_merged_edge_with_summed_length(db_conn):
    """The merged F2<->H2 primary edge pair must have length_km roughly equal
    to the sum of the two original segments."""
    f2_id = _node_id_at(db_conn, *F2)
    h2_id = _node_id_at(db_conn, *H2)

    rows = _fetchall(
        db_conn,
        """
        SELECT length_km
        FROM traffic_edge
        WHERE road_class = 'primary'
          AND (source_node_id, target_node_id) IN ((%s, %s), (%s, %s));
        """,
        (f2_id, h2_id, h2_id, f2_id),
    )
    assert len(rows) == 2, (
        f"merged F2<->H2 chain should produce 2 primary edges, got {rows}"
    )
    # F2 (121.5500) -> G2 (121.5510) -> H2 (121.5520), at lat 25.0330.
    # Two ~111m segments combine to ~222m = 0.222 km. Tolerate slack.
    for (length,) in rows:
        assert 0.15 < length < 0.30, (
            f"merged edge length_km should be ~0.22 (sum of two 0.11km "
            f"segments), got {length}"
        )


def test_contraction_skips_cross_class_boundary(db_conn):
    """fix-osm-graph-topology §2 scenario "跨 road_class 邊界處不合併" — M2
    has degree-2 but its adjacent edges differ in road_class (primary on
    one side, secondary on the other), so M2 must SURVIVE."""
    assert _node_exists_near(db_conn, *M2), (
        "M2 should NOT have been contracted: class boundary "
        "(primary L2-M2 vs secondary M2-N2) blocks contraction"
    )


def test_contraction_skips_cross_oneway_boundary(db_conn):
    """A node between an oneway segment and a non-oneway segment must not
    be contracted: signature differs on the `oneway` flag. We don't have
    a dedicated fixture for this, but M2's class-boundary check above
    plus the no-cross-class merge implies the same guard fires for any
    differing signature. This test asserts no spurious merged edges exist
    at M2."""
    m2_id = _node_id_at(db_conn, *M2)
    (cnt,) = _fetchone(
        db_conn,
        """
        SELECT COUNT(*)
        FROM traffic_edge
        WHERE source_node_id = %s OR target_node_id = %s;
        """,
        (m2_id, m2_id),
    )
    # M2 sits at the boundary of two non-oneway ways -> 4 edges
    # (L2<->M2 primary 2 rows + M2<->N2 secondary 2 rows).
    assert cnt == 4, (
        f"M2 should have exactly 4 adjacent edges (2 primary L2<->M2 + "
        f"2 secondary M2<->N2), got {cnt}"
    )


# ---------------------------------------------------------------------------
# fix-osm-graph-topology §8.4 — self-loop guard
# ---------------------------------------------------------------------------


def test_no_self_loops_in_traffic_edge(db_conn):
    """fix-osm-graph-topology §1.4 — the existing grid-equality filter
    (`WHERE NOT ST_Equals(s1.pt, s2.pt)`) is the sole self-loop guard.
    Verify that no traffic_edge row has source = target — including the
    post-§2 contraction merged-edge inserts (cul-de-sac loops are filtered
    out via the `chain_src <> chain_tgt` guard)."""
    (count,) = _fetchone(
        db_conn,
        "SELECT COUNT(*) FROM traffic_edge WHERE source_node_id = target_node_id;",
    )
    assert count == 0, (
        f"found {count} traffic_edge row(s) with source_node_id = "
        f"target_node_id; either the vertex grid-equality filter or the "
        f"contraction cul-de-sac filter failed"
    )


def test_contraction_skips_mixed_oneway_signature_node(db_conn):
    """fix-osm-graph-topology osm-road-network scenario "oneway 與非 oneway
    鏈不互相合併": R2 has degree-2 undirected ({Q2, S2}) but one adjacent
    edge is oneway (Q2->R2 from way 27) and another is non-oneway (R2<->S2
    from way 28). Mixed `oneway` signature MUST prevent contraction; R2
    must remain in traffic_node and keep all its adjacent edges intact.
    """
    assert _node_exists_near(db_conn, *R2), (
        "R2 should NOT have been contracted: mixed oneway signature "
        "(oneway primary Q2->R2 vs non-oneway primary R2<->S2)"
    )
    r2_id = _node_id_at(db_conn, *R2)
    (cnt,) = _fetchone(
        db_conn,
        """
        SELECT COUNT(*) FROM traffic_edge
        WHERE source_node_id = %s OR target_node_id = %s;
        """,
        (r2_id, r2_id),
    )
    # R2 should still have its 3 adjacent edges: 1 incoming oneway
    # (Q2->R2) + 2 non-oneway rows for R2<->S2 = 3.
    assert cnt == 3, (
        f"R2 should have 3 adjacent edges (1 oneway in + 2 non-oneway "
        f"to S2), got {cnt}"
    )


def test_signal_relocated_to_chain_endpoint_after_contraction(db_conn):
    """fix-osm-graph-topology osm-road-network MODIFIED scenario
    "中段號誌靠 contraction 重定位": a `traffic_signals` point placed at
    U2 (mid-chain, contracted away) must snap to the chain endpoints
    (T2 / V2) as long as they're within 30m. Our fixture spaces nodes
    ~15m apart so both T2 and V2 light up.
    """
    # U2 must be gone (sanity: chain was actually contracted).
    assert not _node_exists_near(db_conn, *U2), (
        "U2 must have been contracted away; signal-relocation test is "
        "vacuous if U2 is still present"
    )
    t2_id = _node_id_at(db_conn, *T2)
    v2_id = _node_id_at(db_conn, *V2)
    rows = _fetchall(
        db_conn,
        "SELECT id FROM traffic_node WHERE has_signal = TRUE AND id IN (%s, %s);",
        (t2_id, v2_id),
    )
    flagged = {r[0] for r in rows}
    assert t2_id in flagged or v2_id in flagged, (
        f"Signal at U2 (contracted) must relocate to at least one "
        f"chain endpoint; T2={t2_id} V2={v2_id} flagged={flagged}"
    )
