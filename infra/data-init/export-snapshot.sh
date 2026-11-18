#!/usr/bin/env bash
# Export the seeded road graph + VD static tables from a running stack into
# infra/data-init/seed/, so other machines restore it in seconds instead of
# running the full OSM pipeline (download + osmium + osm2pgsql + graph build).
#
# Run from the repo root (Git Bash on Windows is fine) after data-init has
# finished successfully:
#   bash infra/data-init/export-snapshot.sh
#
# Re-run it (and commit the result) whenever build_graph_from_osm.sql,
# osm2pgsql.style, post_build_snap_vd.sql or BBOX change — otherwise the
# entrypoint detects the fingerprint mismatch and falls back to a full build.

set -euo pipefail

DB_CONTAINER="${DB_CONTAINER:-traffic_db}"
DB_NAME="${POSTGRES_DB:-traffic_data}"
DB_USER="${POSTGRES_USER:-admin}"
BBOX="${BBOX:-121.45,24.96,121.67,25.21}"   # must match entrypoint.sh
OUT_DIR="infra/data-init/seed"
OUT="$OUT_DIR/taipei-graph.dump"
META="$OUT_DIR/taipei-graph.meta"

# Stop Git Bash from rewriting the /tmp path passed to docker.
export MSYS_NO_PATHCONV=1

nodes=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA \
    -c "SELECT count(*) FROM traffic_node")
if [[ "${nodes:-0}" -lt 1000 ]]; then
    echo "traffic_node has only ${nodes:-0} rows — run data-init first." >&2
    exit 1
fi

echo "dumping traffic_node / traffic_edge / vd_static ($nodes nodes)..."
docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" \
    -Fc -Z9 --data-only --no-owner --no-privileges \
    -t traffic_node -t traffic_node_id_seq \
    -t traffic_edge -t traffic_edge_id_seq \
    -t vd_static \
    -f /tmp/taipei-graph.dump
mkdir -p "$OUT_DIR"
docker cp "$DB_CONTAINER:/tmp/taipei-graph.dump" "$OUT"
docker exec "$DB_CONTAINER" rm -f /tmp/taipei-graph.dump

# Same fingerprint recipe as graph_fingerprint() in entrypoint.sh.
{
    echo "bbox=$BBOX"
    tr -d '\r' < scripts/build_graph_from_osm.sql
    tr -d '\r' < scripts/osm2pgsql.style
    tr -d '\r' < scripts/post_build_snap_vd.sql
} | sha256sum | cut -d' ' -f1 > "$META"

echo "wrote $OUT ($(du -h "$OUT" | cut -f1)) and $META"
