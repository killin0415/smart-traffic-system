-- build_graph_from_osm.sql
--
-- Transform osm2pgsql `planet_osm_line` rows into the routable graph
-- (`traffic_node`, `traffic_edge`).
--
-- Pipeline:
--   1. Filter `planet_osm_line` to driveable highways (no pedestrian/bike/etc.)
--   2. ST_DumpPoints + ST_SnapToGrid(0.00005 deg ~ 5.5 m) -> unique nodes
--   3. INSERT traffic_node (incl. geom)
--   4. Walk vertex pairs along each filtered way -> raw traffic_edge rows.
--      Non-oneway ways get TWO rows (A->B and B->A); oneway=yes gets A->B;
--      oneway=-1 gets B->A. (fix-osm-graph-topology, Requirement
--      `Non-oneway way bidirectional edge`).
--   5. Degree-2 chain contraction (fix-osm-graph-topology, Requirement
--      `Degree-2 chain contraction`): collapse interior pass-through nodes so
--      remaining nodes are mostly real intersections.
--   6. Snap traffic_signals points to traffic_node (30 m) -> has_signal.
--      MUST run AFTER contraction so signals don't get lost to deleted
--      mid-way nodes (Requirement `OSM signal snap order`).
--   7. RAISE INFO/NOTICE health metrics (intersection ratio, main component
--      coverage) — informational only, never rolls back the transaction
--      (Requirement `Topology health thresholds`).
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

-- 4. Build edge pairs by walking consecutive snapped vertices on each way.
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

-- Sequence pairs within each way. The grid-equality filter is a vertex-level
-- self-loop guard: when two consecutive OSM vertices snap to the same grid
-- cell their edge would be A->A, so we drop the pair here (before any
-- traffic_edge row is created). This is the only self-loop guard the pipeline
-- needs — contraction (§5) never receives a self-loop as input.
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
 AND s2.path[1] = s1.path[1] + 1                     -- next vertex along way
WHERE NOT ST_Equals(s1.pt, s2.pt);

-- 4b. Direction-aware insert.
--   direction_kind = 'fwd'  -> single A->B  (oneway=TRUE)
--   direction_kind = 'rev'  -> single B->A  (oneway=TRUE, from OSM `-1`)
--   direction_kind = 'both' -> A->B and B->A as two rows (oneway=FALSE)
INSERT INTO traffic_edge (
    source_node_id, target_node_id, road_name,
    length_km, road_class, max_speed_kmh, oneway, geom
)
WITH classified AS (
    SELECT
        p.osm_id, p.road_class, p.road_name, p.parsed_maxspeed,
        p.pt_a, p.pt_b,
        CASE
            WHEN LOWER(p.oneway_raw) IN ('yes', 'true', '1') THEN 'fwd'
            WHEN p.oneway_raw = '-1'                          THEN 'rev'
            ELSE                                                   'both'
        END AS direction_kind
    FROM _edge_pairs p
),
expanded AS (
    -- fwd: source -> target as authored
    SELECT pt_a AS src_pt, pt_b AS tgt_pt, road_class, road_name,
           parsed_maxspeed, TRUE AS is_oneway
    FROM classified WHERE direction_kind = 'fwd'
    UNION ALL
    -- rev: target -> source (OSM oneway=-1)
    SELECT pt_b AS src_pt, pt_a AS tgt_pt, road_class, road_name,
           parsed_maxspeed, TRUE AS is_oneway
    FROM classified WHERE direction_kind = 'rev'
    UNION ALL
    -- both (forward half)
    SELECT pt_a AS src_pt, pt_b AS tgt_pt, road_class, road_name,
           parsed_maxspeed, FALSE AS is_oneway
    FROM classified WHERE direction_kind = 'both'
    UNION ALL
    -- both (reverse half)
    SELECT pt_b AS src_pt, pt_a AS tgt_pt, road_class, road_name,
           parsed_maxspeed, FALSE AS is_oneway
    FROM classified WHERE direction_kind = 'both'
)
SELECT
    va.id AS source_node_id,
    vb.id AS target_node_id,
    NULLIF(ex.road_name, '')        AS road_name,
    ST_Length(ST_MakeLine(ex.src_pt, ex.tgt_pt)::geography) / 1000.0 AS length_km,
    ex.road_class                   AS road_class,
    COALESCE(ex.parsed_maxspeed, default_maxspeed(ex.road_class)) AS max_speed_kmh,
    ex.is_oneway                    AS oneway,
    ST_MakeLine(ex.src_pt, ex.tgt_pt)::geometry(LineString, 4326) AS geom
