## Context

目前 `infra/docker-compose.yml` 已定義 TimescaleDB（PostgreSQL 14）與 Redis（Alpine）容器，但兩個後端服務皆未設定連線。main-service 缺少 JPA 和 Redis 依賴；multiagent-service 已有 asyncpg、SQLAlchemy 和 redis 套件但尚無連線設定。需要完成 Docker 層與應用程式層的完整串接。

## Goals / Non-Goals

**Goals:**
- 讓 TimescaleDB 容器啟動時自動啟用 `timescaledb` extension 並建立基礎 schema
- main-service 可透過 Spring Data JPA 連線 TimescaleDB、透過 Spring Data Redis 連線 Redis
- multiagent-service 可透過 SQLAlchemy + asyncpg 連線 TimescaleDB、透過 redis-py 連線 Redis
- Docker Compose 服務加入 health check，確保依賴服務就緒後才啟動應用程式
- 新增 Redis volume 確保快取資料持久化

**Non-Goals:**
- 定義完整的業務資料表（僅建立基礎 schema 和範例 hypertable）
- 實作具體的快取邏輯或 cache invalidation 策略
- 設定 production 環境的安全性（TLS、密碼管理等）
- Kubernetes 部署設定

## Decisions

### Decision 1: 使用 init script 啟用 TimescaleDB extension
**選擇**: 透過掛載 `infra/init-db/` 目錄到 `/docker-entrypoint-initdb.d/` 執行初始化 SQL。

**替代方案**: 在應用程式啟動時透過 Flyway/Liquibase migration 執行。

**理由**: init script 方式簡單直接，保證 extension 在任何應用程式連線之前就已啟用。後續可再加入 Flyway 管理業務 schema migration。

### Decision 2: main-service 使用 Spring Data JPA + Hibernate
**選擇**: 新增 `spring-boot-starter-data-jpa` 和 `postgresql` driver 依賴。

**替代方案**: 使用 Spring JDBC Template 或 R2DBC。

**理由**: JPA 提供 ORM 映射，減少重複的 SQL 撰寫，且 Spring Boot 社群生態成熟。TimescaleDB 完全相容 PostgreSQL JDBC driver。

### Decision 3: 環境變數統一管理連線資訊
**選擇**: Docker Compose 層使用固定的 environment 變數，application.yml 使用 `${ENV_VAR:default}` 語法引用。

**理由**: 開發環境使用預設值簡化設定，部署時可透過環境變數覆蓋，不需修改設定檔。

### Decision 4: Redis 新增 volume
**選擇**: 為 Redis 新增 named volume `redis_data`。

**理由**: 預設 redis:alpine 不持久化資料，開發時重啟容器會遺失快取。加入 volume 可保留開發階段的快取資料。

## Risks / Trade-offs

- **[風險] 開發環境密碼寫死在 docker-compose.yml** → 可接受，因為僅用於本地開發。Production 環境將透過 K8s secrets 管理。
- **[風險] TimescaleDB extension 安裝失敗** → 使用官方 `timescale/timescaledb` image，extension 已內建，只需 `CREATE EXTENSION IF NOT EXISTS`。
- **[取捨] JPA 對 TimescaleDB hypertable 的支援** → JPA 可正常讀寫 hypertable，但 `create_hypertable()` 等 TimescaleDB 特有函數需透過 native query 執行。
