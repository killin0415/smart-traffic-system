## Context

### 既有狀態

- **main-service** (Spring Boot 4.0 Kotlin)：
  - `ChatController` 已存在，走 Kafka correlation 模式（`PendingRequestStore` + 30 秒 await）
  - `RouteResponse` DTO 已存在於 `kafka/RouteDtos.kt`，但**沒有 RouteController**
  - `TrafficEventConsumer.onRouteResult` 只印 log，沒做 correlation 橋接
  - `KafkaConfig.kt` 採手動 bean 設定（Spring Boot 4.0 auto-config 失效）

- **multiagent-service** (Python FastAPI + confluent_kafka thread consumer)：
  - `handle_chat_request` 已存在
  - `handle_route_request` 已存在（可直接被 Kafka 觸發跑 A*）
  - `chat_agent.agenerate` 已回傳 `{reply, route_payload}`，`handle_chat_request` 已把 `route_payload` 寫進 `chat.response`
  - `agents/geocoding.py` 是 standalone async function，**目前沒掛到任何 Kafka handler**；且硬編「自動附加『高雄』」（高雄路網時代殘留）

- **前端**：不存在

- **Kafka topics**：`chat.request`、`chat.response`、`route.request`、`route.response`、`traffic.alerts`、`traffic.metrics`（YOLO 來源已棄用）。geocode 相關 topic 尚未存在。

### 約束

- Kafka-only 內部通訊（不可破壞 unify-kafka-remove-grpc 的設計）
- 不可重新引入 gRPC、不可在 main-service ↔ multiagent-service 之間建立 HTTP 直連
- 不登入（demo 範圍）
- Python 一律走 uv
- 維持既有 Spring Boot 4.0 手動 KafkaConfig pattern
- 2 個月 capstone deadline 已過半，ETA 準確度議題另案處理

## Goals / Non-Goals

**Goals:**
- 端到端 demo 路徑跑通：使用者在前端輸入起終點 → 後端規劃 → 地圖顯示
- chat 對話亦能觸發路線（agent 已具備此能力，只需把 `route_payload` 透傳給前端）
- 開發體驗：`./gradlew bootRun` + `uv run python -m main` + `npm run dev` 三條命令本機跑起來
- 為 Phase 2 WebSocket 升級預留乾淨介面

**Non-Goals:**
- 使用者登入註冊
- ETA 數字準確度修正（屬 `tune-eta-signal-density` change）
- WebSocket 實作（只記錄設計）
- 推播通知 / 行動裝置端
- chat token streaming（SSE）
- 多語系
- 部署相關（Docker compose、k8s）

## Decisions

### D1：sync HTTP + Kafka correlation 模式（Phase 1）

**選擇：** route / geocode 端點都複製 `ChatController` 的「register correlationId → produce Kafka request → await 30s → return JSON」模式。

**替代方案：**
- SSE 串流：成本高（要改 multiagent agent 串流輸出 + Kafka chunk 排序），demo 看不出差別。
- WebSocket：需新增 session registry + 重連邏輯，超出本次範圍。
- main-service 直接 HTTP 打 multiagent FastAPI：違反 Kafka-only 原則。

**理由：** 跟既有 chat 端點一致，最快可上線；timeout / 併發 / correlation 行為都已被 `kafka-messaging` spec 規範。

### D2：geocode topic 走 request/response

新增 `geocode.request` / `geocode.response`，遵循 `kafka-messaging` spec 的 `<domain>.<verb>` 命名公約與 correlation pattern。

**替代方案：**
- main-service 直接打 Nominatim → 違反 Kafka-only 原則，且 multiagent 端已有 `geocoding.py`、未來想加 cache / DB 查找會分裂兩處。
- 沿用 `route.request` 攜帶 geocode hint → 把不同職責塞同一 topic、未來難以演進。

**理由：** 一致性 > demo 速度的微小差距。

### D3：geocoding 移除「自動附加『高雄』」+ 改成回傳 list（BREAKING）

`agents/geocoding.py` 的 `geocode_location(query)` 目前：
- 無條件 `f"{normalised} 高雄"`（已棄用的 Kaohsiung 路網時代殘留）
- 回傳 `dict | None`（單筆）

**新行為（兩個 BREAKING 一起做）：**
- 簽名變為 `geocode_location(query: str, city_hint: str | None = None, limit: int = 5) -> list[dict]`
- 若 `city_hint` 非空，附加到查詢字串尾端
- 若 `city_hint` 為 None / 空字串，直接照原 query 查（不再硬編 city）
- 回傳改為 list，長度 0 ~ `limit`；無結果時回 `[]`（不再回 None）
- Kafka handler 帶 `city_hint="台北"` 預設值，讓表單輸入「中正紀念堂」自然回到台北中心
- Nominatim API 參數 `limit` 對應傳入

