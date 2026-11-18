-- post_build_snap_vd.sql
--
-- After OSM graph build + VD static seed, snap each VD point to the nearest
-- traffic_edge (within 100 m) and copy that edge's road_class onto
-- vd_static.snapped_road_class. WeightProvider Tier 2 reads from this column
-- to compute per-class average speed.
--
-- 100 m is generous: VDs sit on gantries above the road; OSM line geometry is
-- the carriageway centreline. Anything beyond 100 m is more likely a stale or
-- mismatched VD point and should stay NULL (Tier 2 fallback won't see it).

\echo 'post_build_snap_vd: snapping VD -> traffic_edge'

UPDATE vd_static AS v
SET snapped_road_class = sub.road_class
FROM (
    SELECT
        v2.vdid,
        e.road_class
    FROM vd_static v2
    CROSS JOIN LATERAL (
        SELECT te.road_class
        FROM traffic_edge te
        -- Bbox prefilter on the raw geometry so ix_traffic_edge_geom is used
        -- (the ::geography cast alone defeats the index -> full scan per VD).
        -- 0.0015 deg is >= 150 m at Taipei's latitude, a strict superset of
        -- the 100 m geography test below.
        WHERE te.geom && ST_Expand(v2.geom, 0.0015)
          AND ST_DWithin(te.geom::geography, v2.geom::geography, 100)
        ORDER BY ST_Distance(te.geom::geography, v2.geom::geography)
        LIMIT 1
    ) e
) sub
WHERE v.vdid = sub.vdid;

\echo 'post_build_snap_vd: stats'
SELECT
    COUNT(*)                                            AS total_vd,
    COUNT(snapped_road_class)                           AS snapped,
    COUNT(*) - COUNT(snapped_road_class)                AS unsnapped
FROM vd_static;
