## ADDED Requirements

### Requirement: 服務重新命名為 multiagent-service
Python AI 服務在所有目錄路徑、Docker/compose 服務名稱、log 前綴及文件中 SHALL 命名為 `multiagent-service`。

#### Scenario: 目錄結構
- **WHEN** 列出 `backend/` 目錄
- **THEN** SHALL 包含 `multiagent-service/`，且 SHALL NOT 包含 `agent-service/`

#### Scenario: Docker compose 服務名稱
- **WHEN** 檢查 `infra/docker-compose.yml`
- **THEN** 服務名稱 SHALL 為 `multiagent-service`（若該服務有定義）

#### Scenario: 應用程式中繼資料
- **WHEN** multiagent-service 啟動
- **THEN** FastAPI app title 和 health check 回應 SHALL 引用 `multiagent-service`

### Requirement: multiagent-service 進入點不含 gRPC
multiagent-service 的主要進入點 SHALL 只啟動 FastAPI（HTTP）和 Kafka consumer — 不包含 gRPC server。

#### Scenario: 服務啟動元件
- **WHEN** multiagent-service 透過 `main.py` 啟動
- **THEN** SHALL 啟動 FastAPI HTTP server 和 Kafka consumer 背景任務，且 SHALL NOT 啟動 gRPC server

#### Scenario: Kafka consumer 訂閱的 topic
- **WHEN** multiagent-service 的 Kafka consumer 啟動
- **THEN** SHALL 訂閱 `chat.request`、`route.request` 和 `traffic.metrics` topic

### Requirement: main-service 不含 gRPC client
main-service SHALL 完全透過 Kafka 與 multiagent-service 通訊。SHALL NOT 存在 gRPC channel 或 stub 設定。

#### Scenario: 無 gRPC 設定類別
- **WHEN** 檢查 main-service 的 Spring 設定
- **THEN** SHALL 不存在 `GrpcClientConfig` 或 `ManagedChannel` bean

#### Scenario: application config 不含 gRPC
- **WHEN** 檢查 `application.yml`
- **THEN** SHALL 不存在 `grpc` 設定區段

#### Scenario: ChatController 使用 Kafka
- **WHEN** `ChatController` 處理 POST `/api/v1/chat/message`
- **THEN** SHALL 向 Kafka `chat.request` topic 發送訊息並從 `chat.response` topic 消費，而非呼叫任何 gRPC stub
