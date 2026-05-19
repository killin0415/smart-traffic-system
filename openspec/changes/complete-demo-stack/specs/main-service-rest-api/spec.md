## ADDED Requirements

### Requirement: 路線規劃 REST 端點
main-service SHALL 暴露 `POST /api/v1/route` 端點，內部走 Kafka `route.request` ↔ `route.response` 並透過 correlation ID 橋接同步回應。

#### Scenario: 成功規劃路線
- **WHEN** 客戶端送 `POST /api/v1/route` 帶 `{ "originLat": float, "originLng": float, "destLat": float, "destLng": float, "topK"?: int }`
- **THEN** main-service SHALL 產生 UUID `correlationId`、註冊 `PendingRequestStore`、向 `route.request` topic 發送訊息（key 為 correlationId、value 含 `correlation_id` / `origin_lat` / `origin_lng` / `dest_lat` / `dest_lng` / `top_k`）
- **AND** 在 30 秒內收到 `route.response` 配對訊息後 SHALL 以 HTTP 200 回 JSON：`{ "routes": [...], "error": null | string }`，schema 與 `kafka-messaging` spec 之 `route.response` 一致

#### Scenario: 缺少必要欄位
- **WHEN** request body 缺少 `originLat` / `originLng` / `destLat` / `destLng` 任一欄位
- **THEN** SHALL 回 HTTP 400 並含 `{ "error": "<欄位名> is required" }`，且 SHALL NOT 送 Kafka 訊息

#### Scenario: multiagent 未在 30 秒內回應
- **WHEN** main-service 等待 `route.response` 配對訊息超過 30 秒
- **THEN** SHALL 回 HTTP 504 並含 `{ "error": "Multiagent service did not respond within 30 seconds" }`

#### Scenario: multiagent 回傳錯誤
- **WHEN** `route.response` 配對訊息 `error` 欄位非 null（例如 "no path found"）
- **THEN** SHALL 以 HTTP 200 回原 payload（`routes` 為空陣列、`error` 帶錯誤字串），由前端決定如何顯示

#### Scenario: 併發路線請求
- **WHEN** 多筆 `POST /api/v1/route` 同時在處理
- **THEN** 每筆 SHALL 透過獨立 correlationId 配對，SHALL NOT 互相干擾

### Requirement: Geocoding REST 端點
main-service SHALL 暴露 `GET /api/v1/geocode` 端點，內部走 Kafka `geocode.request` ↔ `geocode.response` 並橋接同步回應。

#### Scenario: 成功查詢地址
- **WHEN** 客戶端送 `GET /api/v1/geocode?q=台北車站`（query string 必填 `q`、可選 `cityHint`、可選 `limit`，預設 5、上限 10）
- **THEN** main-service SHALL 產生 UUID correlationId、發 `geocode.request` 訊息（value 含 `correlation_id` / `query` / `city_hint` / `limit`）
- **AND** 在 30 秒內收到 `geocode.response` 配對訊息後 SHALL 以 HTTP 200 回 `{ "results": [ { "latitude": float, "longitude": float, "displayName": string }, ... ] }`

#### Scenario: query 參數為空
- **WHEN** `q` 缺失、為空字串或僅空白
- **THEN** SHALL 回 HTTP 400 並含 `{ "error": "q is required" }`，SHALL NOT 送 Kafka 訊息

#### Scenario: limit 超過上限
- **WHEN** `limit` 大於 10
- **THEN** SHALL clamp 至 10 後送 Kafka 訊息，不回錯誤

#### Scenario: 查無結果
- **WHEN** `geocode.response` 配對訊息 `results` 為空陣列
- **THEN** SHALL 回 HTTP 200 並含 `{ "results": [] }`

#### Scenario: multiagent 未在 30 秒內回應
- **WHEN** main-service 等待 `geocode.response` 超過 30 秒
- **THEN** SHALL 回 HTTP 504 並含 `{ "error": "Multiagent service did not respond within 30 seconds" }`

### Requirement: Chat 回應透傳路線結果
main-service `POST /api/v1/chat/message` 回應 SHALL 包含可選 `routeResult` 欄位，當 `chat.response` Kafka 訊息含 `route_payload` 時透傳給客戶端。

#### Scenario: chat agent 觸發路線規劃
- **WHEN** 使用者發送 chat 訊息且 `chat.response` 配對訊息含 `route_payload`（schema 同 `route.response` 但無 `correlation_id`）
- **THEN** main-service SHALL 把 `route_payload` 反序列化為 `RouteResponse` 並設定到 `ChatMessageResponse.routeResult` 欄位回給客戶端

