## ADDED Requirements

### Requirement: VD Static Seed

系統 SHALL 在 service 啟動且 `vd_sensor` 表為空時，透過 TDX `basic/v2/Road/Traffic/VD/City/Kaohsiung` 抓取所有 VD 靜態資料並寫入 DB。

每筆資料 MUST 包含：`vdid` (unique)、`latitude`、`longitude`、`link_id` (nullable)、`road_section_id` (nullable)、`nearest_edge_id` (FK → `traffic_edge.id`, nullable)。

#### Scenario: 空表首次啟動
- **WHEN** service 啟動且 `SELECT COUNT(*) FROM vd_sensor = 0`
- **THEN** 系統 SHALL 呼叫 TDX VD API 分頁抓取所有 VD
- **AND** 每個 VD SHALL 被 snap 到最近的 `TrafficEdge`（沿用 `snap_camera_to_edge` 的 haversine-nearest-endpoint 距離）
- **AND** 結果 SHALL 寫入 `vd_sensor` 表

#### Scenario: 表已有資料
- **WHEN** service 啟動且 `vd_sensor` 表不為空
- **THEN** 系統 SHALL 跳過 seed，log「vd_sensor 已有 N 筆資料，跳過 seed」

#### Scenario: TDX 憑證缺失
- **WHEN** `TDX_CLIENT_ID` / `TDX_CLIENT_SECRET` 未設定
- **THEN** seed SHALL no-op 並 log WARNING，不得阻擋其他 lifespan 步驟

#### Scenario: VD 無法 snap 到任何 edge
- **WHEN** 某個 VD 距離所有 edge 都超過合理範圍（無匹配）
- **THEN** 該 VD SHALL 仍被寫入 `vd_sensor`，但 `nearest_edge_id = NULL`

### Requirement: VD Live Data Fetch

系統 SHALL 透過 TDX `basic/v2/Road/Traffic/Live/VD/City/Kaohsiung` 抓取即時資料，並正規化成 `{vdid, lane_speeds: [float]}` 列表。

#### Scenario: Happy path
- **WHEN** `refresh_traffic_data` 被觸發且 TDX 回 HTTP 200
- **THEN** 系統 SHALL parse `VDLives[].LinkFlows[].Lanes[]`，為每個 VD 收集其所有 lane 的 `Speed` 值

#### Scenario: 感測器錯誤值過濾
- **WHEN** 某個 lane 的 `Speed <= 0` 或 `ErrorType` 非空
- **THEN** 該 lane 的速度 SHALL 從聚合中剔除（不參與平均）

### Requirement: Edge-level Speed Aggregation

系統 SHALL 將 VD 的 live 速度聚合到 `TrafficEdge` 層級：每個 edge 的即時速度 = 該 edge 上所有健康 VD 的算術平均。

#### Scenario: 一個 edge 多個健康 VD
- **WHEN** edge `E` 上有 VD `V1` (Speed=40) 與 `V2` (Speed=50) 兩個都健康
- **THEN** edge `E` 的即時速度 SHALL = 45

#### Scenario: 一個 edge 有故障 VD
- **WHEN** edge `E` 上有 VD `V1` (Speed=40 健康) 與 `V2` (Speed=-99 故障)
- **THEN** edge `E` 的即時速度 SHALL = 40

#### Scenario: 一個 edge 所有 VD 全部故障
- **WHEN** edge `E` 上所有 VD 都 `-99` 或錯誤
- **THEN** 該 edge SHALL 被跳過，`RoadGraph.update_weight()` 不得呼叫

### Requirement: RoadGraph Weight Update

對有健康 VD 的 edge，系統 SHALL 計算 congestion_factor 並呼叫 `RoadGraph.update_weight(edge_id, factor)`。

#### Scenario: 塞車
- **WHEN** edge 的 speed_limit=50、當前聚合速度=10
- **THEN** congestion_factor SHALL = min(50/10, MAX_CONGESTION_FACTOR) = 5.0
- **AND** `graph.update_weight(edge_id, 5.0)` SHALL 被呼叫

#### Scenario: 順暢
- **WHEN** edge 的聚合速度 >= speed_limit
- **THEN** congestion_factor SHALL 被 clamp 到 1.0（不加速通過）

### Requirement: Redis + TimescaleDB 寫入

系統 SHALL 維持 astar-engine-and-tdx-live 建立的既有出口：

- 每個有資料的 edge SHALL 寫入 Redis key `traffic:section:{tdx_section_id}`（從 edge.tdx_section_id 取）、TTL 10 分鐘、value 為 JSON `{travel_speed, travel_time, updated_at}`
- 每次 refresh SHALL 寫入一筆 `traffic_history` row（time, tdx_section_id, travel_speed, travel_time）

#### Scenario: 正常 refresh
- **WHEN** 一輪 refresh 產出 M 個 edge 有 live 資料
- **THEN** Redis SHALL 被更新 M 筆（以 `tdx_section_id` 為 key；若多 edge 共用同一 section_id，以最後寫入的為準）
- **AND** `traffic_history` SHALL 新增 M 筆（time=now, 對應 section_id）

#### Scenario: VD live fetch 失敗
- **WHEN** TDX API 回傳非 200
- **THEN** 系統 SHALL log ERROR 並 return，不得寫入 Redis / TimescaleDB
- **AND** 既有 Redis cache SHALL 保留到 TTL 自然過期

### Requirement: Periodic Refresh 整合

系統 SHALL 用 VD 路徑取代現有 `fetch_live_section_data()`，`run_periodic_refresh()` 的間隔與入口簽名保持不變。

#### Scenario: Lifespan 啟動
- **WHEN** FastAPI lifespan 啟動
- **THEN** 系統 SHALL 先 seed road network、speed cameras、vd_sensor，再載入 RoadGraph，最後啟動 `run_periodic_refresh(graph, async_session)`

#### Scenario: 觀察日誌
- **WHEN** 一輪 refresh 完成
- **THEN** log SHALL 包含「N VDs fetched, M healthy, K edges updated」以利監控覆蓋率

### Requirement: 移除 Kaohsiung-not-accepted 降級分支

系統 SHALL 移除 `traffic.py` 現有的 `_unsupported_city_logged` 狀態與 HTTP 400 特殊處理（該降級路徑是針對 Live Section endpoint 設計，VD 路徑不需要）。

#### Scenario: TDX VD endpoint 正常
- **WHEN** refresh 呼叫 VD Live endpoint 取得 200
- **THEN** 不得有「is not accepted」相關分支被執行