FROM expanded ex
JOIN _vertex_id va ON va.pt = ex.src_pt
JOIN _vertex_id vb ON vb.pt = ex.tgt_pt;

\echo 'build_graph_from_osm: inserted traffic_edge rows (pre-contraction)'

DO $$
DECLARE
    v_pre INT;
BEGIN
    SELECT COUNT(*) INTO v_pre FROM traffic_edge;
    RAISE INFO 'build_graph_from_osm: pre-contraction edge count = %', v_pre;
END $$;

-- ---------------------------------------------------------------------------
-- 5. Degree-2 chain contraction.
--
--   A node is "contractible" iff:
--     - undirected degree (distinct neighbours via incoming OR outgoing) = 2
--     - all adjacent edges share the same (road_class, max_speed_kmh, oneway)
--     - directional pattern matches a true pass-through:
--         oneway pass-through    : 1 incoming + 1 outgoing
--         non-oneway pass-through: 2 incoming + 2 outgoing
--       (mixed patterns like 2-in / 1-out are sinks/sources and stay put)
--
--   Walking a chain starts at an edge that crosses from a non-contractible
--   boundary node into a contractible interior, then follows next-edge hops
--   while staying inside contractible nodes. For non-oneway chains both
--   directions get walked independently (one anchor per boundary endpoint)
--   and produce two merged edges (A->C and C->A).
--
--   At each interior step the "back" edge to the previous node is excluded
--   via `ne.target_node_id != c.prev_node`, so a non-oneway interior never
--   U-turns mid-chain.
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS _node_neighbors;
CREATE TEMP TABLE _node_neighbors AS
SELECT source_node_id AS node_id, target_node_id AS neighbor FROM traffic_edge
UNION
SELECT target_node_id AS node_id, source_node_id AS neighbor FROM traffic_edge;
CREATE INDEX ON _node_neighbors (node_id);

DROP TABLE IF EXISTS _node_degree;
CREATE TEMP TABLE _node_degree AS
SELECT node_id, COUNT(*) AS undirected_degree
FROM _node_neighbors
GROUP BY node_id;
CREATE UNIQUE INDEX ON _node_degree (node_id);

-- Uniform-signature check: every adjacent edge agrees on class/speed/oneway.
DROP TABLE IF EXISTS _node_uniform;
CREATE TEMP TABLE _node_uniform AS
WITH adj AS (
    SELECT source_node_id AS node_id, road_class, max_speed_kmh, oneway
    FROM traffic_edge
    UNION ALL
    SELECT target_node_id AS node_id, road_class, max_speed_kmh, oneway
    FROM traffic_edge
)
SELECT node_id,
       COUNT(DISTINCT (road_class, max_speed_kmh, oneway)) AS sig_count,
       MIN(road_class)    AS sig_road_class,
       MIN(max_speed_kmh) AS sig_max_speed_kmh,
       bool_or(oneway)    AS sig_oneway
FROM adj
GROUP BY node_id;
CREATE UNIQUE INDEX ON _node_uniform (node_id);

DROP TABLE IF EXISTS _node_dir;
CREATE TEMP TABLE _node_dir AS
SELECT n.id AS node_id,
       COALESCE(in_c.c,  0) AS in_count,
       COALESCE(out_c.c, 0) AS out_count
FROM traffic_node n
LEFT JOIN (
    SELECT target_node_id AS node_id, COUNT(*) AS c
    FROM traffic_edge GROUP BY target_node_id
) in_c ON in_c.node_id = n.id
LEFT JOIN (
    SELECT source_node_id AS node_id, COUNT(*) AS c
    FROM traffic_edge GROUP BY source_node_id
) out_c ON out_c.node_id = n.id;
CREATE UNIQUE INDEX ON _node_dir (node_id);

DROP TABLE IF EXISTS _contractible;
CREATE TEMP TABLE _contractible AS
SELECT d.node_id
FROM _node_degree d
JOIN _node_uniform u ON u.node_id = d.node_id
JOIN _node_dir dc    ON dc.node_id = d.node_id
WHERE d.undirected_degree = 2
  AND u.sig_count = 1
  AND (
        (dc.in_count = 2 AND dc.out_count = 2)   -- non-oneway pass-through
     OR (dc.in_count = 1 AND dc.out_count = 1)   -- oneway   pass-through
  );
CREATE UNIQUE INDEX ON _contractible (node_id);

