-- Parking lot static metadata + dynamic availability.
-- Source: data.taipei parking lot dataset (id d5c0656b-5250-4179-a491-c94daa56ef2c).

CREATE TABLE IF NOT EXISTS parking_lot (
    id            INTEGER PRIMARY KEY,
    name          VARCHAR(255),
    address       VARCHAR(512),
    total_car     INTEGER,
    total_motor   INTEGER,
    latitude      DOUBLE PRECISION NOT NULL,
    longitude     DOUBLE PRECISION NOT NULL,
    geom          geometry(Point, 4326)
);

CREATE INDEX IF NOT EXISTS ix_parking_lot_geom
    ON parking_lot USING GIST (geom);

CREATE TABLE IF NOT EXISTS parking_availability (
    ts             TIMESTAMPTZ NOT NULL,
    lot_id         INTEGER     NOT NULL,
    available_car  INTEGER,
    available_motor INTEGER,
    PRIMARY KEY (ts, lot_id)
);

SELECT create_hypertable(
    'parking_availability',
    'ts',
    if_not_exists => TRUE
);

SELECT add_retention_policy(
    'parking_availability',
    INTERVAL '30 days',
    if_not_exists => TRUE
);
