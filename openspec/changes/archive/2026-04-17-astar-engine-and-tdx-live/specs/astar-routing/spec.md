## ADDED Requirements

### Requirement: In-memory 路網圖載入
multiagent-service 啟動時 SHALL 從 DB 載入所有 TrafficNode 與 TrafficEdge，建構 in-memory adjacency dict。

#### Scenario: 成功載入路網圖
- **WHEN** multiagent-service 啟動且 DB 中存在路網資料
- **THEN** SHALL 建構 `graph: dict[int, list[tuple[int, int, float]]]`，其中 key 為 node_id，value 為 `(neighbor_id, edge_id, dynamic_weight)` 的列表
- **AND** 初始 dynamic_weight SHALL 等於 `base_weight`

#### Scenario: DB 無路網資料
- **WHEN** multiagent-service 啟動且 DB 中無 TrafficNode 資料
- **THEN** SHALL 記錄 warning log 並建構空 graph，路徑規劃請求 SHALL 回傳錯誤訊息

### Requirement: A* 路徑搜尋演算法
系統 SHALL 實作 A* 演算法，使用 haversine 直線距離除以全路網最高速限作為 heuristic。

#### Scenario: 找到最短路徑
- **WHEN** 給定有效的起點 node_id 與終點 node_id
- **THEN** A* SHALL 回傳從起點到終點的最短路徑（node 序列）與總 cost（小時）
- **AND** heuristic 函數 SHALL 為 `haversine_km(current, destination) / max_speed_limit_in_graph`

#### Scenario: 無可達路徑
- **WHEN** 起點與終點之間不存在可達路徑
- **THEN** SHALL 回傳 None 或空路徑

### Requirement: Snap to graph
系統 SHALL 將任意 GPS 座標 (latitude, longitude) 對應到路網圖上最合適的 node。

#### Scenario: 找到最近的高 degree node
- **WHEN** 給定一個 GPS 座標
- **THEN** SHALL 找到距離最近的 K=3 個 node，並回傳其中 degree（鄰居數量）最高的 node ID

#### Scenario: 路網為空
- **WHEN** 路網圖無任何 node
- **THEN** SHALL 回傳錯誤

### Requirement: Penalty-based top-K 路徑
系統 SHALL 支援回傳最多 K 條替代路徑，採用 penalty-based re-run 策略。

#### Scenario: 成功回傳多條路徑
- **WHEN** 請求 top-K 路徑（K=3）
- **THEN** SHALL 先執行 A* 找到最佳路徑，然後將該路徑用過的 edge weight 乘以 penalty_factor (3.0)，再次執行 A*，重複至取得 K 條路徑或無更多可達路徑
- **AND** 每條路徑的最終 cost SHALL 使用原始 graph weight 重新計算
- **AND** 回傳結果 SHALL 按真實 cost 由小到大排序

#### Scenario: 可用路徑不足 K 條
- **WHEN** 圖中只有不足 K 條不同路徑可達終點
- **THEN** SHALL 回傳所有找到的路徑，不補空

### Requirement: 動態 weight 更新
系統 SHALL 支援外部模組（Traffic Agent）動態更新 in-memory graph 的 edge weight。

#### Scenario: 更新單一 edge weight
- **WHEN** Traffic Agent 提供 edge_id 與新的 congestion_factor
- **THEN** SHALL 更新 graph 中對應 edge 的 dynamic_weight 為 `base_weight × congestion_factor`
- **AND** 後續 A* 查詢 SHALL 使用更新後的 weight

### Requirement: 路徑結果附帶測速照相機
路徑規劃結果 SHALL 包含沿途的測速照相機資訊。

#### Scenario: 路徑經過有測速照相機的 edge
- **WHEN** A* 回傳的路徑包含某條 edge，且該 edge 上有關聯的 SpeedCamera 紀錄
- **THEN** 路徑結果 SHALL 附帶該相機的 latitude、longitude、direction、speed_limit、address

#### Scenario: 路徑無測速照相機
- **WHEN** A* 回傳的路徑沿途無任何 SpeedCamera 紀錄
- **THEN** 路徑結果的 speed_cameras 清單 SHALL 為空陣列

### Requirement: 路徑規劃完整回應格式
`plan_optimal_route()` SHALL 回傳結構化的路徑規劃結果。

#### Scenario: 成功回傳路徑規劃
- **WHEN** 給定有效的起點與終點座標
- **THEN** SHALL 回傳 JSON 結構包含 `routes` 陣列，每條路徑包含 `path`（node 序列）、`edges`（edge 序列）、`road_names`（途經路名）、`estimated_time_min`（預估分鐘數）、`distance_km`（總距離）、`speed_cameras`（沿途測速照相機）
