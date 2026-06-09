# 智慧交通導航與對話助理 - Python AI Server 開發指南

> **最後更新**: 2026-04-07
> **對齊版本**: 移除 gRPC，採用 Kafka-only 架構；LLM 使用 Gemini + Prompt Engineering；MCP 採 Streamable HTTP transport

---

## 1. 系統定位與架構概述

平台分為前端 (Mobile/Web client)、負責業務邏輯的主伺服器 (Spring Boot Kotlin)，以及負責 AI 計算的 **多智能體服務 (Multi-Agent Service, Python FastAPI)**。兩個後端服務透過 **Kafka** 進行非同步通訊。

### 1.1 Python AI Server 的核心職責

| Agent | 職責 | 狀態 |
|-------|------|------|
| **Chat Manager** | 主協調者，維護對話狀態，分派給下游 Agent | 待開發 |
| **Route Agent** | A* 路徑規劃，基於高雄路網圖資 | 路網解析已完成，A* 待開發 |
| **Traffic Agent** | 取得 TDX 即時車流資料，更新 edge weight | 待開發 |
| **Explainer Agent** | 透過 Gemini 將 AI 推論轉為自然語言解釋 | 待開發 |

> **已決策：不做的事**
> - ~~YOLO 影像辨識~~ → 放到未來計畫（搭配 Rust 高效能重寫）
> - ~~Fine-tune LLM/LSTM~~ → 使用 Prompt Engineering + Context Injection
> - ~~TDX Historical API~~ → 只使用 Basic（路網）+ Live（即時車流）API

### 1.2 MCP (Model Context Protocol) 整合策略

透過 MCP 標準將底層業務邏輯解耦：
* **MCP Client**: 內建於 Chat Manager，負責向外發送工具操作指令
* **MCP Servers**: 將查詢 TimescaleDB、Route Agent 演算法等封裝成標準 MCP Tools，供 Gemini 自動感知與存取
* **Transport**: 採用 **Streamable HTTP**，開發期同 process 掛在 FastAPI 路由下，部署期拆為獨立 K8s Pod（只需改 URL config，程式碼不動）

**開發策略：先合後拆**
```
Phase 2（開發期）:                      Phase 4（K8s 部署期）:
┌──────────────────────────┐           ┌──────────────┐ ┌──────────────┐
│  single FastAPI process  │    →→→    │ chat-manager │ │ route-mcp    │
│  ├── Chat Manager        │           │ (Pod)        │ │ (Pod)        │
│  ├── /mcp/route          │           └──────────────┘ └──────────────┘
│  └── /mcp/traffic        │           ┌──────────────┐
└──────────────────────────┘           │ traffic-mcp  │
                                       │ (Pod)        │
                                       └──────────────┘
```

```python
# 開發期 config
MCP_SERVERS = {
    "route":   "http://localhost:8000/mcp/route",
    "traffic": "http://localhost:8000/mcp/traffic",
}

# K8s 部署期 config（只改 URL）
MCP_SERVERS = {
    "route":   "http://route-mcp:8001/mcp",
    "traffic": "http://traffic-mcp:8002/mcp",
}
```

---

## 2. 系統架構圖

```
┌──────────────────────────────────────────────────────────────────┐
│                        Mobile / Web Client                       │
└────────────────────────────┬─────────────────────────────────────┘
                             │ REST API
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│               Spring Boot 主伺服器 (main-service)                │
│                                                                  │
│  ChatController ──→ ChatRequestProducer ──→ Kafka                │
│  ChatResponseConsumer ←── Kafka ←── multiagent-service           │
│  /route/recommend, /traffic/{id}/current                         │
└────────────────────────────┬─────────────────────────────────────┘
                             │ Kafka (6 topics)
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│            Python AI Server (multiagent-service)                 │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │              Chat Manager (MCP Client)                   │     │
│  │         Gemini API + Prompt Engineering                  │     │
│  └────┬──────────────┬──────────────────┬──────────────────┘     │
│       │ MCP          │ MCP              │ MCP                    │
│       ▼              ▼                  ▼                        │
│  ┌─────────┐   ┌───────────┐   ┌──────────────┐                 │
│  │  Route   │   │ Traffic   │   │  Explainer   │                 │
│  │  Agent   │   │  Agent    │   │   Agent      │                 │
│  │ (A*)     │   │(TDX Live) │   │  (Gemini)    │                 │
│  └────┬─────┘   └─────┬─────┘   └──────────────┘                │
│       │               │                                          │
│       ▼               ▼                                          │
│  ┌─────────┐   ┌───────────┐                                    │
│  │ Road    │   │ TDX Live  │                                    │
│  │ Network │   │ VD API    │                                    │
│  │ (DB)    │   │ Section   │                                    │
│  └─────────┘   └───────────┘                                    │
└──────────────────────────────────────────────────────────────────┘

┌─────── 資料層 ────────┐
│  TimescaleDB (路網圖)  │
│  Redis (即時路況快取)   │
│  ElasticSearch (選用)  │
└────────────────────────┘
```

