## 1. multiagent-service：geocoding 重構 + Kafka handler

- [ ] 1.1 修改 `backend/multiagent-service/src/agents/geocoding.py` 簽名為 `geocode_location(query: str, city_hint: str | None = None, limit: int = 5) -> list[dict]`，移除「自動附加『高雄』」邏輯
- [ ] 1.2 修改 `geocoding.py` 將 Nominatim `limit` 參數對應傳入，回傳 list of `{latitude, longitude, display_name}`，無結果時回 `[]`（**不再** 回 None）；`limit` 大於 10 時 SHALL clamp 至 10
- [ ] 1.3 修改 `backend/multiagent-service/tests/test_geocoding.py`：
  - [ ] 1.3.1 移除「自動附加『高雄』」相關斷言（涵蓋既有 `test_geocode_does_not_double_append_keyword` 等 cases，視語意改寫或刪除）
  - [ ] 1.3.2 既有 `is None` 斷言改為 `== []` 並更新 mock 回傳形狀
  - [ ] 1.3.3 新增「不附加 city」case（`city_hint=None` 時 Nominatim 收到的 query 與輸入完全相同）
  - [ ] 1.3.4 新增「附加 city_hint」case（`city_hint="台北"` 時 query 尾端含 `台北`）
  - [ ] 1.3.5 新增「limit 上限 clamp」case
- [ ] 1.4 在 `backend/multiagent-service/src/kafka/consumer.py` 新增 `handle_geocode_request(key, data)` 函數：解析 `query` / `city_hint` / `limit`、呼叫 `geocode_location`、發 `geocode.response`
- [ ] 1.5 在 `TOPIC_HANDLERS` dict 加 `"geocode.request": handle_geocode_request`
- [ ] 1.6 修改 `DEFAULT_SUBSCRIBE_TOPICS` 為 `"chat.request,route.request,geocode.request"`
- [ ] 1.7 新增 `backend/multiagent-service/tests/test_geocode_handler.py`：mock `publish_message`，驗證成功、缺 query、Nominatim 失敗三條路徑
- [ ] 1.8 用 `uv run pytest` 跑整個 multiagent test suite，確認綠燈

## 2. main-service：route REST 端點

- [ ] 2.1 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/domain/RouteModels.kt`，含 `RouteRequest`（`originLat, originLng, destLat, destLng, topK: Int? = 3`）
- [ ] 2.2 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/kafka/RouteRequestProducer.kt`，仿 `ChatRequestProducer` 結構，發 `route.request`（snake_case keys）
- [ ] 2.3 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/kafka/RouteResponseConsumer.kt`，`@KafkaListener(topics = ["route.response"])`，把 raw JSON 送進 `PendingRequestStore.complete`
- [ ] 2.4 修改 `backend/main-service/src/main/kotlin/com/potato/mainservice/kafka/TrafficEventConsumer.kt`，移除 `onRouteResult` 函數（依 design D8：避免兩個 listener 同訂 `route.response` 同 group 造成 partition 競爭）；保留 `onTrafficAlert`
- [ ] 2.5 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/controller/RouteController.kt`：`POST /api/v1/route`，邏輯參考 `ChatController`（產 correlationId、register、produce、await 30s、反序列化 `RouteResponse`、回傳）
- [ ] 2.6 新增 `backend/main-service/src/test/kotlin/com/potato/mainservice/controller/RouteControllerTest.kt`：mock `PendingRequestStore` 與 producer，驗證 200 success / 504 timeout / 400 missing field 三條路徑；且 200 case SHALL assert `Content-Type` header 含 `charset=UTF-8`
- [ ] 2.7 新增 `backend/main-service/src/test/kotlin/com/potato/mainservice/kafka/RouteResponseConsumerTest.kt`：驗證收到 message 後呼叫 `PendingRequestStore.complete`、key 不在 store 時不拋例外
- [ ] 2.8 用 `./gradlew test` 跑整個 main-service test suite，確認綠燈

## 3. main-service：geocode REST 端點

