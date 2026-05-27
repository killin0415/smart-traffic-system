## 1. Infra：DB image 切換 + extension 啟用

- [x] 1.1 修改 `infra/docker-compose.yml`：`timescaledb` service image 從 `timescale/timescaledb:latest-pg14` 改成 `timescale/timescaledb-ha:pg14-all`（**留 PG14**，不跨大版號）；保留現有 `POSTGRES_DB=traffic_data`、`POSTGRES_USER=admin`、`POSTGRES_PASSWORD=secret` 環境變數
- [x] 1.2 在 `infra/docker-compose.yml` 的 `timescaledb` service 加 volume mount `./../scripts:/scripts:ro`（讓後續 `docker compose exec ... psql -f /scripts/...` 可用）
- [x] 1.3 新增 `infra/init-db/00-extensions.sql`：依序 `CREATE EXTENSION IF NOT EXISTS timescaledb; CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS postgis_topology;`
- [x] 1.4 在 repo root 跑 `docker compose -f infra/docker-compose.yml down -v && docker compose -f infra/docker-compose.yml up -d timescaledb`，驗 image 正確啟動 + 三個 extension 都載入（`docker compose -f infra/docker-compose.yml exec timescaledb psql -U admin -d traffic_data -c "\dx"`）
- [x] 1.5 驗證 PostGIS 版本 ≥ 3.0（`SELECT PostGIS_Version();`），確認 `ST_DumpPoints`、`ST_SnapToGrid`、`ST_DWithin` 可用
- [x] 1.6 派 test engineer sub-agent：寫 `tests/test_infra_extensions.py` 用 testcontainers 起 timescaledb-ha:pg14-all image（`pytest -m integration` mark），assert PostGIS + TimescaleDB extension 都能 CREATE 並可呼叫基本 function；註明 dev 機器要先 docker pull image（~700MB）

## 2. DB Schema 重建（multiagent-service ORM 與 init-db SQL）

- [x] 2.1 改寫 `infra/init-db/02-road-network-tables.sql`：新 `traffic_node` (id, latitude, longitude, geom geometry(Point,4326), `has_signal BOOLEAN NOT NULL DEFAULT FALSE`) + GIST index `ix_traffic_node_geom` + partial index `ix_traffic_node_signal ON traffic_node(has_signal) WHERE has_signal`；新 `traffic_edge` (id, source_node_id, target_node_id, road_name, length_km, road_class, max_speed_kmh, oneway, geom) + GIST index `ix_traffic_edge_geom` + B-tree `ix_traffic_edge_road_class`；**不再含** `tdx_section_id` / `speed_limit_kmh` / `base_weight`
- [x] 2.2 新增 `infra/init-db/03-vd-tables.sql`：`vd_static`（PK vdid、含 geom Point + snapped_road_class TEXT NULL）、`vd_reading` hypertable + retention policy 30 天
- [x] 2.3 新增 `infra/init-db/04-parking-tables.sql`：`parking_lot` (id PK, name, address, total_car, total_motor, lat, lng, geom Point) + GIST index、`parking_availability` hypertable
- [x] 2.4 新增 `infra/init-db/05-speed-limit-exception.sql`（可選，data.taipei #4 速限例外表，本 change tasks 內可留 stub）
- [x] 2.5 新增 `infra/init-db/06-default-maxspeed-fn.sql`：定義 PL/pgSQL function `default_maxspeed(highway TEXT) RETURNS INTEGER` (motorway=80, trunk=70, primary=50, secondary=50, tertiary=40, residential=30, service=20, unclassified=40, link=…)；**這是 DEFAULT_MAXSPEED 的單一 source of truth**，Python 端從 query 拿，不再在 weight_provider 內硬編 dict
- [x] 2.6 改寫 `backend/multiagent-service/src/db/models.py`：移除 `TrafficEdge.tdx_section_id`、`speed_limit_kmh`、`base_weight`；新增 `road_class`、`max_speed_kmh`、`oneway`、`geom` (使用 `geoalchemy2.Geometry`)；新增 `TrafficNode.geom`；新增 `VDStatic`、`VDReading`、`ParkingLot`、`ParkingAvailability` ORM 類別
- [x] 2.7 `cd backend/multiagent-service && uv add geoalchemy2 scipy` 加入依賴；確認 `pyproject.toml` 內已有
- [x] 2.8 派 test engineer sub-agent：寫 `tests/test_db_models.py` 驗每個 ORM class 跟 init SQL schema 一致（欄位名、type、nullable、index 存在）；驗 `default_maxspeed` PL/pgSQL function 對 7+ 種 highway 值都回非零

