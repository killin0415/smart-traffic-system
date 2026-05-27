## Purpose

Defines the Kafka topic schemas, JSON message contracts, and the correlation ID request-response bridging pattern used for all inter-service communication.
## Requirements
### Requirement: Kafka topic schema 定義
系統 SHALL 定義以下 Kafka topic 及其 JSON 訊息契約：
- `chat.request`：main-service 發送的聊天請求
- `chat.response`：multiagent-service 回傳的聊天回覆
- `route.request`：路徑規劃請求
- `route.response`：路徑規劃結果
- `geocode.request`：地址查詢請求
- `geocode.response`：地址查詢結果

所有訊息 SHALL 以 UTF-8 JSON 序列化。所有 request 訊息 SHALL 包含 `correlation_id` 欄位（UUID 字串）作為 Kafka message key。

#### Scenario: 聊天請求訊息格式
- **WHEN** main-service 向 `chat.request` 發送訊息
- **THEN** message key SHALL 為 UUID correlation ID，value SHALL 為 JSON，包含 `correlation_id`（string）、`session_id`（string）、`content`（string）

#### Scenario: 聊天回應訊息格式
- **WHEN** multiagent-service 向 `chat.response` 發送訊息
- **THEN** message key SHALL 為原始請求的相同 correlation ID，value SHALL 為 JSON，包含：
  - `correlation_id`（string，必填）
  - `reply`（string，必填）— DeepSeek chat agent 回覆的自然語言訊息
  - `suggested_actions`（string 陣列，必填）— 即使空陣列也要存在
  - `route_payload`（object，可選）— 當 agent 判斷使用者表達路線意圖時帶入；其 schema 為 `{ "routes": [...], "error"?: string }`，`routes` 元素結構同 `route.response.routes`，**不包含** `correlation_id`（chat.response 自身已帶）；無路線意圖時 SHALL 為 `null` 或省略此欄位
- **AND** 若 `route_payload` 缺失或為 `null`，下游消費者（main-service）SHALL 視為「純文字回覆」處理

#### Scenario: 路徑請求訊息格式
- **WHEN** main-service 向 `route.request` 發送訊息
- **THEN** message value SHALL 為 JSON，包含 `correlation_id`（string，必填）、`origin_lat`（float，必填）、`origin_lng`（float，必填）、`dest_lat`（float，必填）、`dest_lng`（float，必填）、`top_k`（integer，可選，預設 3）

#### Scenario: 路徑回應訊息格式
- **WHEN** multiagent-service 向 `route.response` 發送訊息
- **THEN** message value SHALL 為 JSON，至少包含：
  - `correlation_id`（string，必填）
  - `routes`（array，必填，可為空陣列）— 每個元素為 `{ path: int[], edges: int[], road_names: string[], estimated_time_min: float, distance_km: float, speed_cameras: object[], parking_suggestions: object[] }`，其中 `speed_cameras` 元素為 `{ latitude: float, longitude: float, direction?: string, speed_limit: int, address?: string }`，`parking_suggestions` 元素為 `{ id: int, name?: string, address?: string, available_car: int, distance_m: float }`
  - `error`（string，可選）— 失敗時帶入錯誤描述（例如 `"could not snap origin/destination to graph"` / `"no path found between origin and destination"` / `"service not ready: graph/runtime uninitialised"`）
- **AND** 成功時 `error` SHALL 不存在或為 `null`；失敗時 `routes` SHALL 為空陣列

#### Scenario: Geocode 請求訊息格式
- **WHEN** main-service 向 `geocode.request` 發送訊息
- **THEN** message key SHALL 為 UUID correlation ID，value SHALL 為 JSON，包含：
  - `correlation_id`（string，必填）
  - `query`（string，必填）— 使用者輸入的原始字串
  - `city_hint`（string，可選）— 例如 `"台北"`；缺失或 null 時 multiagent SHALL 不附加任何 city 後綴
  - `limit`（integer，可選，預設 5、上限 10）

#### Scenario: Geocode 回應訊息格式
- **WHEN** multiagent-service 向 `geocode.response` 發送訊息
- **THEN** message key SHALL 為原始請求的相同 correlation ID，value SHALL 為 JSON，至少包含：
  - `correlation_id`（string，必填）
  - `results`（array，必填，可為空陣列）— 每個元素為 `{ latitude: float, longitude: float, display_name: string }`
  - `error`（string，可選）— 例如 `"upstream nominatim error"`、`"rate limited"`
- **AND** 成功時 `error` SHALL 不存在或為 `null`；上游錯誤時 `results` SHALL 為空陣列

### Requirement: Topic naming and pattern conventions
為了讓未來其他微服務能穩定接入，系統 SHALL 對 Kafka topic 命名與互動 pattern 採取統一約定。

#### Scenario: Topic 命名格式
- **WHEN** 任何服務新增一個 Kafka topic
- **THEN** topic 名稱 SHALL 遵循 `<domain>.<verb>` 格式（例如 `chat.request`、`route.response`、`parking.alert`、`incident.event`）
- **AND** `domain` SHALL 為單一語意領域、`verb` SHALL 表達訊息意圖（`request` / `response` / `event` / `alert` 等）

