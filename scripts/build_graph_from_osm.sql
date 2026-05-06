-- build_graph_from_osm.sql
--
-- Transform osm2pgsql `planet_osm_line` rows into the routable graph
-- (`traffic_node`, `traffic_edge`).
--
-- Pipeline:
--   1. Filter `planet_osm_line` to driveable highways (no pedestrian/bike/etc.)
--   2. ST_DumpPoints + ST_SnapToGrid(0.00005 deg ~ 5.5 m) -> unique nodes
--   3. INSERT traffic_node (incl. geom)
--   4. Walk vertex pairs along each filtered way -> traffic_edge
--      length_km via ST_Length(geography), max_speed_kmh = COALESCE(parsed maxspeed, default_maxspeed(highway))
--   5. Snap traffic_signals points to traffic_node (30 m) -> has_signal
--
-- Run via:
--   docker compose -f infra/docker-compose.yml exec -T timescaledb \
--     psql -U admin -d traffic_data -f /scripts/build_graph_from_osm.sql

\echo 'build_graph_from_osm: starting'

BEGIN;

-- 0. Reset target tables (CASCADE wipes speed_camera.nearest_edge_id refs too).
TRUNCATE TABLE traffic_edge, traffic_node CASCADE;

-- 1. Filtered driveable ways (carriageways only).
DROP TABLE IF EXISTS _drivable_ways;
CREATE TEMP TABLE _drivable_ways AS
SELECT
    osm_id,
    highway                           AS road_class,
    name                              AS road_name,
    COALESCE(NULLIF(oneway, ''), 'no') AS oneway_raw,
    -- maxspeed in OSM is text. Pull leading integer if any.
    NULLIF(regexp_replace(COALESCE(maxspeed, ''), '[^0-9].*$', ''), '')::INT
                                      AS parsed_maxspeed,
    way                               AS geom_3857
FROM planet_osm_line
WHERE highway IS NOT NULL
  AND highway NOT IN (
        'pedestrian', 'footway', 'cycleway', 'track', 'steps', 'path',
        'bridleway', 'corridor', 'platform', 'construction', 'proposed'
  );

-- Reproject to 4326 once.
DROP TABLE IF EXISTS _ways_4326;
CREATE TEMP TABLE _ways_4326 AS
SELECT
    osm_id,
    road_class,
    road_name,
    oneway_raw,
    parsed_maxspeed,
    ST_Transform(geom_3857, 4326)::geometry(LineString, 4326) AS geom
FROM _drivable_ways
WHERE GeometryType(geom_3857) = 'LINESTRING';

CREATE INDEX ON _ways_4326 USING GIST (geom);

\echo 'build_graph_from_osm: extracted drivable ways'

-- 2. Extract unique snapped vertices.
DROP TABLE IF EXISTS _vertices;
CREATE TEMP TABLE _vertices AS
SELECT DISTINCT
    ST_SnapToGrid((dp).geom, 0.00005) AS pt
FROM (
    SELECT (ST_DumpPoints(geom)) AS dp
    FROM _ways_4326
) sub;

-- 3. Insert nodes (id auto-assigned by SERIAL).
INSERT INTO traffic_node (latitude, longitude, geom)
SELECT
    ST_Y(pt) AS latitude,
    ST_X(pt) AS longitude,
    pt::geometry(Point, 4326) AS geom
FROM _vertices;

\echo 'build_graph_from_osm: inserted traffic_node rows'

-- Helper map: snapped point -> node id
DROP TABLE IF EXISTS _vertex_id;
CREATE TEMP TABLE _vertex_id AS
SELECT id, geom AS pt
FROM traffic_node;
CREATE INDEX ON _vertex_id USING GIST (pt);
CREATE UNIQUE INDEX ON _vertex_id (pt);

-- 4. Build edges by walking consecutive snapped vertices on each way.
DROP TABLE IF EXISTS _edge_segments;
CREATE TEMP TABLE _edge_segments AS
SELECT
    w.osm_id,
    w.road_class,
    w.road_name,
    w.oneway_raw,
    w.parsed_maxspeed,
    seg.path,
    ST_SnapToGrid(seg.pt, 0.00005)::geometry(Point, 4326) AS pt
FROM _ways_4326 w,
     LATERAL (
         SELECT (dp).path AS path, (dp).geom AS pt
         FROM ST_DumpPoints(w.geom) dp
     ) seg;

-- Sequence pairs within each way.
DROP TABLE IF EXISTS _edge_pairs;
CREATE TEMP TABLE _edge_pairs AS
SELECT
    s1.osm_id,
    s1.road_class,
    s1.road_name,
    s1.oneway_raw,
    s1.parsed_maxspeed,
    s1.pt AS pt_a,
    s2.pt AS pt_b
FROM _edge_segments s1
JOIN _edge_segments s2
  ON s1.osm_id = s2.osm_id
 AND s2.path = s1.path || ARRAY[1]::int[]            -- next vertex along way
WHERE NOT ST_Equals(s1.pt, s2.pt);

-- Resolve to node ids and emit traffic_edge rows.
INSERT INTO traffic_edge (
    source_node_id, target_node_id, road_name,
    length_km, road_class, max_speed_kmh, oneway, geom
)
SELECT
    va.id AS source_node_id,
    vb.id AS target_node_id,
    NULLIF(p.road_name, '')        AS road_name,
    ST_Length(ST_MakeLine(p.pt_a, p.pt_b)::geography) / 1000.0 AS length_km,
    p.road_class                   AS road_class,
    COALESCE(p.parsed_maxspeed, default_maxspeed(p.road_class)) AS max_speed_kmh,
    LOWER(p.oneway_raw) IN ('yes', 'true', '1', '-1') AS oneway,
    ST_MakeLine(p.pt_a, p.pt_b)::geometry(LineString, 4326) AS geom
FROM _edge_pairs p
JOIN _vertex_id va ON va.pt = p.pt_a
JOIN _vertex_id vb ON vb.pt = p.pt_b;

\echo 'build_graph_from_osm: inserted traffic_edge rows'

-- 5. Signal snap: mark traffic_node.has_signal where an OSM traffic_signals
--    point sits within 30 m of the node.
UPDATE traffic_node tn
SET has_signal = TRUE
WHERE EXISTS (
    SELECT 1
    FROM planet_osm_point p
    WHERE p.highway = 'traffic_signals'
      AND ST_DWithin(
              ST_Transform(p.way, 4326)::geography,
              tn.geom::geography,
              30
          )
);

\echo 'build_graph_from_osm: updated traffic_node.has_signal'

COMMIT;

-- Final stats
\echo 'build_graph_from_osm: stats'
SELECT
    (SELECT COUNT(*) FROM traffic_node)                       AS nodes,
    (SELECT COUNT(*) FROM traffic_node WHERE has_signal)      AS signal_nodes,
    (SELECT COUNT(*) FROM traffic_edge)                       AS edges,
    (SELECT COUNT(DISTINCT road_class) FROM traffic_edge)     AS distinct_classes;
