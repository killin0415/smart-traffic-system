## 1. multiagent-service：geocoding 重構 + Kafka handler

- [x] 1.1 修改 `backend/multiagent-service/src/agents/geocoding.py` 簽名為 `geocode_location(query: str, city_hint: str | None = None, limit: int = 5) -> list[dict]`，移除「自動附加『高雄』」邏輯
- [x] 1.2 修改 `geocoding.py` 將 Nominatim `limit` 參數對應傳入，回傳 list of `{latitude, longitude, display_name}`，無結果時回 `[]`（**不再** 回 None）；`limit` 大於 10 時 SHALL clamp 至 10
- [x] 1.3 修改 `backend/multiagent-service/tests/test_geocoding.py`：
  - [x] 1.3.1 移除「自動附加『高雄』」相關斷言（涵蓋既有 `test_geocode_does_not_double_append_keyword` 等 cases，視語意改寫或刪除）
  - [x] 1.3.2 既有 `is None` 斷言改為 `== []` 並更新 mock 回傳形狀
  - [x] 1.3.3 新增「不附加 city」case（`city_hint=None` 時 Nominatim 收到的 query 與輸入完全相同）
  - [x] 1.3.4 新增「附加 city_hint」case（`city_hint="台北"` 時 query 尾端含 `台北`）
  - [x] 1.3.5 新增「limit 上限 clamp」case
- [x] 1.4 在 `backend/multiagent-service/src/kafka/consumer.py` 新增 `handle_geocode_request(key, data)` 函數：解析 `query` / `city_hint` / `limit`、呼叫 `geocode_location`、發 `geocode.response`
- [x] 1.5 在 `TOPIC_HANDLERS` dict 加 `"geocode.request": handle_geocode_request`
- [x] 1.6 修改 `DEFAULT_SUBSCRIBE_TOPICS` 為 `"chat.request,route.request,geocode.request"`
- [x] 1.7 新增 `backend/multiagent-service/tests/test_geocode_handler.py`：mock `publish_message`，驗證成功、缺 query、Nominatim 失敗三條路徑
- [x] 1.8 用 `uv run pytest` 跑整個 multiagent test suite，確認綠燈

## 2. main-service：route REST 端點

- [x] 2.1 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/domain/RouteModels.kt`，含 `RouteRequest`（`originLat, originLng, destLat, destLng, topK: Int? = 3`）
- [x] 2.2 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/kafka/RouteRequestProducer.kt`，仿 `ChatRequestProducer` 結構，發 `route.request`（snake_case keys）
- [x] 2.3 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/kafka/RouteResponseConsumer.kt`，`@KafkaListener(topics = ["route.response"])`，把 raw JSON 送進 `PendingRequestStore.complete`
- [x] 2.4 修改 `backend/main-service/src/main/kotlin/com/potato/mainservice/kafka/TrafficEventConsumer.kt`，移除 `onRouteResult` 函數（依 design D8：避免兩個 listener 同訂 `route.response` 同 group 造成 partition 競爭）；保留 `onTrafficAlert`
- [x] 2.5 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/controller/RouteController.kt`：`POST /api/v1/route`，邏輯參考 `ChatController`（產 correlationId、register、produce、await 30s、反序列化 `RouteResponse`、回傳）
- [x] 2.6 新增 `backend/main-service/src/test/kotlin/com/potato/mainservice/controller/RouteControllerTest.kt`：mock `PendingRequestStore` 與 producer，驗證 200 success / 504 timeout / 400 missing field 三條路徑；且 200 case SHALL assert `Content-Type` header 含 `charset=UTF-8`
- [x] 2.7 新增 `backend/main-service/src/test/kotlin/com/potato/mainservice/kafka/RouteResponseConsumerTest.kt`：驗證收到 message 後呼叫 `PendingRequestStore.complete`、key 不在 store 時不拋例外
- [x] 2.8 用 `./gradlew test` 跑整個 main-service test suite，確認綠燈

## 3. main-service：geocode REST 端點