DROP TABLE IF EXISTS _chains_to_merge;
CREATE TEMP TABLE _chains_to_merge AS
WITH RECURSIVE chain_walk AS (
    -- Anchor: edge crossing from boundary into a contractible interior.
    SELECT
        e.id              AS first_edge_id,
        e.source_node_id  AS chain_src,
        e.source_node_id  AS prev_node,
        e.target_node_id  AS cur_node,
        e.road_class,
        e.max_speed_kmh,
        e.oneway,
        ARRAY[e.id]               AS edge_ids,
        e.length_km               AS total_length,
        ARRAY[e.geom]::geometry[] AS geoms,
        ARRAY[NULLIF(e.road_name, '')]::text[] AS names,
        1                         AS depth
    FROM traffic_edge e
    WHERE e.target_node_id IN (SELECT node_id FROM _contractible)
      AND e.source_node_id NOT IN (SELECT node_id FROM _contractible)

    UNION ALL

    -- Extend by one edge that exits cur_node going away from prev_node.
    -- The signature equality is structurally guaranteed (cur_node is
    -- contractible -> all adjacent edges share signature), but we keep the
    -- equality joins as defence in depth.
    SELECT
        c.first_edge_id,
        c.chain_src,
        c.cur_node        AS prev_node,
        ne.target_node_id AS cur_node,
        c.road_class,
        c.max_speed_kmh,
        c.oneway,
        c.edge_ids || ne.id,
        c.total_length + ne.length_km,
        c.geoms || ne.geom,
        c.names || NULLIF(ne.road_name, ''),
        c.depth + 1
    FROM chain_walk c
    JOIN traffic_edge ne
      ON ne.source_node_id = c.cur_node
     AND ne.target_node_id <> c.prev_node
     AND ne.road_class      = c.road_class
     AND ne.max_speed_kmh   = c.max_speed_kmh
     AND ne.oneway          = c.oneway
    WHERE c.cur_node IN (SELECT node_id FROM _contractible)
      AND c.depth < 1000      -- runaway safety
)
SELECT DISTINCT ON (first_edge_id)
    chain_src,
    cur_node           AS chain_tgt,
    road_class,
    max_speed_kmh,
    oneway,
    edge_ids,
    total_length,
    geoms,
    names
FROM chain_walk
WHERE cur_node NOT IN (SELECT node_id FROM _contractible)
  AND array_length(edge_ids, 1) >= 2  -- skip anchor-only walks (no contraction)
  AND cur_node <> chain_src           -- skip cul-de-sac loops (would self-loop)
ORDER BY first_edge_id, array_length(edge_ids, 1) DESC;

DO $$
DECLARE
    v_chain_count INT;
    v_node_drop   INT;
BEGIN
    SELECT COUNT(*) INTO v_chain_count FROM _chains_to_merge;
    SELECT COUNT(*) INTO v_node_drop   FROM _contractible;
    RAISE INFO 'build_graph_from_osm: contraction found % chains over % interior nodes',
        v_chain_count, v_node_drop;
END $$;

-- Drop the original chain edges (must happen before we delete the interior
-- nodes those edges reference — traffic_edge -> traffic_node has no CASCADE).
DELETE FROM traffic_edge
WHERE id IN (
    SELECT unnest(edge_ids) FROM _chains_to_merge
);

-- Insert merged edges. road_name is the first non-null name along the chain;
-- chain segments inside one OSM way share a name, and cross-way chains where
-- names differ keep the first one (good enough for display).
INSERT INTO traffic_edge (
    source_node_id, target_node_id, road_name,
    length_km, road_class, max_speed_kmh, oneway, geom
)
SELECT
    chain_src,
    chain_tgt,
    (SELECT n FROM unnest(names) AS t(n) WHERE n IS NOT NULL LIMIT 1) AS road_name,
    total_length,
    road_class,
    max_speed_kmh,
    oneway,
    ST_LineMerge(ST_Collect(geoms))::geometry(LineString, 4326) AS geom
FROM _chains_to_merge;

-- Now the contracted interior nodes are FK-orphaned (no edges reference them
-- any more) and safe to delete. The NOT EXISTS guard keeps any contractible
-- node that wasn't actually contracted (e.g. an interior point of a cul-de-
-- sac loop excluded by the `chain_src <> chain_tgt` filter above) — those
-- still have their original edges referencing them.
DELETE FROM traffic_node tn
WHERE tn.id IN (SELECT node_id FROM _contractible)
  AND NOT EXISTS (
      SELECT 1 FROM traffic_edge te
      WHERE te.source_node_id = tn.id OR te.target_node_id = tn.id
  );

\echo 'build_graph_from_osm: degree-2 chain contraction complete'

-- ---------------------------------------------------------------------------
-- 6. Signal snap — MUST be after contraction. Mid-way OSM nodes that have
--    been deleted no longer compete for the nearest-traffic_node slot, so a
--    signal that was previously closest to a deleted mid-node will now snap
--    to the chain endpoint instead (still within 30 m for typical Taipei
--    block sizes).
-- ---------------------------------------------------------------------------

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

