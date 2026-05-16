## Context

智慧交通系統目前有兩個後端服務，透過 gRPC（同步聊天）和 Kafka（非同步交通事件）進行通訊。本專案為大學專題，單人開發，時限兩個月。物理架構圖規定所有服務間通訊應完全透過 Kafka。目前 gRPC 涉及兩個 service 中約 15 個檔案（proto 定義、生成碼、client config、controller 橋接）。

現有的 Kafka 基礎設施在兩個 service 中已可運作 — main-service 有 `TrafficEventConsumer`，agent-service 有 `consumer.py`/`producer.py`。

## Goals / Non-Goals

**Goals：**
- 從兩個 service 中移除所有 gRPC 程式碼、依賴和 proto 檔案
- 確立 Kafka 為唯一的服務間通訊管道
- 定義清晰的 topic schema 和訊息契約（JSON）
- 實作 correlation ID 模式，讓 main-service 能透過非同步 Kafka 橋接 HTTP request-response
- 將 `agent-service` 重新命名為 `multiagent-service`，與架構圖一致

**Non-Goals：**
- 實作實際的 agent 邏輯（Route Agent、Explainer Agent、Chat Manager）— 那是獨立的 change
- 新增現有範圍以外的 REST API 端點
- 資料庫建置（TimescaleDB、Redis、ElasticSearch）
- 前端開發
- Kafka Streams 或複雜事件處理

## Decisions

### 1. 使用 Correlation ID 模式實現 Kafka 上的 request-response

**決策**：在 main-service 中使用 `ConcurrentHashMap<String, CompletableFuture<String>>` 將 Kafka 回應關聯回 HTTP 請求。

**流程**：
```
HTTP POST /api/v1/chat/message
  → 產生 correlationId (UUID)
  → 將 CompletableFuture 存入 pendingRequests map
  → Produce 到 "chat.request" topic，correlationId 作為 message key
  → future.get(30, SECONDS) — 阻塞等待回應或超時
  → 回傳 HTTP response

@KafkaListener("chat.response")
  → 從 message key 取出 correlationId
  → Complete 對應的 CompletableFuture
```

**替代方案評估**：
- **WebSocket/SSE**：對 client 端更複雜，聊天不需要串流，會增加前端複雜度
- **Client 端輪詢**：需要 client 端輪詢邏輯和狀態查詢端點，整體程式碼更多
- **Spring ReplyingKafkaTemplate**：Spring Kafka 內建 request-reply 支援。但手動 `ConcurrentHashMap` 更易理解和除錯，適合學習型專案。若時間允許可改用

### 2. JSON 訊息契約（不使用 Schema Registry）

**決策**：使用純 JSON 搭配文件化的欄位契約。不使用 Avro、Protobuf 或 Schema Registry。

**理由**：這是單人開發的專題。Schema 演進和向後相容不是問題。JSON 可直接在 Kafka console consumer 中閱讀，方便除錯。

### 3. Topic 命名慣例

**決策**：使用 `<domain>.<action>` 模式：
- `chat.request` / `chat.response`
- `route.request` / `route.response`
- `traffic.metrics`（YOLO → 系統）
- `traffic.alerts`（系統 → mobile 推播，現有的 `traffic-alerts` 改名）

**理由**：點號分隔是 Kafka 慣例，允許依前綴設定 topic 層級 ACL。

**備註**：`TrafficEventConsumer.kt` 中現有的 `traffic-alerts` 和 `route-results` topic 會改名以符合此慣例。

### 4. 服務重新命名：agent-service → multiagent-service

**決策**：重新命名目錄和所有引用，更新 docker-compose 服務名稱。

**理由**：與物理架構圖一致，反映該服務承載多個專業 agent（Route、Explainer、Chat Manager），而非單一通用 agent。

## Risks / Trade-offs

- **[增加延遲]** Kafka 相較 gRPC 直接呼叫增加約 10-100ms → 可接受，因為 LLM 推論將主導延遲（數秒級），使用者不會感知差異
- **[超時處理]** 若 multiagent-service 下線，HTTP 請求會等到 30 秒超時 → 緩解方案：`CompletableFuture.get()` 超時確保請求不會無限期懸掛。Health 端點可檢查 Kafka consumer lag
- **[訊息排序]** Kafka 保證單一 partition 內的排序，但使用多 partition 時回應可能亂序到達 → 緩解方案：correlation ID 配對不依賴排序，partition 數量不影響正確性
- **[訊息遺失]** 若 multiagent-service 在處理中崩潰，該請求會遺失 → 專題可接受。正式環境需要 dead letter queue 和重試機制
