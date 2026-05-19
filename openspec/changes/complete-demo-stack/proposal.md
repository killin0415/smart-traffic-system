## Why

目前 main-service 只有 `POST /api/v1/chat/message` 一個 REST 端點，路線規劃只能透過 chat agent 的自然語言觸發；同時整個專案還缺前端，無法 demo 給人看。為了在 2 個月 capstone deadline 前產出「能完整 demo 的東西」，需要：

- 把 route + geocode 也暴露成 REST 端點（讓表單也能直接規劃路線）
- 補上一個 React 前端，能顯示地圖、輸入起終點、跟 chat agent 對話
- 維持現有 Kafka-only 內部通訊架構（不破壞 unify-kafka-remove-grpc 的設計）

## What Changes

### Phase 1（本次實作）

- main-service 新增 `POST /api/v1/route`，內部走 `route.request` ↔ `route.response`，沿用 `PendingRequestStore` correlation 模式
- main-service 新增 `GET /api/v1/geocode?q={query}`，內部走新的 `geocode.request` ↔ `geocode.response` topic
- main-service `ChatMessageResponse` 擴充選填欄位 `routeResult`，把 multiagent 端 chat agent 已經在送的 `route_payload` 透傳給前端
- main-service 補 `RouteResponseConsumer`、`GeocodeResponseConsumer`（與既有 `TrafficEventConsumer.onRouteResult` 不同——後者只 log 沒做 correlation 橋接，需要重構或取代）
- multiagent-service 新增 `geocode.request` handler，封裝既有 `agents/geocoding.py` 並透過 `geocode.response` 回傳結果；handler registry 新增對應條目
- multiagent-service 預設 `KAFKA_SUBSCRIBE_TOPICS` 從 `chat.request,route.request` 擴充為 `chat.request,route.request,geocode.request`
- **BREAKING**：`agents/geocoding.py` 移除「自動附加『高雄』」邏輯（沿用既有 Taipei 路網設定，繼續硬編 city 已不正確）。改為呼叫端傳什麼就查什麼，並讓 Kafka 請求負載可選 `city_hint` 字串
- **BREAKING**：`agents/geocoding.py` 函數簽名變更：`geocode_location(query: str) -> dict | None` → `geocode_location(query: str, city_hint: str | None = None, limit: int = 5) -> list[dict]`（回傳改成最多 `limit` 筆結果，無結果時回空 list；現存 caller 只有測試檔，可同步更新）
- **BREAKING**：multiagent-service `DEFAULT_SUBSCRIBE_TOPICS` 由 `chat.request,route.request` 擴增為 `chat.request,route.request,geocode.request`（部署端若依賴預設值不顯式設 `KAFKA_SUBSCRIBE_TOPICS`，啟動後會自動多訂 `geocode.request` topic — Kafka broker 必須允許該 topic 存在或自動建立）
- 新增 `frontend/` 目錄，Vite + React 18 + TypeScript + Tailwind CSS + zustand + Leaflet
  - `RouteForm` 起終點 + 規劃按鈕（地址 autocomplete 用 geocode API，也可在地圖上點選 marker）
  - `MapView` Leaflet 地圖，顯示路線 polyline、起終點 marker、測速照相、停車場
  - `ChatPanel` 跟現有 chat 端點對話，當回應帶 `routeResult` 時把路線也丟到地圖上
  - `RouteSummary` 顯示距離 / 預估時間 / 測速照相 / 停車場
  - dark mode（Tailwind `dark:` variant）
  - 不做使用者登入；session_id 在前端用 `crypto.randomUUID()` 產生並存 `localStorage`
- 開發環境：Vite dev server proxy `/api/*` → `localhost:8080`（main-service），避免 CORS；既有 `compose.yaml` 不動

### Phase 2（本次只設計、**不實作**）

- WebSocket 升級路徑：main-service 加 `/ws/session` 端點、`WsEventBridge` 訂閱 `route.response` / `chat.response` / `traffic.alerts` 並推送給對應 session
- HTTP 端點保留以維持向下相容
- 詳見 design.md §Phase 2

## Capabilities

### New Capabilities

- `main-service-rest-api`: main-service 的 REST 端點集合（route、geocode、擴充後的 chat），含 Kafka correlation 橋接行為
- `frontend-demo-app`: React 前端應用（地圖、路線表單、chat、autocomplete、dark mode、sessionless）

### Modified Capabilities

- `kafka-messaging`: 新增 `geocode.request` / `geocode.response` 兩個 topic 的 JSON schema 定義
- `geocoding`: **BREAKING**——移除「自動附加『高雄』」行為；新增 Kafka handler 行為（透過 `geocode.request` 觸發、結果寫到 `geocode.response`）

## Impact

**程式碼：**
- `backend/main-service/src/main/kotlin/com/potato/mainservice/`
  - 新增：`controller/RouteController.kt`、`controller/GeocodeController.kt`、`kafka/RouteRequestProducer.kt`、`kafka/RouteResponseConsumer.kt`、`kafka/GeocodeRequestProducer.kt`、`kafka/GeocodeResponseConsumer.kt`、`domain/RouteModels.kt`、`domain/GeocodeModels.kt`
  - 修改：`domain/ChatModels.kt`（加 `routeResult` 欄位）、`controller/ChatController.kt`（透傳 routeResult）、`kafka/TrafficEventConsumer.kt`（移除 `onRouteResult` 或讓它只剩 alert 處理）
- `backend/multiagent-service/src/`
  - 新增：`kafka/consumer.py` 內 `handle_geocode_request` 函數及 `TOPIC_HANDLERS` 條目
  - 修改：`agents/geocoding.py`（移除「高雄」硬編、改為帶 `city_hint` 參數）
- `frontend/`（全新；需確認根 `.gitignore` 沒有 ignore 整個 `frontend/`，task 5.7 涵蓋）

**Spec：**
- 新增 `openspec/specs/main-service-rest-api/`、`openspec/specs/frontend-demo-app/`
- 修改 `openspec/specs/kafka-messaging/spec.md`、`openspec/specs/geocoding/spec.md`

**依賴：**
- frontend：`react`, `react-dom`, `react-leaflet`, `leaflet`, `zustand`, `tailwindcss`, `typescript`, `vite`, `@types/leaflet`
- main-service：無新依賴（沿用既有 spring-kafka）
- multiagent-service：無新依賴

**測試：**
- main-service：`RouteControllerTest`、`GeocodeControllerTest`、`RouteResponseConsumerTest`、`GeocodeResponseConsumerTest`
- multiagent-service：`test_geocode_handler.py`（Kafka handler 整合）、修正 `tests/test_geocoding.py`（移除「高雄」斷言）
- frontend：基本 component smoke test（Vitest + React Testing Library）

**ETA 準確度（已知議題）不在本次範圍**。本次只把 e2e flow 跑通，數字準確度由獨立的 `tune-eta-signal-density` change 處理。

**Phase 2 WebSocket 不在本次 tasks 範圍**，只出現在 design.md 作為架構記錄。
