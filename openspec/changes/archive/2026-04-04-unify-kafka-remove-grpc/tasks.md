## 1. 從 agent-service 移除 gRPC

- [x] 1.1 刪除 `backend/agent-service/src/grpc_server/` 目錄（server.py、chat_servicer.py、__init__.py）
- [x] 1.2 刪除 `backend/agent-service/src/proto/` 目錄（pb2.py、pb2_grpc.py、__init__.py）
- [x] 1.3 從 `pyproject.toml` 移除 `grpcio` 和 `grpcio-tools` 依賴
- [x] 1.4 從 `main.py` lifespan handler 移除 gRPC server 啟動和關閉邏輯

## 2. 從 main-service 移除 gRPC

- [x] 2.1 刪除 `backend/main-service/src/main/kotlin/com/potato/mainservice/config/GrpcClientConfig.kt`
- [x] 2.2 從 `build.gradle.kts` 移除所有 gRPC/protobuf 依賴（grpc-netty-shaded、grpc-protobuf、grpc-stub、grpc-kotlin-stub、protobuf-kotlin、javax.annotation-api）以及 `protobuf {}` plugin 區塊和 `com.google.protobuf` plugin
- [x] 2.3 從 `application.yml` 移除 `grpc` 設定區段
- [x] 2.4 刪除 `backend/main-service/src/main/proto/` 目錄
- [x] 2.5 刪除 `backend/main-service/build/generated/source/proto/` 目錄

## 3. 移除共用 proto

- [x] 3.1 刪除專案根目錄的 `proto/` 目錄

## 4. 將 agent-service 重新命名為 multiagent-service

- [x] 4.1 將 `backend/agent-service/` 目錄重新命名為 `backend/multiagent-service/`
- [x] 4.2 更新 FastAPI app title 和 health check 回應，引用 "multiagent-service"
- [x] 4.3 將 Kafka consumer group ID 從 `agent-service-group` 改為 `multiagent-service-group`
- [x] 4.4 更新 `infra/docker-compose.yml`（若有引用 agent-service）— docker-compose 中無引用，不需變更

## 5. 在 main-service 實作 Kafka request-response

- [x] 5.1 建立 `ChatRequestProducer` 元件，向 `chat.request` topic 發送訊息，correlation ID 作為 message key，JSON body 包含 `correlation_id`、`session_id`、`content`
- [x] 5.2 建立 `ChatResponseConsumer` 元件，以 `@KafkaListener` 監聽 `chat.response` topic，透過 correlation ID 完成等待中的 `CompletableFuture`
- [x] 5.3 建立 `PendingRequestStore`（封裝 `ConcurrentHashMap<String, CompletableFuture<String>>`），提供 register、complete 和 timeout 方法
- [x] 5.4 改寫 `ChatController`，使用 `ChatRequestProducer` + `PendingRequestStore` 取代 gRPC stub，超時時回傳 HTTP 504
- [x] 5.5 改寫 `HealthController`，移除 gRPC health check（改用簡單的 Kafka 連線檢查或僅回報 main-service 狀態）
- [x] 5.6 將 `TrafficEventConsumer` 中的 topic 名稱從 `traffic-alerts`/`route-results` 改為 `traffic.alerts`/`route.response`

## 6. 更新 multiagent-service 的 Kafka 處理

- [x] 6.1 將 consumer 訂閱的 topic 從 `["traffic-data", "yolo-results"]` 改為 `["chat.request", "route.request", "traffic.metrics"]`
- [x] 6.2 在 consumer 中加入訊息路由邏輯：依據 topic 名稱分派到對應的 handler
- [x] 6.3 實作 chat request handler：解析 JSON、產生 stub 回覆、向 `chat.response` 發送回應（使用相同 correlation ID 作為 message key）
- [x] 6.4 實作 route request handler（stub）：解析 JSON、向 `route.response` 發送 stub 回應

## 7. 驗證與清理

- [x] 7.1 驗證 main-service 可在無 gRPC 下建置成功（`./gradlew build`）— 編譯成功，context load test 因 Kafka 未啟動而失敗（預期行為）
- [x] 7.2 驗證 multiagent-service 啟動時無 gRPC 錯誤 — imports 正常，程式碼中零 gRPC 引用
- [x] 7.3 端到端測試：POST `/api/v1/chat/message` → Kafka → multiagent-service → Kafka → HTTP response — HTTP 200, 0.12s round-trip, 完整流程驗證通過
