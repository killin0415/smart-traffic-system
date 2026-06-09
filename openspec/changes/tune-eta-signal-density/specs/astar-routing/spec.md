## MODIFIED Requirements

### Requirement: A* signal penalty for traffic-light nodes
A* g_score 計算 SHALL 對通過有號誌的 node 加上 `SIGNAL_PENALTY_HR` 停等延遲，模擬紅綠燈等待時間。`SIGNAL_PENALTY_SECONDS` 預設值 SHALL 為 **40 秒**（自 `taipei-opendata-rebuild` 的 20s 提高，反映 Taipei 一般信號週期下的平均停等 + 起步加速 30-45 秒）。預設值 SHALL 可由環境變數 `SIGNAL_PENALTY_SECONDS` 覆寫。

#### Scenario: 一般中間節點有號誌
- **WHEN** A* expand 一個 neighbor node 且該 node `has_signal == TRUE`
- **AND** 該 neighbor node 不是路徑的 end_id
- **THEN** SHALL 計算 `tentative_g = g_score[current] + edge_weight + SIGNAL_PENALTY_HR`
- **AND** `SIGNAL_PENALTY_HR = SIGNAL_PENALTY_SECONDS / 3600`（環境變數預設 **40s = 1/90 hr**）

#### Scenario: 終點 node 即使有號誌也不加 penalty
- **WHEN** A* expand 到 end_id 且 `graph.nodes[end_id].has_signal == TRUE`
- **THEN** SHALL NOT 加 SIGNAL_PENALTY_HR（停車本來就要等紅燈，不視為額外延遲）
- **AND** 該節點的 `tentative_g = g_score[current] + edge_weight`

#### Scenario: 起點不重複加 penalty
- **WHEN** A* 從 start_id 開始
- **THEN** SHALL 不對 start_id 自身加 SIGNAL_PENALTY（penalty 加在「進入」的 node 不是「離開」）

#### Scenario: env override 仍可調整
- **WHEN** 設定環境變數 `SIGNAL_PENALTY_SECONDS=60`
- **THEN** `SIGNAL_PENALTY_HR` SHALL 為 `60 / 3600 = 1/60 hr`，A* 使用此值計算
- **AND** 預設值 40s 僅在環境變數未設定時生效

### Requirement: 動態 weight 更新
系統 SHALL 支援透過 WeightProvider 取得 dynamic weight，A* 引擎不直接讀取 `traffic_edge.base_weight`。WeightProvider `apply_to_graph(graph)` 在計算每條 edge 的 weight 時 SHALL 對非 trunk / motorway 系列 edge 套用 intersection-density multiplier。

#### Scenario: WeightProvider 套用 weight（含 multiplier）
- **WHEN** WeightProvider 完成 `apply_to_graph(graph)`
- **THEN** SHALL 對 `graph.edges` 中每一條 edge 計算 `base = length_km / max(speed_kmh, 5.0)`
- **AND** 若 `edge.road_class IN ('motorway', 'trunk', 'motorway_link', 'trunk_link')` SHALL 設 `multiplier = 1.0`
- **AND** 其餘 edge SHALL 設 `multiplier = 1.0 + INTERSECTION_MULTIPLIER_FACTOR × edge.intersection_count`，其中 `INTERSECTION_MULTIPLIER_FACTOR` 預設為 **0.15**
- **AND** 最終 `dynamic_weight = base × multiplier`，寫入該 edge 在 forward adjacency 與 reverse adjacency 中對應的 weight 槽位

#### Scenario: 啟動時先套一次 weight
- **WHEN** multiagent-service 啟動 lifespan 完成 `RoadGraph.from_db()`
- **THEN** SHALL 立即呼叫 `weight_provider.rebuild() + weight_provider.apply_to_graph(graph)` 一次，避免 first request 用初始 weight

#### Scenario: trunk / motorway edge 不受 multiplier 影響
- **WHEN** 某條 edge 的 `road_class` 為 `motorway`、`trunk`、`motorway_link` 或 `trunk_link`
- **THEN** `multiplier` SHALL 為 1.0 不論 `intersection_count` 為何
- **AND** weight SHALL 等於純粹的 `length_km / max(speed_kmh, 5.0)`

#### Scenario: intersection_count = 0 的非 trunk edge
- **WHEN** 某條 surface street（如 residential, service）edge 的 `intersection_count = 0`
- **THEN** `multiplier = 1.0 + 0.15 × 0 = 1.0`，weight 等於 base
- **AND** 對 A* 行為沒有額外影響

#### Scenario: intersection_count > 0 的 surface edge
- **WHEN** 某條 secondary edge 有 `intersection_count = 4`
- **THEN** `multiplier = 1.0 + 0.15 × 4 = 1.6`
- **AND** weight = base × 1.6（時間估算上升 60%）
- **AND** A* 會傾向選擇 intersection 較少的替代路徑（若存在）

## ADDED Requirements

### Requirement: GraphEdge 包含 intersection_count
`GraphEdge` dataclass SHALL 新增 `intersection_count: int = 0` 欄位，由 `RoadGraph.from_db()` 從 `TrafficEdge.intersection_count` 載入。

#### Scenario: 從 DB 載入 intersection_count
- **WHEN** `RoadGraph.from_db(session)` 執行
- **THEN** 每個 `GraphEdge` SHALL 帶入對應 `TrafficEdge.intersection_count` 的整數值
- **AND** 預設值 SHALL 為 0（向後相容 DB schema 沒有此欄位的舊部署）

#### Scenario: GraphEdge dataclass 預設值
- **WHEN** 直接構造 `GraphEdge(...)` 不傳 `intersection_count`
- **THEN** SHALL 取 dataclass 預設值 `intersection_count = 0`
- **AND** 用此 edge 計算 weight 時 multiplier 退化為 1.0（不影響既有測試）

### Requirement: Intersection multiplier 常數可調整
`INTERSECTION_MULTIPLIER_FACTOR` 與 `INTERSECTION_MULTIPLIER_EXEMPT_CLASSES` SHALL 以模組層級常數宣告於 `weight_provider.py`，方便測試 monkeypatch 與未來經由配置覆寫。

#### Scenario: 模組常數宣告位置
- **WHEN** 檢查 `weight_provider.py`
- **THEN** SHALL 存在 `INTERSECTION_MULTIPLIER_FACTOR: float = 0.15`
- **AND** SHALL 存在 `INTERSECTION_MULTIPLIER_EXEMPT_CLASSES: frozenset[str] = frozenset({"motorway", "trunk", "motorway_link", "trunk_link"})`

#### Scenario: 測試可 monkeypatch
- **WHEN** 測試以 `monkeypatch.setattr(weight_provider, "INTERSECTION_MULTIPLIER_FACTOR", 0.5)` 覆寫
- **THEN** `apply_to_graph` 後續呼叫 SHALL 使用 0.5 而非預設 0.15
- **AND** 同樣方式 SHALL 可覆寫 `INTERSECTION_MULTIPLIER_EXEMPT_CLASSES`（例如測試「全部 edge 都套 multiplier」場景時設 frozenset()）
