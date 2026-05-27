## 1. VD 程式碼與資料移除

- [x] 1.1 刪除 `src/db/vd_sensor.py`、`tests/test_vd.py`
- [x] 1.2 從 `src/db/models.py` 移除 `VDSensor` model
- [x] 1.3 從 `main.py` 移除 `seed_vd_sensors` import 與 lifespan 呼叫
- [x] 1.4 從 `infra/init-db/02-road-network-tables.sql` 移除 `vd_sensor` 表與索引區塊
- [x] 1.5 對既有 DB 執行 `DROP TABLE IF EXISTS vd_sensor;`
- [x] 1.6 從 `backend/multiagent-service/scripts/` 移除 `inspect_vd_raw.py`、`demo_vd_live.py`（已不適用）；保留 `inspect_tainan_*` / `inspect_taipei_*` 作為調查紀錄或一併刪除

## 2. Taipei 路網匯入

- [x] 2.1 修改 `scripts/import_tdx_road_network.py`：`TDX_SECTION_URL` 與 `TDX_SECTION_SHAPE_URL` 改 Taipei、`BBOX_SW=(25.0278, 121.4970)`、`BBOX_NE=(25.0678, 121.5370)`、`OUTPUT_PATH=data/taipei_road_sections.json`
- [x] 2.2 跑 `python scripts/import_tdx_road_network.py` 抓取資料、寫出 `data/taipei_road_sections.json`，目標筆數 ≥ 50 sections
- [x] 2.3 修改 `src/db/road_network.py:DEFAULT_JSON_PATH` 指到 `data/taipei_road_sections.json`
- [x] 2.4 對既有 DB 執行 `TRUNCATE TABLE traffic_node, traffic_edge, traffic_history RESTART IDENTITY CASCADE;`
- [x] 2.5 重啟 service 確認 `seed_road_network` 走過、`SELECT COUNT(*) FROM traffic_edge` > 50
- [x] 2.6 把舊的 `data/kaohsiung_road_sections.json` 留檔不刪、不再被讀取

## 3. Section-based live fetch 還原

- [x] 3.1 重寫 `src/agents/traffic.py`：URL 改為 `https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/Live/City/Taipei`、resurrect `fetch_live_section_data`（**從 git history `cdfa0d1`**（"feat: A* + TDX Live traffic integration"）取 Section 版骨架；注意：`aece31f` 是「Section → VD」切換的 commit，那邊已沒有 Section 程式碼可用 + 加 Taipei filter `TravelSpeed <= 0` / `TravelTime <= 0` / `CongestionLevel = "-99"`）；保留 `update_redis_cache` / `update_timescaledb` / `update_graph_weights` 介面
- [x] 3.2 移除所有 VD 相關 import、helper、cache（`load_vd_edge_map`、`aggregate_edge_speeds`、`_filter_healthy`、`_edge_map_cache`）
- [x] 3.3 重寫 `tests/test_traffic.py`：mock httpx MockTransport 回 `{LiveTraffics: [{...}]}` Section payload，驗證 `refresh_traffic_data` 正確 filter `-99` 並更新 Redis/DB/graph
- [x] 3.4 跑 `uv run pytest -q` 確認 Section path 全綠

## 4. MCP routing tool

- [x] 4.1 建立 `src/mcp_servers/routing_tool.py`：`PlanRouteInput` / `RouteResponse` Pydantic 模型；`build_routing_mcp_server()` 註冊 `plan_route` tool
- [x] 4.2 在 `src/agents/routing.py` 把 `plan_optimal_route` 的回傳 dict 改成 `RouteResponse(...).model_dump()`，確保欄位穩定
- [x] 4.3 新增 `tests/test_routing_tool.py`：input schema validation（缺欄位、型別錯誤）、output schema 對齊、tool 呼叫 happy path（mock plan_optimal_route）

## 5. Gemini chat agent

