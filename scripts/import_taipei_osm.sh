#!/usr/bin/env bash
#
# Import Taipei OSM road network into the running TimescaleDB+PostGIS container.
#
# Steps:
#   1. Download taiwan-latest.osm.pbf from Geofabrik (24h cache).
#   2. Run osm2pgsql --bbox against the timescaledb service (Docker, on traffic-net).
#      (osm2pgsql does the bbox crop in-process, so we don't need a separate
#      osmconvert/osmium step.)
#
# Run from repo root: bash scripts/import_taipei_osm.sh
#
# Requires: docker, the timescaledb container running (compose up -d timescaledb).

set -euo pipefail

DATA_DIR="${DATA_DIR:-data/osm}"
mkdir -p "$DATA_DIR"

PBF_URL="https://download.geofabrik.de/asia/taiwan-latest.osm.pbf"
PBF_PATH="$DATA_DIR/taiwan-latest.osm.pbf"

# Bbox: (left=lng_min, bottom=lat_min, right=lng_max, top=lat_max)
BBOX_LEFT="121.45"
BBOX_BOTTOM="24.96"
BBOX_RIGHT="121.67"
BBOX_TOP="25.21"

# DB connection (matches infra/docker-compose.yml)
DB_NAME="${POSTGRES_DB:-traffic_data}"
DB_USER="${POSTGRES_USER:-admin}"
DB_PASSWORD="${POSTGRES_PASSWORD:-secret}"
DB_HOST="${DB_HOST:-timescaledb}"
DB_PORT="${DB_PORT:-5432}"

DOCKER_NETWORK="${DOCKER_NETWORK:-infra_traffic-net}"
OSM2PGSQL_IMAGE="${OSM2PGSQL_IMAGE:-iboates/osm2pgsql:latest}"

CACHE_AGE_SEC=$((24 * 3600))

# --- 1. Download taiwan PBF (with 24h cache) ---
download_pbf() {
    if [[ -f "$PBF_PATH" ]]; then
        local mtime
        if stat -c %Y "$PBF_PATH" >/dev/null 2>&1; then
            mtime=$(stat -c %Y "$PBF_PATH")
        else
            # macOS / BSD stat
            mtime=$(stat -f %m "$PBF_PATH")
        fi
        local age=$(( $(date +%s) - mtime ))
        if (( age < CACHE_AGE_SEC )); then
            echo "[import_taipei_osm] using cached $PBF_PATH (age ${age}s)"
            return
        fi
        echo "[import_taipei_osm] cache expired (age ${age}s), re-downloading"
    fi
    echo "[import_taipei_osm] downloading $PBF_URL"
    curl -fL --progress-bar -o "$PBF_PATH" "$PBF_URL"
}

# --- 2. Run osm2pgsql against running timescaledb (with built-in bbox filter) ---
run_osm2pgsql() {
    echo "[import_taipei_osm] running osm2pgsql -> $DB_HOST:$DB_PORT/$DB_NAME (bbox crop in-process)"
    
    docker run --rm \
        --network "$DOCKER_NETWORK" \
        -e PGPASSWORD="$DB_PASSWORD" \
        -v "$(pwd)/$DATA_DIR:/data:ro" \
        -v "$(pwd)/scripts:/scripts:ro" \
        "$OSM2PGSQL_IMAGE" \
        osm2pgsql \
            --create --slim --hstore \
            --bbox "$BBOX_LEFT,$BBOX_BOTTOM,$BBOX_RIGHT,$BBOX_TOP" \
            --style //scripts/osm2pgsql.style \
            -d "$DB_NAME" \
            -U "$DB_USER" \
            -H "$DB_HOST" \
            -P "$DB_PORT" \
            /data/taiwan-latest.osm.pbf
}

download_pbf
run_osm2pgsql

echo "[import_taipei_osm] done. Next: build graph with"
echo "  docker compose -f infra/docker-compose.yml exec -T timescaledb \\"
echo "    psql -U $DB_USER -d $DB_NAME -f /scripts/build_graph_from_osm.sql"
