# taipei-opendata-rebuild — implementation notes

End-to-end notes from rebuilding the Taipei routing stack on top of OSM
(road network) + data.taipei (live VD, parking) + PostGIS (spatial ops).

## Migration order (one-shot, dev DB)

```bash
# repo root
docker compose -f infra/docker-compose.yml down -v
docker compose -f infra/docker-compose.yml up -d timescaledb
# verify extensions
docker compose -f infra/docker-compose.yml exec timescaledb \
  psql -U admin -d traffic_data -c "\dx"
# expect: timescaledb, postgis, postgis_topology

bash scripts/import_taipei_osm.sh                         # download + osm2pgsql

docker compose -f infra/docker-compose.yml exec -T timescaledb \
  psql -U admin -d traffic_data -f /scripts/build_graph_from_osm.sql

uv run --script scripts/seed_vd_static.py                  # vd_static + geom

docker compose -f infra/docker-compose.yml exec -T timescaledb \
  psql -U admin -d traffic_data -f /scripts/post_build_snap_vd.sql

cd backend/multiagent-service && uv run python main.py
```

## Decisions worth re-reading later

- **PG14 stays.** Switched image to `timescale/timescaledb-ha:pg14-all`
  (carries PostGIS + TimescaleDB + Toolkit). Cross-major upgrades cost more
  than they buy at capstone scale.
- **Graph is rebuilt as `traffic_node`/`traffic_edge`** rather than queried
  live from `planet_osm_*`. Keeps the A* hot path stable. Raw OSM tables
  remain untouched as a future Phase-2 source for nearby-POI queries.
- **Single-source `default_maxspeed`.** PL/pgSQL function in
  `infra/init-db/06-default-maxspeed-fn.sql`; both `build_graph_from_osm.sql`
  and `WeightProvider.rebuild()` read from it. Do not add a Python copy.
- **VD static seed is offline.** `scripts/seed_vd_static.py` is not in the
  lifespan; service boot does not depend on data.taipei reachability.
- **Signal penalty is per-node, not per-edge.** OSM has
  `highway=traffic_signals` points; we snap them to `traffic_node.has_signal`
  with `ST_DWithin(30 m)`. A* adds `SIGNAL_PENALTY_HR` once per
  `has_signal=TRUE` traversal, except the destination node.

## Things that bite

- The dynamic VD endpoint is **plain XML, not gzip**, despite some old docs
  hinting at gzip. Use `httpx.get` + `ET.fromstring(text)`. No Content-Encoding
  handling needed.
- `osm2pgsql` Windows native install is painful; use the Docker image
  (`iboates/osm2pgsql`) and run on the same compose network as the DB so
  `-H timescaledb` resolves. The compose project name (e.g. `infra`) is
  prefixed onto the network name (`infra_traffic-net`).
- `cKDTree.query(distance_upper_bound=…)` returns `inf` distances and
  out-of-range indices when there's no neighbour within the radius. Guard
  with `np.isfinite(dists) & (idxs < N)`.
- `RoadGraph.update_weight(edge_id, w)` is now an **absolute** weight (hours),
  not a multiplicative factor. The old `base_weight × congestion_factor`
  pattern is gone with `traffic_edge.base_weight`.
- The Kotlin DTO needs `@JsonIgnoreProperties(ignoreUnknown = true)` because
  Python adds fields more often than the Kotlin side rebuilds.

## Acceptance smoke checks

| What | Where to verify |
|---|---|
| Image swap + extensions | `\dx` in psql shows timescaledb + postgis + postgis_topology |
| OSM graph size | `SELECT COUNT(*) FROM traffic_node` > 30k; `traffic_edge` > 80k |
| VD ingestion alive | `SELECT COUNT(*) FROM vd_reading WHERE ts > NOW() - INTERVAL '5 min'` > 500 |
| Parking refresher alive | `SELECT MAX(ts) FROM parking_availability` within last 5 min |
| signal_nodes plausible | `SELECT COUNT(*) FROM traffic_node WHERE has_signal` > 1k |
| ETA ±50% vs Google Maps | manual: 台北車站 → 101 |

## Removed / deprecated

- `src/agents/traffic.py` — TDX Live Section integration. Gone.
- `scripts/import_tdx_road_network.py`, `data/taipei_road_sections.json`,
  `data/speed_cameras.csv` (OD 6489 全國表). Gone.
- `traffic_history` hypertable. Replaced by `vd_reading` (per-lane
  granularity, retention 30 d).
- Env vars `TDX_CLIENT_ID`, `TDX_CLIENT_SECRET`, `TDX_LIVE_REFRESH_SECONDS`.
  No longer read.
- Internal API `RoadGraph.update_weight(edge_id, congestion_factor)`.
  Replaced by `update_weight(edge_id, new_weight)` (absolute hours).
