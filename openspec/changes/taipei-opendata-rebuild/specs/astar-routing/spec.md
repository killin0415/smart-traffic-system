## MODIFIED Requirements

### Requirement: 動態 weight 更新
系統 SHALL 支援透過 WeightProvider 取得 dynamic weight，A* 引擎不直接讀取 `traffic_edge.base_weight`。

#### Scenario: WeightProvider 套用 weight
- **WHEN** WeightProvider 完成 `apply_to_graph(graph)`
- **THEN** SHALL 對 `graph.edges` 中每一條 edge 計算 `dynamic_weight = length_km / max(speed_kmh, 5.0)` 並寫入 adjacency 中該 edge 對應的雙向 weight 槽位
- **AND** 後續 A* 查詢 SHALL 使用更新後的 weight

#### Scenario: 啟動時先套一次 weight
- **WHEN** multiagent-service 啟動 lifespan 完成 `RoadGraph.from_db()`
- **THEN** SHALL 立即呼叫 `weight_provider.rebuild() + weight_provider.apply_to_graph(graph)` 一次，避免 first request 用初始 weight

#### Scenario: A* 不再讀取 traffic_edge.base_weight
- **WHEN** 檢查 `RoadGraph.from_db` 實作
- **THEN** SHALL 不再從 `TrafficEdge.base_weight` 欄位讀取（該欄位已移除）
- **AND** 初始 dynamic_weight SHALL 在啟動時由 WeightProvider 計算後寫入

### Requirement: 路徑規劃完整回應格式
`plan_optimal_route()` SHALL 回傳結構化的路徑規劃結果，包含路徑、預估時間、沿途測速相機、終點附近停車場。

#### Scenario: 成功回傳路徑規劃
- **WHEN** 給定有效的起點與終點座標
- **THEN** SHALL 回傳 JSON 結構包含 `routes` 陣列，每條路徑包含 `path`（node 序列）、`edges`（edge 序列）、`road_names`（途經路名）、`estimated_time_min`（預估分鐘數）、`distance_km`（總距離）、`speed_cameras`（沿途測速照相機）、`parking_suggestions`（終點 1km 內前 5 個剩餘車位 ≥10 的停車場）

#### Scenario: 路徑回應包含 parking_suggestions
- **WHEN** A* 找到至少一條路徑
- **THEN** SHALL 對最佳路徑的終點呼叫 `query_parking_near_destination(session, dest_lat, dest_lng)` 並把結果放到 `routes[0].parking_suggestions`

#### Scenario: parking_suggestions default 為空陣列
- **WHEN** A* 找到路徑但終點 1km 內無符合條件的停車場
- **THEN** `routes[0].parking_suggestions` SHALL 為 `[]`（不是 null 或缺欄位）
- **AND** 確保 main-service Kotlin 端 deserializer 不會因為 null 而失敗

#### Scenario: 非最佳路徑的 parking_suggestions
- **WHEN** A* 回傳 K 條路徑
- **THEN** 只有 `routes[0]`（最佳路徑）SHALL 含 `parking_suggestions`
- **AND** `routes[1..]` 的 `parking_suggestions` SHALL 為 `[]`（避免 N 次 PostGIS 查詢）

## ADDED Requirements

### Requirement: A* search bbox runtime frontier pruning
A* 演算法 SHALL 支援 search bounding box 限制探索範圍，per-request 動態計算。

#### Scenario: compute_search_bbox 公式
- **WHEN** 給定 origin `(o_lat, o_lng)` 和 destination `(d_lat, d_lng)`
- **THEN** `compute_search_bbox(o, d, padding_ratio=0.3, min_padding_km=2.0)` SHALL 計算：
  - `direct_km = haversine_km(o, d)`
  - `pad_km = max(direct_km * 0.3, 2.0)`
  - `pad_deg_lat = pad_km / 111.0`
  - `pad_deg_lng = pad_km / (111.0 * cos(mid_lat))`
- **AND** 回傳 `SearchBox` 含 `sw_lat, ne_lat, sw_lng, ne_lng`，是 origin/destination bbox 加上 padding

#### Scenario: A* successor 跳過 bbox 外 node
- **WHEN** A* 主迴圈處理一個 current node 的 neighbor
- **THEN** 若提供 `search_box` 參數且 `not search_box.contains(neighbor.lat, neighbor.lng)`
- **AND** SHALL 跳過該 neighbor，不 push 到 heap

#### Scenario: 找不到路徑時 retry with wider bbox
- **WHEN** `find_top_k_routes` 用初始 bbox `padding_ratio=0.3` 找不到任何路徑
- **THEN** SHALL 自動 retry 一次用 `padding_ratio=0.6`
- **AND** retry 仍失敗才回傳空 list

