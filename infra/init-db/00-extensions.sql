-- Enable required PostgreSQL extensions in dependency order.
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;
-- hstore is needed by osm2pgsql --hstore (raw OSM tags column on planet_osm_*).
CREATE EXTENSION IF NOT EXISTS hstore;
