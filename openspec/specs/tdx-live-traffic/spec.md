## Purpose

定義 multiagent-service 從 TDX Live API 拉取台北市即時路段車速資料、寫入 Redis 與 TimescaleDB、並計算 congestion factor 更新 in-memory graph edge weight 的需求規範。

## Requirements

### Requirement: TDX Live API 定時拉取
系統 SHALL 定時從 TDX Live Section API 拉取 **Taipei** 即時路段車速資料。

#### Scenario: 定時輪詢
- **WHEN** multiagent-service 啟動後
- **THEN** SHALL 每 1-5 分鐘（可設定 `TDX_LIVE_REFRESH_SECONDS`，預設 300）向 TDX `GET /api/basic/v2/Road/Traffic/Live/City/Taipei` 發送請求
- **AND** SHALL 使用 TDX OAuth2 token 進行認證

#### Scenario: API 請求失敗
- **WHEN** TDX Live API 回傳錯誤或逾時
- **THEN** SHALL 記錄 error log 並保留前次快取資料，下次輪詢時重試
- **AND** SHALL NOT 清除 Redis 中的既有快取

#### Scenario: TDX `-99` 哨兵資料過濾
- **WHEN** 一筆 LiveTraffics 記錄的 `TravelSpeed <= 0` 或 `TravelTime <= 0` 或 `CongestionLevel = "-99"`
- **THEN** SHALL 視為無資料、不更新該 section 的 Redis cache、不寫入 traffic_history、不更新 graph 權重

### Requirement: 即時資料寫入 Redis
Traffic Agent SHALL 將 TDX Live 資料以 `tdx_section_id` 為 key 寫入 Redis。

#### Scenario: 成功更新 Redis 快取
- **WHEN** 從 TDX Live API 取得即時路段資料
- **THEN** SHALL 對每筆路段資料，以 `traffic:section:{tdx_section_id}` 為 key，寫入包含 `travel_speed`、`travel_time`、`updated_at` 的 JSON value
- **AND** 每筆 key SHALL 設定 TTL 為 10 分鐘

#### Scenario: 查詢指定 edge 的即時資料
- **WHEN** 以 edge_id 或 tdx_section_id 查詢即時路況
- **THEN** SHALL 從 Redis 讀取對應 key 的資料並回傳

### Requirement: 即時資料寫入 TimescaleDB
Traffic Agent SHALL 將 TDX Live 資料寫入 TimescaleDB 作為時序紀錄。

#### Scenario: 成功寫入時序紀錄
- **WHEN** 從 TDX Live API 取得即時路段資料
- **THEN** SHALL 將每筆路段的 `tdx_section_id`、`travel_speed`、`travel_time`、`timestamp` 寫入 `traffic_history` hypertable

### Requirement: Congestion factor 計算與 edge weight 更新
Traffic Agent SHALL 根據通過 `-99` 哨兵過濾後的有效即時車速計算 congestion factor，並更新 in-memory graph 的 edge weight。

#### Scenario: 有有效即時資料時計算 congestion factor
- **WHEN** 取得某路段的即時車速 `current_speed` 且該筆已通過 `-99` 哨兵過濾（即 `current_speed > 0`）
- **THEN** congestion_factor SHALL 為 `min(speed_limit / current_speed, 10.0)`
- **AND** SHALL 更新對應 edge 的 dynamic_weight 為 `base_weight × congestion_factor`

#### Scenario: 無即時資料的 edge
- **WHEN** 某 edge 沒有對應的 TDX Live 資料（含被 `-99` 哨兵過濾掉的筆）
- **THEN** congestion_factor SHALL 維持 `1.0`（使用 base_weight）

### Requirement: TDX Section ID 與 Edge 的 Mapping
系統 SHALL 透過 `TrafficEdge.tdx_section_id` 欄位將 TDX Live 資料 mapping 到路網 edge。Taipei 的 SectionID 格式為 `L_XXXXXXXXXXXXXXXX`（Taipei 路段使用 `L_2*` / `L_6*` 等前綴），與 Kaohsiung（`L_61*` / `L_62*`）不同但同 schema。

#### Scenario: 成功 mapping
- **WHEN** TDX Live 資料的 SectionID 與某 TrafficEdge 的 `tdx_section_id` 相符
- **THEN** SHALL 更新該 edge 的 Redis 快取與 in-memory weight

#### Scenario: 無法 mapping
- **WHEN** TDX Live 資料的 SectionID 在 TrafficEdge 中找不到對應紀錄（例如 bbox 之外的 section）
- **THEN** SHALL 忽略該筆資料並記錄 debug log