## 3. OSM 路網 ingestion script（osm-road-network capability）

- [x] 3.1 新增 `scripts/import_taipei_osm.sh`：curl 下載 `https://download.geofabrik.de/asia/taiwan-latest.osm.pbf` → 24 小時 cache 跳過下載 → `osmconvert` 在 Docker container (例如 `iboates/osm2pgsql` 或 `phusion/baseimage` + apt) 中跑 → 裁 bbox `(121.45,24.96,121.67,25.21)` → `osm2pgsql --create --slim --hstore --style /scripts/osm2pgsql.style -d traffic_data -U admin -H timescaledb -P 5432`（在 docker network 內跑）
- [x] 3.2 新增 `scripts/osm2pgsql.style`：只保留 `highway`, `maxspeed`, `oneway`, `name`, `ref`, `lanes` tags
- [x] 3.3 新增 `scripts/build_graph_from_osm.sql`：TRUNCATE traffic_edge, traffic_node CASCADE → 用 `ST_DumpPoints` + `ST_SnapToGrid(0.00005)` 抽 unique nodes → INSERT traffic_node → 對相鄰 vertex 用 `ST_Length(geography)/1000` 算 length → INSERT traffic_edge（含 road_class、max_speed_kmh = COALESCE(parsed_maxspeed, default_maxspeed(highway))、oneway、geom）；highway filter **明確排除** `pedestrian`, `footway`, `cycleway`, `track`, `steps`, `path`, `bridleway`
- [x] 3.4 新增 `scripts/post_build_snap_vd.sql`：對 `vd_static` 每筆用 `ST_DWithin(traffic_edge.geom, vd.geom, 100)` + `ORDER BY ST_Distance LIMIT 1` UPDATE 寫入 `vd_static.snapped_road_class`；100m 內無 edge 則保持 NULL
- [x] 3.4b 在 `scripts/build_graph_from_osm.sql` 末尾加 signal snap：`UPDATE traffic_node tn SET has_signal = TRUE WHERE EXISTS (SELECT 1 FROM planet_osm_point p WHERE p.highway = 'traffic_signals' AND ST_DWithin(p.way::geography, tn.geom::geography, 30))`；同 SQL 檔案以保持 graph build 原子性
- [x] 3.5 端對端跑：依 design Migration Plan 第 3-6 步順序執行，量測 `traffic_node`/`traffic_edge` 筆數（acceptance: node > 30k、edge > 80k）和耗時
- [x] 3.6 派 test engineer sub-agent：寫 `tests/test_build_graph_sql.py` (`pytest -m integration`) 用 testcontainers 起 timescaledb-ha:pg14-all + 灌入小型 OSM PBF fixture（1km² 信義一塊，預先準備在 `tests/fixtures/`）→ 跑 build SQL → assert traffic_node/edge 數量在預期範圍、road_class 分布合理（無 pedestrian 等被排除的類）、oneway 處理正確、ST_SnapToGrid 去重正確、**`has_signal` snap 正確**（fixture 內含至少 1 個 `traffic_signals` point，assert 對應 traffic_node `has_signal=TRUE`）

## 4. VD 即時資料 ingestion（vd-live-traffic capability）

