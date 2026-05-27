## ADDED Requirements

### Requirement: 前端專案結構
專案 SHALL 包含 `frontend/` 目錄，內含 Vite + React 19 + TypeScript + Tailwind CSS + zustand + react-leaflet 應用。

#### Scenario: 目錄就位
- **WHEN** 檢查 repo 根目錄
- **THEN** SHALL 存在 `frontend/package.json`、`frontend/vite.config.ts`、`frontend/tsconfig.json`、`frontend/tailwind.config.js`、`frontend/index.html`、`frontend/src/main.tsx`、`frontend/src/App.tsx`

#### Scenario: 開發伺服器啟動
- **WHEN** 在 `frontend/` 執行 `npm install && npm run dev`
- **THEN** Vite SHALL 在 `http://localhost:5173` 啟動

#### Scenario: production build
- **WHEN** 執行 `npm run build`
- **THEN** SHALL 產出 `frontend/dist/` 內含可用的 SPA 靜態檔，且 SHALL 通過 `tsc --noEmit` 型別檢查

### Requirement: Vite dev proxy
`frontend/vite.config.ts` SHALL 在 `server.proxy` 設定 `/api` 轉發到 `http://localhost:8081`，避免在 main-service 加 CORS 設定。

#### Scenario: API 請求轉發
- **WHEN** 前端在 dev 模式下發送 `fetch('/api/v1/route', ...)` 或任何 `/api/*` 請求
- **THEN** Vite dev server SHALL 把該請求轉發到 `http://localhost:8081`，並把 main-service 的回應原封轉回瀏覽器
- **AND** 瀏覽器 SHALL NOT 看到 CORS 錯誤

#### Scenario: proxy 設定缺失偵測
- **WHEN** 檢查 `frontend/vite.config.ts`
- **THEN** SHALL 看到 `server: { proxy: { '/api': { target: 'http://localhost:8081', changeOrigin: true } } }` 或等效 TS 設定

### Requirement: 地圖顯示
應用 SHALL 用 react-leaflet 顯示一張 Leaflet 地圖，預設中心為台北車站（25.0478, 121.5170），預設 zoom 14，圖磚來源為 OpenStreetMap (`https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png`)。

#### Scenario: 地圖初始化
- **WHEN** 使用者開啟應用
- **THEN** SHALL 看到台北車站附近的地圖，可拖曳、滾輪縮放

#### Scenario: 顯示路線
- **WHEN** zustand store 內 `currentRoute` 非 null
- **THEN** 地圖 SHALL 畫出 polyline（路徑各節點以 lat/lng 連線）、起點 marker、終點 marker、所有測速照相 marker、所有停車場建議 marker
- **AND** SHALL 自動 fitBounds 縮放到能涵蓋整條路線

#### Scenario: 清除路線
- **WHEN** zustand store 內 `currentRoute` 設為 null
- **THEN** 地圖上 polyline、起終點 marker、測速照相、停車場 SHALL 全部清除

### Requirement: 路線表單
應用 SHALL 提供一個表單元件，使用者可以指定起點與終點並送出路線規劃請求。

#### Scenario: 地址輸入 autocomplete
- **WHEN** 使用者在「起點」或「終點」輸入框內輸入文字
- **THEN** 應用 SHALL 在使用者停止輸入 300 ms 後呼叫 `GET /api/v1/geocode?q=<query>&limit=5` 並把回傳的 `results` 顯示為下拉建議
- **AND** 點選任一建議 SHALL 把該座標填入該輸入框並把 marker 放上地圖

#### Scenario: 地圖點選
- **WHEN** 使用者在地圖上單擊
- **THEN** 應用 SHALL 將該座標填入目前焦點的輸入框（起點或終點，依使用者選擇的模式）

#### Scenario: 送出規劃
- **WHEN** 起點與終點皆已設定且使用者點擊「規劃路線」按鈕
- **THEN** 應用 SHALL 呼叫 `POST /api/v1/route` 帶 `{ originLat, originLng, destLat, destLng, topK: 3 }`
- **AND** 在等待回應期間 SHALL 顯示 loading 狀態（按鈕 disabled、spinner）

