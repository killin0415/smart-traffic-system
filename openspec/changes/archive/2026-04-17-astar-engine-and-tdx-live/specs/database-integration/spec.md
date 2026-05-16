## MODIFIED Requirements

### Requirement: 建立 traffic_edge 表
- **WHEN** init-db SQL script 執行
- **THEN** SHALL 建立 `traffic_edge` 表，包含欄位：`id`（SERIAL PRIMARY KEY）、`source_node_id`（INTEGER NOT NULL, REFERENCES traffic_node）、`target_node_id`（INTEGER NOT NULL, REFERENCES traffic_node）、`road_name`（VARCHAR）、`length_km`（DOUBLE PRECISION NOT NULL）、`speed_limit_kmh`（INTEGER NOT NULL）、`base_weight`（DOUBLE PRECISION NOT NULL）、`tdx_section_id`（VARCHAR, NULLABLE）

#### Scenario: tdx_section_id 欄位存在
- **WHEN** 檢查 `traffic_edge` 表結構
- **THEN** SHALL 包含 `tdx_section_id` 欄位（VARCHAR, NULLABLE），用於對應 TDX Live API 的 RoadSectionID

#### Scenario: 重複啟動不重複執行
- **WHEN** timescaledb 容器重新啟動（已有資料）
- **THEN** `CREATE TABLE IF NOT EXISTS` SHALL 確保不重複建立

## ADDED Requirements

### Requirement: 建立 speed_camera 表
init-db SQL script SHALL 建立 `speed_camera` 表。

#### Scenario: speed_camera 表結構
- **WHEN** init-db SQL script 執行
- **THEN** SHALL 建立 `speed_camera` 表，包含欄位：`id`（SERIAL PRIMARY KEY）、`latitude`（DOUBLE PRECISION NOT NULL）、`longitude`（DOUBLE PRECISION NOT NULL）、`direction`（VARCHAR）、`speed_limit`（INTEGER NOT NULL）、`address`（VARCHAR）、`nearest_edge_id`（INTEGER, REFERENCES traffic_edge）

### Requirement: 建立 traffic_history hypertable
init-db SQL script SHALL 建立 `traffic_history` hypertable 儲存即時路況時序資料。

#### Scenario: traffic_history 表結構
- **WHEN** init-db SQL script 執行
- **THEN** SHALL 建立 `traffic_history` 表，包含欄位：`time`（TIMESTAMPTZ NOT NULL）、`tdx_section_id`（VARCHAR NOT NULL）、`travel_speed`（DOUBLE PRECISION）、`travel_time`（DOUBLE PRECISION）
- **AND** SHALL 執行 `SELECT create_hypertable('traffic_history', 'time')` 將其轉為 TimescaleDB hypertable