---

## 3. 資料來源策略

### 3.0 資料來源總覽

| 來源 | API / URL | 用途 | 呼叫時機 |
|------|-----------|------|----------|
| TDX | `GET /v2/Basic/Road/Section/Kaohsiung` | 靜態路網拓撲（geometry, 路長, 速限） | 啟動時 seed 一次 |
| TDX | `GET /v2/Live/Road/Traffic/VD/Kaohsiung` | 即時車流量、車速、佔有率 | 定期輪詢（每 1-5 分鐘） |
| TDX | `GET /v2/Live/Road/Traffic/Section/Kaohsiung` | 即時路段旅行時間 | 定期輪詢 |
| 政府開放資料 | [測速執法設置點 (dataset #7320)](https://data.gov.tw/dataset/7320) | 測速照相機位置、方向、速限 | 啟動時 seed 一次 |

**不使用**: TDX Historical API（畢專不需要歷史分析）

### 3.0.1 測速照相機資料

**來源**: 內政部警政署開放資料 CSV
**欄位**: `CityName, RegionName, Address, Longitude, Latitude, direct (拍攝方向), limit (速限)`

**資料模型**: 獨立 `SpeedCamera` table（一條 edge 上可能有多個相機）
```
SpeedCamera
├── id (PK)
├── latitude, longitude
├── direction (str)       — 如 "南北雙向"
├── speed_limit (int)     — 如 50
├── address (str)
├── nearest_edge_id (FK → TrafficEdge)
```

**Seed 流程**: 啟動時從 CSV 篩選 `CityName == "高雄市"`，對每個相機 snap 到最近的 TrafficEdge，寫入 DB。

**導航整合**: `plan_optimal_route()` 回傳路徑時，順帶 JOIN speed_camera table，在結果中附上 `speed_cameras` 清單。Explainer Agent 將相機資訊組進 prompt，讓 Gemini 生成自然語言提醒（如「中山一路近五福路口有測速照相，速限 50 km/h」）。

### 3.1 即時資料更新流程

```
TDX Live API ──(定時拉取)──→ Traffic Agent
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼              ▼
              Redis 快取    TimescaleDB     更新 edge weight
           (最新路況 L.O.S)  (時序紀錄)    (A* 動態成本)
```

Traffic Agent 定期從 TDX 拉取即時資料，做三件事：
1. **更新 Redis** — `SET traffic:edge:{id} "{speed: 35, los: C}"` 供快速查詢
2. **寫入 TimescaleDB** — 留存時序紀錄，未來可用於分析（但本專題不做）
3. **更新 edge weight** — 根據即時車速調整 A* 的動態權重

---

## 4. 模組詳細開發指南

### 4.1 技術棧

| 層級 | 技術 |
|------|------|
| 核心語言 | Python 3.10+, asyncio |
| Web 框架 | FastAPI + uvicorn |
| 訊息佇列 | confluent-kafka (已完成) |
| MCP 框架 | `mcp` (Anthropic 官方 Python SDK), Streamable HTTP transport |
| LLM | `google-generativeai` SDK + Google AI Studio API key (Google AI Pro 帳號) |
| LLM 模型 | Gemini 2.0 Flash (開發/測試), Gemini 2.5 Pro (正式) — prompt engineering, 不做 fine-tune |
| 圖演算法 | 自建 A* (基於 road_network.py 的 node/edge 結構) |
| 儲存 | asyncpg (TimescaleDB), redis.asyncio |
| 搜尋 (選用) | elasticsearch-py (RAG 增強，有餘力再做) |

### 4.2 Chat Manager (核心 Orchestrator)

**職責**: 消費 `chat.request`，協調 MCP Tools，產出回應到 `chat.response`

**運作流程**:
1. Kafka Consumer 收到 `chat.request`（含 `correlation_id`, `session_id`, `content`）
2. 組合 system prompt + 對話歷史 + 即時路況 context，送給 Gemini
3. 若 Gemini 決定呼叫工具 (Tool Call) → 轉發給 MCP Server 執行
4. 收到工具結果後，再次提供給 Gemini 生成最終回應
5. 發布到 `chat.response`（含 `reply`, `suggested_actions`）

**Gemini ↔ MCP 橋接流程**:
1. Chat Manager 啟動時，以 MCP Client 連線所有 MCP Server，取得 tool schema
2. 將 MCP tool schema 轉換為 Gemini `FunctionDeclaration` 格式
3. Gemini 回傳 `function_call` → MCP Client 執行對應 tool → 結果餵回 Gemini 生成最終回答

**對話歷史**: 存 Redis（TTL 30 min），session 過期自動清除。

**Gemini Prompt 策略**:
```
你是高雄市智慧交通導航助理。

你可以使用以下工具:
- plan_route(origin, destination): 規劃最佳路徑
- get_traffic(edge_ids): 查詢即時路況
- explain_route(route_data, traffic_data): 解釋路徑推薦理由

當前即時路況摘要:
{traffic_context_from_redis}

請根據用戶問題，選擇合適的工具回答。
```

### 4.3 Route Agent → MCP Tool

**封裝為**: `plan_optimal_route(origin_lat, origin_lng, dest_lat, dest_lng)`

**路網圖策略**: 啟動時從 DB 一次載入，建構 in-memory adjacency dict：
```python
# graph[node_id] = [(neighbor_id, edge_id, weight), ...]
graph: dict[int, list[tuple[int, int, float]]]
```
Traffic Agent 更新即時路況時，直接 patch 對應 edge 的 weight（不需整張圖 reload）。

**Snap to Graph**: 採用 snap to node（非 snap to edge）。對使用者座標找最近的 K=3 個 node，優先選 degree 最高的（交叉路口而非 dead end），避免 A* 從死路出發。目前路網規模（幾百 node）用 O(N) 暴力掃即可，未來擴大可用 KD-Tree 優化至 O(log N)。

**A* Heuristic**: `h(n) = haversine_km(n, destination) / max_speed_in_graph`。單位為小時（與 g(n) 一致），除以全路網最高速限保證 admissible（不高估）。不加 congestion 預估 — 壅塞已反映在 g(n) 的 dynamic_weight 中。

**Weight 公式**:
```
base_weight = length_km / speed_limit_kmh                    （seed 時算好，靜態）
congestion_factor = min(speed_limit / current_speed, 10.0)   （即時更新）
  - current_speed ≤ 0 → factor = 10.0（路段封閉/極度壅塞）
  - 無即時資料 → factor = 1.0（假設暢通）
dynamic_weight = base_weight × congestion_factor              （A* 用此值）
```

**Top-K 路徑**: 採用 Penalty-Based Re-run（非 Yen's K-Shortest Paths）。每次 A* 找到一條路徑後，將該路徑用過的 edge weight × penalty_factor (3.0)，再重跑 A*。K=3，回傳時用原始 graph 重算真實 cost 排序。優點：實作簡單、路徑差異大、使用者有真正不同的選擇。

**Geocoding**: 前端地圖 SDK 為主（使用者點選地圖即有座標）。聊天場景透過 MCP Tool `geocode_location(query)` 解析地名，底層先用 Nominatim (OSM)，不足再換 Google Places API。

**內部邏輯**:
1. 啟動時從 DB 載入路網圖（TrafficNode + TrafficEdge）→ in-memory graph
2. Snap to graph：找最近的起點/終點 node（優先高 degree node）
3. 動態調整 edge weight：`dynamic_weight = base_weight × congestion_factor`
4. A* 搜尋 + Penalty-Based top-K（K=3, penalty=3.0）
5. 對每條路徑 JOIN speed_camera table，附上沿途測速照相機
6. 回傳路徑資料（node 序列、預估時間、途經路名、測速照相機）

### 4.4 Traffic Agent → MCP Tool

**封裝為**: `get_current_traffic(edge_ids?)`, `refresh_traffic_data()`

**TDX ↔ Edge Mapping**: `TrafficEdge.tdx_section_id` 欄位（String）對應 TDX 的 `RoadSectionID`（如 `"L_6190010300020E"`），seed 時從 JSON 寫入，Live 資料透過此欄位 mapping。

**內部邏輯**:
- `get_current_traffic`: 從 Redis 讀取指定 edge 的即時車速/LOS 等級
- `refresh_traffic_data`: 呼叫 TDX Live API，以 `tdx_section_id` mapping 到 edge，批次更新 Redis + TimescaleDB + in-memory graph weight

### 4.5 Explainer Agent → MCP Tool

**封裝為**: `explain_routing_decision(route_data, traffic_data)`

**內部邏輯**:
- 將 A* 路徑結果 + 即時路況資料組成 prompt
- 送給 Gemini 生成自然語言解釋
- 例：「建議走建工路而非中山路，因為中山路目前車速僅 15 km/h（壅塞等級 E），建工路車速 45 km/h，預估可節省 12 分鐘。」

---

## 5. Kafka Topic 設計（已定義）

| Topic | 方向 | 用途 |
|-------|------|------|
| `chat.request` | main → multiagent | 用戶聊天訊息 |
| `chat.response` | multiagent → main | AI 回覆 |
| `route.request` | main → multiagent | 路徑規劃請求 |
| `route.response` | multiagent → main | 路徑規劃結果 |
| `traffic.metrics` | multiagent → main | 即時路況推播 |
| `traffic.events` | multiagent → main | 交通事件通知 |

---

## 6. 開發 Roadmap（對齊 timeline.md）

### Phase 1: 基礎設施整理 — Week 1-2 ✅ 進行中

**已完成**:
- [x] 移除 gRPC，統一 Kafka 通訊
- [x] Kafka producer/consumer 骨架
- [x] TimescaleDB + Redis + Kafka docker-compose
- [x] TDX 路網解析（road_network.py → nodes + edges）
- [x] DB models (TrafficNode, TrafficEdge) + seed 機制
- [x] main-service: ChatController, Kafka producers/consumers, correlation ID

**待完成**:
- [ ] 修復 e2e Kafka 測試
- [ ] 取得 TDX 路網 JSON 快照（kaohsiung_road_sections.json）
- [ ] 驗證 seed 完整流程

### Phase 2: 核心引擎 — Week 3-5

**目標**: 讓 multiagent-service 能實際回應路徑規劃請求

**前置修復** (Phase 1 → 2 過渡):
- [ ] `TrafficEdge` 加 `tdx_section_id` (String) 欄位
- [ ] `ParsedEdge` + `road_network.py` 解析時保留 `RoadSectionID`
- [ ] `seed.py` 寫入時帶上 `tdx_section_id`
- [ ] `SpeedCamera` model + seed（從政府開放資料 CSV 篩選高雄市，snap 到最近 edge）

**Week 3** (①② 可並行):
1. **A* 路徑規劃引擎**
   - 啟動時從 DB 載入路網 → in-memory adjacency dict
   - 實作 A* with haversine heuristic
   - 支援 top-K 路徑回傳
   - 路徑結果附帶沿途測速照相機資訊

2. **TDX Live 資料整合**
   - 定時從 TDX Live API 拉取車流資料
   - 以 `tdx_section_id` mapping 到 edge
   - 寫入 Redis（即時查詢）+ TimescaleDB（時序儲存）
   - 動態 patch in-memory graph 的 edge weight

**Week 4** (③④ 可並行):
3. **MCP Server 建置 (Streamable HTTP)**
   - Route MCP Server (`/mcp/route`): `plan_optimal_route()`, `geocode_location()` (Nominatim)
   - Traffic MCP Server (`/mcp/traffic`): `get_current_traffic()`, `refresh_traffic_data()`
   - Explainer MCP Tool: `explain_routing_decision()` — 可掛在 Route 或獨立 server

4. **Gemini API 串接**
   - `google-generativeai` SDK + AI Studio API key
   - 先跑通 function calling hello world
   - 建立 MCP tool schema → Gemini FunctionDeclaration 轉換器

**Week 5** (⑤⑥ 必須串行):
5. **Chat Manager 整合**
   - MCP Client 連線所有 MCP Server，取得 tool schema
   - 組合 system prompt + 對話歷史 (Redis) + 即時路況 context
   - 串通 chat.request → Gemini → Tool Calls → MCP → chat.response

6. **YOLO 用 mock 資料替代**
   - traffic.metrics topic 以假資料餵入
   - 保持介面不變，未來可抽換為真實 YOLO 資料

### Phase 3: main-service API 完善 — Week 5-6

- [ ] `POST /chat/message` — Kafka 橋接 + correlation ID（骨架已有）
- [ ] `GET /route/recommend` — 路徑推薦 API
- [ ] `GET /traffic/{id}/current` — 即時路況查詢
- [ ] 基本 Auth (JWT，簡單做)

### Phase 4: 前端 + 整合 — Week 7-8

- [ ] React 或 Android（看時間決定）
- [ ] 地圖顯示 + 路徑繪製
- [ ] 聊天介面
- [ ] 端到端串接 & demo 準備

---

## 7. K8s 部署架構

```
┌─── K8s Cluster ──────────────────────────────────────────────────┐
│                                                                   │
│  ┌─────────────┐   ┌─────────────┐   ┌──────────────┐           │
│  │ main-service│   │chat-manager │   │ route-mcp    │           │
│  │ Spring Boot │──▶│ Python      │──▶│ Python       │           │
│  │ (Deployment)│   │ MCP Client  │   │ MCP Server   │           │
│  │             │   │ + Gemini    │   │ A* engine    │           │
│  └──────┬──────┘   └──────┬──────┘   └──────────────┘           │
│         │                 │                                      │
│         │ Kafka           │          ┌──────────────┐           │
│         ▼                 └─────────▶│ traffic-mcp  │           │
│  ┌─────────────┐                     │ Python       │           │
│  │   Kafka     │                     │ MCP Server   │           │
│  │ (StatefulSet│                     │ TDX poller   │           │
│  └─────────────┘                     └──────┬───────┘           │
│                                              │                   │
│  ┌─────────────┐   ┌─────────────┐          │                   │
│  │ TimescaleDB │   │   Redis     │◀─────────┘                   │
│  │(StatefulSet)│   │(Deployment) │                               │
│  └─────────────┘   └─────────────┘                               │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

**關鍵設計**:
- main-service ↔ chat-manager: 透過 Kafka 非同步通訊
- chat-manager ↔ MCP Servers: 透過 HTTP 同步呼叫（Gemini tool calling 是 request-response 模式）
- 每個 MCP Server 是獨立 K8s Deployment + Service，可獨立 scale/update
- MCP Client URL 由 K8s Service name 解析，無需 hardcode IP

---

## 8. 未來優化方向（不在本專題範圍）

本專題聚焦於 **即時資料 + Prompt Engineering** 的導航推薦。以下為口試可展示的未來方向：

| 方向 | 說明 |
|------|------|
| **YOLO 影像辨識** | 串接高雄市交通攝影機 CCTV，即時偵測車流/事故，取代 mock 資料 |
| **Rust 重寫** | 將 A* 路徑規劃、即時資料處理等 performance-critical 模組以 Rust 重寫 |
| **Historical API 分析** | 利用 TDX 歷史資料訓練壅塞預測模型（LSTM/GRU），提前預判塞車 |
| **ElasticSearch RAG** | 建立交通知識庫，讓 Gemini 回答更精確的交通問題 |
| **OpenTelemetry** | 分散式鏈路追蹤，精確觀測每個推論環節的延遲 |
