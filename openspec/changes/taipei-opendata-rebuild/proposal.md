## Why

現行台北路網用 TDX `Section + SectionShape` API，限縮在信義/松山一塊 4×4 km bounding box 只抓到 81 條路段（巷弄全缺，主幹道也不完整），且 `SpeedLimit` 是用 `RoadClass` 推估而非真實值。A* 用 `base_weight = length / speed_limit` 做 free-flow 估算 → 已知 ETA 比 Google Maps **樂觀 3-4 倍**（見 `eta_accuracy_followup` 開放問題）。換句話說：**路網覆蓋差 + 速度估算錯**兩個根本性問題同時存在。

本次切換資料源並重建路網——OSM 提供完整含巷弄的台北市全市路網結構、data.taipei VD 提供 5 分鐘更新的真實平均車速（取代用速限的猜估），讓 A* 估算貼近實際。

## What Changes

### 路網結構
- **BREAKING**：`scripts/import_tdx_road_network.py` 退役並於本 change tasks §11 中刪除。新增 `scripts/import_taipei_osm.sh`：下載 `taiwan-latest.osm.pbf`、用 `osmconvert` 裁切到台北市 bbox `(121.45,24.96,121.67,25.21)`、用 `osm2pgsql` 灌進 PostGIS `planet_osm_*` raw tables。
- **BREAKING**：新增 `scripts/build_graph_from_osm.sql`：從 `planet_osm_line WHERE highway IS NOT NULL` transform 成現有的 `traffic_node` / `traffic_edge` schema（保留 A* 介面）。
- **BREAKING**：`data/taipei_road_sections.json` 不再使用，於本 change tasks §11 中刪除。
- 預期路網規模：`traffic_node` > 30k、`traffic_edge` > 80k（含巷弄）。

### 即時車速 (取代 TDX Live)
- **BREAKING**：拿掉 `src/agents/traffic.py`（含整段 TDX `Live/City/Kaohsiung` 不支援降級邏輯），改寫為 `src/agents/vd_traffic.py`，從 `https://tcgbusfs.blob.core.windows.net/blobtisv/GetVDDATA.xml` 抓 data.taipei VD 動態 XML。實測 endpoint 回 plain XML（非 gzip），5 min 更新、含 `AvgSpeed/Volume/Occupancy`、免 API key、含約 700+ 個 device（VDInfoSet 內 VDDevice 數隨資料而動，新 spec 不寫死）。
- 新增 `src/agents/weight_provider.py`：三層 weight 邏輯（VD 鄰近反距離加權 → 同 highway class 全市平均 → maxspeed × calibration），抽成 `WeightProvider` Protocol，A* 透過介面取得 dynamic weight。
- 新增 `scripts/seed_vd_static.py`：**獨立 CLI script**，從 `https://tcgbusfs.blob.core.windows.net/blobtisv/VD.xml` 一次性匯入 VD metadata（vdid + 座標 + LinkID + RoadName）。在 OSM graph build 完之後、`scripts/post_build_snap_vd.sql` 之前執行。**不在 lifespan 內 seed**（避免啟動依賴外部 endpoint）。

### 測速相機
- **BREAKING**：`data/speed_cameras.csv`（OD 6489）退役並於本 change tasks §11 中刪除，換成 `data/taipei_speed_cameras.csv`（data.taipei「臺北市固定測速照相地點表」），欄位固定（緯度 / 經度 / 速限 / 拍攝方向 / 設置地點）。
- `snap_camera_to_edge` 改用 PostGIS `ST_Distance + ORDER BY LIMIT 1`，比現行 Python 端點距離更準。

### 停車場（新功能）
- 新增 `src/agents/parking.py`：data.taipei 停車場資訊 5 min 拉一次，寫 `parking_availability` hypertable。
- A* 路徑回應新增 `parking_suggestions` 欄位：終點 1km 內、剩餘車位 ≥ 10 的前 5 個停車場。**Kafka `route.response` schema 新增此欄位**，主 service (Spring Boot Kotlin) 端 deserializer 需同步加欄位（向後相容：欄位 default `[]`）。