**為什麼一起改回傳型別：** autocomplete 需要多筆建議，與其讓 caller 多包一層 `[result] if result else []`，不如讓 module 本身就回 list；現存 caller 只有 `tests/test_geocoding.py` 5 個 case，更新成本低。

**替代方案：**
- 改硬編「台北」→ 還是會在切換城市時又壞一次。
- 用環境變數 `GEOCODE_DEFAULT_CITY` → 多一層設定，capstone 範圍過頭。
- 維持 `dict | None`、在 Kafka handler 包成 list → 兩處邏輯分散，難維護。

**Migration：** 既有 spec scenario「自動附加『高雄』」會在 `geocoding` 的 MODIFIED delta 改寫，並在 `tests/test_geocoding.py` 移除對應斷言。`agents/geocoding.py` 是 standalone function，沒有其他 production caller，移除硬編 + 改回傳型別無回溯相容問題。

### D4：`ChatMessageResponse` 擴充 `routeResult`

`multiagent` 端 `chat_agent.agenerate` 已回 `route_payload`，並寫入 `chat.response`。但 main-service `ChatMessageResponse` 目前只反序列化 `reply` + `suggested_actions`，`route_payload` 被丟掉。

**新增可選欄位 `routeResult: RouteResponse?`**（與 `POST /api/v1/route` 回傳同型別），由 `ChatController` 從 Kafka response JSON 解析。

**替代方案：**
- 把整段 raw JSON 回給前端 → 失去型別安全。
- 新增 `chat.route` topic 把路線資料分流 → 過度設計、雙路徑 race condition 風險。

**理由：** 既有架構已預備好，只是 main-service 沒接通，最低成本。

### D5：前端架構（React + Vite + TS + Tailwind + zustand + react-leaflet）

**目錄：** `frontend/` 在 repo 根目錄（與 `backend/` 平行）。

**狀態管理：** zustand 單一 store，分 slice：`routeSlice`（目前路線、起終點 marker）、`chatSlice`（訊息列表、session_id）、`uiSlice`（loading、error toast）。

**API 層：** `src/api/*.ts` 包 fetch + zod 驗證 response。Vite dev proxy `/api/* → http://localhost:8081`。

**地圖：** `react-leaflet` + OSM tile server（無 API key、無 quota）。

**dark mode：** Tailwind `dark:` variant，由 `<html class="dark">` 切換，state 存 localStorage。

**Sessionless：** `session_id = localStorage.getItem('sid') ?? (() => { const id = crypto.randomUUID(); localStorage.setItem('sid', id); return id; })()`。

**替代方案：**
- Next.js → 加 SSR 與路由抽象，demo 不需要。
- Redux Toolkit → 比 zustand 重，沒有對應收益。
- Mapbox / Google Maps → API key / 費用麻煩。

### D6：Vite proxy 取代 CORS

dev 階段透過 Vite proxy 把 `/api/*` 轉發給 main-service，避免在 Spring 加 CORS 設定。production demo（如果有）走同源部署（前端 build 後由 Spring static 提供，或 nginx）。

**Non-Goal：** 本次不做 production build 部署。

### D7：Phase 2 WebSocket 預留

main-service 之後可加：

- `/ws/session` 端點（Spring WebSocket，STOMP 不必要 — 用 raw text frame）
- `WsSessionRegistry`（`ConcurrentHashMap<String, WebSocketSession>`，key=session_id）
- `WsEventBridge`：訂閱 `route.response`、`chat.response`、`traffic.alerts`，依 correlation_id 或 session_id 推給對應 WS

WS 訊息協定（先定下來避免之後翻牆）：
```json
{ "type": "route.response" | "chat.response" | "traffic.alert" | "error",
  "correlationId": "...",
  "payload": { /* 與 HTTP 版完全相同 */ } }
```

Phase 1 為此預留：
- DTO 不混入 HTTP-specific 欄位（已自然滿足）
- 前端 `api/` 層每個函數都回 Promise，未來 WS 版以同樣 Promise 介面（內部用 correlationId 對應 once handler）替換
- zustand action 命名與傳輸層無關（`setCurrentRoute`、`appendChatMessage`）

### D8：移除 `TrafficEventConsumer.onRouteResult`

現有 `kafka/TrafficEventConsumer.kt` 同時掛了兩個 `@KafkaListener`：

