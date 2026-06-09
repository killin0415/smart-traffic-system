# kafka-communication-impl

> 這份文件記錄了 `unify-kafka-remove-grpc` 這個 change 的實作細節，  
> 目的是幫助理解「為什麼這樣做」以及「每個元件怎麼串起來的」。

---

## 1. 為什麼要做這個 Change？

原本專案有兩套服務間通訊機制：
- **gRPC**：main-service 直接呼叫 agent-service 的 RPC method
- **Kafka**：用於事件驅動的非同步通訊

問題：
- 單人開發維護兩套通訊協議成本太高（proto 檔、code generation、兩邊 stub）
- Kafka 已經能滿足所有需求（包含 request-response 模式）
- gRPC 的優勢（低延遲、streaming）在這個專案中不是剛需

決定：**全部統一用 Kafka，移除 gRPC。**

---

## 2. 移除 gRPC 的步驟

### 2.1 Python 端（原 agent-service）

刪除的東西：
```
backend/agent-service/src/grpc_server/   ← gRPC server 實作
backend/agent-service/src/proto/         ← protobuf 生成碼
```

從 `pyproject.toml` 移除：
```toml
grpcio = "..."
grpcio-tools = "..."
```

從 `main.py` 移除 gRPC server 的啟動/關閉邏輯。

### 2.2 Kotlin 端（main-service）

刪除的東西：
```
config/GrpcClientConfig.kt              ← gRPC client 設定
src/main/proto/                          ← .proto 檔案
build/generated/source/proto/            ← 生成碼
proto/                                   ← 專案根目錄的共用 proto
```

從 `build.gradle.kts` 移除：
```kotlin
// 這些全部刪掉
id("com.google.protobuf")                         // plugin
implementation("io.grpc:grpc-netty-shaded")        // 依賴
implementation("io.grpc:grpc-protobuf")
implementation("io.grpc:grpc-stub")
implementation("io.grpc:grpc-kotlin-stub")
implementation("com.google.protobuf:protobuf-kotlin")
protobuf { ... }                                    // protobuf plugin 設定區塊
```

從 `application.yml` 移除 `grpc` 設定區段。

### 學到的事

- 移除依賴時要從 **plugin → dependency → config → generated code → source code** 全部清理
- 先刪 proto 生成碼再刪 proto 檔，否則 build 會嘗試重新生成

---

## 3. 重新命名 agent-service → multiagent-service

為什麼改名：後續要實作多 Agent 架構（Chat Manager → Route Agent + Explainer Agent），名稱應該反映這點。

改動：
1. 資料夾 `backend/agent-service/` → `backend/multiagent-service/`
2. FastAPI app title 和 health check 回應改為 `"multiagent-service"`
3. Kafka consumer group ID：`agent-service-group` → `multiagent-service-group`

---

## 4. Kafka Request-Response 模式（核心設計）

這是這個 change 最重要的部分。用 Kafka 實作 **同步的 request-response**，取代 gRPC 的直接呼叫。

### 4.1 整體資料流

```
[Client]                    [main-service]                    [Kafka]                    [multiagent-service]
   |                              |                              |                              |
   | POST /api/v1/chat/message    |                              |                              |
   |----------------------------->|                              |                              |
   |                              |  1. 產生 correlationId (UUID) |                              |
   |                              |  2. register(correlationId)   |                              |
   |                              |     → 放入 ConcurrentHashMap  |                              |
   |                              |  3. produce 到 chat.request   |                              |
   |                              |------------------------------>|                              |
   |                              |                              |  4. consume from chat.request |
   |                              |                              |----------------------------->|
   |                              |                              |                              |
   |                              |                              |  5. 處理 + produce chat.response
   |                              |                              |<-----------------------------|
   |                              |  6. consume from chat.response|                              |
   |                              |<------------------------------|                              |
   |                              |  7. complete(correlationId)   |                              |
   |                              |     → CompletableFuture 完成  |                              |
   |                              |  8. await() 解除阻塞，回傳    |                              |
   |  HTTP 200 JSON response      |                              |                              |
   |<-----------------------------|                              |                              |
```