- [ ] 3.1 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/domain/GeocodeModels.kt`，含 `GeocodeResult(latitude, longitude, displayName)`、`GeocodeResponse(results: List<GeocodeResult>, error: String? = null)`
- [ ] 3.2 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/kafka/GeocodeRequestProducer.kt`，發 `geocode.request`（含 `correlation_id, query, city_hint, limit`）
- [ ] 3.3 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/kafka/GeocodeResponseConsumer.kt`，訂閱 `geocode.response`、送進 `PendingRequestStore.complete`
- [ ] 3.4 新增 `backend/main-service/src/main/kotlin/com/potato/mainservice/controller/GeocodeController.kt`：`GET /api/v1/geocode?q=&cityHint=&limit=`；空 `q` 回 400、`limit` clamp 至 1-10
- [ ] 3.5 新增 `backend/main-service/src/test/kotlin/com/potato/mainservice/controller/GeocodeControllerTest.kt`：成功、空 q、limit 超界 clamp、timeout 504；且 200 case SHALL assert `Content-Type` header 含 `charset=UTF-8`
- [ ] 3.6 新增 `backend/main-service/src/test/kotlin/com/potato/mainservice/kafka/GeocodeResponseConsumerTest.kt`

## 4. main-service：擴充 chat 回應透傳 routeResult + Jackson naming

- [ ] 4.1 修改 `backend/main-service/src/main/kotlin/com/potato/mainservice/domain/ChatModels.kt`，`ChatMessageResponse` 加 `routeResult: RouteResponse? = null`
- [ ] 4.2 修改 `backend/main-service/src/main/kotlin/com/potato/mainservice/kafka/RouteDtos.kt`：在 `RouteResponse` / `RouteItem` / `SpeedCamera` / `ParkingSuggestion` 各欄位加 `@JsonAlias` 宣告對應 snake_case 別名（`estimated_time_min` → `estimatedTimeMin`、`distance_km` → `distanceKm`、`road_names` → `roadNames`、`speed_cameras` → `speedCameras`、`parking_suggestions` → `parkingSuggestions`、`speed_limit` → `speedLimit`、`available_car` → `availableCar`、`distance_m` → `distanceM`）；不更動序列化時的 camelCase 行為
- [ ] 4.3 修改 `backend/main-service/src/main/kotlin/com/potato/mainservice/controller/ChatController.kt`，反序列化 chat.response 時把 `route_payload` 欄位解析成 `RouteResponse`（容錯：格式錯誤時 log WARN 後設 null，文字回覆仍正常回）
- [ ] 4.4 修改 `backend/main-service/src/test/kotlin/com/potato/mainservice/controller/ChatControllerTest.kt`：
  - [ ] 4.4.1 新增「chat.response 帶 route_payload 且內部欄位為 snake_case」測試，assert `routeResult.routes[0].estimatedTimeMin` 非 0、`distanceKm` 非 0、`roadNames` 非空（驗證 D9 的 @JsonAlias 真的生效）
  - [ ] 4.4.2 新增「route_payload 格式錯誤」測試
  - [ ] 4.4.3 既有 happy path 測試補上 `Content-Type` header 含 `charset=UTF-8` 的 assertion（涵蓋 main-service-rest-api spec 「字元編碼 → 驗證測試」scenario）

## 5. frontend：專案初始化

- [ ] 5.1 在 repo 根 `frontend/` 跑 `npm create vite@latest . -- --template react-ts`，刪除 default `App.tsx` / `App.css` 樣板
- [ ] 5.2 加入依賴：`npm install react-leaflet leaflet zustand`、`npm install -D tailwindcss postcss autoprefixer @types/leaflet`
- [ ] 5.3 `npx tailwindcss init -p`、設定 `tailwind.config.js`（content 指向 `./index.html`、`./src/**/*.{ts,tsx}`、`darkMode: 'class'`）
- [ ] 5.4 設定 `src/index.css` 引入 `@tailwind base/components/utilities` 與 Leaflet CSS（`import 'leaflet/dist/leaflet.css'`）
- [ ] 5.5 設定 `vite.config.ts` 內 `server.proxy = { '/api': { target: 'http://localhost:8080', changeOrigin: true } }`（對應 frontend-demo-app spec「Vite dev proxy」Requirement）
- [ ] 5.6 新增 `frontend/.gitignore`（node_modules, dist, .env.local）
- [ ] 5.7 修改根 `.gitignore` 不要 ignore `frontend/`（確認）

## 6. frontend：型別與 API 層

- [ ] 6.1 新增 `frontend/src/types/api.ts`：`RouteResponse`、`RouteItem`、`SpeedCamera`、`ParkingSuggestion`、`GeocodeResult`、`GeocodeResponse`、`ChatMessageRequest`、`ChatMessageResponse`（與 Kotlin DTO 對齊，camelCase）
- [ ] 6.2 新增 `frontend/src/api/client.ts`：統一 fetch wrapper、處理 5xx/4xx/network error、回 typed Promise
- [ ] 6.3 新增 `frontend/src/api/route.ts`：`postRoute(req: RouteRequest): Promise<RouteResponse>`
- [ ] 6.4 新增 `frontend/src/api/geocode.ts`：`geocode(q: string, opts?: { cityHint?: string; limit?: number }): Promise<GeocodeResult[]>`
- [ ] 6.5 新增 `frontend/src/api/chat.ts`：`postChatMessage(content: string, sessionId: string): Promise<ChatMessageResponse>`

## 7. frontend：狀態管理

- [ ] 7.1 新增 `frontend/src/store/index.ts`：zustand store 含 `routeSlice`（currentRoute, originMarker, destMarker, selectedRouteIndex）、`chatSlice`（messages, sessionId）、`uiSlice`（loading, errorToast, theme）
- [ ] 7.2 在 store 內處理 sessionId：初始化時讀 `localStorage.sid`，不存在則 `crypto.randomUUID()` 寫回
- [ ] 7.3 在 store 內處理 theme：初始化時讀 `localStorage.theme` 或 `prefers-color-scheme`，套用 `<html class="dark">`

## 8. frontend：地圖元件

- [ ] 8.1 新增 `frontend/src/components/MapView.tsx`：`<MapContainer center={[25.0478, 121.5170]} zoom={14}>`，OSM tile layer
- [ ] 8.2 訂閱 store `currentRoute` 並畫 `<Polyline>`（用 path node 的 lat/lng）、起終點 marker、`speedCameras` markers（紅）、`parkingSuggestions` markers（綠）
- [ ] 8.3 路線變動時 `useEffect` 呼叫 `map.fitBounds(...)` 自動縮放
- [ ] 8.4 加 onClick handler，把點到的座標填入 store 內目前焦點輸入框（透過 uiSlice 的 `focusedInput` 狀態）

## 9. frontend：路線表單 + autocomplete

- [ ] 9.1 新增 `frontend/src/components/AddressInput.tsx`：輸入框 + 下拉建議；用 debounce hook 300ms 後呼叫 `geocode(q)`
- [ ] 9.2 新增 `frontend/src/hooks/useDebounce.ts`
- [ ] 9.3 新增 `frontend/src/components/RouteForm.tsx`：兩個 `AddressInput`（起點 / 終點）+「規劃路線」按鈕 + loading 狀態；送出時呼叫 `postRoute` 寫進 store
- [ ] 9.4 新增 `frontend/src/components/RouteSummary.tsx`：顯示 `distanceKm` (1 位小數)、`estimatedTimeMin` (四捨五入整數)、測速照相數量、停車場列表
- [ ] 9.5 在 `RouteSummary` 內加路線切換器（當 `routes.length > 1`）

## 10. frontend：chat 面板

- [ ] 10.1 新增 `frontend/src/components/ChatPanel.tsx`：訊息列表 + 輸入框 + 送出按鈕
- [ ] 10.2 新增 `frontend/src/components/ChatMessage.tsx`：分 user / agent 兩種樣式
- [ ] 10.3 送出後若回應帶 `routeResult` 非 null，把它寫進 store `currentRoute`（地圖會自動更新）
- [ ] 10.4 顯示「agent 思考中...」loading 狀態

## 11. frontend：整體 layout + dark mode 切換

- [ ] 11.1 修改 `frontend/src/App.tsx`：左側 panel（RouteForm + RouteSummary + ChatPanel）、右側 MapView，響應式 layout
- [ ] 11.2 新增 `frontend/src/components/ThemeToggle.tsx`：切換 light / dark，更新 store + localStorage + `<html class>`
- [ ] 11.3 把 ThemeToggle 放進 header

## 12. frontend：錯誤處理 / toast

- [ ] 12.1 新增 `frontend/src/components/Toast.tsx`：訂閱 store `errorToast`，顯示後 3 秒自動隱藏
- [ ] 12.2 在 `api/client.ts` 內把 fetch 錯誤統一寫進 store `errorToast`

## 13. frontend：基本測試

- [ ] 13.1 加入 Vitest + React Testing Library：`npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom`
- [ ] 13.2 設定 `vite.config.ts` 加 `test: { environment: 'jsdom' }`
- [ ] 13.3 寫 smoke test：`App.test.tsx` 驗證 render 不噴錯、含 MapView、含 RouteForm
- [ ] 13.4 寫 `useDebounce.test.ts`
- [ ] 13.5 跑 `npm test` 確認綠燈

## 14. 整合驗收

- [ ] 14.1 啟動三服務：infra (Kafka/Postgres/Redis 用 compose) + main-service + multiagent-service + frontend dev server
- [ ] 14.2 在前端測試：輸入「台北車站」「中正紀念堂」→ autocomplete 出建議 → 選一個 → 點規劃 → 看到路線
- [ ] 14.3 在前端測試：地圖上點兩下 → 規劃 → 看到路線
- [ ] 14.4 在前端測試：chat 打「我要從台北車站到忠孝復興」→ 回應帶路線並顯示在地圖
- [ ] 14.5 測試 dark mode 切換、重新整理後保留
- [ ] 14.6 測試 504 timeout（暫停 multiagent）→ toast 顯示
- [ ] 14.7 測試 504 timeout（query 無結果）→ toast 顯示

## 15. 文件 / 收尾

- [ ] 15.1 在 `README.md` 新增「frontend 啟動方式」一段
- [ ] 15.2 在 `references/manual-acceptance-runbook.md`（已有）新增本 change 的手動驗收步驟（對應 §14）
- [ ] 15.3 用 `openspec validate complete-demo-stack --strict` 確認無誤
- [ ] 15.4 commit 本 change 的 openspec/changes 內所有檔案（依專案慣例）
