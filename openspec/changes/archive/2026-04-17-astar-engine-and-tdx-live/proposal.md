
## Why

multiagent-service 目前的 Kafka handler 全是 stub 回覆，沒有真正的路徑規劃或即時路況能力。Phase 2 的首要目標是讓系統能實際回應路徑規劃請求：從 DB 載入路網圖、執行 A* 演算法計算 top-K 路徑、整合 TDX Live API 即時車流資料動態調整 edge weight，並附帶沿途測速照相機資訊。

## What Changes

- **新增 A\* 路徑規劃引擎**：啟動時從 DB 載入路網建構 in-memory adjacency dict，實作 A* with haversine heuristic + penalty-based top-K 路徑
- **新增 snap to graph 機制**：將使用者 GPS 座標對應到路網 node（優先高 degree 交叉路口）
- **新增 TDX Live 資料整合**：定時從 TDX Live Section API 拉取即時車速，以 `tdx_section_id` mapping 到 edge，更新 Redis 快取 + TimescaleDB 時序儲存 + in-memory graph weight
- **新增 congestion factor 動態權重公式**：`min(speed_limit / current_speed, 10.0)`，無資料時 fallback 為 1.0
- **擴充 `TrafficEdge` model**：新增 `tdx_section_id` (String) 欄位，供 TDX Live 資料 mapping
- **擴充 `ParsedEdge` + `road_network.py`**：解析時保留 `RoadSectionID`，seed 時寫入 `tdx_section_id`
- **新增 `SpeedCamera` model + seed**：從政府開放資料 CSV 篩選高雄市測速照相機，snap 到最近 edge，路徑結果附帶沿途相機資訊
- **新增 geocoding 支援**：透過 Nominatim (OSM) API 解析地名為經緯度，供聊天場景使用

## Capabilities

### New Capabilities
- `astar-routing`: A* 路徑規劃引擎，包含 in-memory graph、haversine heuristic、penalty-based top-K、snap to graph、測速照相機整合
- `tdx-live-traffic`: TDX Live API 即時車流資料整合，包含定時拉取、Redis 快取、TimescaleDB 儲存、dynamic weight 更新
- `speed-camera`: 測速照相機靜態資料 seed 與路徑查詢整合
- `geocoding`: 地名轉經緯度服務（Nominatim API）

### Modified Capabilities
- `database-integration`: `traffic_edge` 表新增 `tdx_section_id` (VARCHAR) 欄位；新增 `speed_camera` 表
- `road-network-import`: `ParsedEdge` 新增 `tdx_section_id` 欄位，seed 時寫入

## Impact

- **multiagent-service**：新增 `src/agents/routing.py`（A* 引擎）、`src/agents/traffic.py`（TDX Live poller）、`src/agents/geocoding.py`、`src/db/models.py`（擴充 + 新增 SpeedCamera）、`src/db/seed.py`（擴充）、`src/db/road_network.py`（擴充）
- **資料層**：TimescaleDB schema 變更（traffic_edge 加欄位、新增 speed_camera 表）、Redis 新增 `traffic:edge:{tdx_section_id}` key pattern
- **外部依賴**：TDX Live API（需 OAuth2 token）、Nominatim API（免費、有 rate limit）、政府開放資料 CSV（測速照相機）
- **Kafka**：`route.request` handler 從 stub 替換為真正的 A* 路徑規劃