- [x] 5.1 建立 `src/agents/chat_agent.py`：`ChatAgent` class 用 google-genai SDK + 上面的 MCP tool；提供 `agenerate(content: str) -> {reply: str, route_payload: dict | None}`
- [x] 5.2 加 `GEMINI_API_KEY` 缺失時的 graceful fallback（log WARNING、回 stub 訊息、`route_payload = None`）
- [x] 5.3 加 15 秒 timeout 與通用錯誤訊息（API fail / timeout）
- [x] 5.4 改 `src/kafka/consumer.py:handle_chat_request`：原 stub 改成 `await chat_agent.agenerate(content)`、把 `reply` 與 `route_payload` 寫入 `chat.response`；`suggested_actions` 維持
- [x] 5.5 在 `main.py` lifespan 初始化 `chat_agent` 並透過 `kafka_runtime` 暴露給 consumer thread
- [x] 5.6 新增 `tests/test_chat_agent.py`：mock `genai.Client`，測試「路線意圖→tool-call→帶 route_payload」、「閒聊→reply only、route_payload=None」、「API key 缺失→fallback」、「timeout→錯誤訊息」
- [x] 5.7 從 `src/kafka/consumer.py` 移除 `handle_traffic_metrics` 函數、`TOPIC_HANDLERS["traffic.metrics"]` 條目
- [x] 5.8 從 `tests/` 移除任何依賴 `handle_traffic_metrics` 的測試（若有）；`uv run pytest -q` 維持綠
- [x] 5.9 把 `TOPICS` list 改為從 `os.getenv("KAFKA_SUBSCRIBE_TOPICS", "chat.request,route.request").split(",")` 解析；空字串/空白容錯
- [x] 5.10 改 `_consumer_loop`：當訊息來自訂閱了但 `TOPIC_HANDLERS` 找不到的 topic 時，log WARN（含 topic + key）後跳過；JSON decode 失敗時 log ERROR 跳過；handler 拋例外時 log ERROR 跳過；任一情況皆不得讓 consumer thread 終止
- [x] 5.11 新增 `tests/test_consumer_extensibility.py`：mock Consumer 驗證 (a) `KAFKA_SUBSCRIBE_TOPICS=foo.bar,baz.qux` 時實際訂閱清單為 `["foo.bar", "baz.qux"]`、(b) 收到無 handler 的 topic 訊息只 log WARN 不 crash、(c) JSON 壞掉只 log ERROR 不 crash、(d) handler 拋例外只 log ERROR 不 crash、(e) 空字串 / 全空白 env var 時 fallback 到預設清單

## 6. Spec 同步與文件

- [x] 6.1 確認 `openspec validate taipei-agent-routing` 通過
- [x] 6.2 更新 `backend/multiagent-service/README.md`（如有）說明新增的 `GEMINI_API_KEY` 環境變數與 chat agent 流程
- [x] 6.3 在 `references/` 留一份簡短 implementation note（taipei-agent-routing-implementation.md）對應這次的實作要點

## 7. 整合驗證與 demo

- [x] 7.1 重啟 service，觀察 log：road network seed 完成、TDX live refresh 至少打一輪且 N edges updated > 0
- [x] 7.2 `docker exec traffic_db psql -c "SELECT COUNT(*) FROM traffic_history;"` ≥ 1
- [x] 7.3 `docker exec traffic_cache redis-cli --scan --pattern 'traffic:section:*' | wc -l` ≥ 1
- [x] 7.4 直接呼叫 `plan_route` MCP tool（用一支小 demo script）走「台北車站 → 信義誠品」，確認回傳 routes 陣列非空
- [x] 7.5 從 Kafka 端送一筆 `chat.request`（用 `kafka-console-producer` 或 Python 小工具），訊息 `「我從台北車站想去信義誠品」`；驗證 `chat.response` 上有 `reply` 與非空 `route_payload`
- [x] 7.6 既有非 VD 測試（baseline 63 個）+ 新增 traffic / routing tool / chat agent 測試 → `uv run pytest -q` 全綠且總數 ≥ 80
- [x] 7.7 Commit + push `develop`  (commit `7e917e4`; push deferred to user)
