## 1. DB Schema 擴充與 Model 更新

- [x] 1.1 `TrafficEdge` model 新增 `tdx_section_id` Column (String, nullable) — `src/db/models.py`
- [x] 1.2 `ParsedEdge` dataclass 新增 `tdx_section_id: str` 欄位 — `src/db/road_network.py`
- [x] 1.3 `parse_road_network()` 解析時從 section dict 提取 `RoadSectionID` 寫入 `ParsedEdge.tdx_section_id`
- [x] 1.4 `seed_road_network()` 建立 TrafficEdge 時帶上 `tdx_section_id` — `src/db/seed.py`
- [x] 1.5 新增 `SpeedCamera` model（id, latitude, longitude, direction, speed_limit, address, nearest_edge_id FK）— `src/db/models.py`
- [x] 1.6 新增 `traffic_history` hypertable model（time, tdx_section_id, travel_speed, travel_time）— `src/db/models.py`
- [x] 1.7 更新 `infra/init-db/` SQL script：`traffic_edge` 加 `tdx_section_id`、建立 `speed_camera` 表、建立 `traffic_history` hypertable

## 2. 測速照相機 Seed

- [x] 2.1 下載政府開放資料測速照相機 CSV 至 `data/speed_cameras.csv`（高雄三民區科技執法固定點位，過濾 超速 + 闖紅燈兼超速，9 筆）
- [x] 2.2 實作 `parse_speed_cameras(csv_path)` 函數：讀取 CSV、篩選高雄市、回傳結構化清單 — `src/db/speed_camera.py`
- [x] 2.3 實作 snap camera to nearest edge 邏輯：計算每個相機座標到所有 TrafficEdge 的距離，找最近的 edge
- [x] 2.4 實作 `seed_speed_cameras(session)` 函數：偵測表是否為空、讀取 CSV、snap to edge、寫入 DB — `src/db/speed_camera.py`
- [x] 2.5 在 `main.py` lifespan 中呼叫 `seed_speed_cameras()`

## 3. A* 路徑規劃引擎

- [x] 3.1 實作 `RoadGraph` class：從 DB 載入路網建構 adjacency dict、提供 `update_weight(edge_id, factor)` 方法 — `src/agents/routing.py`
- [x] 3.2 實作 `snap_to_graph(lat, lng, graph, nodes, k=3)` 函數：找最近 K 個 node、回傳 degree 最高的
- [x] 3.3 實作 `astar(graph, start_id, end_id, nodes)` 函數：使用 haversine/max_speed heuristic，回傳 path + cost
- [x] 3.4 實作 `find_top_k_routes(graph, start, end, k=3, penalty=3.0)` 函數：penalty-based re-run，用原始 graph 重算真實 cost 排序
- [x] 3.5 實作 `plan_optimal_route(origin_lat, origin_lng, dest_lat, dest_lng)` 入口函數：snap → top-K A* → JOIN speed_cameras → 結構化回傳
- [x] 3.6 在 `main.py` lifespan 中啟動時載入 RoadGraph
- [x] 3.7 更新 Kafka `route.request` handler：從 stub 替換為呼叫 `plan_optimal_route()`

## 4. TDX Live 即時資料整合

- [x] 4.1 實作 TDX OAuth2 token 取得（複用現有 `fetch_tdx_road_sections.py` 的 token 邏輯）— `src/agents/traffic.py`
- [x] 4.2 實作 `fetch_live_section_data()` 函數：呼叫 TDX Live Section API、回傳結構化資料
- [x] 4.3 實作 `update_redis_cache(section_data)` 函數：以 `traffic:section:{tdx_section_id}` 為 key 寫入 Redis（TTL 10 min）
- [x] 4.4 實作 `update_timescaledb(session, section_data)` 函數：寫入 `traffic_history` hypertable
- [x] 4.5 實作 `update_graph_weights(graph, section_data)` 函數：計算 congestion_factor、呼叫 `graph.update_weight()`
- [x] 4.6 實作 `refresh_traffic_data()` 整合函數：fetch → Redis + TimescaleDB + graph weight 一次完成
- [x] 4.7 實作定時排程：在 `main.py` lifespan 中以 `asyncio` 定時呼叫 `refresh_traffic_data()`（間隔可設定，預設 5 分鐘）
- [x] 4.8 實作 `get_current_traffic(edge_ids)` 函數：從 Redis 讀取指定 edge 的即時路況

## 5. Geocoding

- [x] 5.1 實作 `geocode_location(query: str)` 函數：呼叫 Nominatim API、自動附加「高雄」、回傳 lat/lng/display_name — `src/agents/geocoding.py`
- [x] 5.2 加入 rate limit 控制（每次請求間隔 ≥ 1 秒）
- [x] 5.3 設定自定義 User-Agent header 以遵守 Nominatim 使用條款

## 6. 測試

- [x] 6.1 A* 單元測試：小型手建圖驗證最短路徑正確性 — `tests/test_routing.py`
- [x] 6.2 Snap to graph 測試：驗證優先回傳高 degree node
- [x] 6.3 Top-K 測試：驗證回傳多條不同路徑、cost 排序正確
- [x] 6.4 Congestion factor 測試：驗證邊界情況（speed=0、無資料、正常值）
- [x] 6.5 Speed camera seed 測試：驗證 CSV 解析、高雄篩選、snap to edge
- [x] 6.6 Geocoding 測試：mock Nominatim API 驗證回傳格式
- [x] 6.7 更新既有 `test_road_network.py`：驗證 ParsedEdge 包含 tdx_section_id
