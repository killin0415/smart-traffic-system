## Why

目前的 codebase 同時使用 gRPC 和 Kafka 進行服務間通訊，造成不必要的複雜度 — 兩套通訊協定、proto 檔案管理、程式碼生成流程、以及兩個 service 中重複的依賴。由於 Kafka 基礎設施已經存在，且所有非聊天的通訊本來就規劃走 Kafka，移除 gRPC 並統一使用 Kafka 可以簡化架構、減少依賴、並建立單一的服務間通訊心智模型。在專題只有兩個月、一人開發的條件下，這點尤為關鍵。

## What Changes

- **BREAKING**：移除所有 gRPC 基礎設施（proto 檔案、生成碼、agent-service 的 gRPC server、main-service 的 gRPC client config、兩個 service 中的 gRPC 依賴）
- 將 `agent-service` 重新命名為 `multiagent-service`，與物理架構圖對齊
- 定義所有服務間通訊的 Kafka topic schema：
  - `chat.request` / `chat.response`：Chat Manager 對話（取代 gRPC `ChatService.SendMessage`）
  - `route.request` / `route.response`：Route Agent 路徑規劃
  - `traffic.metrics`：YOLO 擁塞資料輸入
- 在 main-service 實作 correlation ID 模式，用於 Kafka 上的 request-response 橋接（取代同步 gRPC 呼叫）
- 更新 main-service REST controller，改用 Kafka producer/consumer 取代 gRPC stub

## Capabilities

### New Capabilities
- `kafka-messaging`：統一的 Kafka 服務間通訊，包含 topic schema 定義、correlation ID request-response 模式、訊息序列化契約
- `service-structure`：重新命名與重整服務結構（agent-service → multiagent-service），與物理架構圖對齊

### Modified Capabilities
<!-- 無需修改的現有 spec -->

## Impact

- **agent-service（→ multiagent-service）**：移除 `src/grpc_server/`、`src/proto/`、`grpcio`/`grpcio-tools` 依賴。Kafka consumer 接手處理聊天請求，取代 gRPC servicer。
- **main-service**：移除 `GrpcClientConfig.kt`、`build.gradle.kts` 中的 gRPC 依賴、protobuf plugin。`ChatController` 和 `HealthController` 改用 Kafka producer + correlation ID consumer，取代 blocking gRPC stub。
- **proto/**：整個目錄移除。訊息契約改為 Kafka topic 定義中的 JSON schema 文件。
- **infra/docker-compose.yml**：若服務名稱變更則需更新。
