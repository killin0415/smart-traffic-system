## 1. Docker Compose 基礎設施

- [x] 1.1 建立 `infra/init-db/01-init-timescaledb.sql`，包含 `CREATE EXTENSION IF NOT EXISTS timescaledb`
- [x] 1.2 修改 `infra/docker-compose.yml` 的 timescaledb 服務：掛載 init-db volume、新增 health check（`pg_isready`）
- [x] 1.3 修改 `infra/docker-compose.yml` 的 redis 服務：新增 health check（`redis-cli ping`）、掛載 `redis_data` volume
- [x] 1.4 在 `volumes` 區段新增 `redis_data` named volume

## 2. main-service 資料庫整合

- [x] 2.1 在 `build.gradle.kts` 新增 `spring-boot-starter-data-jpa` 和 `org.postgresql:postgresql` 依賴
- [x] 2.2 在 `application.yml` 新增 `spring.datasource` 設定（JDBC URL、帳號、密碼）
- [x] 2.3 在 `application.yml` 新增 `spring.jpa` 設定（hibernate ddl-auto、PostgreSQL dialect）

## 3. main-service Redis 整合

- [x] 3.1 在 `build.gradle.kts` 新增 `spring-boot-starter-data-redis` 依賴
- [x] 3.2 在 `application.yml` 新增 `spring.data.redis` 設定（host、port）

## 4. multiagent-service 資料庫整合

- [x] 4.1 建立 `backend/multiagent-service/src/db/` 模組，包含 SQLAlchemy async engine 設定
- [x] 4.2 從環境變數 `DATABASE_URL` 讀取連線字串，設定預設值為 `postgresql+asyncpg://admin:secret@localhost:5432/traffic_data`

## 5. multiagent-service Redis 整合

- [x] 5.1 建立 `backend/multiagent-service/src/cache/` 模組，包含 Redis async client 設定
- [x] 5.2 從環境變數 `REDIS_URL` 讀取連線字串，設定預設值為 `redis://localhost:6379`

## 6. 驗證

- [x] 6.1 執行 `docker compose up` 確認所有服務啟動且 health check 通過
- [x] 6.2 驗證 main-service 可成功連線 TimescaleDB 和 Redis
- [x] 6.3 驗證 multiagent-service 可成功連線 TimescaleDB 和 Redis