-- ---------------------------------------------------------------------------
-- 7. Topology health metrics. Never aborts — RAISE NOTICE is informational
--    so the import script's exit code stays 0 (Requirement
--    `Topology health thresholds`, scenario "違反門檻不阻斷流程").
-- ---------------------------------------------------------------------------

-- The pre-contraction `_node_neighbors` temp table is now STALE (it still
-- references deleted chain edges). Rebuild it from the post-contraction
-- traffic_edge so the BFS below walks the current graph.
DROP TABLE IF EXISTS _node_neighbors;
CREATE TEMP TABLE _node_neighbors AS
SELECT source_node_id AS node_id, target_node_id AS neighbor FROM traffic_edge
UNION
SELECT target_node_id AS node_id, source_node_id AS neighbor FROM traffic_edge;
CREATE INDEX ON _node_neighbors (node_id);

DO $$
DECLARE
    v_node_count           INT;
    v_edge_count           INT;
    v_intersection_ratio   NUMERIC;
    v_main_component_size  INT;
    v_main_component_pct   NUMERIC;
    v_seed                 INT;
BEGIN
    SELECT COUNT(*) INTO v_node_count FROM traffic_node;
    SELECT COUNT(*) INTO v_edge_count FROM traffic_edge;

    IF v_node_count = 0 THEN
        RAISE NOTICE 'build_graph_from_osm: traffic_node is empty after build';
        RETURN;
    END IF;

    -- intersection ratio = degree>=3 nodes / total nodes (undirected degree)
    WITH adj AS (
        SELECT source_node_id AS node_id, target_node_id AS neighbor
        FROM traffic_edge
        UNION
        SELECT target_node_id, source_node_id FROM traffic_edge
    ),
    deg AS (
        SELECT node_id, COUNT(*) AS d FROM adj GROUP BY node_id
    )
    SELECT (COUNT(*) FILTER (WHERE d >= 3))::NUMERIC / NULLIF(COUNT(*), 0)
    INTO v_intersection_ratio
    FROM deg;

    -- Largest connected component (undirected). Seed at the highest-degree
    -- node so we have the strongest chance of starting inside the main blob
    -- (any single tiny island would otherwise underreport coverage).
    SELECT node_id INTO v_seed
    FROM (
        SELECT node_id, COUNT(*) AS d FROM _node_neighbors GROUP BY node_id
    ) t
    ORDER BY d DESC, node_id ASC
    LIMIT 1;

    IF v_seed IS NULL THEN
        v_main_component_pct := NULL;
    ELSE
        -- PostgreSQL recursive CTEs allow only one self-reference, so we
        -- walk via the previously-built `_node_neighbors` (undirected) table
        -- instead of two separate JOINs against traffic_edge.
        WITH RECURSIVE bfs AS (
            SELECT v_seed AS node_id
            UNION
            SELECT n.neighbor
            FROM bfs b
            JOIN _node_neighbors n ON n.node_id = b.node_id
        )
        SELECT COUNT(*) INTO v_main_component_size FROM bfs;

        v_main_component_pct := v_main_component_size::NUMERIC / v_node_count;
    END IF;

    RAISE INFO 'build_graph_from_osm: node_count=% edge_count=% intersection_ratio=% main_component_pct=%',
        v_node_count,
        v_edge_count,
        ROUND(v_intersection_ratio, 4),
        ROUND(COALESCE(v_main_component_pct, 0), 4);

    IF v_intersection_ratio < 0.5 THEN
        RAISE NOTICE 'build_graph_from_osm: intersection_ratio % below 0.5 threshold — contraction may be incomplete',
            ROUND(v_intersection_ratio, 4);
    END IF;

    IF v_main_component_pct IS NULL OR v_main_component_pct < 0.99 THEN
        RAISE NOTICE 'build_graph_from_osm: main_component_pct % below 0.99 threshold — graph has significant islands',
            ROUND(COALESCE(v_main_component_pct, 0), 4);
    END IF;
END $$;

COMMIT;

-- Final stats (also printed by psql so the import script captures them).
\echo 'build_graph_from_osm: stats'
SELECT
    (SELECT COUNT(*) FROM traffic_node)                       AS nodes,
    (SELECT COUNT(*) FROM traffic_node WHERE has_signal)      AS signal_nodes,
    (SELECT COUNT(*) FROM traffic_edge)                       AS edges,
    (SELECT COUNT(DISTINCT road_class) FROM traffic_edge)     AS distinct_classes;