- [x] 3.1 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/domain/GeocodeModels.kt`，含 `GeocodeResult(latitude, longitude, displayName)`、`GeocodeResponse(results: List<GeocodeResult>, error: String? = null)`
- [x] 3.2 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/kafka/GeocodeRequestProducer.kt`，發 `geocode.request`（含 `correlation_id, query, city_hint, limit`）
- [x] 3.3 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/kafka/GeocodeResponseConsumer.kt`，訂閱 `geocode.response`、送進 `PendingRequestStore.complete`
- [x] 3.4 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/controller/GeocodeController.kt`：`GET /api/v1/geocode?q=&cityHint=&limit=`；空 `q` 回 400、`limit` clamp 至 1-10
- [x] 3.5 新增 `backend/main-service/src/test/kotlin/com/potato/mainservice/controller/GeocodeControllerTest.kt`：成功、空 q、limit 超界 clamp、timeout 504；且 200 case SHALL assert `Content-Type` header 含 `charset=UTF-8`
- [x] 3.6 新增 `backend/main-service/src/test/kotlin/com/potato/mainservice/kafka/GeocodeResponseConsumerTest.kt`

## 4. main-service：擴充 chat 回應透傳 routeResult + Jackson naming

- [x] 4.1 修改 `backend/main-service/src/main/kotlin/com/potato/mainservice/domain/ChatModels.kt`，`ChatMessageResponse` 加 `routeResult: RouteResponse? = null`
- [x] 4.2 修改 `backend/main-service/src/main/kotlin/com/potato/mainservice/kafka/RouteDtos.kt`：在 `RouteResponse` / `RouteItem` / `SpeedCamera` / `ParkingSuggestion` 各欄位加 `@JsonAlias` 宣告對應 snake_case 別名（`estimated_time_min` → `estimatedTimeMin`、`distance_km` → `distanceKm`、`road_names` → `roadNames`、`speed_cameras` → `speedCameras`、`parking_suggestions` → `parkingSuggestions`、`speed_limit` → `speedLimit`、`available_car` → `availableCar`、`distance_m` → `distanceM`）；不更動序列化時的 camelCase 行為
- [x] 4.3 修改 `backend/main-service/src/main/kotlin/com/potato/mainservice/controller/ChatController.kt`，反序列化 chat.response 時把 `route_payload` 欄位解析成 `RouteResponse`（容錯：格式錯誤時 log WARN 後設 null，文字回覆仍正常回）
- [x] 4.4 修改 `backend/main-service/src/test/kotlin/com/potato/mainservice/controller/ChatControllerTest.kt`：
  - [x] 4.4.1 新增「chat.response 帶 route_payload 且內部欄位為 snake_case」測試，assert `routeResult.routes[0].estimatedTimeMin` 非 0、`distanceKm` 非 0、`roadNames` 非空（驗證 D9 的 @JsonAlias 真的生效）
  - [x] 4.4.2 新增「route_payload 格式錯誤」測試
  - [x] 4.4.3 既有 happy path 測試補上 `Content-Type` header 含 `charset=UTF-8` 的 assertion（涵蓋 main-service-rest-api spec 「字元編碼 → 驗證測試」scenario）

## 5. frontend：專案初始化

- [x] 5.1 在 repo 根 `frontend/` 跑 `npm create vite@latest . -- --template react-ts`，刪除 default `App.tsx` / `App.css` 樣板
- [x] 5.2 加入依賴：`npm install react-leaflet leaflet zustand`、`npm install -D tailwindcss postcss autoprefixer @types/leaflet`
- [x] 5.3 `npx tailwindcss init -p`、設定 `tailwind.config.js`（content 指向 `./index.html`、`./src/**/*.{ts,tsx}`、`darkMode: 'class'`）
- [x] 5.4 設定 `src/index.css` 引入 `@tailwind base/components/utilities` 與 Leaflet CSS（`import 'leaflet/dist/leaflet.css'`）
- [x] 5.5 設定 `vite.config.ts` 內 `server.proxy = { '/api': { target: 'http://localhost:8081', changeOrigin: true } }`（對應 frontend-demo-app spec「Vite dev proxy」Requirement）
- [x] 5.6 新增 `frontend/.gitignore`（node_modules, dist, .env.local）
- [x] 5.7 修改根 `.gitignore` 不要 ignore `frontend/`（確認）

