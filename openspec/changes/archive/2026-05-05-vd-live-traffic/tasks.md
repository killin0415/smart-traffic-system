## 1. DB Schema 與 Model

- [x] 1.1 新增 `VDSensor` model：`id` PK、`vdid` unique、`latitude`、`longitude`、`link_id` nullable、`road_section_id` nullable、`nearest_edge_id` FK nullable — `src/db/models.py`
- [x] 1.2 更新 `infra/init-db/02-road-network-tables.sql`：`CREATE TABLE vd_sensor` + `ix_vd_sensor_vdid` + `ix_vd_sensor_nearest_edge_id`
- [x] 1.3 對既有 DB 執行 `ALTER`/`CREATE TABLE` migration（不清空 `traffic_edge` / `speed_camera`），確認表結構生效

## 2. VD 靜態資料 Seed

- [x] 2.1 實作 `fetch_vd_static(token)` 函數：呼叫 `basic/v2/Road/Traffic/VD/City/Kaohsiung` 分頁抓取所有 VD，回傳 `[{vdid, latitude, longitude, link_id, road_section_id}]` — `src/db/vd_sensor.py`
- [x] 2.2 實作 `snap_vd_to_edge(vd, edges, node_coords)` 函數：複用 `snap_camera_to_edge` 的距離邏輯
- [x] 2.3 實作 `seed_vd_sensors(session)` 函數：偵測表是否為空、取 token、抓 VD、snap、bulk insert；TDX 憑證缺失時 no-op + WARNING — `src/db/vd_sensor.py`
- [x] 2.4 在 `main.py` lifespan 中於 `seed_speed_cameras()` 之後呼叫 `seed_vd_sensors()`
- [x] 2.5 手動驗證：重啟 service 後 `SELECT COUNT(*) FROM vd_sensor` 與 `SELECT COUNT(*) FROM vd_sensor WHERE nearest_edge_id IS NOT NULL` 都 > 0

## 3. VD Live 資料抓取與聚合

- [x] 3.1 實作 `fetch_live_vd_data()` 函數：呼叫 `Live/VD/City/Kaohsiung`、parse `VDLives[].LinkFlows[].Lanes[]`、回傳 `{vdid: [speed, speed, ...]}` — `src/agents/traffic.py`
- [x] 3.2 實作 `_filter_healthy(lanes)` helper：過濾 `Speed <= 0` 與 `ErrorType` 非空的 lane 讀數
- [x] 3.3 實作 `aggregate_edge_speeds(vd_data, edge_vd_map)` 函數：以 `vdid → nearest_edge_id` 對應表，算每個 edge 的 `mean(lane_speeds of all healthy VDs on this edge)`；全部故障的 edge 回傳空值不輸出
- [x] 3.4 實作 `load_vd_edge_map(session)` 函數：從 `vd_sensor` JOIN `traffic_edge` 產生 `{vdid: (edge_id, tdx_section_id, speed_limit_kmh)}`；由 `run_periodic_refresh` 首輪載入並快取

## 4. 整合回既有出口

- [x] 4.1 改寫 `refresh_traffic_data()`：呼叫 `fetch_live_vd_data` → `aggregate_edge_speeds` → 產出 `section_data`（以 edge 為單位但仍帶 `tdx_section_id` 供 Redis / TimescaleDB 用）
- [x] 4.2 沿用 `update_redis_cache` / `update_timescaledb` / `update_graph_weights` 既有函數，不得改介面
- [x] 4.3 更新 `refresh_traffic_data` 的 log：加入「N VDs fetched, M healthy, K edges updated」
- [x] 4.4 刪除 `TDX_LIVE_SECTION_URL` 常數與 `_unsupported_city_logged` 降級分支

## 5. 測試

- [x] 5.1 `test_vd.py`: VD CSV/JSON parse 測試（mock VDLives payload → 驗證 healthy filter 與 lane speed 收集）
- [x] 5.2 `test_vd.py`: `aggregate_edge_speeds` 測試 — 單 VD、多 VD、全故障、部分故障四個 case
- [x] 5.3 `test_vd.py`: `snap_vd_to_edge` smoke test（可直接複用 `test_speed_camera.py` 的 pattern）
- [x] 5.4 `test_traffic.py`: 整合測試 — mock httpx MockTransport 回 VD live payload，驗證 `refresh_traffic_data` 正確呼叫 Redis/DB/graph
- [x] 5.5 既有 63 個測試維持綠色

## 6. 驗證與清理

- [x] 6.1 重啟 service → 觀察 log 覆蓋率（期望至少部分 edge 有 live update）
- [x] 6.2 `docker exec traffic_db psql` 驗證 `traffic_history` 有 N 筆新增
- [x] 6.3 `redis-cli keys "traffic:section:*"` 至少有資料
- [x] 6.4 更新 `src/agents/traffic.py` 的 module docstring：說明現在走 VD 路徑
- [x] 6.5 Commit + push `develop`  <!-- commit e4ad794 done; push pending user approval -->