### 4.2 Correlation ID 機制

**問題**：Kafka 是非同步的，但 HTTP 是同步的。怎麼讓 HTTP handler 等到 Kafka 的回應？

**解法**：Correlation ID + CompletableFuture

```
                    PendingRequestStore
                    (ConcurrentHashMap)
                 ┌──────────────────────┐
  register() →   │ "uuid-abc" → Future  │  ← complete()
                 └──────────────────────┘
                         ↑
                    await() 阻塞等待
```

1. 每個 HTTP request 產生一個 UUID 作為 `correlationId`
2. `register()` 把 `correlationId → CompletableFuture` 放進 ConcurrentHashMap
3. 發送 Kafka 訊息時，correlationId 同時作為 **message key** 和 **payload 欄位**
4. multiagent-service 回覆時，用同一個 correlationId 作為 response 的 key
5. `ChatResponseConsumer` 收到回覆，用 key 找到對應的 Future，呼叫 `complete()`
6. `await()` 解除阻塞，HTTP handler 回傳結果

### 4.3 main-service 各元件的職責

#### `ChatController.kt` — HTTP 入口

```kotlin
@PostMapping("/message")
fun sendMessage(@RequestBody request: ChatMessageRequest): ResponseEntity<Any> {
    val correlationId = UUID.randomUUID().toString()

    // 1. 註冊等待
    pendingRequestStore.register(correlationId)
    // 2. 發送到 Kafka
    chatRequestProducer.send(correlationId, request.session_id, request.content)

    return try {
        // 3. 阻塞等待回應（最多 30 秒）
        val responseJson: String = pendingRequestStore.await(correlationId, 30)
        // 4. 解析並回傳
        val responseMap = objectMapper.readTree(responseJson)
        val response = ChatMessageResponse(
            reply = responseMap["reply"]?.asText() ?: "",
            suggested_actions = responseMap["suggested_actions"]?.map { it.asText() } ?: emptyList(),
        )
        ResponseEntity.ok(response)
    } catch (e: TimeoutException) {
        // 5. 超時回 504
        ResponseEntity.status(HttpStatus.GATEWAY_TIMEOUT)
            .body(mapOf("error" to "Multiagent service did not respond within 30 seconds"))
    }
}
```

**重點**：`register()` 一定要在 `send()` 之前，否則會有 race condition（回應可能在註冊前就到了）。

#### `ChatRequestProducer.kt` — 發送 Kafka 訊息

```kotlin
fun send(correlationId: String, sessionId: String, content: String) {
    val payload = objectMapper.writeValueAsString(
        mapOf(
            "correlation_id" to correlationId,
            "session_id" to sessionId,
            "content" to content,
        )
    )
    // correlationId 同時作為 Kafka message key
    kafkaTemplate.send(TOPIC, correlationId, payload)
}
```

**為什麼 correlationId 要同時放在 key 和 body？**
- **key**：讓 consumer 不需要反序列化 body 就能快速配對
- **body**：讓 multiagent-service 解析 JSON 時也能取得，不依賴 Kafka header

#### `PendingRequestStore.kt` — 橋接同步與非同步

```kotlin
@Component
class PendingRequestStore {
    private val pending = ConcurrentHashMap<String, CompletableFuture<String>>()

    fun register(correlationId: String): CompletableFuture<String> {
        val future = CompletableFuture<String>()
        pending[correlationId] = future
        return future
    }

    fun complete(correlationId: String, response: String): Boolean {
        val future = pending.remove(correlationId)  // remove + get 是原子操作
        return if (future != null) {
            future.complete(response)  // 解除 await() 的阻塞
            true
        } else {
            false
        }
    }

    fun await(correlationId: String, timeoutSeconds: Long = 30): String {
        val future = pending[correlationId]
            ?: throw IllegalStateException("No pending request for correlationId: $correlationId")
        return try {
            future.get(timeoutSeconds, TimeUnit.SECONDS)  // 阻塞直到 complete() 或超時
        } catch (e: TimeoutException) {
            pending.remove(correlationId)  // 超時也要清理，避免記憶體洩漏
            throw e
        }
    }
}
```

