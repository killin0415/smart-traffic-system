## ADDED Requirements

### Requirement: SpeedCamera 資料模型
系統 SHALL 定義 `speed_camera` 資料表儲存測速照相機資訊。

#### Scenario: 資料表結構
- **WHEN** 檢查資料庫 schema
- **THEN** SHALL 存在 `speed_camera` 表，包含欄位：`id`（SERIAL PRIMARY KEY）、`latitude`（DOUBLE PRECISION NOT NULL）、`longitude`（DOUBLE PRECISION NOT NULL）、`direction`（VARCHAR）、`speed_limit`（INTEGER NOT NULL）、`address`（VARCHAR）、`nearest_edge_id`（INTEGER, REFERENCES traffic_edge）

### Requirement: 測速照相機資料 Seed
系統 SHALL 在啟動時從政府開放資料 CSV 匯入高雄市測速照相機資料。

#### Scenario: 首次 seed
- **WHEN** multiagent-service 啟動且 `speed_camera` 表為空
- **THEN** SHALL 從預下載的 CSV 檔案讀取資料
- **AND** SHALL 篩選 `CityName == "高雄市"` 的紀錄
- **AND** 對每筆相機，SHALL 計算其座標到所有 TrafficEdge 的距離，找到最近的 edge 作為 `nearest_edge_id`
- **AND** 寫入 `speed_camera` 表

#### Scenario: 已有資料時跳過
- **WHEN** multiagent-service 啟動且 `speed_camera` 表已有資料
- **THEN** SHALL 跳過 seed 並記錄 info log

#### Scenario: CSV 檔案不存在
- **WHEN** 預下載的 CSV 檔案不存在
- **THEN** SHALL 記錄 warning log 並跳過 seed，服務繼續啟動

### Requirement: 路徑沿途測速照相機查詢
系統 SHALL 支援根據一組 edge_id 查詢沿途的測速照相機。

#### Scenario: 查詢成功
- **WHEN** 給定一組 edge_id 清單
- **THEN** SHALL 回傳所有 `nearest_edge_id` 在清單中的 SpeedCamera 紀錄
- **AND** 每筆紀錄包含 latitude、longitude、direction、speed_limit、address、nearest_edge_id
