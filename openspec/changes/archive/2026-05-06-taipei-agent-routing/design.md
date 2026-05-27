## Context

vd-live-traffic 已 archive，但實測發現 TDX VD live 在 Kaohsiung 幾乎全是哨兵值（255 / -99）；同時測試 `Live/City/Tainan` 在台南火車站 2.2km 內 0 個 section 有資料。`Live/City/Taipei` 在 03:16 凌晨離峰已有 1058 sections / 611 healthy（57.8%），台北車站 2.2km bbox 81/81 全覆蓋，是目前唯一適合 capstone urban routing demo 的 dataset。

當前程式狀態（commit `e4ad794`）：
- `traffic.py` 走 VD 路徑（`Live/VD/City/Kaohsiung`、`fetch_live_vd_data` + `aggregate_edge_speeds`）
- DB 有 154 條 Kaohsiung edges、156 顆 vd_sensor、78 筆 traffic_history
- `handle_route_request`（Kafka）已串好，能呼叫 `plan_optimal_route` 並回 `route.response`
- `handle_chat_request` 是 stub（`reply = f"...AI 推論功能開發中..."`），尚未接 Gemini
- `pyproject.toml` 已裝 `google-genai` 與 `mcp`，但 `src/mcp_servers/` 還空著

## Goals / Non-Goals

**Goals:**
- 把所有 VD 程式碼/資料/測試清乾淨，不留半殘狀態
- 路網改成 Taipei 火車站 ±2.2km bbox，A* 走得到城市道路
- `traffic.py` 還原成 Section-level fetch，且 filter 規則對齊 TDX `-99` 哨兵
- 把 `plan_optimal_route` 包成 MCP tool 暴露給 Gemini agent
- chat.request 自然語言 → Gemini agent → 自動觸發路徑規劃 → chat.response 帶結構化 `route_payload`
- 讓 capstone demo 可以「使用者輸入『我從台北車站想去信義誠品』→ 系統規劃路線並顯示」

**Non-Goals:**
- 不重寫 A* 演算法本身（已穩定，astar-routing spec 不動）
- 不開 speed camera（Kaohsiung 資料已失效，Taipei 資料這次不抓）
- 不做 Kafka 訊息版本協商（chat.response 加新欄位採向下相容，舊 client 忽略即可）
- 不做 TDX 多城市 fallback（Taipei 是唯一目標城市）
- 不做 streaming response（agent 同步回完整訊息一次）
- 不重新訓練/部署 YOLO（traffic.metrics 流程不在這次 scope 內）

## Decisions

### D1. 連 VD 一起 hard delete，不加 feature flag
**選擇**：完全刪除 `VDSensor` model、`vd_sensor` table、`src/db/vd_sensor.py`、`tests/test_vd.py`、`seed_vd_sensors` 呼叫；DB 執行 `DROP TABLE vd_sensor`。
**理由**：vd-live-traffic 已 archive，原始碼有完整紀錄；保留 feature flag 會帶兩條 code path 維護成本，capstone 沒這個預算。
**替代方案**：`LIVE_SOURCE=section|vd` env flag。放棄理由：使用者明確選 hard delete；且 VD 資料不可能短期內變好。

### D2. Live data filter 規則
**選擇**：`fetch_live_section_data` 過濾 `TravelSpeed <= 0` 或 `TravelTime <= 0`（兩者皆對應 TDX `-99` 哨兵）。`CongestionLevel = "-99"` 也視為無資料。
**理由**：實測 Taipei 凌晨資料 `-99` 出現在所有三個欄位，filter 任一就能丟掉整筆。
**代價**：偶有 `TravelSpeed > 0 但 TravelTime = -99` 的混合狀況也被丟掉，但保守一點對 demo 較好。

### D3. MCP tool 接 plan_optimal_route 的形狀
**選擇**：建立 `RoutingMCPServer`，暴露 `plan_route` tool，input schema 為：
```python
class PlanRouteInput(BaseModel):
    origin_lat: float
    origin_lng: float
    dest_lat: float
    dest_lng: float
    top_k: int = 3
```
output 直接回 `plan_optimal_route` 的 dict（routes 陣列），但用 `RouteResponse` Pydantic model 確保欄位穩定（`path`, `edges`, `road_names`, `estimated_time_min`, `distance_km`, `speed_cameras`）。
**理由**：MCP tool 必須有結構化 schema 才能讓 Gemini 自動 tool-call；與既有 `RouteResult` dataclass 對齊以免重複定義。
**替代方案**：直接讓 agent 呼叫 Python function（不走 MCP）。放棄理由：使用者明確說要 agent 識別，MCP 是 Gemini 識別 tool 的標準介面；且未來可以讓 main-service 也走 MCP。

