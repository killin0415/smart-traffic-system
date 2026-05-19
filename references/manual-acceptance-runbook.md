# Manual Acceptance Runbook — taipei-opendata-rebuild §11

Step-by-step verification for the two manual gates left in `tasks.md`:

- **11.2** — VD ingestion produces > 500 rows per 5-minute window
- **11.3** — Route `台北車站 → 101` ETA within ±50 % of Google Maps

Pre-requisite: §11.5 (`uv run pytest`) is green. Both gates require a
running stack with the migration already applied (see
`references/taipei-opendata-rebuild-implementation.md` §"Migration order").

---

## 0. Bring up the stack (once per session)

```powershell
# repo root
docker compose -f infra/docker-compose.yml up -d timescaledb kafka zookeeper

# Verify extensions are loaded
docker compose -f infra/docker-compose.yml exec timescaledb `
  psql -U admin -d traffic_data -c "\dx"
# Expect: timescaledb, postgis, postgis_topology

# Confirm road network is already imported (skip if previously done)
docker compose -f infra/docker-compose.yml exec timescaledb `
  psql -U admin -d traffic_data -c "SELECT COUNT(*) FROM traffic_node;"
# Expect ≥ 30000
```

If `traffic_node` is empty, run the migration sequence:
```powershell
bash scripts/import_taipei_osm.sh
docker compose -f infra/docker-compose.yml exec -T timescaledb `
  psql -U admin -d traffic_data -f /scripts/build_graph_from_osm.sql
Set-Location backend\multiagent-service; uv run python ..\..\scripts\seed_vd_static.py
docker compose -f infra/docker-compose.yml exec -T timescaledb `
  psql -U admin -d traffic_data -f /scripts/post_build_snap_vd.sql
```

Then start the service:
```powershell
Set-Location E:\smart-traffic-system\backend\multiagent-service
uv run python main.py
# Leave running in a separate terminal. Logs should show:
#   "graph loaded: N nodes / M edges"
#   "weight provider rebuilt"
#   "started periodic VD refresh"
#   "started periodic parking refresh"
```

---

## Gate 11.2 — VD ingestion smoke check

**Goal.** After the service has been running ≥ 5 minutes, the
`run_periodic_vd_refresh` loop should have completed at least one cycle
and persisted recent readings.

### Steps

1. **Note the start time** (when the service finished booting and the
   first "VD cycle complete" log line appeared).
2. **Wait 6 minutes.** The default `VD_REFRESH_SECONDS` is 300 s
   (5 min), so this guarantees the second cycle has fired *and* committed.
   If `VD_REFRESH_SECONDS` is overridden in `.env`, wait
   `2 × VD_REFRESH_SECONDS + 60 s`.
3. **Query the hypertable:**
   ```powershell
   docker compose -f infra/docker-compose.yml exec timescaledb `
     psql -U admin -d traffic_data -c `
     "SELECT COUNT(*) FROM vd_reading WHERE ts > NOW() - INTERVAL '5 min';"
   ```
4. **Pass criterion:** count `> 500`. Taipei has on the order of 800–1500
   active VDs × multiple lanes → easily 1000+ rows per 5-min window.

### If it fails

| Symptom | Likely cause | Check |
|---|---|---|
| Count = 0 | Refresh loop not running, or `vd_static` not seeded | `SELECT COUNT(*) FROM vd_static;` should be ≥ 800. Service logs should contain "VD cycle complete". |
| Count < 100 | Most VDs returning `avg_speed ≤ 0` (filtered out) — check raw fetch | Tail service logs for `fetch_vd_dynamic` exceptions. Hit `https://tcgbusfs.blob.core.windows.net/blobtisv/GetVDDATA.xml` directly to confirm endpoint health. |
| 100 < count < 500 | Endpoint partially down or rate-limited | Re-run after another 5-min cycle. If persistent, accept and note — degraded but functional. |

---

## Gate 11.3 — Route ETA vs Google Maps