- [x] 4.1 新增 `backend/multiagent-service/src/agents/vd_traffic.py`：`fetch_vd_dynamic()` httpx GET `https://tcgbusfs.blob.core.windows.net/blobtisv/GetVDDATA.xml` (plain XML, 非 gzip) + ET.fromstring 解析 → `list[VDReading]`
- [x] 4.2 `refresh_vd_cycle(graph, weight_provider, session_factory)`：fetch → INSERT vd_reading with `ON CONFLICT DO NOTHING` (PK ts+vdid+lane_no) → `await weight_provider.rebuild()` → `weight_provider.apply_to_graph(graph)`
- [x] 4.3 `run_periodic_vd_refresh(graph, weight_provider, session_factory)`：while True + try/except + `asyncio.sleep(VD_REFRESH_SECONDS)` (預設 300)
- [x] 4.4 砍掉所有對舊 `backend/multiagent-service/src/agents/traffic.py` 的 import / wire-up（lifespan、kafka_runtime、tests）；實體檔案於 §11.7 刪除
- [x] 4.5 派 test engineer sub-agent：寫 `tests/test_vd_traffic.py` 用 sample VD XML fixture mock httpx response → assert 解析正確、lane aggregation、ON CONFLICT 行為、network error 不 crash loop（用 raise + 確認下一個 cycle 仍會跑）

## 5. VD 靜態資料 seed（CLI script，非 lifespan）

- [x] 5.1 新增 `scripts/seed_vd_static.py`（**獨立 CLI**，**不在 lifespan**）：用 httpx GET `https://tcgbusfs.blob.core.windows.net/blobtisv/VD.xml` → 解析 `<VD>` 元素 → INSERT vd_static (vdid, link_id, road_name, road_class, bidirectional, bearing, lat, lng, geom = ST_MakePoint(lng,lat))；用 `INSERT ... ON CONFLICT (vdid) DO UPDATE SET ...` upsert
- [x] 5.2 新增 `__main__` block：用 `argparse` 接 `--db-url` 預設讀環境變數 `DATABASE_URL`；輸出 N rows seeded log
- [x] 5.3 派 test engineer sub-agent：寫 `tests/test_seed_vd_static.py` mock VD.xml response → assert upsert 正確、空 endpoint 回應時 graceful exit、parser 對缺欄位的 `<VD>` 不 crash

## 6. WeightProvider 三層邏輯（weight-provider capability）

- [x] 6.1 新增 `backend/multiagent-service/src/agents/weight_provider.py`：`WeightProvider` Protocol + `TaipeiWeightProvider` class skeleton；保留 `get_speed(edge) -> tuple[float, str]` 回 source 標籤
- [x] 6.2 實作 `TaipeiWeightProvider.rebuild(session_factory)`：query 最近 10 min vd_reading（DISTINCT ON vdid AVG by ts+vdid + WHERE avg_speed > 0）→ 載入 vd_static + snapped_road_class → 建 cKDTree → 算 class_avg、calibration
- [x] 6.3 在 `rebuild()` 啟動時一次性從 DB load `default_maxspeed_by_class`：`SELECT highway, default_maxspeed(highway) FROM (VALUES ('motorway'),('trunk'),('primary'),('secondary'),('tertiary'),('unclassified'),('residential'),('service'),('motorway_link'),('trunk_link'),('primary_link'),('secondary_link'),('tertiary_link'),('living_street')) v(highway)`，cache 在 module-level dict（**不要硬編**）
- [x] 6.4 實作 `get_speed(edge) -> (speed, source)`：Tier 1 (`kdtree.query` k=3 distance_upper_bound=0.01 度 ≈ 1km，inverse-distance) → Tier 2 (class_avg) → Tier 3 (`max_speed_kmh × calibration[road_class]`，缺值 default 0.5)
- [x] 6.5 實作 `apply_to_graph(graph)`：對 graph.edges 全掃，計算 dynamic_weight = length_km / max(speed, 5.0)，呼叫 `graph.update_weight(edge_id, w)`（注意：新 update_weight 吃絕對 weight）
- [x] 6.6 新增 `PersonalizedWeightProvider(base, user_id)` pass-through wrapper class，Phase 1 永遠 delegate `base.get_speed`
- [x] 6.7 **修改** `backend/multiagent-service/src/agents/routing.py:135` `RoadGraph.update_weight` signature：從 `update_weight(self, edge_id: int, congestion_factor: float)` 改為 `update_weight(self, edge_id: int, new_weight: float)`，內部直接寫 new_weight 到 adjacency；**不再依賴 `edge.base_weight`**（該欄位移除）
- [x] 6.8 派 test engineer sub-agent：寫 `tests/test_weight_provider.py` 重點測：(a) Tier 1 觸發條件（fixture VDs 在 1km 內）、(b) Tier 2 觸發（VDs 全在 1km 外但 class_avg 有值）、(c) Tier 3 觸發、(d) calibration 公式正確（推算 expected ratio）、(e) k-d tree 結果穩定（同 input 跑兩次結果一致）、(f) apply_to_graph 對 100 edges fixture 全更新、(g) `update_weight` 新 signature 正確處理絕對 weight