### D4. Gemini agent 的觸發點
**選擇**：把 chat agent 寫在 multiagent-service 內、由 `handle_chat_request` 呼叫；agent 會回兩個東西：
- 自然語言 reply（給 chat.response.reply）
- optional `route_payload`（當 agent 判斷使用者要規劃路線時，把 routing tool 的結果原樣帶上）
**理由**：對 demo 來說最直接；main-service 不用知道 LLM 細節。
**替代方案**：agent 在 main-service。放棄理由：違反現有「LLM/agent 邏輯都放 multiagent-service」的分工。

### D5. 路網重生流程
**選擇**：
1. 改 `scripts/import_tdx_road_network.py`：`SECTION_URL` 改 Taipei、`BBOX_SW=(25.0278, 121.4970)` / `BBOX_NE=(25.0678, 121.5370)`（≈2.2km）、`OUTPUT_PATH=data/taipei_road_sections.json`
2. 跑 import script 抓資料寫檔
3. `docker exec traffic_db psql ... TRUNCATE traffic_node, traffic_edge, traffic_history RESTART IDENTITY CASCADE; DROP TABLE vd_sensor;`
4. 改 `src/db/road_network.py:DEFAULT_JSON_PATH` 指到 `taipei_road_sections.json`
5. 重啟 service → lifespan 自動 seed 新路網

**理由**：對齊既有 seed 流程；DB 不需要新 migration script，靠人工 SQL 一次性遷移即可（capstone scale）。
**代價**：truncate `traffic_history` 會丟掉測試期間累積的 78 筆 Kaohsiung 歷史，但這些資料本來就沒用。

### D6. 對 kafka-messaging spec 的更動
**選擇**：在 `route.response` 補嚴格 schema（每個欄位的型別、是否必填），新增 `chat.response.route_payload`（optional dict，等同 `route.response` payload）。
**理由**：使用者要求「agent 回應能被識別解析」；要被 main-service / frontend 穩定解析就要有合約。

### D7. 測試策略
**選擇**：
- 刪除 17 個 VD-related tests
- 重寫 `tests/test_traffic.py` 用 Section payload (`LiveTraffics`)
- 新增 `tests/test_routing_tool.py`：MCP tool input/output schema validation + plan_route tool function smoke test（mock plan_optimal_route）
- 新增 `tests/test_chat_agent.py`：mock Gemini API 與 MCP tool，驗證 agent 在路線意圖訊息下會呼叫 routing tool 並 emit `route_payload`；同時涵蓋 fallback / timeout
- 新增 `tests/test_consumer_extensibility.py`：驗 `KAFKA_SUBSCRIBE_TOPICS` 解析、未知 topic / JSON 壞掉 / handler 例外不 crash consumer

**理由**：每一層都有單測；既有 63 個非 VD 測試保留。Kafka chat handler → agent → response 的整合測試暫不獨立寫一個 `test_kafka_route_e2e.py`，因 `test_chat_agent.py`（mock Gemini）+ `test_consumer_extensibility.py`（驗 dispatcher）+ 7.5 手動 demo（真打一次 Kafka）已涵蓋路徑，再加一個整合測對 capstone 邊際效益低。

### D8. 移除 `traffic.metrics` 並把 inbound consumer 設計成 registry-extensible
**選擇**：
1. 徹底刪除 `traffic.metrics` topic、`handle_traffic_metrics` 函數
2. 同時把 inbound consumer 的訂閱機制做成可擴充：訂閱清單由 `KAFKA_SUBSCRIBE_TOPICS` env var 決定（預設 `chat.request,route.request`）；handler 仍由程式碼註冊到 `TOPIC_HANDLERS` dict；訂閱了沒有對應 handler 的 topic 時，dispatcher SHALL log WARN 跳過、不 crash
3. 在 spec 寫清楚 topic 命名與 pattern 規範：`<domain>.<verb>` 命名；`*.request`/`*.response` 為 request/response pattern（必帶 `correlation_id`）、`*.event` 為 fire-and-forget pattern（`correlation_id` 可選用於 tracing）

**理由**：
- 移除 YOLO 訊號來源（graph 權重完全靠 TDX live polling 驅動）
- 但保留「未來其他微服務（停車、事件、外部資料）能透過 Kafka 接入」的擴充點，避免每接一個服務就要動 dispatcher 結構
- `KAFKA_SUBSCRIBE_TOPICS` 讓營運/部署能不改 code 就調整訂閱範圍（Level 2 彈性，符合 capstone scope）
- 兩種 pattern（req/resp + event）足以涵蓋目前能想到的微服務情境；command pattern（`route.invalidate` 之類）暫不規範，等真有需求再補