### A* 引擎
- 新增 search bbox runtime frontier pruning：per-request 算 `bbox(origin, destination, padding=max(direct_km*0.3, 2km))`，A* successor 跳過 bbox 外 node；找不到時自動 retry padding 0.6。
- **BREAKING (內部 API)**：`plan_optimal_route()` signature 多 `weight_provider` 參數；`graph` 不再需要查 `traffic_edge.base_weight`，dynamic weight 全由 weight_provider 提供。
- **BREAKING (內部 API)**：`RoadGraph.update_weight(edge_id, congestion_factor)` 改 signature 為 `update_weight(edge_id, new_weight)`，吃絕對 weight 而非 factor。內部不再 `base_weight × factor` 計算（`base_weight` 欄位也已移除）。caller 必須自行算好 weight 再傳入。
- 新增 `user_id: Optional[str] = None` 參數（個人化 Phase 2 預留，Phase 1 永遠不傳）。
- Kafka `route.request` payload 新增 optional `user_id`（向後相容，不傳預設 None）。
- **新增 signal penalty**：A* g_score 對通過有號誌 node 加 `SIGNAL_PENALTY_SECONDS / 3600` 小時。預設 20s（台北號誌典型 cycle 60-90s × 綠燈比 ~50% ≈ 期望停等 20s），可由環境變數 `SIGNAL_PENALTY_SECONDS` 覆寫。終點 node 即使有號誌也不加（你停車不需要等綠燈）；起點透過 `g_score[start]=0` 自然處理。Heuristic（直線距離 / max_speed）仍 admissible 不破。

### DB schema
- **BREAKING**：`traffic_edge` 砍 `tdx_section_id`、`speed_limit_kmh`、`base_weight`，加 `road_class VARCHAR(32)`、`max_speed_kmh INTEGER`、`oneway BOOLEAN`、`geom geometry(LineString, 4326)`。
- **BREAKING**：`traffic_node` 加 `geom geometry(Point, 4326)`、`has_signal BOOLEAN NOT NULL DEFAULT FALSE`（號誌節點標記，build graph 後從 `planet_osm_point WHERE highway='traffic_signals'` 用 `ST_DWithin(30m)` snap 得來）。
- 新表：`vd_static`、`vd_reading` (hypertable)、`parking_lot`、`parking_availability` (hypertable)、`speed_limit_exception`（可選）。
- 既有 `traffic_history` hypertable 砍掉（被 `vd_reading` 取代）。

### Infra
- **BREAKING**：Docker image `timescale/timescaledb:latest-pg14` → `timescale/timescaledb-ha:pg14-all`。**留 PG14 不跨大版號**：新 image 同樣是 PG14 但同時帶 PostGIS + TimescaleDB + Toolkit，避免 PG14→PG16 跨版資料 + extension 雙重重建。
- `infra/init-db/00-extensions.sql` 加 `CREATE EXTENSION postgis; CREATE EXTENSION postgis_topology;`。
- **BREAKING**：環境變數 `TDX_LIVE_REFRESH_SECONDS` → `VD_REFRESH_SECONDS`（預設仍 300）；新增 `PARKING_REFRESH_SECONDS`（預設 300）、`SIGNAL_PENALTY_SECONDS`（預設 20）。
- DB 連線參數沿用現有 `infra/docker-compose.yml`：`POSTGRES_DB=traffic_data`、`POSTGRES_USER=admin`、`POSTGRES_PASSWORD=secret`。
- Migration 動作（在 repo root 執行）：
  ```bash
  docker compose -f infra/docker-compose.yml down -v
  docker compose -f infra/docker-compose.yml up -d timescaledb
  bash scripts/import_taipei_osm.sh
  docker compose -f infra/docker-compose.yml exec -T timescaledb \
    psql -U admin -d traffic_data -f /scripts/build_graph_from_osm.sql
  uv run --script scripts/seed_vd_static.py   # script 用 PEP 723 inline dep declaration 自包含
  docker compose -f infra/docker-compose.yml exec -T timescaledb \
    psql -U admin -d traffic_data -f /scripts/post_build_snap_vd.sql
  cd backend/multiagent-service && uv run python main.py
  ```

## Capabilities

### New Capabilities
- `vd-live-traffic`: data.taipei VD 動態 XML 5 分鐘輪詢、寫入 `vd_reading` hypertable、即時讀數查詢；VD 靜態資料以 CLI script 一次性 seed。取代退役的 `tdx-live-traffic` capability。
- `weight-provider`: 三層 edge speed 估算（VD 鄰近反距離加權 / 同 class 全市平均 / maxspeed × 資料推得 calibration）、`WeightProvider` Protocol、`apply_to_graph()` 把 dynamic weight 套到 in-memory graph。
- `osm-road-network`: Taiwan OSM PBF 一次性下載、osm2pgsql 灌 PostGIS、`build_graph_from_osm.sql` transform 成 `traffic_node` / `traffic_edge`、PostGIS spatial index、`vd_static.snapped_road_class` pre-snap、`traffic_node.has_signal` 從 OSM `traffic_signals` point snap。取代退役的 `road-network-import` capability。
- `parking-availability`: data.taipei 停車場 metadata + 即時剩餘車位 5 分鐘輪詢、`parking_lot` / `parking_availability` 表、PostGIS `ST_DWithin` 1km 半徑查詢 + `LATERAL` join 取最新筆。

