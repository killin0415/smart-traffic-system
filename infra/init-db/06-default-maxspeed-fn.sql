-- Single source of truth for default max speed by OSM highway class.
-- Both `build_graph_from_osm.sql` and the Python WeightProvider read from this
-- function so we never have two copies drifting apart.
--
-- Values are urban-Taipei reasonable defaults (km/h) for cars; pedestrian /
-- bicycle / footway classes are filtered out at graph-build time and never
-- reach this function.

CREATE OR REPLACE FUNCTION default_maxspeed(highway TEXT)
RETURNS INTEGER
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    RETURN CASE LOWER(COALESCE(highway, ''))
        WHEN 'motorway'        THEN 80
        WHEN 'motorway_link'   THEN 60
        WHEN 'trunk'           THEN 70
        WHEN 'trunk_link'      THEN 50
        WHEN 'primary'         THEN 50
        WHEN 'primary_link'    THEN 40
        WHEN 'secondary'       THEN 50
        WHEN 'secondary_link'  THEN 40
        WHEN 'tertiary'        THEN 40
        WHEN 'tertiary_link'   THEN 30
        WHEN 'unclassified'    THEN 40
        WHEN 'residential'     THEN 30
        WHEN 'living_street'   THEN 20
        WHEN 'service'         THEN 20
        ELSE 30
    END;
END;
$$;
