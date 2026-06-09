## ADDED Requirements

### Requirement: Routing MCP tool 暴露
multiagent-service SHALL 透過 MCP server 暴露名為 `plan_route` 的 tool，包裝 `plan_optimal_route()` 函數，提供 Pydantic-validated 的 input/output schema。

#### Scenario: tool 註冊
- **WHEN** multiagent-service 啟動
- **THEN** SHALL 在記憶體中初始化一個 MCP server 並註冊 `plan_route` tool
- **AND** tool 的 input schema SHALL 為 `PlanRouteInput { origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float, top_k: int = 3 }`
- **AND** tool 的 output schema SHALL 對齊 `RouteResponse`，每筆 route 包含 `path: list[int]`、`edges: list[int]`、`road_names: list[str]`、`estimated_time_min: float`、`distance_km: float`、`speed_cameras: list[dict]`

#### Scenario: 成功呼叫 plan_route tool
- **WHEN** 帶入合法 origin/dest 座標呼叫 `plan_route`
- **THEN** tool SHALL 透過 in-memory RoadGraph 與 async session 呼叫 `plan_optimal_route`
- **AND** SHALL 回傳序列化後的 `{ "routes": [...] }` JSON

#### Scenario: 起終點無法 snap 到 graph
- **WHEN** origin 或 dest 座標距離所有 graph node 都太遠
- **THEN** tool SHALL 回傳 `{ "routes": [], "error": "could not snap origin/destination to graph" }` 而不拋例外

#### Scenario: 找不到路徑
- **WHEN** origin 與 dest 在路網上不連通
- **THEN** tool SHALL 回傳 `{ "routes": [], "error": "no path found between origin and destination" }`

### Requirement: Gemini chat agent 整合
multiagent-service SHALL 以 google-genai SDK 建立 chat agent，會自動辨識使用者的路線規劃意圖並透過 MCP `plan_route` tool 取得結果。

#### Scenario: 使用者表達路線意圖
- **WHEN** chat 訊息明顯表達「從 A 到 B 規劃路線」之類的意圖（自然語言）
- **THEN** Gemini agent SHALL 經由 tool-calling 機制呼叫 `plan_route`，使用 agent 從訊息抽取的 `origin` / `destination` 座標
- **AND** agent SHALL 回傳一個包含 `reply`（自然語言）與 `route_payload`（`plan_route` 的原始回傳，dict）的結果物件

#### Scenario: 使用者非路線意圖
- **WHEN** chat 訊息與路線無關（例如打招呼、問天氣）
- **THEN** agent SHALL 直接以自然語言回覆且 `route_payload = None`

#### Scenario: GEMINI_API_KEY 未設定
- **WHEN** 環境變數 `GEMINI_API_KEY` 不存在
- **THEN** agent SHALL fallback 為原 stub 回覆「AI 推論功能尚未啟用」並 log WARNING，不阻擋 service 啟動

#### Scenario: Gemini API 故障或逾時
- **WHEN** Gemini API 呼叫失敗或在 15 秒內未回應
- **THEN** agent SHALL 回傳通用錯誤訊息（例如「目前服務忙線，請稍後再試」），且 `route_payload = None`，並記錄 ERROR log

### Requirement: Chat handler 整合 agent
`handle_chat_request` SHALL 把訊息交給 chat agent 處理，並把 agent 的結果送回 `chat.response`。

#### Scenario: 路線意圖訊息流
- **WHEN** Kafka topic `chat.request` 收到一筆訊息且 agent 判定為路線意圖
- **THEN** `handle_chat_request` SHALL 呼叫 chat agent、把 agent 的 `reply` 寫入 `chat.response.reply`、把 `route_payload` 寫入 `chat.response.route_payload`、`suggested_actions` 維持原有提示

#### Scenario: 非路線意圖訊息流
- **WHEN** agent 判定訊息與路線無關
- **THEN** `chat.response.route_payload` SHALL 為 `null`（或省略），`reply` 為 agent 的自然語言回覆

#### Scenario: Routing tool 未 ready
- **WHEN** RoadGraph / session_factory 尚未透過 `kafka_runtime.set_runtime` 注入
- **THEN** agent 偵測到 `plan_route` 不可用時 SHALL 回覆「服務啟動中」訊息，並 log WARNING
