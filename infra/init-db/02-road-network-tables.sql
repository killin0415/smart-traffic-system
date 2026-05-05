-- Road network static tables for A* pathfinding

CREATE TABLE IF NOT EXISTS traffic_node (
    id          SERIAL PRIMARY KEY,
    latitude    DOUBLE PRECISION NOT NULL,
    longitude   DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS traffic_edge (
    id              SERIAL PRIMARY KEY,
    source_node_id  INTEGER NOT NULL REFERENCES traffic_node(id),
    target_node_id  INTEGER NOT NULL REFERENCES traffic_node(id),
    road_name       VARCHAR(255),
    length_km       DOUBLE PRECISION NOT NULL,
    speed_limit_kmh INTEGER NOT NULL,
    base_weight     DOUBLE PRECISION NOT NULL,
    tdx_section_id  VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS ix_traffic_edge_tdx_section_id
    ON traffic_edge (tdx_section_id);

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

-- Live traffic time-series (TimescaleDB hypertable)
CREATE TABLE IF NOT EXISTS traffic_history (
    time            TIMESTAMPTZ      NOT NULL,
    tdx_section_id  VARCHAR(64)      NOT NULL,
    travel_speed    DOUBLE PRECISION,
    travel_time     DOUBLE PRECISION,
    PRIMARY KEY (time, tdx_section_id)
);

-- Convert to hypertable (idempotent via if_not_exists)
SELECT create_hypertable(
    'traffic_history',
    'time',
    if_not_exists => TRUE
);
