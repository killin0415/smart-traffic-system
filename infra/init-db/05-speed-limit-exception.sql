-- Stub for data.taipei #4 speed-limit exception table.
-- Implementation deferred to a future change; this table is created so
-- downstream queries that LEFT JOIN against it return zero rows instead of
-- erroring on missing relation.

CREATE TABLE IF NOT EXISTS speed_limit_exception (
    id            SERIAL PRIMARY KEY,
    road_name     VARCHAR(255),
    section_desc  VARCHAR(512),
    speed_limit   INTEGER,
    latitude      DOUBLE PRECISION,
    longitude     DOUBLE PRECISION,
    geom          geometry(Point, 4326)
);

CREATE INDEX IF NOT EXISTS ix_speed_limit_exception_geom
    ON speed_limit_exception USING GIST (geom);
