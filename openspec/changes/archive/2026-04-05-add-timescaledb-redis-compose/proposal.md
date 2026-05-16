## Why

目前 `infra/docker-compose.yml` 已定義 TimescaleDB 與 Redis 容器，但 main-service 和 multiagent-service 尚未設定任何資料庫或快取連線。服務無法持久化交通資料或使用快取，阻礙後續功能開發（如路線規劃、歷史查詢）。現在需要將 Docker 服務與應用程式層完整串接。

## What Changes

- 為 `infra/docker-compose.yml` 加入 health check、init script 掛載，並確保 TimescaleDB 啟用 TimescaleDB extension
- main-service 新增 Spring Data JPA + TimescaleDB 連線設定及 Redis 連線設定
- multiagent-service 新增 asyncpg/SQLAlchemy + Redis 連線設定
- 建立資料庫初始化 SQL script（啟用 TimescaleDB extension、建立基礎 schema）
- 新增 Redis 快取 volume 以確保資料持久性

## Capabilities

### New Capabilities
- `database-integration`: 定義 TimescaleDB 資料庫連線、初始化、health check 及基礎 schema 的需求規範
- `redis-caching`: 定義 Redis 快取連線設定與基礎快取策略的需求規範

### Modified Capabilities

## Impact

- `infra/docker-compose.yml`：修改 timescaledb 和 redis 服務設定（加入 health check、init script volume）
- `backend/main-service/build.gradle.kts`：新增 Spring Data JPA、PostgreSQL driver、Spring Data Redis 依賴
- `backend/main-service/src/main/resources/application.yml`：新增 datasource 和 redis 設定區段
- `backend/multiagent-service/pyproject.toml`：新增 asyncpg、SQLAlchemy、redis 依賴
- 新增 `infra/init-db/` 目錄放置初始化 SQL script