### Modified Capabilities
- `astar-routing`: 新增 search bbox runtime frontier pruning + `compute_search_bbox` 公式 + retry-with-wider-bbox；A* 從 `WeightProvider` 取得 dynamic weight 而非 graph adjacency 直接讀；`RoadGraph.update_weight` signature 改吃絕對 weight；`plan_optimal_route` signature 多 `weight_provider` 與 `user_id` 參數；路徑回應新增 `parking_suggestions`；A* g_score 對 `has_signal=TRUE` 的 node 加 `SIGNAL_PENALTY_HR` 停等延遲。
- `speed-camera`: 資料源從 OD 6489 全國表（`篩選 CityName == "高雄市"`）換成 data.taipei「臺北市固定測速照相地點表」（欄位固定）；`snap_camera_to_edge` 從 Python O(n) 端點距離改用 PostGIS `ST_Distance + ORDER BY LIMIT 1`。

### Removed Capabilities (in this change)
- `tdx-live-traffic`: 整個被 `vd-live-traffic` 取代（data.taipei VD 不需要 OAuth、不需 `-99` 哨兵、不需 SectionID mapping）。spec.md 在本 change archive 後刪除。
- `road-network-import`: 整個被 `osm-road-network` 取代（不再用 TDX OAuth、Section/SectionShape API、JSON snapshot、Haversine node 去重）。spec.md 在本 change archive 後刪除。

## Impact

### Affected code
- 退役檔案（本 change tasks §11 中刪除）：`src/agents/traffic.py`、`scripts/import_tdx_road_network.py`、`data/taipei_road_sections.json`、`data/speed_cameras.csv`
- 新增檔案：`src/agents/vd_traffic.py`、`src/agents/weight_provider.py`、`src/agents/parking.py`、`src/db/seed_taipei.py`、`scripts/import_taipei_osm.sh`、`scripts/osm2pgsql.style`、`scripts/build_graph_from_osm.sql`、`scripts/seed_vd_static.py`、`scripts/post_build_snap_vd.sql`、`data/taipei_speed_cameras.csv`
- 重大改動：`src/agents/routing.py`（bbox + WeightProvider 整合 + `update_weight` signature）、`src/db/models.py`（schema 變更，使用 `geoalchemy2.Geometry`）、`src/db/seed.py`（seed flow）、`src/db/speed_camera.py`（CSV 換源、snap 改 PostGIS）、`main.py`（lifespan 重排）、`src/kafka/runtime.py`（新增 `_weight_provider` global + `set_weight_provider` / `get_weight_provider`，沿用既有 module-level globals 模式）、`src/kafka/consumer.py`（route.request handler 多傳 `weight_provider` 與 `user_id`、route.response 多 `parking_suggestions` 欄位）、`infra/init-db/*.sql`、`infra/docker-compose.yml`

### APIs / 介面
- 內部：`plan_optimal_route()` signature 變動（多 `weight_provider`、`user_id`），所有 caller 要更新
- 內部：A* `astar()` 多 `search_box` 參數
- 內部 BREAKING：`RoadGraph.update_weight(edge_id, w)` 改吃絕對 weight；舊 `(edge_id, congestion_factor)` 用法移除
- Kafka `route.request` payload 加 optional `user_id`（向後相容）
- Kafka `route.response` payload 加 `parking_suggestions` 欄位（向後相容：default `[]`）— main-service Kotlin 端 DTO 需同步新增欄位

### Dependencies
- 新增 Python：`scipy`（cKDTree）、`geoalchemy2`（PostGIS ORM 支援）；`asyncpg` 已在
- OS-level：`osm2pgsql`、`osmconvert` 兩個 CLI（透過 Docker container 跑，不要求開發者本機裝；具體 image 在 import script 內指定）

### 移除的 env / config
- `TDX_CLIENT_ID` / `TDX_CLIENT_SECRET`（VD endpoint 不需要 OAuth；保留檔案在 .env 也無妨但不再讀取）
- `TDX_LIVE_REFRESH_SECONDS` → 改名 `VD_REFRESH_SECONDS`

### 已知 trade-off / 留給未來的事
- 個人化 weight 不在這次範圍（`PersonalizedWeightProvider` 留 stub 介面）
- Time-of-day weight 剖面不做（vd_reading 留 30 天歷史，未來分析用）
- 即時道路事件 / 道挖：data.taipei 沒有，TDX News API 不在本次範圍
- 多城市：寫死 Taipei
- `planet_osm_*` raw 表本次無 query 使用，僅作為 graph build 的中介層；未來 spatial query 才會用到（design D2）