**關鍵概念**：
- `ConcurrentHashMap` — thread-safe 的 HashMap，因為 register 和 complete 在不同 thread
- `CompletableFuture` — Java 的非同步原語，`get()` 會阻塞直到 `complete()` 被呼叫
- `remove()` 回傳被移除的值 — 一行就能做到「取出 + 刪除 + 檢查是否存在」

#### `ChatResponseConsumer.kt` — 消費 Kafka 回應

```kotlin
@KafkaListener(topics = ["chat.response"], groupId = "main-service-group")
fun onChatResponse(record: ConsumerRecord<String, String>) {
    val correlationId = record.key() ?: return  // key 為 null 就跳過
    pendingRequestStore.complete(correlationId, record.value())
}
```

`@KafkaListener` 由 Spring Kafka 管理，自動在背景 thread 中輪詢 Kafka。

### 4.4 multiagent-service 各元件的職責

#### `consumer.py` — Kafka Consumer（Thread-based）

```python
# 為什麼用 threading 而不是 asyncio？
# confluent_kafka 的 C library 在 asyncio 的 run_in_executor 中會 segfault
# 所以用 dedicated thread + threading.Event 來控制生命週期

def _consumer_loop():
    consumer = Consumer(KAFKA_CONFIG)
    consumer.subscribe(TOPICS)  # ["chat.request", "route.request", "traffic.metrics"]

    while not _stop_event.is_set():
        msg = consumer.poll(1.0)  # 每秒輪詢一次
        if msg is None or msg.error():
            continue

        value = json.loads(msg.value().decode("utf-8"))
        key = msg.key().decode("utf-8") if msg.key() else ""
        topic = msg.topic()

        # 根據 topic 分派到對應 handler
        handler = TOPIC_HANDLERS.get(topic)
        if handler:
            handler(key, value)
```

**Topic → Handler 對應**：
```python
TOPIC_HANDLERS = {
    "chat.request":     handle_chat_request,
    "route.request":    handle_route_request,
    "traffic.metrics":  handle_traffic_metrics,
}
```

#### `handle_chat_request()` — 處理聊天請求

```python
def handle_chat_request(key: str, data: dict):
    correlation_id = data.get("correlation_id", key)  # 優先從 body 取，fallback 用 key

    reply = f"[Multiagent Service] 收到您的訊息: '{content}'. AI 推論功能開發中..."

    publish_message(
        topic="chat.response",
        key=correlation_id,        # 重要：回覆時用同一個 correlation_id 作為 key
        value={
            "correlation_id": correlation_id,
            "reply": reply,
            "suggested_actions": ["查看即時路況", "規劃路線", "查詢停車位"],
        },
    )
```

#### `producer.py` — Kafka Producer（Singleton）

```python
_producer: Producer | None = None  # 全域 singleton

def publish_message(topic: str, key: str, value: dict):
    producer = get_producer()
    producer.produce(
        topic=topic,
        key=key.encode("utf-8"),
        value=json.dumps(value).encode("utf-8"),
        callback=delivery_report,
    )
    producer.flush()  # 確保訊息立即送出（不要留在 buffer）
```

### 4.5 `KafkaConfig.kt` — 為什麼要手動定義 Bean？

Spring Boot 通常會根據 `application.yml` 自動設定 Kafka，但在 **Spring Boot 4.0** 中自動設定似乎不生效。所以手動定義：

