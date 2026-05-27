-- Road network static tables for A* pathfinding (OSM-derived, PostGIS-backed).

CREATE TABLE IF NOT EXISTS traffic_node (
    id          SERIAL PRIMARY KEY,
    latitude    DOUBLE PRECISION NOT NULL,
    longitude   DOUBLE PRECISION NOT NULL,
    geom        geometry(Point, 4326),
    has_signal  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS ix_traffic_node_geom
    ON traffic_node USING GIST (geom);

CREATE INDEX IF NOT EXISTS ix_traffic_node_signal
    ON traffic_node (has_signal) WHERE has_signal;

CREATE TABLE IF NOT EXISTS traffic_edge (
    id              SERIAL PRIMARY KEY,
    source_node_id  INTEGER NOT NULL REFERENCES traffic_node(id),
    target_node_id  INTEGER NOT NULL REFERENCES traffic_node(id),
    road_name       VARCHAR(255),
    length_km       DOUBLE PRECISION NOT NULL,
    road_class      VARCHAR(32),
    max_speed_kmh   INTEGER,
    oneway          BOOLEAN NOT NULL DEFAULT FALSE,
    geom            geometry(LineString, 4326)
);

CREATE INDEX IF NOT EXISTS ix_traffic_edge_geom
    ON traffic_edge USING GIST (geom);

CREATE INDEX IF NOT EXISTS ix_traffic_edge_road_class
    ON traffic_edge (road_class);

-- Speed camera static data
CREATE TABLE IF NOT EXISTS speed_camera (
    id              SERIAL PRIMARY KEY,
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    direction       VARCHAR(64),
    speed_limit     INTEGER NOT NULL,
    address         VARCHAR(255),
    nearest_edge_id INTEGER REFERENCES traffic_edge(id)
);
