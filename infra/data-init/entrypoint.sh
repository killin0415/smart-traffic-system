#!/usr/bin/env bash
# One-shot data initialisation for the smart-traffic-system stack.
#
# Idempotent: re-running on an already-seeded database exits early.
#
# Two paths:
#   FAST  — restore a pre-built snapshot (/app/seed/taipei-graph.dump, made by
#           infra/data-init/export-snapshot.sh). Seconds, no network needed.
#   FULL  — download Taiwan PBF -> osmium crop -> osm2pgsql -> graph build ->
#           VD seed -> VD snap. Used when no snapshot is shipped, when the
#           snapshot was built from a different build_graph_from_osm.sql /
#           bbox, or when FORCE_REBUILD=1.

set -euo pipefail

DB_HOST="${DB_HOST:-timescaledb}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-traffic_data}"
DB_USER="${POSTGRES_USER:-admin}"
DB_PASSWORD="${POSTGRES_PASSWORD:-secret}"

export PGPASSWORD="$DB_PASSWORD" PGHOST="$DB_HOST" PGPORT="$DB_PORT" \
       PGUSER="$DB_USER" PGDATABASE="$DB_NAME"

PBF_URL="${PBF_URL:-https://download.geofabrik.de/asia/taiwan-latest.osm.pbf}"
BBOX="${BBOX:-121.45,24.96,121.67,25.21}"
DATA_DIR="/data/osm"
PBF_PATH="$DATA_DIR/taiwan-latest.osm.pbf"

SKIP_MIN_NODES="${SKIP_MIN_NODES:-1000}"
FORCE_REBUILD="${FORCE_REBUILD:-0}"
SNAPSHOT_PATH="${SNAPSHOT_PATH:-/app/seed/taipei-graph.dump}"
SNAPSHOT_META="${SNAPSHOT_PATH%.dump}.meta"

# osm2pgsql tuning. All overridable via env so low-RAM hosts can dial back.
#   OSM_NUM_PROCESSES — worker threads (default: all CPUs)
#   OSM_CACHE_MB      — in-RAM node cache in MB
#   OSM_SLIM          — 1 = --slim (middle tables in the DB). Default 0: the
#                       cropped Taipei extract fits in RAM easily, and non-slim
#                       skips writing/indexing the planet_osm_{nodes,ways,rels}
#                       middle tables we never read.
#   OSM_FLAT_NODES    — flat-node file path (slim mode only). Default empty:
#                       flat-nodes pays off for a planet/continent import, but
#                       for a city extract it just creates a huge sparse file,
#                       which is slow on Docker Desktop's WSL2 disk.
OSM_NUM_PROCESSES="${OSM_NUM_PROCESSES:-$(nproc 2>/dev/null || echo 4)}"
OSM_CACHE_MB="${OSM_CACHE_MB:-1024}"
OSM_SLIM="${OSM_SLIM:-0}"
OSM_FLAT_NODES="${OSM_FLAT_NODES:-}"

log() { echo "[data-init] $(date +%H:%M:%S) $*" >&2; }
q()   { psql -X -tA -v ON_ERROR_STOP=1 -c "$1"; }

# Fingerprint of the inputs that determine the graph. CRs stripped so a
# Windows checkout (autocrlf) hashes the same as the Linux one.
graph_fingerprint() {
    {
        echo "bbox=$BBOX"
        tr -d '\r' < /app/scripts/build_graph_from_osm.sql
        tr -d '\r' < /app/scripts/osm2pgsql.style
        tr -d '\r' < /app/scripts/post_build_snap_vd.sql
    } | sha256sum | cut -d' ' -f1
}

log "waiting for $DB_HOST:$DB_PORT to accept connections..."
for _ in $(seq 1 60); do
    pg_isready -q && break
    sleep 2
done
if ! pg_isready -q; then
    log "ERROR: postgres did not become ready"
    exit 1
fi

# Idempotency: if the graph is already populated, do nothing.
if [[ "$FORCE_REBUILD" != "1" ]]; then
    table_exists=$(q "SELECT 1 FROM information_schema.tables WHERE table_name='traffic_node';" 2>/dev/null || true)
    if [[ "$table_exists" == "1" ]]; then
        rows=$(q "SELECT count(*) FROM traffic_node;" 2>/dev/null || echo 0)
        if [[ "${rows:-0}" -ge "$SKIP_MIN_NODES" ]]; then
            log "traffic_node already has $rows rows (>= $SKIP_MIN_NODES); skipping import."
            exit 0
        fi
        log "traffic_node has only ${rows:-0} rows; importing..."
    else
        log "traffic_node not present; importing..."
    fi
fi