```kotlin
@EnableKafka        // 啟用 @KafkaListener 註解掃描
@Configuration
class KafkaConfig {

    // Producer
    @Bean
    fun kafkaTemplate(): KafkaTemplate<String, String> { ... }

    // Consumer
    @Bean
    fun consumerFactory(): DefaultKafkaConsumerFactory<String, String> { ... }

    // 將 consumerFactory 交給 Spring Kafka 的 Listener Container
    @Bean
    fun kafkaListenerContainerFactory(
        consumerFactory: DefaultKafkaConsumerFactory<String, String>,
    ): ConcurrentKafkaListenerContainerFactory<String, String> {
        val factory = ConcurrentKafkaListenerContainerFactory<String, String>()
        factory.setConsumerFactory(consumerFactory)
        return factory
    }

    @Bean
    fun objectMapper(): ObjectMapper = jacksonObjectMapper()
}
```

**三個 Bean 的關係**：
```
kafkaListenerContainerFactory
    └── consumerFactory (建立 Kafka Consumer 實例)
            └── ConsumerConfig (bootstrap servers, deserializers, group ID...)

@KafkaListener 預設找名為 "kafkaListenerContainerFactory" 的 bean
```

---

## 5. Kafka Topic 設計

| Topic | 方向 | Key | Value |
|---|---|---|---|
| `chat.request` | main → multiagent | correlationId | `{correlation_id, session_id, content}` |
| `chat.response` | multiagent → main | correlationId | `{correlation_id, reply, suggested_actions}` |
| `route.request` | main → multiagent | correlationId | `{correlation_id, origin, destination}` |
| `route.response` | multiagent → main | correlationId | `{correlation_id, route_id, path, estimated_time}` |
| `traffic.metrics` | YOLO → multiagent | — | 車流資料 |
| `traffic.alerts` | system → main | — | 推播告警 |

**命名慣例**：用 `.` 分隔（Kafka 慣例），不用 `-`。

---

## 6. 端到端測試（Task 7.3）— 已通過

### 測試結果

```
POST /api/v1/chat/message → main-service produce 到 chat.request ✓
multiagent-service consume from chat.request ✓
multiagent-service produce 到 chat.response ✓
main-service ChatResponseConsumer 收到回應 ✓
CompletableFuture complete → HTTP 200 ✓
Round-trip 時間：0.12 秒
```

### 測試方式

```bash
# 1. 確保 Kafka 在跑
docker compose up -d kafka

# 2. 啟動 multiagent-service
cd backend/multiagent-service && uv run python main.py

# 3. 啟動 main-service
cd backend/main-service && ./gradlew bootRun

# 4. 發送測試請求（注意：Git Bash 的 curl 對中文有 UTF-8 編碼問題，建議用 ASCII 測試）
curl -X POST http://localhost:8081/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -d '{"session_id":"test-session","content":"hello traffic"}'

# 5. 預期回應：
# HTTP 200
# {"reply":"[Multiagent Service] 收到您的訊息: 'hello traffic'. AI 推論功能開發中...",
#  "suggested_actions":["查看即時路況","規劃路線","查詢停車位"]}
```

### Debug Log（排查用）

在 `ChatResponseConsumer` 和 `PendingRequestStore` 有 `SLF4J` log：
- `register()` 時印出 correlationId 和 pending map 大小
- `onChatResponse()` 時印出收到的 key 和 value
- `complete()` 後印出是否成功配對（`matched=true/false`）

### 之前遇到的問題與排查方向

如果遇到 HTTP 504 timeout，依序檢查：

1. **`record.key()` 為 null** — ChatResponseConsumer 第 16 行 `record.key() ?: return` 會 silent return。確認 Python producer 的 key 有正確送出
2. **Correlation ID 不匹配** — `pending.remove(correlationId)` 回傳 null 代表 map 裡找不到。確認 register 在 send 之前
3. **Consumer group 搶 partition** — 如果 Spring 自動設定也建了 consumer，可能跟手動設定搶 partition。檢查是否有重複的 consumer group

---

## 7. 重要觀念總結

