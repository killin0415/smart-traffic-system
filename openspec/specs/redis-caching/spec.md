## ADDED Requirements

### Requirement: Redis Docker 服務設定 health check 與持久化
Docker Compose 中的 redis 服務 SHALL 定義 health check 並掛載 volume 持久化資料。

#### Scenario: Health check 成功
- **WHEN** redis 容器啟動完成
- **THEN** health check SHALL 使用 `redis-cli ping` 驗證 Redis 可接受連線

#### Scenario: 資料持久化
- **WHEN** 檢查 docker-compose.yml 的 redis 服務
- **THEN** SHALL 掛載 named volume `redis_data` 到 `/data`

### Requirement: main-service Redis 連線設定
main-service SHALL 能透過 Spring Data Redis 連線至 Redis。

#### Scenario: application.yml redis 設定
- **WHEN** 檢查 main-service 的 `application.yml`
- **THEN** SHALL 包含 `spring.data.redis` 設定區段，指定 host 和 port

#### Scenario: Redis 依賴存在
- **WHEN** 檢查 `build.gradle.kts`
- **THEN** SHALL 包含 `spring-boot-starter-data-redis` 依賴

### Requirement: multiagent-service Redis 連線設定
multiagent-service SHALL 能透過 redis-py 連線至 Redis。

#### Scenario: Redis 連線設定模組
- **WHEN** multiagent-service 啟動
- **THEN** SHALL 從環境變數讀取 `REDIS_URL`，並建立 Redis async client 連線

#### Scenario: 預設連線字串
- **WHEN** `REDIS_URL` 環境變數未設定
- **THEN** SHALL 使用預設值 `redis://localhost:6379`