## 7. 停車場（parking-availability capability）

- [x] 7.1 新增 `backend/multiagent-service/src/agents/parking.py`：`fetch_parking_availability()` HTTP GET data.taipei dataset (id `d5c0656b-5250-4179-a491-c94daa56ef2c`) → list[ParkingReading]
- [x] 7.2 `run_periodic_parking_refresh(session_factory)`：while True + try/except + INSERT parking_availability with ON CONFLICT DO NOTHING + `asyncio.sleep(PARKING_REFRESH_SECONDS)` 預設 300
- [x] 7.3 在 `backend/multiagent-service/src/db/seed_taipei.py` 中新增 `seed_parking_lots(session)`：拉靜態 metadata + INSERT parking_lot (含 geom)；空表 seed、非空跳過
- [x] 7.4 在 `routing.py` 新增 `query_parking_near_destination(session, lat, lng, radius_km=1.0, top=5)`：PostGIS `ST_DWithin` + LATERAL join 取最新 availability + WHERE available_car >= 10 + ORDER BY dist + LIMIT 5；無結果回 `[]`
- [x] 7.5 派 test engineer sub-agent：寫 `tests/test_parking.py` 測 fetch parser、query 函式（fixture parking_lot + parking_availability，驗 1km 內、available_car>=10、最近 5 個正確排序、無結果 graceful 回空 list）、refresh task error handling

## 8. 測速相機資料源切換（speed-camera capability）

- [x] 8.1 下載 data.taipei「臺北市固定測速照相地點表」CSV 存到 `data/taipei_speed_cameras.csv`（commit 進 repo）
- [x] 8.2 改寫 `backend/multiagent-service/src/db/speed_camera.py`：`_LAT_KEYS = ('緯度',)`, `_LNG_KEYS = ('經度',)`, `_SPEED_LIMIT_KEYS = ('速限',)`, `_DIRECTION_KEYS = ('拍攝方向',)`, `_ADDRESS_KEYS = ('設置地點',)`；移除「篩選 高雄市」邏輯
- [x] 8.3 改寫 `snap_camera_to_edge`：用 SQL `SELECT id FROM traffic_edge ORDER BY ST_Distance(geom::geography, ST_MakePoint(:lng,:lat)::geography) LIMIT 1` 取代現有 Python 端點距離計算
- [x] 8.4 派 test engineer sub-agent：寫/更新 `tests/test_speed_camera.py` 用 sample CSV (新格式) 測 parser 欄位 mapping、PostGIS snap 與 fixture edges 結果驗證

## 9. A* bbox bounding（astar-routing capability）