#### Scenario: chat agent 純文字回應
- **WHEN** `chat.response` 配對訊息無 `route_payload` 或其值為 null
- **THEN** main-service SHALL 在 `ChatMessageResponse` 中省略 `routeResult` 欄位（或設為 null），SHALL NOT 因缺欄位而失敗

#### Scenario: route_payload 格式錯誤
- **WHEN** `route_payload` 存在但 JSON 結構不符 `RouteResponse` schema
- **THEN** main-service SHALL log WARN 並把 `routeResult` 設為 null 後正常回應（不影響 `reply` 文字回覆）

### Requirement: 路線回應 Kafka consumer
main-service SHALL 訂閱 `route.response` topic 並把訊息透過 correlation ID 寫回 `PendingRequestStore`，以解除 `POST /api/v1/route` 對應的等待。

#### Scenario: 收到配對的路線回應
- **WHEN** consumer 從 `route.response` 收到一筆訊息且其 key 為某個 pending correlationId
- **THEN** SHALL 呼叫 `PendingRequestStore.complete(correlationId, rawJson)`、SHALL NOT 阻塞 consumer 執行緒

#### Scenario: 收到無對應 pending 的回應
- **WHEN** consumer 收到 `route.response` 訊息但其 key 不在 `PendingRequestStore` 中（例如已超時）
- **THEN** SHALL log INFO 並丟棄該訊息，SHALL NOT 拋例外

### Requirement: Geocode 回應 Kafka consumer
main-service SHALL 訂閱 `geocode.response` topic 並把訊息透過 correlation ID 寫回 `PendingRequestStore`，以解除 `GET /api/v1/geocode` 對應的等待。

#### Scenario: 收到配對的 geocode 回應
- **WHEN** consumer 從 `geocode.response` 收到一筆訊息且其 key 為某個 pending correlationId
- **THEN** SHALL 呼叫 `PendingRequestStore.complete(correlationId, rawJson)`

#### Scenario: 收到無對應 pending 的回應
- **WHEN** key 不在 `PendingRequestStore` 中
- **THEN** SHALL log INFO 並丟棄該訊息

### Requirement: 字元編碼
所有 REST 端點 SHALL 以 `application/json;charset=UTF-8` 回應，確保中文路名、測速照相地址不亂碼。

#### Scenario: 回應 header
- **WHEN** 任一 `/api/v1/*` 端點（`/route`、`/geocode`、`/chat/message`）回應
- **THEN** `Content-Type` header SHALL 為 `application/json;charset=UTF-8`

#### Scenario: 驗證測試
- **WHEN** 執行 controller 測試（`RouteControllerTest`、`GeocodeControllerTest`、`ChatControllerTest`）
- **THEN** 每個測試 SHALL 至少有一個 assertion 驗證回應 `Content-Type` header 包含 `charset=UTF-8`

### Requirement: routeResult 反序列化欄位命名兼容
main-service 從 Kafka `chat.response` / `route.response` 收到的 JSON 內部欄位使用 snake_case（multiagent 端 Python 慣例），但 main-service 對外 HTTP 回應使用 camelCase。`RouteResponse` / `RouteItem` / `SpeedCamera` / `ParkingSuggestion` DTO SHALL 同時接受 snake_case 與 camelCase 欄位反序列化。

#### Scenario: snake_case 來源 JSON 正確反序列化
- **WHEN** 從 Kafka `chat.response.route_payload` 或 `route.response` 收到含 `estimated_time_min`、`distance_km`、`road_names`、`speed_cameras`、`parking_suggestions`、`available_car`、`distance_m`、`speed_limit` 的 JSON
- **THEN** 反序列化後的 Kotlin 物件 SHALL 正確帶值（不為 0、不為空 list），且 `RouteResponse` / `RouteItem` 等類別 SHALL 使用 `@JsonAlias` 或等效 Jackson 機制宣告 snake_case 別名

#### Scenario: HTTP 回應序列化維持 camelCase
- **WHEN** main-service 把 `RouteResponse` 序列化為 HTTP 回應
- **THEN** JSON 欄位名 SHALL 為 camelCase（`estimatedTimeMin`、`distanceKm` 等），讓前端 TypeScript 介面對齊一致
