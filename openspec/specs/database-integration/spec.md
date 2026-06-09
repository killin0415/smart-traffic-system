## ADDED Requirements

### Requirement: TimescaleDB Docker 服務設定 health check
Docker Compose 中的 timescaledb 服務 SHALL 定義 health check，確保 PostgreSQL 完全就緒後才允許依賴服務啟動。

#### Scenario: Health check 成功
- **WHEN** timescaledb 容器啟動完成
- **THEN** health check SHALL 使用 `pg_isready` 指令驗證資料庫可接受連線

#### Scenario: 依賴服務等待 DB 就緒
- **WHEN** 其他服務依賴 timescaledb
- **THEN** SHALL 使用 `depends_on` 搭配 `condition: service_healthy` 確保 DB 就緒後才啟動

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
- **THEN** SHALL 建立 `traffic_edge` 表，包含欄位：`id`（SERIAL PRIMARY KEY）、`source_node_id`（INTEGER NOT NULL, REFERENCES traffic_node）、`target_node_id`（INTEGER NOT NULL, REFERENCES traffic_node）、`road_name`（VARCHAR）、`length_km`（DOUBLE PRECISION NOT NULL）、`speed_limit_kmh`（INTEGER NOT NULL）、`base_weight`（DOUBLE PRECISION NOT NULL）、`tdx_section_id`（VARCHAR, NULLABLE）

#### Scenario: tdx_section_id 欄位存在
- **WHEN** 檢查 `traffic_edge` 表結構
- **THEN** SHALL 包含 `tdx_section_id` 欄位（VARCHAR, NULLABLE），用於對應 TDX Live API 的 RoadSectionID

#### Scenario: 重複啟動不重複執行
- **WHEN** timescaledb 容器重新啟動（已有資料）
- **THEN** init script SHALL NOT 重複執行（PostgreSQL entrypoint 預設行為）

#### Scenario: 表使用 IF NOT EXISTS
- **WHEN** init SQL 中的 CREATE TABLE 語句執行
- **THEN** SHALL 使用 `CREATE TABLE IF NOT EXISTS` 語法，避免重複建立時報錯

### Requirement: main-service TimescaleDB 連線設定
main-service SHALL 能透過 Spring Data JPA 連線至 TimescaleDB。

#### Scenario: application.yml datasource 設定
- **WHEN** 檢查 main-service 的 `application.yml`
- **THEN** SHALL 包含 `spring.datasource` 設定區段，指向 TimescaleDB 的 JDBC URL、帳號和密碼

#### Scenario: JPA 依賴存在
- **WHEN** 檢查 `build.gradle.kts`
- **THEN** SHALL 包含 `spring-boot-starter-data-jpa` 和 `org.postgresql:postgresql` 依賴

#### Scenario: Hibernate DDL 設定
- **WHEN** 檢查 `application.yml` 的 JPA 設定
- **THEN** `spring.jpa.hibernate.ddl-auto` SHALL 設為 `update`，`spring.jpa.properties.hibernate.dialect` SHALL 設為 PostgreSQL dialect

### Requirement: multiagent-service TimescaleDB 連線設定
multiagent-service SHALL 能透過 SQLAlchemy + asyncpg 連線至 TimescaleDB。

#### Scenario: 資料庫連線設定模組
- **WHEN** multiagent-service 啟動
- **THEN** SHALL 從環境變數讀取 `DATABASE_URL`，並使用 SQLAlchemy async engine 建立連線

#### Scenario: 預設連線字串
- **WHEN** `DATABASE_URL` 環境變數未設定
- **THEN** SHALL 使用預設值 `postgresql+asyncpg://admin:secret@localhost:5432/traffic_data`

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