- [x] 9.1 在 `backend/multiagent-service/src/agents/routing.py` 新增 `SearchBox` dataclass + `contains(lat, lng)` method
- [x] 9.2 新增 `compute_search_bbox(o, d, padding_ratio=0.3, min_padding_km=2.0)`：依 design 公式，回傳 SearchBox
- [x] 9.3 改 `astar(graph, start, end, weight_overrides=None, search_box=None)`：在 successor 處加 `if search_box and not search_box.contains(n.lat, n.lng): continue`
- [x] 9.3b 在同一個 successor 區塊加 signal penalty：讀環境變數 `SIGNAL_PENALTY_SECONDS`（預設 20）算 `SIGNAL_PENALTY_HR = penalty_sec / 3600`（module-level 常數）；**順序：先做 bbox check（9.3）跳過 bbox 外 node、再計算** `tentative_g = g_score[current] + edge_weight + (SIGNAL_PENALTY_HR if (graph.nodes[neighbor_id].has_signal and neighbor_id != end_id) else 0)`；起點靠 `g_score[start]=0` 自然處理
- [x] 9.3c 在 `GraphNode` dataclass 加 `has_signal: bool = False` 欄位（**default `False` 保證既有 instantiation 點向後相容**，例如 `routing.py:100` 的 keyword 呼叫）；`RoadGraph.from_db` 內 `select(TrafficNode)` 整列拉、ORM 自動帶欄位，**不需要改 SELECT 子句**
- [x] 9.4 改 `find_top_k_routes` 接 `search_box` 參數並轉傳給每輪 `astar()` 呼叫
- [x] 9.5 改 `plan_optimal_route`：簽章變 `(session, graph, weight_provider, o_lat, o_lng, d_lat, d_lng, user_id=None, k=3)`；先算 bbox → snap → find_top_k_routes(search_box=bbox)；空結果時 retry padding=0.6 一次；最後對每條 route enrich speed_cameras + 對最佳 route 加 parking_suggestions（無則 `[]`）
- [x] 9.6 改 `RoadGraph.from_db()`：不再讀 `traffic_edge.base_weight` 欄位（已移除）；初始 dynamic_weight 暫設為 None / 0（lifespan 中 weight_provider.apply_to_graph 後才有實際值）
- [x] 9.7 派 test engineer sub-agent：寫/更新 `tests/test_routing.py` 測 (a) `SearchBox.contains` 邊界值、(b) `compute_search_bbox` 公式（含極短距離 → 2km min padding、長距離 → 30% padding）、(c) A* with bbox：手建 graph 含 bbox 內外 node，assert 外部 node 不被 expand、(d) retry-with-wider-bbox 觸發、(e) plan_optimal_route response shape 含所有預期欄位（含 parking_suggestions default `[]`）、(f) **signal penalty**：手建小 graph 含一條路徑，分別測 `has_signal=False` 全程 vs `has_signal=True` 中間節點，assert 後者 cost 多 `SIGNAL_PENALTY_HR`、起點/終點即使 `has_signal=True` 也不加 penalty

## 10. Lifespan 整合 + Kafka runtime + main-service 協調

