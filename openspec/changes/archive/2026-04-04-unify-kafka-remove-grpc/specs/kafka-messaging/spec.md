## ADDED Requirements

### Requirement: Kafka topic schema 定義
系統 SHALL 定義以下 Kafka topic 及其 JSON 訊息契約：
- `chat.request`：main-service 發送的聊天請求
- `chat.response`：multiagent-service 回傳的聊天回覆
- `route.request`：路徑規劃請求
- `route.response`：路徑規劃結果
- `traffic.metrics`：YOLO 節點擁塞資料輸出

所有訊息 SHALL 以 UTF-8 JSON 序列化。所有 request 訊息 SHALL 包含 `correlation_id` 欄位（UUID 字串）作為 Kafka message key。

#### Scenario: 聊天請求訊息格式
- **WHEN** main-service 向 `chat.request` 發送訊息
- **THEN** message key SHALL 為 UUID correlation ID，value SHALL 為 JSON，包含 `correlation_id`（string）、`session_id`（string）、`content`（string）

#### Scenario: 聊天回應訊息格式
- **WHEN** multiagent-service 向 `chat.response` 發送訊息
- **THEN** message key SHALL 為原始請求的相同 correlation ID，value SHALL 為 JSON，包含 `correlation_id`（string）、`reply`（string）、`suggested_actions`（string 陣列）

#### Scenario: 路徑請求訊息格式
- **WHEN** main-service 向 `route.request` 發送訊息
- **THEN** message value SHALL 為 JSON，包含 `correlation_id`（string）、`origin`（string，"lat,lng"）、`destination`（string，"lat,lng"）、`preferences`（string，可選）

#### Scenario: 路徑回應訊息格式
- **WHEN** multiagent-service 向 `route.response` 發送訊息
- **THEN** message value SHALL 為 JSON，包含 `correlation_id`（string）、`route_id`（string）、`path`（string）、`estimated_time`（integer，分鐘）

#### Scenario: 交通指標訊息格式
- **WHEN** YOLO 節點向 `traffic.metrics` 發送訊息
- **THEN** message value SHALL 為 JSON，包含 `road_code`（string）、`direction`（string）、`intersection_type`（string）、`longitude`（double）、`latitude`（double）、`avg_vehicle_count`（integer）、`congestion_level`（string）、`confidence`（float）、`timestamp`（ISO-8601 string）

### Requirement: Correlation ID request-response 橋接
main-service SHALL 實作 correlation ID 機制，在非同步 Kafka topic 上橋接同步 HTTP 請求。

#### Scenario: 成功的聊天 request-response 循環
- **WHEN** 客戶端發送 POST `/api/v1/chat/message`，包含 `session_id` 和 `content`
- **THEN** main-service SHALL 產生 UUID correlation ID、向 `chat.request` 發送訊息、等待 `chat.response` 上配對的回應（透過 correlation ID 配對）、並以 HTTP JSON 回傳結果

#### Scenario: 請求超時
- **WHEN** 客戶端發送聊天請求且 30 秒內未收到回應
- **THEN** main-service SHALL 回傳 HTTP 504 Gateway Timeout 及錯誤訊息

#### Scenario: 併發請求
- **WHEN** 多個 HTTP 請求同時在進行中
- **THEN** 每個請求 SHALL 透過其唯一 correlation ID 獨立關聯，且 SHALL NOT 干擾其他等待中的請求

### Requirement: gRPC 移除
系統 SHALL NOT 包含任何 gRPC 依賴、proto 檔案、生成碼或 gRPC server/client 基礎設施。

#### Scenario: agent-service 依賴中無 gRPC
- **WHEN** 檢查 multiagent-service 的 `pyproject.toml`
- **THEN** SHALL 不存在 `grpcio` 或 `grpcio-tools` 依賴

#### Scenario: main-service 依賴中無 gRPC
- **WHEN** 檢查 main-service 的 `build.gradle.kts`
- **THEN** SHALL 不存在 `io.grpc` 或 `com.google.protobuf` 依賴，且無 `protobuf` plugin

#### Scenario: 專案中無 proto 檔案
- **WHEN** 在整個 repository 中搜尋 `.proto` 檔案
- **THEN** SHALL 找到零個結果
