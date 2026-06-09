## Why

`taipei-opendata-rebuild` 在 commit `c8136ba` 上線了 OSM-based 路網 build（`scripts/build_graph_from_osm.sql`），但實測 dump 顯示 **graph topology 不健康**：224,162 個 node 之中只有 **16%（36,750 個）degree ≥ 3 是真路口**，58% 是 OSM way 中段點（degree=2），剩下 25% 是斷頭或孤島（degree ≤ 1）。健康 OSM 路網經過 topology 簡化後 intersection 比例應在 60–80%。

兩個根因：

1. **非 oneway way 沒建反向 edge**：現行 SQL 對每條 `planet_osm_line` way 不論 oneway 與否，相鄰 vertex 都只 INSERT 一條 `A→B` edge。`oneway='yes'` 的 way 這樣做是對的，但**非 oneway 的 way 缺了 `B→A`**，導致正常雙向街道在 A\* forward 搜尋下變成單向。
2. **沒有 degree-2 chain contraction**：`ST_DumpPoints(way)` 把每個 OSM way vertex 都變成 traffic_node。一條長路上的中段點（沒有岔路）也成了 node，徒增 graph 大小、扭曲 `snap_to_graph` 對「最近高 degree node」的選擇。

這兩個 bug 直接造成 user-facing failure：route `(25.0478, 121.5170) → (25.0337, 121.5645)`（北車 → 信義區，主幹道 4.5 km）回 `no path found`。診斷：終點 snap 到 node `55083`（degree=1，只有 OUT-edge），forward BFS 從 start 出發可達 219,900 個 node（98% 主圖）但**進不來這個 dead-end node**。

## What Changes

### `build_graph_from_osm.sql` — oneway 修正
- **BREAKING**：非 oneway way（`oneway` 不為 `'yes'` / `'true'` / `'1'`）SHALL 在相鄰 vertex 之間建立**雙向 edge**（同一 OSM way 段產生 `A→B` 與 `B→A` 兩筆 `traffic_edge`），共享 `geom`、`length_km`、`road_class`、`max_speed_kmh`。
- oneway way 行為不變：仍只插一條 `A→B`。
- `oneway` 欄位語意保留為「該 edge 是否屬於 oneway way」（單筆 edge 是 directed 的事實由它在 adjacency 出現方向決定，與 `oneway` flag 解耦）。

### Degree-2 chain contraction
- **新增 requirement**：build SQL 末段 SHALL 執行一輪 topology contraction：將連續 degree-2 node 鏈合併成單一較長 edge，保留鏈頭尾兩端 node 作為 intersection。
- 合併後 edge：`length_km` 為鏈中所有 segment 之和；`road_class` / `max_speed_kmh` 取鏈中首段（OSM 同一 way 內 tag 一致，跨 way 邊界處鏈會被斷開所以不會跨值）；`geom` 為 `ST_MakeLine` 串接後的 LINESTRING。
- 目標：intersection 比例（degree ≥ 3 / total）≥ 50%。預估 node 數從 224k 降到 ~30–50k。
- 號誌 snap（`has_signal`）SHALL 在 contraction **之後**執行，避免被合併掉的中段 node 丟失號誌標記。

### `snap_to_graph` 強化（defense-in-depth）
- **行為變動**（callers 看得到）：`snap_to_graph` 預設 `k` 從 3 提高到 15，仍以 highest-degree → tie-break 距離排序。**所有既有 caller** 在 stub-rich 區域的 snap 結果會改變（更傾向選真路口、放棄極近的 stub）；非 stub 區域結果通常不變。視為**內部 API 行為變動**，但因 `plan_optimal_route` 之外無第三方 caller、wire schema 不破，未列為 BREAKING。
- **新增 requirement**：`snap_to_graph` 支援可選 `return_top_n: int = 1` 參數，≥ 2 時回 `list[int]`。
- **新增 requirement**：`plan_optimal_route` SHALL 對 origin 端 snap 做 forward-reachability 預檢；若 snap 結果無法在主圖被走到 / 走出，SHALL 依距離順序試下一個候選 node（最多試 5 個）。
- 既有 `bbox padding 0.3 → 0.6 retry` 流程保留不動。

### 重新 import
- **BREAKING（資料層）**：執行 `bash scripts/import_taipei_osm.sh` + `psql -f scripts/build_graph_from_osm.sql` 全部重跑，現有 224k node / 248k edge 會被 TRUNCATE。
- VD `snapped_road_class` SHALL 在 graph rebuild 後重新計算（同 taipei-opendata-rebuild 既定流程）。
- 既有 VD readings（hypertable `vd_reading`）SHALL **不被影響**（透過 vdid 對應，不依賴 node id）。

### 不做的事
- 不動 `routing.py` 的 A\* 主迴圈、bbox pruning、top-K、congestion-factor 邏輯。
- 不動 Kafka `route.request` / `route.response` wire schema。
- 不動 VD ingestion、weight_provider 三層 tier、speed-camera 邏輯。

## Capabilities

### New Capabilities
（無）

### Modified Capabilities
- `osm-road-network`：oneway 處理改為非 oneway 強制雙向；新增 degree-2 chain contraction；新增 intersection ratio 驗收門檻；號誌 snap 改在 contraction 之後執行
- `astar-routing`：`snap_to_graph` k 從 3 → 15；新增 snap-with-reachability fallback 流程

## Impact

### 受影響檔案
- `scripts/build_graph_from_osm.sql` — 核心修改（oneway + contraction + signal 順序）
- `backend/multiagent-service/src/agents/routing.py:253-275`（`snap_to_graph`）+ `:476-527`（`plan_optimal_route` snap 流程）
- `backend/multiagent-service/tests/test_routing.py`、`test_build_graph_sql.py`（測試延伸）

### 不變的對外介面
- `route.request` / `route.response` Kafka payload schema 不變
- `plan_optimal_route()` Python signature 不變
- `traffic_node` / `traffic_edge` SQL schema 不變（仍是 c8136ba 上線的欄位）

### 對效能 / 行為的影響
- `RoadGraph.from_db()` 載入時間預估從目前 ~3–5 s 降到 < 1 s（edge 數量降到 ~1/4）。
- A\* 路徑搜尋預期更快（node 數量降；同時 contraction 後 heuristic estimate 更貼近真實 hop count）。
- 部分 user-facing 路徑可能與舊圖**不完全一致**（contraction 後路徑表達層改變、但實際走的道路不變）；`route.response` 的 `path` node 序列會變短，這由 main-service 無腦轉發到前端，前端只用 `geometry` 不依賴 node id 故不破前端。
- 由於非 oneway way 現在會有兩筆 edge，**memory footprint 預估增 ~30–50%**（部分被 contraction 抵銷），仍遠小於原本 224k node 的記憶體。

### 對 `taipei-opendata-rebuild` 的關係
本 change 修正 `taipei-opendata-rebuild` 引入的 SQL build 邏輯缺陷。若 `taipei-opendata-rebuild` 此時尚未 archive，本 change 的 spec deltas 應視為對該 change 已 ADDED 之 requirement 的進一步 ADDED / MODIFIED。建議 `taipei-opendata-rebuild` archive 後再對 main specs apply 本 change，避免 delta 衝突。