| 觀念 | 說明 |
|---|---|
| **Correlation ID** | 在非同步系統中追蹤 request-response 配對的唯一識別碼 |
| **CompletableFuture** | Java 的 Promise 等價物，`get()` 阻塞、`complete()` 解除阻塞 |
| **ConcurrentHashMap** | Thread-safe HashMap，多個 thread 可以安全讀寫 |
| **@KafkaListener** | Spring Kafka 的註解，自動在背景 thread 消費 Kafka 訊息 |
| **confluent_kafka** | Python 的 Kafka client（C-based），效能好但不能跟 asyncio 混用 |
| **Message Key** | Kafka 訊息的 key，決定 partition 分配，也用於業務邏輯配對 |
| **Consumer Group** | 同一 group 內的 consumer 共享 partition，保證每條訊息只被消費一次 |

---

## 8. Unit Tests

為確保所有 service 和 endpoint 正常運作，兩個服務都加上了 unit test。

### 8.1 main-service（Kotlin / JUnit 5 + Mockito）

新增依賴：`build.gradle.kts` 加入 `org.mockito.kotlin:mockito-kotlin:5.4.0`

測試檔案位於 `backend/main-service/src/test/kotlin/com/potato/mainservice/`：

| 測試類別 | 測試數 | 涵蓋內容 |
|---|---|---|
| `kafka/PendingRequestStoreTest.kt` | 7 | register、complete、await、timeout、unknown ID、cleanup |
| `kafka/ChatRequestProducerTest.kt` | 2 | 發送到正確 topic/key、JSON payload 序列化（含中文） |
| `kafka/ChatResponseConsumerTest.kt` | 2 | correlationId 配對、null key 處理 |
| `kafka/TrafficEventConsumerTest.kt` | 4 | traffic alert、route result、空訊息處理 |
| `controller/ChatControllerTest.kt` | 3 | HTTP 200 成功回應、HTTP 504 超時、register-before-send 順序驗證 |
| `controller/HealthControllerTest.kt` | 2 | HTTP 200、回傳 SERVING 狀態 |

**共 21 個測試**，全部通過。

#### 技術細節

- Controller 測試用 `MockMvcBuilders.standaloneSetup()` 而非 `@WebMvcTest`
  - 原因：Spring Boot 4.0 移除了 `@WebMvcTest` 從 `spring-boot-test-autoconfigure` 模組
  - `standaloneSetup()` 不需要啟動 Spring context，速度更快
- `TimeoutException` 是 checked exception，Mockito 的 `thenThrow()` 會拒絕
  - 解法：改用 `thenAnswer { throw TimeoutException("timeout") }`
- Kafka 相關元件全部 mock，不需要真實的 Kafka broker

#### 執行方式

```bash
cd backend/main-service && ./gradlew test
```

### 8.2 multiagent-service（Python / pytest + pytest-asyncio）

新增依賴：`pyproject.toml` 的 `[project.optional-dependencies]` 加入 `pytest`、`pytest-asyncio`、`httpx`

測試檔案位於 `backend/multiagent-service/tests/`：

| 測試檔案 | 測試數 | 涵蓋內容 |
|---|---|---|
| `test_producer.py` | 5 | singleton 建立、produce 參數、flush 呼叫、Unicode 編碼 |
| `test_consumer.py` | 10 | topic 設定、chat/route/traffic handler、publish 呼叫、欄位驗證、fallback correlationId |
| `test_main.py` | 3 | `/health` endpoint（status + JSON content type）、404 路由 |

**共 18 個測試**，全部通過。

#### 技術細節

- FastAPI endpoint 測試用 `httpx.AsyncClient` + `ASGITransport`，不需啟動真實 server
- Kafka Producer/Consumer 全部用 `unittest.mock.patch` mock 掉
- `test_producer.py` 測完後要清理全域 `_producer = None`，避免 test isolation 問題

#### 執行方式

```bash
cd backend/multiagent-service && uv run pytest tests/ -v
```