# ---------------------------------------------------------------------------
# FAST path: restore snapshot.
# ---------------------------------------------------------------------------
want_fp="$(graph_fingerprint)"
if [[ "$FORCE_REBUILD" != "1" && -s "$SNAPSHOT_PATH" ]]; then
    have_fp="$(tr -d '\r[:space:]' < "$SNAPSHOT_META" 2>/dev/null || true)"
    if [[ "$have_fp" == "$want_fp" ]]; then
        log "restoring snapshot $SNAPSHOT_PATH ..."
        q "TRUNCATE TABLE traffic_edge, traffic_node CASCADE; TRUNCATE TABLE vd_static;" >/dev/null
        # --disable-triggers: FK order between node/edge doesn't matter.
        # --single-transaction: a failed restore leaves the tables empty, so
        # the next run retries instead of skipping on a half-loaded graph.
        pg_restore --data-only --disable-triggers --no-owner --no-privileges \
            --single-transaction -d "$DB_NAME" "$SNAPSHOT_PATH"
        # Re-sync SERIAL sequences in case the dump didn't carry them.
        q "SELECT setval(pg_get_serial_sequence('traffic_node','id'), GREATEST((SELECT max(id) FROM traffic_node), 1));
           SELECT setval(pg_get_serial_sequence('traffic_edge','id'), GREATEST((SELECT max(id) FROM traffic_edge), 1));" >/dev/null
        q "ANALYZE traffic_node; ANALYZE traffic_edge; ANALYZE vd_static;" >/dev/null
        log "snapshot restored: nodes=$(q 'SELECT count(*) FROM traffic_node') edges=$(q 'SELECT count(*) FROM traffic_edge') vd=$(q 'SELECT count(*) FROM vd_static')"
        log "import complete."
        exit 0
    fi
    log "snapshot fingerprint mismatch (graph SQL/style/bbox changed since export); falling back to full build."
fi

# ---------------------------------------------------------------------------
# FULL path.
# ---------------------------------------------------------------------------

# VD static fetch is pure network + a small upsert, independent of the OSM
# pipeline — run it concurrently and join before the snap step.
log "seeding VD static (background)..."
DATABASE_URL="postgresql+asyncpg://$DB_USER:$DB_PASSWORD@$DB_HOST:$DB_PORT/$DB_NAME" \
    python /app/scripts/seed_vd_static.py &
vd_pid=$!

# 1. Download PBF (cached via /data volume).
mkdir -p "$DATA_DIR"
if [[ ! -s "$PBF_PATH" ]]; then
    log "downloading $PBF_URL ..."
    curl -fL --retry 3 --progress-bar -o "$PBF_PATH.part" "$PBF_URL"
    mv "$PBF_PATH.part" "$PBF_PATH"
else
    log "using cached $PBF_PATH"
fi

# 2. Pre-crop the PBF to the target bbox with osmium (osm2pgsql --bbox only
#    filters OUTPUT and would still parse all of Taiwan). Cached: re-cropping
#    is skipped when the source PBF and bbox are unchanged.
#    --strategy complete_ways keeps ways crossing the bbox edge intact.
IMPORT_PBF="$DATA_DIR/cropped.osm.pbf"
CROP_STAMP="$IMPORT_PBF.stamp"
crop_key="bbox=$BBOX src=$(stat -c '%s-%Y' "$PBF_PATH")"
if [[ -s "$IMPORT_PBF" && "$(cat "$CROP_STAMP" 2>/dev/null)" == "$crop_key" ]]; then
    log "using cached crop $IMPORT_PBF"
else
    log "cropping PBF to bbox=$BBOX with osmium..."
    osmium extract --bbox "$BBOX" --strategy complete_ways --overwrite \
        -o "$IMPORT_PBF" "$PBF_PATH"
    echo "$crop_key" > "$CROP_STAMP"
fi

# 3. osm2pgsql import of the cropped extract.
osm2pgsql_args=(
    --create --hstore
    --number-processes "$OSM_NUM_PROCESSES"
    --style /app/scripts/osm2pgsql.style
    -d "$DB_NAME" -U "$DB_USER" -H "$DB_HOST" -P "$DB_PORT"
)
if [[ "${OSM_CACHE_MB:-0}" -gt 0 ]]; then
    osm2pgsql_args+=(-C "$OSM_CACHE_MB")
fi
if [[ "$OSM_SLIM" == "1" ]]; then
    osm2pgsql_args+=(--slim --drop)
    if [[ -n "$OSM_FLAT_NODES" ]]; then
        osm2pgsql_args+=(--flat-nodes "$OSM_FLAT_NODES")
        rm -f "$OSM_FLAT_NODES"   # stale file from an aborted run corrupts the index
    fi
fi
log "running osm2pgsql (procs=$OSM_NUM_PROCESSES, cache=${OSM_CACHE_MB}MB, slim=$OSM_SLIM)..."
osm2pgsql "${osm2pgsql_args[@]}" "$IMPORT_PBF"

# 4. Build the routing graph.
log "running build_graph_from_osm.sql..."
psql -X -v ON_ERROR_STOP=1 -f /app/scripts/build_graph_from_osm.sql

# 5. Join the VD seeder.
if ! wait "$vd_pid"; then
    log "ERROR: VD static seeding failed"
    exit 1
fi

# 6. Snap VD to nearest edge.
log "running post_build_snap_vd.sql..."
psql -X -v ON_ERROR_STOP=1 -f /app/scripts/post_build_snap_vd.sql

log "import complete."
