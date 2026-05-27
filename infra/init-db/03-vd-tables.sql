-- VD (Vehicle Detector) static metadata + dynamic readings.
-- Source: data.taipei VD.xml (static) and GetVDDATA.xml (dynamic, 5-min update).

CREATE TABLE IF NOT EXISTS vd_static (
    vdid                VARCHAR(64) PRIMARY KEY,
    link_id             VARCHAR(64),
    road_name           VARCHAR(255),
    road_class          VARCHAR(32),
    bidirectional       BOOLEAN NOT NULL DEFAULT FALSE,
    bearing             VARCHAR(16),
    latitude            DOUBLE PRECISION NOT NULL,
    longitude           DOUBLE PRECISION NOT NULL,
    geom                geometry(Point, 4326),
    snapped_road_class  VARCHAR(32)
);

CREATE INDEX IF NOT EXISTS ix_vd_static_geom
    ON vd_static USING GIST (geom);

CREATE TABLE IF NOT EXISTS vd_reading (
    ts            TIMESTAMPTZ      NOT NULL,
    vdid          VARCHAR(64)      NOT NULL,
    lane_no       INTEGER          NOT NULL,
    avg_speed     DOUBLE PRECISION,
    volume        INTEGER,
    occupancy     DOUBLE PRECISION,
    PRIMARY KEY (ts, vdid, lane_no)
);

SELECT create_hypertable(
    'vd_reading',
    'ts',
    if_not_exists => TRUE
);

-- Drop readings older than 30 days
SELECT add_retention_policy(
    'vd_reading',
    INTERVAL '30 days',
    if_not_exists => TRUE
);