- `onTrafficAlert(topics = ["traffic.alerts"])` — push notification 預留位（保留）
- `onRouteResult(topics = ["route.response"])` — 只 println、沒對 correlationId 做橋接（移除）

本次新增 `RouteResponseConsumer.kt` 專責 `route.response` 並對接 `PendingRequestStore`。兩個 listener 同訂 `route.response` 同 group 會搶 partition、造成 message race condition，所以舊的必須拿掉。

**新行為：**
- `TrafficEventConsumer.kt` 只剩 `onTrafficAlert`
- `route.response` 由新的 `RouteResponseConsumer.kt` 獨佔處理

**替代方案：**
- 在 `TrafficEventConsumer.onRouteResult` 直接補 correlation 橋接邏輯 → 違反單一職責、難測試。

### D9：routeResult Jackson 反序列化 naming strategy

multiagent 端寫進 `chat.response` 的 `route_payload` 是 snake_case（`estimated_time_min`、`road_names`、`speed_cameras`），但 main-service Kotlin DTO `RouteItem` / `RouteResponse` 使用 camelCase 欄位。預設 Jackson 不會做大小寫轉換，會悄悄反序列化成 0 / 空清單而不報錯。

**做法：**
- `RouteResponse` / `RouteItem` / `SpeedCamera` / `ParkingSuggestion` 全部加 `@JsonNaming(PropertyNamingStrategies.SnakeCaseStrategy::class)`，或在 `ObjectMapper` bean 設 global `PropertyNamingStrategies.SNAKE_CASE`
- HTTP 回應送回前端時，main-service 仍以 camelCase 序列化（前端對應）→ 需要 dual strategy：global default camelCase（Spring 預設）+ 反序列化 route_payload 時臨時用 SNAKE_CASE，或最簡單：給 RouteResponse / RouteItem 等 DTO 加 `@JsonProperty` 對每個欄位顯式宣告 snake_case 別名

**選擇：** 在 `RouteDtos.kt` 內每個欄位掛 `@JsonAlias("snake_case_name")`，讓反序列化接受兩種命名、序列化仍走 camelCase（最低風險、不影響其他 controller）。

## Risks / Trade-offs

- **同步 HTTP → Kafka 串接的 30 秒 timeout** → 如果 A* + DeepSeek 鏈路慢，使用者會看到 504。Mitigation：前端顯示明確 toast「服務暫時無法回應，請稍後再試」，並保留 chat 介面讓使用者改打字。
- **Nominatim rate limit（1 req/s）** → 多人同時打 geocode 會排隊。Mitigation：demo 級別影響可忽略；前端 autocomplete debounce 300ms 已大幅降低頻率。
- **`routeResult` 為可選欄位** → 反序列化失敗風險。Mitigation：`@JsonIgnoreProperties(ignoreUnknown = true)` 已套用、欄位 nullable、Kotlin data class default `null`。
- **前端 build 加進 monorepo 後 CI / 測試流程改變** → 既有 GH Actions 沒設前端 job。Mitigation：先不加 CI job，本次只跑 `npm run build` 在本機驗證；後續另立 change 處理 CI。
- **Leaflet 在 React 18/19 strict mode 雙重 mount 議題** → react-leaflet v5+ 已修正，照官方範例做即可。
- **Phase 2 WS 設計可能過早承諾介面** → 只記在 design.md，不寫成 spec requirement，未來實作時若發現更好方案可重新討論。

## Migration Plan

本次無 production 流量需要遷移；deploy 順序：

1. 合併 backend 變更 → multiagent 與 main-service 同時上（向下相容：geocode topic 不存在時 multiagent 不 crash，因為 handler registry pattern 跳過未訂閱 topic）
2. 前端是新增目錄，獨立部署 / 本地跑

**Rollback：**
- 若 main-service 新端點壞掉，回滾僅影響新端點，既有 `POST /api/v1/chat/message` 不變
- `geocoding.py` 行為改變（移除「高雄」）若造成測試失敗，可改帶 `city_hint="高雄"` 在 caller 端 patch；spec 改回原版即可

## Open Questions

- **Vite dev server port** 預設 5173，若需與 main-service 8081 共用 nginx，待 demo 部署時再決定。
- **地圖預設 center / zoom** 用台北車站（25.0478, 121.5170, zoom 14）作為硬編預設，是否需要 env 可調？暫不開放。
- **autocomplete 結果數量** 預設 5 筆，是否需要使用者設定？暫不開放。
- **chat session_id 過期策略** 目前永久存 localStorage，是否需要 TTL？demo 範圍不處理。
