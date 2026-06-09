## Why

Kaohsiung VD live data 實測證實不可用：156 顆 VD 中只有 ~17 顆回得到「健康」資料，但這些「健康」值幾乎都是 `255` 感測器哨兵值（Speed/Occupancy/Volume 同時為 255、無 ErrorType 可過濾），不是真實車速；同時 `Live/City/Tainan` 在台南火車站 2.2km 範圍內 0 個 section 有 live 資料。實測 `Live/City/Taipei` 凌晨離峰時段已有 1058 sections 中 611 筆（57.8%）報實際 TravelSpeed（min 2 / max 93 / mean 49 km/h），台北車站 2.2km bbox 內 81 sections 全部有 live 資料。Capstone 剩下 6 週，必須立刻把資料底盤切到 Taipei 才能讓 A* 真的吃到即時路況；同時要把 `plan_optimal_route` 接到 Gemini agent 讓 chat.request 能觸發路徑規劃，否則整套演算法在 demo 時看不到。

## What Changes

### BREAKING

- 路網資料從 Kaohsiung 切換到 Taipei，bbox 改為台北車站 (25.0478, 121.5170) 半徑 2.2km
- 完整移除 VD 路徑相關程式碼：`VDSensor` model、`vd_sensor` table、`src/db/vd_sensor.py`、`tests/test_vd.py`、`main.py` 中的 `seed_vd_sensors()` lifespan 呼叫
- `route.request` / `route.response` Kafka 訊息 wire schema 完全重寫：`route.request` 從 `origin`/`destination` (string `"lat,lng"`) + `preferences` 改為 `origin_lat`/`origin_lng`/`dest_lat`/`dest_lng`/`top_k` 純結構化欄位；`route.response` 從 `route_id` + `path` (string) + `estimated_time` (int) 改為 `routes` 陣列（每筆含 `path`/`edges`/`road_names`/`estimated_time_min`/`distance_km`/`speed_cameras`）+ optional `error`。實作端 `plan_optimal_route` 早已採用新簽章，本次是把 spec 對齊；但任何仍依賴舊字串 schema 的 main-service 客戶端必須同步調整
- 移除 `traffic.metrics` Kafka topic 與 `handle_traffic_metrics` consumer handler — 原本由外部 YOLO 節點上報擁塞資料，現在系統一律走 TDX live API、不再吃外部訊號

### Non-breaking

- 還原 `src/agents/traffic.py` 為 Section-based fetch（`Live/City/Taipei`），filter 條件統一為 `TravelSpeed <= 0` 或 `TravelTime <= 0`（TDX 的 `-99` 哨兵）
- 更新 `scripts/import_tdx_road_network.py`：city 改為 Taipei、bbox 改為台北車站 2.2km、輸出檔改名 `data/taipei_road_sections.json`
- DB migration：drop `vd_sensor` table、truncate `traffic_node` / `traffic_edge` / `traffic_history`、re-seed
- 新增 `src/mcp_servers/routing_tool.py`：把 `plan_optimal_route` 包成 MCP tool，使用 Pydantic schema 定義 input / output，讓 Gemini agent 可發現並呼叫
- 新增 `src/agents/chat_agent.py`：Gemini-based chat agent，能依使用者自然語言（例如「幫我從 A 到 B 規劃路線」）決定呼叫 routing tool；用 google-genai SDK 與既有的 mcp 套件整合
- 改寫 `handle_chat_request`：從目前的 stub 改成轉交給 chat agent 處理；agent 回覆同時包含自然語言訊息與結構化 `route_payload`，發送到 `chat.response`（向下相容：`route_payload` 為 optional 欄位，缺少時 main-service 視為純文字回覆）
- speed camera 相關程式碼維持原狀但**先不打開**（`seed_speed_cameras` lifespan 呼叫保留，但因 `data/speed_cameras.csv` 是 Kaohsiung 的，會 silently no-op）
- 為日後其他微服務（停車、事件回報、第三方資料源等）能透過 Kafka 接入，新增 topic 命名與 pattern 規範（`*.request`/`*.response` 為 request/response、`*.event` 為 fire-and-forget）、訊息 envelope 約定、以及 inbound topic registry 擴充點：訂閱清單由 `KAFKA_SUBSCRIBE_TOPICS` env var 控制（預設 `chat.request,route.request`）、新 handler 透過 `TOPIC_HANDLERS` dict 註冊、未知 topic 不會 crash consumer