## 6. frontend：型別與 API 層

- [x] 6.1 新增 `frontend/src/types/api.ts`：`RouteResponse`、`RouteItem`、`SpeedCamera`、`ParkingSuggestion`、`GeocodeResult`、`GeocodeResponse`、`ChatMessageRequest`、`ChatMessageResponse`（與 Kotlin DTO 對齊，camelCase）
- [x] 6.2 新增 `frontend/src/api/client.ts`：統一 fetch wrapper、處理 5xx/4xx/network error、回 typed Promise
- [x] 6.3 新增 `frontend/src/api/route.ts`：`postRoute(req: RouteRequest): Promise<RouteResponse>`
- [x] 6.4 新增 `frontend/src/api/geocode.ts`：`geocode(q: string, opts?: { cityHint?: string; limit?: number }): Promise<GeocodeResult[]>`
- [x] 6.5 新增 `frontend/src/api/chat.ts`：`postChatMessage(content: string, sessionId: string): Promise<ChatMessageResponse>`

## 7. frontend：狀態管理

- [x] 7.1 新增 `frontend/src/store/index.ts`：zustand store 含 `routeSlice`（currentRoute, originMarker, destMarker, selectedRouteIndex）、`chatSlice`（messages, sessionId）、`uiSlice`（loading, errorToast, theme）
- [x] 7.2 在 store 內處理 sessionId：初始化時讀 `localStorage.sid`，不存在則 `crypto.randomUUID()` 寫回
- [x] 7.3 在 store 內處理 theme：初始化時讀 `localStorage.theme` 或 `prefers-color-scheme`，套用 `<html class="dark">`

## 7b. RouteItem 加 coordinates 欄位（Phase 8 阻塞修正）

> 實作 Phase 8 時發現：`RouteItem.path` 只是 node ID，前端沒有 ID→lat/lng 對照表，畫不出 polyline。需要在 wire format 加 `coordinates: list[[lat, lng]]`，從 `graph.nodes[nid].latitude/longitude` 填。

- [x] 7b.1 修改 `backend/multiagent-service/src/mcp_servers/routing_tool.py`：`RouteItem` 加 `coordinates: list[list[float]] = Field(default_factory=list)`
- [x] 7b.2 修改 `backend/multiagent-service/src/agents/routing.py` `plan_optimal_route`：為每條 route 從 `graph.nodes[nid]` 組 `[(lat, lng), ...]` 並塞進 `RouteItem`
- [x] 7b.3 修改 `backend/main-service/.../kafka/RouteDtos.kt` `RouteItem` 加 `coordinates: List<List<Double>> = emptyList()`（無需 alias，欄位本身就 camelCase 對 camelCase；Python 端 `coordinates` 不變）
- [x] 7b.4 修改 `frontend/src/types/api.ts` `RouteItem` 加 `coordinates: [number, number][]`
- [x] 7b.5 修跑 `uv run pytest`、`./gradlew test`，補/改任何因新欄位失敗的測試（236 Python + Kotlin 全綠）

## 8. frontend：地圖元件

- [x] 8.1 新增 `frontend/src/components/MapView.tsx`：`<MapContainer center={[25.0478, 121.5170]} zoom={14}>`，OSM tile layer
- [x] 8.2 訂閱 store `currentRoute` 並畫 `<Polyline>`（用 path node 的 lat/lng）、起終點 marker、`speedCameras` markers（紅）、`parkingSuggestions` markers（綠）
- [x] 8.3 路線變動時 `useEffect` 呼叫 `map.fitBounds(...)` 自動縮放
- [x] 8.4 加 onClick handler，把點到的座標填入 store 內目前焦點輸入框（透過 uiSlice 的 `focusedInput` 狀態）

