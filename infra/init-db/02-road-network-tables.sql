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
    base_weight     DOUBLE PRECISION NOT NULL
);