## Capabilities

### New Capabilities
- `agent-routing-tool`: Gemini chat agent 透過 MCP tool 呼叫 `plan_optimal_route`，把自然語言路線請求轉成結構化路徑回覆

### Modified Capabilities
- `tdx-live-traffic`: 目標城市從 Kaohsiung 改為 Taipei；endpoint 統一使用 `basic/v2/Road/Traffic/Live/City/Taipei`（Section level）
- `road-network-import`: 目標城市從 Kaohsiung 改為 Taipei；bounding box 改為台北車站 (25.0478, 121.5170) ±2.2km
- `kafka-messaging`: `route.response` 訊息加上嚴格 JSON schema 定義；`chat.response` 訊息加上選擇性 `route_payload` 欄位；移除 `traffic.metrics` topic 與其訊息格式；新增 topic 命名與 pattern 規範（request/response、event 兩類）以及 inbound topic registry 擴充點規範
- `service-structure`: Kafka consumer 訂閱清單由 `KAFKA_SUBSCRIBE_TOPICS` env var 決定（預設 `chat.request,route.request`），不再硬寫 `traffic.metrics`

## Impact

- **程式碼新增**：`src/mcp_servers/routing_tool.py`、`src/agents/chat_agent.py`、`tests/test_routing_tool.py`、`tests/test_chat_agent.py`
- **程式碼移除**：`src/db/vd_sensor.py`、`tests/test_vd.py`、`tests/test_traffic.py`（重寫成 Section-based）
- **程式碼修改**：`src/agents/traffic.py`（還原成 Section）、`src/agents/routing.py`（新增 input/output Pydantic schema）、`src/db/models.py`（移除 `VDSensor`）、`main.py`（移除 `seed_vd_sensors` 呼叫、新增 chat agent lifespan 啟動）、`src/kafka/consumer.py`（chat handler 改用 agent；移除 `traffic.metrics` 訂閱與 `handle_traffic_metrics`）
- **DB schema**：drop `vd_sensor` 表；`traffic_node` / `traffic_edge` / `traffic_history` truncate
- **資料**：新增 `data/taipei_road_sections.json`，移除 `data/kaohsiung_road_sections.json` 的使用（檔案保留作為歷史紀錄）
- **環境變數**：新增 `GEMINI_API_KEY`（Gemini agent 需要）、保留 `TDX_CLIENT_ID` / `TDX_CLIENT_SECRET`
- **Kafka topics**：不新增 topic，但 `chat.response` payload 新增 `route_payload` 欄位（向下相容：缺少時 main-service 仍可運作）
- **測試**：既有 80 個測試刪除 17 個 VD 相關（剩 63）；重寫 `tests/test_traffic.py`（≥ 3 cases）；新增 `tests/test_routing_tool.py`（≥ 4 cases）、`tests/test_chat_agent.py`（≥ 5 cases）、`tests/test_consumer_extensibility.py`（4 cases，對應 spec 中 env var / 未知 topic / JSON 壞掉 / handler 例外四個 scenario）。預估 63 + ~16 ≈ 79，目標 ≥ 75 測試全綠
- **外部依賴**：需要可用的 Gemini API key；TDX 已有
- **向後相容**：BREAKING — 現有 Kaohsiung 路網資料失效，必須重新 seed Taipei 路網才能 demo 路線規劃