#### Scenario: Request/response pattern
- **WHEN** topic 命名為 `<domain>.request` 或 `<domain>.response`
- **THEN** 訊息 value SHALL 必填 `correlation_id`（UUID string）；request 與其對應 response SHALL 共用同一個 `correlation_id`
- **AND** request topic 與 response topic SHALL 成對存在（每個 `<domain>.request` 都對應一個 `<domain>.response`）
- **AND** 呼叫端 SHALL 假定 producer→consumer→producer 的同步等待語意（caller 等回覆，主流程 timeout 為 30 秒）

#### Scenario: Event pattern (fire-and-forget)
- **WHEN** topic 命名為 `<domain>.event`、`<domain>.alert`、`<domain>.metrics` 或其他非 request/response 動詞
- **THEN** producer SHALL NOT 期待對應 response topic 回覆
- **AND** 訊息 value MAY 帶 `correlation_id`（用於分散式 tracing），但非必填
- **AND** consumer 端 SHALL 假定可能收到重複訊息（at-least-once）並自行做 idempotent 處理

#### Scenario: Envelope 約定
- **WHEN** 任何 topic 上的訊息
- **THEN** value SHALL 為 UTF-8 JSON
- **AND** SHALL 至少包含 schema 中定義的欄位
- **AND** 額外欄位 SHALL 被消費端忽略（forward-compatible）

### Requirement: Inbound topic registry extensibility
multiagent-service 的 Kafka consumer SHALL 採用 registry pattern 註冊 topic handler，且訂閱清單 SHALL 可由環境變數設定，讓新微服務能在不改 dispatcher 結構的前提下接入。

#### Scenario: 訂閱清單由 env var 控制
- **WHEN** multiagent-service 啟動 Kafka consumer
- **THEN** SHALL 讀取環境變數 `KAFKA_SUBSCRIBE_TOPICS`（comma-separated topic 名稱），訂閱該清單上的 topics
- **AND** 若 `KAFKA_SUBSCRIBE_TOPICS` 未設定、為空字串、或全為空白，SHALL 使用預設清單 `chat.request,route.request,geocode.request`
- **AND** 個別 topic 名稱前後空白 SHALL 被 trim 掉；逗號分隔後產生的空 token SHALL 被忽略

#### Scenario: Handler registry 註冊
- **WHEN** 程式碼新增一個 topic handler
- **THEN** SHALL 在 `TOPIC_HANDLERS` dict（key=topic 名稱、value=handler callable）登錄一筆對應，無需修改 dispatcher 主迴圈

#### Scenario: 已訂閱但無 handler 的 topic
- **WHEN** consumer 收到 `KAFKA_SUBSCRIBE_TOPICS` 內、但 `TOPIC_HANDLERS` 找不到對應 handler 的訊息
- **THEN** SHALL log WARN（含 topic 名稱與 message key）並跳過該訊息，不 crash 整個 consumer

#### Scenario: 訊息 JSON 解析失敗
- **WHEN** 一筆訊息 value 無法解析為 JSON
- **THEN** SHALL log ERROR（含原始 bytes 摘要）並跳過該訊息，不影響後續訊息消費

#### Scenario: Handler 拋例外
- **WHEN** 任何 handler 在處理訊息時拋出未捕捉例外
- **THEN** SHALL log ERROR（含 stack trace、topic、key）並跳過該訊息，不影響 consumer loop 繼續運轉

### Requirement: Correlation ID request-response 橋接
main-service SHALL 實作 correlation ID 機制，在非同步 Kafka topic 上橋接同步 HTTP 請求。本機制 SHALL 適用於所有 `<domain>.request` ↔ `<domain>.response` topic 對（chat / route / geocode 等）。

#### Scenario: 成功的 request-response 循環
- **WHEN** 客戶端發送 `POST /api/v1/chat/message`、`POST /api/v1/route`、`GET /api/v1/geocode` 任一端點
- **THEN** main-service SHALL 產生 UUID correlation ID、向對應 `<domain>.request` topic 發送訊息、等待對應 `<domain>.response` 上配對的回應（透過 correlation ID 配對）、並以 HTTP JSON 回傳結果

#### Scenario: 請求超時
- **WHEN** 客戶端發送任一 request 端點且 30 秒內未收到對應 response
- **THEN** main-service SHALL 回傳 HTTP 504 Gateway Timeout 及錯誤訊息

#### Scenario: 併發請求
- **WHEN** 多個 HTTP 請求同時在進行中（跨 chat / route / geocode）
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

### Requirement: Configurable multiagent Kafka producer broker
multiagent-service 的 Kafka response producer SHALL 使用 `KAFKA_BOOTSTRAP_SERVERS` 作為 broker address 設定來源，並在未設定時預設為 `localhost:9092`。

#### Scenario: Docker broker override is honored
- **WHEN** multiagent-service 啟動時環境變數 `KAFKA_BOOTSTRAP_SERVERS` 設為 `kafka:29092`
- **THEN** Kafka producer SHALL 使用 `kafka:29092` 建立 producer client
- **AND** `chat.response`、`route.response`、`geocode.response` SHALL publish 到該 broker

#### Scenario: Local development default is preserved
- **WHEN** `KAFKA_BOOTSTRAP_SERVERS` 未設定
- **THEN** Kafka producer SHALL 使用 `localhost:9092` 作為預設 broker

#### Scenario: Consumer and producer share broker contract
- **WHEN** multiagent-service 同時 consume request topic 並 publish response topic
- **THEN** consumer 與 producer SHALL 讀取相同的 `KAFKA_BOOTSTRAP_SERVERS` contract
- **AND** response publishing path SHALL NOT hard-code a different broker address

