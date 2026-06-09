## Why

`fix-osm-graph-topology` 修好了 graph topology 後實測：原失敗座標 `(25.0478,121.5170) → (25.0337,121.5645)` 現在會回 3 條 route。但 Google Maps 對同一條路（也走市民大道高架）日間給 **18 min**、深夜給 **8 min**；我的 A* 給 **7.6 min**。差不多就是「Google 的深夜值」。

排查確認：

1. VD ingest 正常（1611 筆 / 10 min，均速 48 km/h），weight_provider 53.9% edge 拿 Tier 1（per-edge VD spatial）、45.8% 拿 Tier 2（class-average）、0.3% 拿 Tier 3。VD 報的速度合理（trunk 63、primary 47、secondary 43 km/h）。
2. 真正缺的是 **「停等成本」**：VD 量的是「通過偵測器當下的瞬時速度」，車子停在紅燈時 VD 量不到；所以 VD-driven weight 接近 free-flow ETA、跟 Google 深夜值對得上。
3. 既有 A* 對 `has_signal=TRUE` 的 node 加 20 秒 `SIGNAL_PENALTY`，但只有 8,877 / 67,454 = 13% 的 node 標號誌，覆蓋率太低（30m snap 半徑漏掉很多偏遠端點），且 20 秒嚴重低估號誌週期（紅燈 30-60 秒、平均等待 15-30 秒 + 起步延遲）。

## What Changes

### A — 號誌偵測 + penalty 加重

- `scripts/build_graph_from_osm.sql`：號誌 snap `ST_DWithin` 半徑 **30m → 50m**（提高 OSM `traffic_signals` point 抓得到 traffic_node 的比例）
- `backend/multiagent-service/src/agents/routing.py`：`SIGNAL_PENALTY_SECONDS` 預設 **20 → 40 秒**（保留 env override `SIGNAL_PENALTY_SECONDS`）。注意：此預設值變動會讓既有 caller（含 Kafka `route.response.estimated_time_min`）的 ETA 觀察值上移（一條 5 號誌的路徑多 ~100 秒）；不視為 wire schema BREAKING（欄位、型別、語意未變），但屬於「同樣輸入下數值會位移」的可見變動 — 部署時請告知 main-service 端

### C — Per-edge 路口密度 multiplier

- **新增**：`scripts/build_graph_from_osm.sql` 在 build 末段（contraction + signal snap 都跑完後）為每條 `traffic_edge` 計算「edge 幾何 15m buffer 內的 `highway=traffic_signals` point 數量」，存入新欄位 `traffic_edge.intersection_count` (INTEGER NOT NULL DEFAULT 0)。需要對應 `infra/init-db/02-road-network-tables.sql` 新增此欄位。
- **新增**：`backend/multiagent-service/src/agents/weight_provider.py:apply_to_graph()` 在計算 weight 時把 multiplier `(1 + 0.15 × intersection_count)` 乘到 weight 上：`weight_hr = (length_km / speed_kmh) × multiplier`
- 對 `trunk` / `motorway` / `trunk_link` / `motorway_link` road_class 的 edge **不套** multiplier（高架、國道、匝道實際上沒紅綠燈，避免重複懲罰）
- multiplier 係數 `0.15` 與 trunk 排除清單以模組層級常數提供，方便日後微調或經由 env 覆寫。

### 不做的事

- 不改 `weight_provider` 的 Tier 1/2/3 分層邏輯（Tier 1 KDTree 半徑、Tier 2 class-average 算法都不動）
- 不改 A\* 主迴圈（heuristic、bbox pruning、top-K 邏輯不動）
- 不引入外部 ETA 服務（Google Roads、OSRM 等）— 屬於 `eta_accuracy_followup.md` 的 Option C，超出本 change 範圍
- 不改 Kafka `route.request` / `route.response` wire schema
- 不改 osm2pgsql 匯入流程（`scripts/import_taipei_osm.sh`）

## Capabilities

### New Capabilities
（無）

### Modified Capabilities

- `osm-road-network`：號誌 snap 半徑 30m → 50m；新增 `traffic_edge.intersection_count` 欄位 + build 末段填值邏輯
- `astar-routing`：`SIGNAL_PENALTY_SECONDS` 預設值 20 → 40；weight_provider `apply_to_graph` 改套 intersection-density multiplier

## Impact

### 受影響檔案

- `scripts/build_graph_from_osm.sql` — signal snap 半徑 + intersection_count 計算區塊（會放在 contraction 與 signal snap 都跑完之後）
- `infra/init-db/02-road-network-tables.sql` — `traffic_edge` 新增 `intersection_count INTEGER NOT NULL DEFAULT 0` 欄位
- `backend/multiagent-service/src/agents/routing.py` — `SIGNAL_PENALTY_SECONDS` 預設值
- `backend/multiagent-service/src/agents/weight_provider.py` — `apply_to_graph` weight 公式 + 常數宣告
- `backend/multiagent-service/src/db/models.py` — `TrafficEdge.intersection_count` ORM 欄位
- `backend/multiagent-service/src/agents/routing.py` — `GraphEdge.intersection_count` dataclass 欄位 + `RoadGraph.from_db` 載入時填值
- `backend/multiagent-service/tests/test_routing.py`、`tests/test_weight_provider.py`、`tests/test_build_graph_sql.py` — 對應測試延伸

### 不變的對外介面

- `route.request` / `route.response` Kafka payload schema 不變
- `plan_optimal_route()` Python signature 不變
- `traffic_node` schema 不變
- `traffic_edge` 既有欄位不變（只新增 `intersection_count`）

### 對效能 / 行為的影響

- Build SQL 多一輪 `ST_DWithin` 查詢計算 `intersection_count`：估 ~30-60 秒額外時間（67k edges × spatial index lookup）
- A\* 行為：路徑選擇會更傾向 elevated / 高密度 surface street 中間「intersection-count 較少」的 alternative。對既有路徑來說：
  - 高架路徑：weight 完全不變（trunk 不套 multiplier）
  - Surface 多燈路徑：weight 上升 15% × 號誌數（一條 500m 含 4 個號誌的 surface street weight × 1.6）
- 預期效果：原失敗座標的 surface alternative 變昂貴 → A* 仍選高架，但 high-traffic edge 的 ETA 因為 SIGNAL_PENALTY 從 20 → 40 + intersection-density 加成，更接近 Google 日間值
- Memory：每條 edge 多一個 INTEGER 欄位（4 byte × 159,914 ≈ 0.6 MB），可忽略

### 對 `fix-osm-graph-topology` / `taipei-opendata-rebuild` 的關係

- 本 change 建立在 `fix-osm-graph-topology` 已 apply 的基礎上（reverse_adjacency、snap-with-reachability、chain contraction 都假設存在）
- `taipei-opendata-rebuild` + `fix-osm-graph-topology` 都尚未 archive。本 change 的 spec deltas 視為對該兩 change 已 ADDED 之 requirement 的進一步 MODIFIED。建議按 `taipei-opendata-rebuild` → `fix-osm-graph-topology` → `tune-eta-signal-density` 順序 archive 到 main specs。
- 反過來說：若需要 revert 本 change，git revert 就夠，不需要再跑一次 graph rebuild —— intersection_count 欄位有預設值 0、multiplier 乘 1.0 等同 no-op；既有 weight 計算公式仍能運作。
