## MODIFIED Requirements

### Requirement: TimescaleDB extension 自動初始化
TimescaleDB 容器首次啟動時 SHALL 自動啟用 `timescaledb` extension，並建立路網相關的靜態表。

#### Scenario: 首次啟動執行 init script
- **WHEN** timescaledb 容器首次啟動（無既有資料）
- **THEN** SHALL 執行 `infra/init-db/` 目錄下的 SQL script，包含 `CREATE EXTENSION IF NOT EXISTS timescaledb`

#### Scenario: 建立 traffic_node 表
- **WHEN** init-db SQL script 執行
- **THEN** SHALL 建立 `traffic_node` 表，包含欄位：`id`（SERIAL PRIMARY KEY）、`latitude`（DOUBLE PRECISION NOT NULL）、`longitude`（DOUBLE PRECISION NOT NULL）

#### Scenario: 建立 traffic_edge 表
- **WHEN** init-db SQL script 執行
- **THEN** SHALL 建立 `traffic_edge` 表，包含欄位：`id`（SERIAL PRIMARY KEY）、`source_node_id`（INTEGER NOT NULL, REFERENCES traffic_node）、`target_node_id`（INTEGER NOT NULL, REFERENCES traffic_node）、`road_name`（VARCHAR）、`length_km`（DOUBLE PRECISION NOT NULL）、`speed_limit_kmh`（INTEGER NOT NULL）、`base_weight`（DOUBLE PRECISION NOT NULL）

#### Scenario: 重複啟動不重複執行
- **WHEN** timescaledb 容器重新啟動（已有資料）
- **THEN** init script SHALL NOT 重複執行（PostgreSQL entrypoint 預設行為）

#### Scenario: 表使用 IF NOT EXISTS
- **WHEN** init SQL 中的 CREATE TABLE 語句執行
- **THEN** SHALL 使用 `CREATE TABLE IF NOT EXISTS` 語法，避免重複建立時報錯