**替代方案**：
- 保留 `traffic.metrics` 與 stub handler 當未來插槽。放棄理由：spec 與實作不一致會誤導 reader；既有 stub 沒任何 producer 端可依賴
- 走 Level 3（generic envelope + dispatcher routing by `type`）。放棄理由：對 capstone overkill，要重寫所有 handler 與下游消費者

**對應 spec**：
- `kafka-messaging`：MODIFIED 既有「Kafka topic schema 定義」拿掉 `traffic.metrics`；ADDED「Topic naming and pattern conventions」與「Inbound topic registry extensibility」兩個 Requirement
- `service-structure`：MODIFIED「Kafka consumer 訂閱的 topic」scenario，改為由 env var 決定

## Risks / Trade-offs

- **[Gemini API 成本/limits]** demo 期間若 Gemini API 被密集呼叫可能觸 quota
  → Mitigation: 預設用 `gemini-1.5-flash`（便宜）、加 in-memory rate limit、未配 `GEMINI_API_KEY` 時 chat agent fallback 回原 stub 訊息

- **[MCP tool latency 疊加]** chat → agent → MCP → routing → Redis/DB → response，可能 > 10 秒
  → Mitigation: route.request 端已有 10s timeout；chat agent 走同 future.result(timeout=15)；超時就回「規劃中，請稍後重試」

- **[Taipei 路網 size 比 Kaohsiung 大]** 1058 vs 154 sections，A* heuristic 仍可線性擴展，但 RoadGraph.from_db 啟動時間會變
  → Mitigation: 仍可接受（單位是 hundreds of edges 而非 thousands）；若 import 後實際 edges > 500，回頭看是否要 sub-bbox

- **[Pydantic schema 對齊問題]** MCP tool 與 plan_optimal_route 的 dict 欄位若 drift，agent tool-call 會失敗
  → Mitigation: 讓 `plan_optimal_route` 直接 return `RouteResponse(...).model_dump()`，單一事實來源

- **[現場 demo 沒網路或 TDX 故障]** 規劃功能仍可走（base weight），但 live update 失效
  → Mitigation: 既有「TDX fetch fail 保留前次 Redis cache」邏輯仍適用；A* 永遠至少能用 base weight 規劃

## Migration Plan

1. Branch 新建 `taipei-agent-routing` 從 `develop`
2. **Phase 1 — VD cleanup（task 1.x）**：刪除 `VDSensor` / `vd_sensor.py` / `test_vd.py` / `seed_vd_sensors` 呼叫；migration SQL 在 `infra/init-db/02-road-network-tables.sql` 移除 `vd_sensor` 區塊
3. **Phase 2 — Taipei road network（task 2.x）**：改 import script、跑 import、輸出 `data/taipei_road_sections.json`、執行 DB truncate + drop migration、改 `DEFAULT_JSON_PATH`
4. **Phase 3 — Section live fetch（task 3.x）**：還原 `traffic.py`（從 git `aece31f` 取舊版作骨架，URL 改 Taipei，filter 加 `-99` 規則）；更新 `tests/test_traffic.py`
5. **Phase 4 — MCP routing tool（task 4.x）**：寫 `RoutingMCPServer` 與 `PlanRouteInput`/`RouteResponse` schema；Pydantic 化 `plan_optimal_route` 的 return
6. **Phase 5 — Gemini chat agent（task 5.x）**：寫 `chat_agent.py`、改 `handle_chat_request` 改 dispatch
7. **Phase 6 — kafka-messaging spec delta（task 6.x）**：補 schema 文字
8. **Phase 7 — 整合驗證（task 7.x）**：service 重啟、台北 demo route 跑成功、log 看到 live update 與 agent tool-call

**Rollback**: `git revert` 整個 PR；DB 因為 truncate 過 Kaohsiung 資料無法回復，但這些資料本來就只是測試 seed，重跑 `git checkout` 舊 import script 即可重生。

## Open Questions

- 是否需要把 `import_tdx_road_network.py` 改成 module-style 接受 city/bbox 參數，方便日後切其他城市？暫定不做，YAGNI；本次 hard-code Taipei
- chat agent 是否要支援多輪對話（conversation history）？暫定第一版只看單則訊息，避免 session 管理複雜化
- speed_camera 之後若要加 Taipei 資料，是否也走同個 bbox？暫定是；但這次 scope 不做