## 9. frontend：路線表單 + autocomplete

- [x] 9.1 新增 `frontend/src/components/AddressInput.tsx`：輸入框 + 下拉建議；用 debounce hook 300ms 後呼叫 `geocode(q)`
- [x] 9.2 新增 `frontend/src/hooks/useDebounce.ts`
- [x] 9.3 新增 `frontend/src/components/RouteForm.tsx`：兩個 `AddressInput`（起點 / 終點）+「規劃路線」按鈕 + loading 狀態；送出時呼叫 `postRoute` 寫進 store
- [x] 9.4 新增 `frontend/src/components/RouteSummary.tsx`：顯示 `distanceKm` (1 位小數)、`estimatedTimeMin` (四捨五入整數)、測速照相數量、停車場列表
- [x] 9.5 在 `RouteSummary` 內加路線切換器（當 `routes.length > 1`）

## 10. frontend：chat 面板

- [x] 10.1 新增 `frontend/src/components/ChatPanel.tsx`：訊息列表 + 輸入框 + 送出按鈕
- [x] 10.2 新增 `frontend/src/components/ChatMessage.tsx`：分 user / agent 兩種樣式
- [x] 10.3 送出後若回應帶 `routeResult` 非 null，把它寫進 store `currentRoute`（地圖會自動更新）
- [x] 10.4 顯示「agent 思考中...」loading 狀態

## 11. frontend：整體 layout + dark mode 切換

- [x] 11.1 修改 `frontend/src/App.tsx`：左側 panel（RouteForm + RouteSummary + ChatPanel）、右側 MapView，響應式 layout
- [x] 11.2 新增 `frontend/src/components/ThemeToggle.tsx`：切換 light / dark，更新 store + localStorage + `<html class>`
- [x] 11.3 把 ThemeToggle 放進 header

## 12. frontend：錯誤處理 / toast

- [x] 12.1 新增 `frontend/src/components/Toast.tsx`：訂閱 store `errorToast`，顯示後 3 秒自動隱藏
- [x] 12.2 在 `api/client.ts` 內把 fetch 錯誤統一寫進 store `errorToast`

## 13. frontend：基本測試

- [x] 13.1 加入 Vitest + React Testing Library：`npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom`
- [x] 13.2 設定 `vite.config.ts` 加 `test: { environment: 'jsdom' }`
- [x] 13.3 寫 smoke test：`App.test.tsx` 驗證 render 不噴錯、含 MapView、含 RouteForm
- [x] 13.4 寫 `useDebounce.test.ts`
- [x] 13.5 跑 `npm test` 確認綠燈（4 tests pass）

## 14. 整合驗收

- [x] 14.1 啟動三服務：infra (Kafka/Postgres/Redis 用 compose) + main-service + multiagent-service + frontend dev server
- [x] 14.2 在前端測試：輸入「台北車站」「中正紀念堂」→ autocomplete 出建議 → 選一個 → 點規劃 → 看到路線
- [x] 14.3 在前端測試：地圖上點兩下 → 規劃 → 看到路線
- [x] 14.4 在前端測試：chat 打「我要從台北車站到忠孝復興」→ 回應帶路線並顯示在地圖
- [x] 14.5 測試 dark mode 切換、重新整理後保留
- [x] 14.6 測試 504 timeout（暫停 multiagent）→ toast 顯示
- [x] 14.7 測試 504 timeout（query 無結果）→ toast 顯示

## 15. 文件 / 收尾

- [x] 15.1 在 `README.md` 新增「frontend 啟動方式」一段（README 從空檔重寫，含所有四層的啟動方式 + 測試命令）
- [x] 15.2 在 `references/manual-acceptance-runbook.md`（已有）新增本 change 的手動驗收步驟（對應 §14）
- [x] 15.3 用 `openspec validate complete-demo-stack --strict` 確認無誤（`Change 'complete-demo-stack' is valid`）
- [x] 15.4 commit 本 change 的 openspec/changes 內所有檔案（依專案慣例）
