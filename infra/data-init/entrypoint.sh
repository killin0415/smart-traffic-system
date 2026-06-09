#!/usr/bin/env bash
# One-shot data initialisation for the smart-traffic-system stack.
#
# Idempotent: re-running on an already-seeded database exits early.

set -euo pipefail

DB_HOST="${DB_HOST:-timescaledb}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-traffic_data}"
DB_USER="${POSTGRES_USER:-admin}"
DB_PASSWORD="${POSTGRES_PASSWORD:-secret}"

export PGPASSWORD="$DB_PASSWORD"

PBF_URL="${PBF_URL:-https://download.geofabrik.de/asia/taiwan-latest.osm.pbf}"
BBOX="${BBOX:-121.45,24.96,121.67,25.21}"
DATA_DIR="/data/osm"
PBF_PATH="$DATA_DIR/taiwan-latest.osm.pbf"

SKIP_MIN_NODES="${SKIP_MIN_NODES:-1000}"

# osm2pgsql parallelism / memory tuning. All overridable via env so CI or
# low-RAM hosts can dial them back.
#   OSM_NUM_PROCESSES — worker threads for the processing phase (default: all CPUs)
#   OSM_CACHE_MB      — in-RAM node cache in MB (0 disables; pairs with flat-nodes)
#   OSM_FLAT_NODES    — flat-node file path; off-DB node store, much faster for
#                       a whole-country PBF scan. Set empty to fall back to DB.
OSM_NUM_PROCESSES="${OSM_NUM_PROCESSES:-$(nproc 2>/dev/null || echo 4)}"
OSM_CACHE_MB="${OSM_CACHE_MB:-2048}"
OSM_FLAT_NODES="${OSM_FLAT_NODES:-$DATA_DIR/flat-nodes.bin}"

log() { echo "[data-init] $*" >&2; }

log "waiting for $DB_HOST:$DB_PORT to accept connections..."
for _ in $(seq 1 60); do
    if pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
if ! pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
    log "ERROR: postgres did not become ready"
    exit 1
fi

# Idempotency: if the graph is already populated, do nothing.
table_exists=$(psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -tA -c \
    "SELECT 1 FROM information_schema.tables WHERE table_name='traffic_node';" \
    2>/dev/null | tr -d '[:space:]' || true)
if [[ "$table_exists" == "1" ]]; then
    rows=$(psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -tA -c \
        "SELECT count(*) FROM traffic_node;" 2>/dev/null | tr -d '[:space:]' || echo 0)
    if [[ "${rows:-0}" -ge "$SKIP_MIN_NODES" ]]; then
        log "traffic_node already has $rows rows (>= $SKIP_MIN_NODES); skipping import."
        exit 0
    fi
    log "traffic_node has only ${rows:-0} rows; running full import..."
else
    log "traffic_node not present; running full import..."
fi

# 1. Download PBF (cached via /data volume).
mkdir -p "$DATA_DIR"
if [[ ! -f "$PBF_PATH" ]]; then
    log "downloading $PBF_URL ..."
    curl -fL --progress-bar -o "$PBF_PATH" "$PBF_URL"
else
    log "using cached $PBF_PATH"
fi

# 2. Pre-crop the PBF to the target bbox with osmium.
#    osm2pgsql --bbox only filters OUTPUT — it still parses every node/way in
#    the whole-country PBF (24.9M nodes for Taiwan) and builds their geometry,
#    which is what made the import take ~1h. osmium extract crops the INPUT
#    first (one sequential pass, a few minutes) so osm2pgsql then sees only the
#    Taipei subset (~100x fewer features) and finishes in minutes.
#    --strategy complete_ways keeps ways that cross the bbox edge intact, so the
#    routing graph isn't truncated at the boundary.
IMPORT_PBF="$DATA_DIR/cropped.osm.pbf"
log "cropping PBF to bbox=$BBOX with osmium..."
osmium extract \
    --bbox "$BBOX" \
    --strategy complete_ways \
    --overwrite \
    -o "$IMPORT_PBF" \
    "$PBF_PATH"

# 3. osm2pgsql import of the cropped extract.
#    --number-processes spreads the processing phase across CPUs, -C keeps nodes
#    in RAM, and --flat-nodes stores the node index in a file instead of DB
#    tables. No --bbox here: the input is already cropped.
osm2pgsql_args=(
    --create --slim --hstore
    --number-processes "$OSM_NUM_PROCESSES"
    --style /app/scripts/osm2pgsql.style
    -d "$DB_NAME" -U "$DB_USER"
    -H "$DB_HOST" -P "$DB_PORT"
)
if [[ "${OSM_CACHE_MB:-0}" -gt 0 ]]; then
    osm2pgsql_args+=(-C "$OSM_CACHE_MB")
fi
if [[ -n "$OSM_FLAT_NODES" ]]; then
    osm2pgsql_args+=(--flat-nodes "$OSM_FLAT_NODES")
    # Stale flat-node file from a prior aborted run would corrupt the index.
    rm -f "$OSM_FLAT_NODES"
fi
log "running osm2pgsql (procs=$OSM_NUM_PROCESSES, cache=${OSM_CACHE_MB}MB)..."
osm2pgsql "${osm2pgsql_args[@]}" "$IMPORT_PBF"

# 4. Build the routing graph.
log "running build_graph_from_osm.sql..."
psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" \
    -v ON_ERROR_STOP=1 \
    -f /app/scripts/build_graph_from_osm.sql

# 5. Seed VD static via the multiagent venv (script imports from src.*).
log "seeding VD static..."
cd /app/multiagent
DATABASE_URL="postgresql+asyncpg://$DB_USER:$DB_PASSWORD@$DB_HOST:$DB_PORT/$DB_NAME" \
KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS:-kafka:29092}" \
uv run --no-sync python /app/scripts/seed_vd_static.py

# 6. Snap VD to nearest edge.
log "running post_build_snap_vd.sql..."
psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" \
    -v ON_ERROR_STOP=1 \
    -f /app/scripts/post_build_snap_vd.sql

log "import complete."