#### Scenario: 規劃失敗
- **WHEN** API 回傳 HTTP 504 或 `routes` 為空陣列且 `error` 非 null
- **THEN** SHALL 顯示 toast 含後端回傳的 error 字串（或預設「找不到可行路線」）並讓使用者重試

### Requirement: 路線資訊顯示
應用 SHALL 顯示目前路線的距離、預估時間、測速照相數量、附近停車場列表。

#### Scenario: 顯示路線摘要
- **WHEN** `currentRoute` 已設定
- **THEN** SHALL 顯示 `distanceKm`（公里、1 位小數）、`estimatedTimeMin`（分鐘、整數四捨五入）、`speedCameras` 數量、`parkingSuggestions` 列表（名稱、空位數、距離）

#### Scenario: 多條路線切換
- **WHEN** API 回傳 `routes.length > 1`
- **THEN** SHALL 顯示分頁切換器（例如 "路線 1 / 2 / 3"）讓使用者切換顯示
- **AND** 預設選中第一條

### Requirement: Chat 對話面板
應用 SHALL 提供一個 chat 面板，使用者可與 multiagent chat agent 對話，且 agent 回應若帶 `routeResult` SHALL 同步顯示於地圖。

#### Scenario: 送出訊息
- **WHEN** 使用者於 chat 輸入框打字並按 Enter 或點送出
- **THEN** SHALL 呼叫 `POST /api/v1/chat/message` 帶 `{ session_id, content }`
- **AND** 在等待期間 SHALL 顯示「agent 思考中...」狀態

#### Scenario: 收到純文字回應
- **WHEN** API 回傳 `{ reply, suggested_actions }` 且無 `routeResult`
- **THEN** SHALL 把 `reply` 加進對話紀錄

#### Scenario: 收到帶路線的回應
- **WHEN** API 回傳含非 null `routeResult`
- **THEN** SHALL 把 `reply` 加進對話紀錄，並把 `routeResult` 寫入 zustand `currentRoute`（地圖會自動更新）

#### Scenario: session_id 持久化
- **WHEN** 應用啟動
- **THEN** SHALL 從 `localStorage` 讀取 `sid`，若不存在則用 `crypto.randomUUID()` 產生並寫回 `localStorage`
- **AND** 整個 session 內所有 chat 請求 SHALL 帶同一個 `session_id`

### Requirement: Dark mode
應用 SHALL 支援 light / dark 兩種主題，使用者可手動切換，且選擇 SHALL 持久化。

#### Scenario: 預設主題
- **WHEN** 使用者首次開啟應用
- **THEN** SHALL 依 `prefers-color-scheme` media query 決定預設主題

#### Scenario: 手動切換
- **WHEN** 使用者點擊主題切換鈕
- **THEN** SHALL 切換 `<html>` 元素的 `dark` class、並把選擇寫入 `localStorage.theme`（值 `"dark"` 或 `"light"`）

#### Scenario: 持久化
- **WHEN** 使用者重新整理或重新開啟頁面
- **THEN** 應用 SHALL 從 `localStorage.theme` 還原上次選擇

### Requirement: API 呼叫錯誤處理
所有對 main-service 的 fetch SHALL 統一錯誤處理：網路失敗、HTTP 4xx/5xx 都顯示 toast。

#### Scenario: 網路斷線
- **WHEN** fetch 拋出（無法連線）
- **THEN** SHALL 顯示 toast「無法連線後端，請檢查網路或服務狀態」並讓使用者重試

#### Scenario: HTTP 5xx
- **WHEN** API 回傳 5xx
- **THEN** SHALL 顯示 toast 含 response body 的 `error` 字串（若有），否則「伺服器發生錯誤」

#### Scenario: HTTP 4xx
- **WHEN** API 回傳 4xx
- **THEN** SHALL 顯示 toast 含 response body 的 `error` 字串（若有），否則「請求格式錯誤」

### Requirement: 不做使用者登入
應用 SHALL NOT 包含登入、註冊、密碼重設等使用者身份相關 UI 或邏輯。

#### Scenario: 進入即可用
- **WHEN** 使用者首次開啟應用
- **THEN** SHALL 直接看到主畫面（地圖 + 表單 + chat），無登入頁、無身份驗證攔截