**Goal.** `plan_optimal_route(台北車站 → 台北 101)` returns an ETA that is
within ±50 % of Google Maps' current driving ETA for the same OD pair.

Coordinates:
- 台北車站: `(25.0478, 121.5170)`
- 台北 101: `(25.0337, 121.5645)`

### Option A — via the MCP tool (interactive)

If you have an MCP client wired (e.g. through the chat agent), send:
```
plan_route(origin_lat=25.0478, origin_lng=121.5170,
           dest_lat=25.0337, dest_lng=121.5645, top_k=3)
```

### Option B — via Kafka request (matches production wiring)

```powershell
# In a new terminal, publish a route.request:
docker compose -f infra/docker-compose.yml exec kafka `
  kafka-console-producer --bootstrap-server localhost:9092 --topic route.request
# Paste one line then Ctrl-Z + Enter:
{"request_id":"manual-11-3","origin_lat":25.0478,"origin_lng":121.5170,"dest_lat":25.0337,"dest_lng":121.5645,"top_k":3}
```

Listen for the response:
```powershell
docker compose -f infra/docker-compose.yml exec kafka `
  kafka-console-consumer --bootstrap-server localhost:9092 `
  --topic route.response --from-beginning --max-messages 1
```

### Option C — via Python REPL (fastest for one-off verification)

```powershell
Set-Location E:\smart-traffic-system\backend\multiagent-service
uv run python -c @'
import asyncio
from src.db.async_session import async_session
from src.agents.routing import RoadGraph, plan_optimal_route
from src.agents.weight_provider import TaipeiWeightProvider

async def main():
    async with async_session() as s:
        graph = await RoadGraph.from_db(s)
    wp = TaipeiWeightProvider()
    await wp.rebuild(async_session)
    wp.apply_to_graph(graph)
    async with async_session() as s:
        out = await plan_optimal_route(
            s, graph, wp,
            25.0478, 121.5170,   # 台北車站
            25.0337, 121.5645,   # 台北 101
            k=3,
        )
    for i, r in enumerate(out["routes"], 1):
        print(f"Route {i}: {r['estimated_time_min']:.1f} min  "
              f"({r['distance_km']:.2f} km)  via {', '.join(r['road_names'][:3])}")

asyncio.run(main())
'@
```

### Compare against Google Maps

1. Open https://maps.google.com.
2. Set origin = `台北車站`, destination = `台北 101`, mode = **driving**.
3. Note Google's "X 分鐘" estimate **for current traffic** (not "in light
   traffic"). At rush hour expect 18–28 min; off-peak 10–15 min.
4. **Pass criterion:** `|our_eta - google_eta| / google_eta ≤ 0.5`.
   E.g. Google says 20 min → we must produce 10–30 min.

### If it fails

Record both numbers in the open issue
`memory/eta_accuracy_followup.md`. The known follow-up paths there are:
1. Tune intersection penalty (`SIGNAL_PENALTY_SECONDS`, default 20).
2. Apply a global `effective_speed_factor` to all WeightProvider tiers.
3. Wire an external ETA API as a sanity post-filter.

A first-pass failure on this gate is **not** a blocker for archiving the
change — the gate spec is ±50 %, and the systemic
underestimation is already tracked. Calibration tuning belongs to a
follow-up change, not this one.

---

## Sign-off

After both gates clear, flip the two checkboxes in
`openspec/changes/taipei-opendata-rebuild/tasks.md`:

- `- [ ] 11.2 manual acceptance: ...` → `- [x] 11.2 ...`
- `- [ ] 11.3 manual acceptance: ...` → `- [x] 11.3 ...`

Then archive with `/opsx:archive`.

---

# Manual Acceptance — complete-demo-stack §14

End-to-end browser verification for the demo stack (frontend + REST +
Kafka + multiagent). Pre-requisite: §11.2 and §11.3 above already
green, OR the multiagent service has been re-bootstrapped against a
fresh Taipei graph in this session.

## 0. Bring up all four tiers

```powershell
# Terminal 1 — infra
docker compose -f infra/docker-compose.yml up -d timescaledb kafka zookeeper

