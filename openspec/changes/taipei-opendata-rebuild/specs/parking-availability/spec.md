## ADDED Requirements

### Requirement: 停車場 metadata seed
系統 SHALL 在啟動時從 data.taipei 停車場資訊匯入停車場靜態資料。

#### Scenario: 首次 seed
- **WHEN** multiagent-service 啟動且 `parking_lot` 表為空
- **THEN** SHALL 從 data.taipei 停車場 dataset (id=`d5c0656b-5250-4179-a491-c94daa56ef2c`) 抓取靜態資料
- **AND** 寫入 `parking_lot (id, name, address, total_car, total_motor, lat, lng, geom)`
- **AND** `geom` SHALL 為 `ST_MakePoint(lng, lat)`

#### Scenario: 已有資料時跳過
- **WHEN** `parking_lot` 表已有資料
- **THEN** SHALL 跳過 seed 並記錄 info log

#### Scenario: parking_lot 含 GIST index
- **WHEN** 檢查 `parking_lot` schema
- **THEN** GIST 索引 `ix_parking_lot_geom` SHALL 存在以支援空間查詢

### Requirement: 停車場剩餘車位定時拉取
系統 SHALL 透過 background task 定時從 data.taipei 拉取即時剩餘車位資訊。

#### Scenario: 定時輪詢
- **WHEN** multiagent-service 啟動後
- **THEN** SHALL 每 `PARKING_REFRESH_SECONDS` 秒（預設 300）抓取 data.taipei 停車場 dataset 的即時資料
- **AND** 對每筆停車場 INSERT 一筆 `parking_availability (ts, parking_id, available_car, available_motor)` with `ON CONFLICT DO NOTHING`

#### Scenario: 抓取失敗不中斷
- **WHEN** HTTP 抓取失敗
- **THEN** SHALL log exception 但 background task SHALL 繼續下個 cycle
- **AND** A* 路徑回應仍可用上一輪的 availability 資料

#### Scenario: parking_availability 是 hypertable
- **WHEN** 檢查 `parking_availability` 表
- **THEN** SHALL 為 TimescaleDB hypertable，partition 欄位為 `ts`，PRIMARY KEY 為 `(ts, parking_id)`

### Requirement: 終點附近停車場查詢
系統 SHALL 提供 `query_parking_near_destination(session, lat, lng)` 函式，回傳目的地附近的停車場推薦。

#### Scenario: 1km 內、剩餘車位 ≥10 的前 5 個
- **WHEN** 給定終點座標 `(lat, lng)`
- **THEN** SHALL 用 PostGIS `ST_DWithin(parking_lot.geom::geography, ST_MakePoint(lng, lat)::geography, 1000)` 找 1km 內的停車場
- **AND** 用 `LATERAL` join 取每個停車場最新一筆 `parking_availability`
- **AND** 篩選 `available_car >= 10`
- **AND** ORDER BY 距離（`ST_Distance` 升冪），LIMIT 5

#### Scenario: 1km 內無符合條件停車場
- **WHEN** 終點 1km 內沒有停車場、或所有停車場 available_car < 10
- **THEN** SHALL 回傳空 list
- **AND** A* 路徑回應的 `parking_suggestions` 欄位 SHALL 為 `[]`

#### Scenario: 回傳欄位
- **WHEN** 查詢成功
- **THEN** 每筆結果 SHALL 包含 `id`、`name`、`address`、`available_car`、`distance_m`（從終點到停車場的公尺距離）

### Requirement: 環境變數 PARKING_REFRESH_SECONDS
系統 SHALL 透過環境變數 `PARKING_REFRESH_SECONDS` 控制停車場 refresh 間隔。

#### Scenario: 預設值
- **WHEN** 環境變數 `PARKING_REFRESH_SECONDS` 未設定
- **THEN** SHALL 使用預設值 300 (5 分鐘)