- [x] 10.1 改 `backend/multiagent-service/main.py` lifespan：依序執行 `seed_speed_cameras` → (vd_static 假設已 offline seed，僅檢查；空時 log warning 並提示) → `seed_parking_lots` → `RoadGraph.from_db` → `weight_provider = TaipeiWeightProvider()` → `await weight_provider.rebuild(session_factory)` → `weight_provider.apply_to_graph(graph)`
- [x] 10.2 lifespan 啟動兩個 background task：`asyncio.create_task(run_periodic_vd_refresh(graph, weight_provider, session_factory))` + `asyncio.create_task(run_periodic_parking_refresh(session_factory))`，shutdown 時 cancel + try/except CancelledError
- [x] 10.3 改 `backend/multiagent-service/src/kafka/runtime.py`：沿用既有 module-level globals 模式新增 `_weight_provider: WeightProvider | None = None` global、`set_weight_provider(wp)`、`get_weight_provider() -> WeightProvider | None`；**不引入** `RuntimeContext` class
- [x] 10.4 改 `backend/multiagent-service/src/kafka/runtime.py:set_runtime` 簽章：考慮加 `weight_provider` 參數（為了一處設定全 globals），或保留現有 4 個參數另外呼叫 `set_weight_provider`（兩種寫法擇一，傾向後者保持現有 set_runtime 簽章不破壞）
- [x] 10.5 改 Kafka `route.request` handler (位於 `backend/multiagent-service/src/kafka/consumer.py` 或對應 handler 模組)：從 payload 讀 `user_id`（optional, 預設 None），呼叫 `plan_optimal_route(session, graph, get_weight_provider(), ..., user_id=payload.get('user_id'))`
- [x] 10.6 改 Kafka `route.response` payload 序列化：每條 route 多 `parking_suggestions` 欄位（list[dict]，無則 `[]`）
- [x] 10.7 環境變數：`TDX_LIVE_REFRESH_SECONDS` → `VD_REFRESH_SECONDS`；新增 `PARKING_REFRESH_SECONDS`、`SIGNAL_PENALTY_SECONDS`（預設 20）；移除 `TDX_CLIENT_ID` / `TDX_CLIENT_SECRET` 的讀取
- [x] 10.8 **main-service Kotlin 端 DTO 同步**：在 Spring Boot main-service 的 `RouteResponse` data class 新增 `parkingSuggestions: List<ParkingSuggestion> = emptyList()` 欄位；`ParkingSuggestion(id: Int, name: String, address: String, availableCar: Int, distanceM: Double)` 新 data class；確認 Jackson/Kotlin serialization 對未知欄位 / null 處理是 lenient (`@JsonIgnoreProperties(ignoreUnknown = true)`)
- [x] 10.9 派 test engineer sub-agent：寫 `tests/test_lifespan_integration.py` (`pytest -m integration`) 用 testcontainers 起 timescaledb-ha:pg14-all + 灌小 OSM fixture + 起 service → assert lifespan log 內容（graph loaded N nodes/edges、weight provider rebuilt、tasks started）

## 11. End-to-end integration test + acceptance + cleanup

- [x] 11.1 派 test engineer sub-agent：寫 `tests/test_e2e_route.py` (`pytest -m integration`) 用 testcontainers full stack（timescaledb-ha + Kafka）→ 灌小 OSM fixture + mock VD/parking response → 對 Kafka 丟 route.request (台北車站 → 101) → assert response 含 routes (≥1)、estimated_minutes、speed_cameras、parking_suggestions 欄位
- [x] 11.2 manual acceptance：實機跑 5 min cycle → `SELECT COUNT(*) FROM vd_reading WHERE ts > NOW() - INTERVAL '5 min'` 確認 > 500
- [ ] 11.3 manual acceptance：route 台北車站 → 101，比對 estimated_minutes 跟 Google Maps，記錄差距（acceptance: ±50%）；若差距 > 50% 重新評估 calibration 公式
- [x] 11.4 manual acceptance：`grep -r 'tdx_section_id\|TDX Live\|Kaohsiung\|section_to_edge\|MAX_CONGESTION_FACTOR' backend/ scripts/ infra/` 應該沒有結果（除 archive 文件以外）
- [x] 11.5 跑 `cd backend/multiagent-service && uv run pytest` 全綠（含 integration tests，需先 docker pull image）
- [x] 11.6 文件：在 `references/` 新增 `taipei-opendata-rebuild-implementation.md`，記錄踩過的坑、decisions、operate 流程
- [x] 11.7 **Cleanup（在本 change 內完成）**：刪除 `backend/multiagent-service/src/agents/traffic.py`
- [x] 11.8 **Cleanup**：刪除 `scripts/import_tdx_road_network.py`
- [x] 11.9 **Cleanup**：刪除 `data/taipei_road_sections.json`
- [x] 11.10 **Cleanup**：刪除 `data/speed_cameras.csv`（OD 6489 全國表）
- [x] 11.11 **Cleanup**：清理 `.env.example` 中 `TDX_CLIENT_ID` / `TDX_CLIENT_SECRET` / `TDX_LIVE_REFRESH_SECONDS` 等變數，新增 `VD_REFRESH_SECONDS` / `PARKING_REFRESH_SECONDS`
- [x] 11.12 **Cleanup**：移除 `pyproject.toml` 中任何 TDX 相關依賴（如有）