# Terminal 2 — main-service
Set-Location backend\main-service
.\gradlew.bat bootRun
# Wait for "Started MainServiceApplicationKt"

# Terminal 3 — multiagent-service
Set-Location backend\multiagent-service
uv run python main.py
# Wait for "graph loaded", "started periodic VD refresh"

# Terminal 4 — frontend dev server
Set-Location frontend
npm install   # first time only
npm run dev
# Vite prints "Local: http://localhost:5173/"
```

Open <http://localhost:5173> in a browser.

## 14.2 — Address autocomplete → 規劃路線

1. Click in the **起點** input.
2. Type `台北車站`. After ~300 ms a dropdown SHALL appear with one or
   more suggestions.
3. Click a suggestion. The input now shows the full address and the
   coords line under it shows `25.0xxxx, 121.5xxxx`.
4. Click in the **終點** input, type `中正紀念堂`, select a suggestion.
5. Click **規劃路線**. Button SHALL change to `規劃中…` then back.
6. The map SHALL show:
   - a blue polyline along the route
   - blue start marker, purple end marker
   - red markers for any speed cameras
   - green markers for any parking lots near the destination
7. **RouteSummary** panel SHALL show distance (km, 1 decimal), ETA
   (min, integer), camera count, parking count, and a "途經" list.

**Pass criterion:** all 7 hold and no toast appears.

## 14.3 — Map click → 規劃路線

1. Reload the page (clears markers).
2. Click in the **起點** input to focus it (input shows a blue ring).
3. Click anywhere on the map. A blue marker appears, coords appear
   under the 起點 input.
4. Click in the **終點** input. Click another map location. Purple
   marker appears.
5. Click **規劃路線**. Same outcome as 14.2 (polyline + markers + summary).

## 14.4 — Chat-driven routing

1. In the chat panel, type `我要從台北車站到忠孝復興` and press Enter.
2. The message appears as a blue bubble on the right; an italic
   "agent 思考中…" line appears below.
3. After at most 30 s the agent reply appears as a grey bubble on the
   left.
4. If the reply carried a `routeResult`, the map SHALL update with the
   same visual elements as 14.2 — even though the 起點/終點 inputs were
   never touched.

**Pass criterion:** map updates from chat output.

## 14.5 — Dark mode persistence

1. Click the **🌙 Dark** / **☀ Light** button in the header.
2. UI SHALL flip light ↔ dark immediately. `<html>` SHALL gain/lose
   the `dark` class.
3. Reload the page. Theme SHALL be remembered.

## 14.6 — 504 timeout on multiagent stop

1. In Terminal 3, Ctrl-C the multiagent-service process. Wait 5 s.
2. In the frontend, send a route request (any pair of points).
3. After ~30 s, a red toast SHALL appear at the bottom-centre with a
   server error message (typically "伺服器發生錯誤" or the body's
   `error` string).
4. Restart the multiagent-service (`uv run python main.py`) to recover.

## 14.7 — Toast on no-results geocode

1. In the **起點** input, type a clearly-nonsense string such as
   `zzzzz nowhere place`.
2. Dropdown SHALL stay empty (no toast — geocode failures are
   suppressed for the autocomplete path).
3. To trigger the toast: pick a real 起點, then a real 終點, but with
   coordinates outside the Taipei graph (e.g. click the map far out
   into the sea). Click **規劃路線**. Toast SHALL appear with the
   backend's "no path found" / "could not snap" error.

## Sign-off

Flip checkboxes 14.1–14.7 (and 15.1–15.4) in
`openspec/changes/complete-demo-stack/tasks.md`, then run:

```powershell
openspec validate complete-demo-stack --strict
```

Archive with `/opsx:archive complete-demo-stack`.