#### Scenario: search_box 為 None 時 A* 退化為全圖搜尋
- **WHEN** 呼叫 `astar(graph, start, end, search_box=None)`
- **THEN** SHALL 不做 bbox 檢查，等同未限制範圍的 A*

### Requirement: plan_optimal_route 接受 weight_provider 與 user_id 參數
`plan_optimal_route()` SHALL 接受 weight_provider 與 optional user_id 參數，user_id 為個人化 Phase 2 預留。

#### Scenario: 函式簽章
- **WHEN** 檢查 `plan_optimal_route` 簽章
- **THEN** SHALL 為 `async def plan_optimal_route(session, graph, weight_provider, o_lat, o_lng, d_lat, d_lng, user_id: str | None = None, k: int = 3)`

#### Scenario: user_id=None 時用 base WeightProvider
- **WHEN** 呼叫時 `user_id=None`
- **THEN** SHALL 直接使用傳入的 `weight_provider`（即 TaipeiWeightProvider）算 weight

#### Scenario: user_id 有值時包成 PersonalizedWeightProvider（Phase 2）
- **WHEN** 呼叫時 `user_id="some_user"`
- **THEN** Phase 1 中 SHALL 仍用 base provider（PersonalizedWeightProvider Phase 2 才實作）
- **AND** 介面 SHALL 已預留以避免 Phase 2 改 A* 程式

### Requirement: A* signal penalty for traffic-light nodes
A* g_score 計算 SHALL 對通過有號誌的 node 加上 `SIGNAL_PENALTY_HR` 停等延遲，模擬紅綠燈等待時間。

#### Scenario: 一般中間節點有號誌
- **WHEN** A* expand 一個 neighbor node 且該 node `has_signal == TRUE`
- **AND** 該 neighbor node 不是路徑的 end_id
- **THEN** SHALL 計算 `tentative_g = g_score[current] + edge_weight + SIGNAL_PENALTY_HR`
- **AND** `SIGNAL_PENALTY_HR = SIGNAL_PENALTY_SECONDS / 3600`（環境變數預設 20s = 1/180 hr）

#### Scenario: 終點 node 即使有號誌也不加 penalty
- **WHEN** A* expand 到 end_id 且 `graph.nodes[end_id].has_signal == TRUE`
- **THEN** SHALL NOT 加 SIGNAL_PENALTY_HR（停車本來就要等紅燈，不視為額外延遲）
- **AND** 該節點的 `tentative_g = g_score[current] + edge_weight`

#### Scenario: 起點不重複加 penalty
- **WHEN** A* 從 start_id 開始
- **THEN** start_id 即使 `has_signal == TRUE` SHALL NOT 加 SIGNAL_PENALTY_HR
- **AND** 透過 `g_score[start_id] = 0` 初始化自然處理（A* 不會 re-expand 起點）

#### Scenario: SIGNAL_PENALTY_SECONDS 環境變數
- **WHEN** 環境變數 `SIGNAL_PENALTY_SECONDS` 未設定
- **THEN** SHALL 使用預設值 20（秒）

#### Scenario: A* heuristic 仍 admissible
- **WHEN** 加入 signal penalty 後的 A* 計算 cost
- **THEN** heuristic（`haversine_km / max_speed_kmh`）SHALL 仍 underestimate true cost（因為它不含 signal、也不含 congestion），保證 A* 找到最佳路徑
- **AND** 不需要修改 heuristic 公式

#### Scenario: Signal penalty 不被 top-K penalty 乘倍
- **WHEN** `find_top_k_routes` 在第 2 / 第 3 輪 A* 對已用 edge 套 penalty multiplier (預設 3.0x)
- **THEN** SHALL 只對 `edge_weight` 套乘倍（既有行為）
- **AND** SHALL NOT 對 `SIGNAL_PENALTY_HR` 套乘倍（signal penalty 是 per-node additive、跟 top-K diversity 機制正交）
- **AND** 因此第 2 條替代路徑若仍經過同一號誌 node，signal cost 仍為原始 20s，不會疊加成 60s

### Requirement: Kafka route.request 接受 user_id
Kafka `route.request` payload SHALL 接受 optional `user_id` 欄位，向後相容（沒提供時預設 None）。

#### Scenario: payload 不含 user_id
- **WHEN** `route.request` payload 不含 `user_id` 鍵
- **THEN** handler SHALL 視為 `user_id=None`，不報錯

#### Scenario: payload 含 user_id
- **WHEN** payload 含 `user_id="abc"`
- **THEN** handler SHALL 把該值傳給 `plan_optimal_route(..., user_id="abc")`
