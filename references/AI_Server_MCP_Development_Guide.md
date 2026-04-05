# 智慧交通導航與對話助理 - Python AI Server 開發指南

> **最後更新**: 2026-04-06
> **對齊版本**: 移除 gRPC，採用 Kafka-only 架構；LLM 使用 Gemini + Prompt Engineering

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

## 3. 資料來源策略（TDX API）

| API | 用途 | 呼叫時機 |
|-----|------|----------|
| `GET /v2/Basic/Road/Section/Kaohsiung` | 靜態路網拓撲（geometry, 路長, 速限） | 啟動時 seed 一次 |
| `GET /v2/Live/Road/Traffic/VD/Kaohsiung` | 即時車流量、車速、佔有率 | 定期輪詢（每 1-5 分鐘） |
| `GET /v2/Live/Road/Traffic/Section/Kaohsiung` | 即時路段旅行時間 | 定期輪詢 |

**不使用**: Historical API（畢專不需要歷史分析）

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
| MCP 框架 | `mcp` (Anthropic 官方 Python SDK) |
| LLM | Google Gemini API (prompt engineering, 不做 fine-tune) |
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

**內部邏輯**:
1. 從 DB 載入路網圖（TrafficNode + TrafficEdge）
2. 找到最近的起點/終點 node（snap to graph）
3. 從 Redis 讀取即時路況，動態調整 edge weight：
   ```
   dynamic_weight = base_weight * congestion_factor(current_speed, speed_limit)
   ```
4. 執行 A* 計算 top-K 路徑
5. 回傳路徑資料（node 序列、預估時間、途經路名）

### 4.4 Traffic Agent → MCP Tool

**封裝為**: `get_current_traffic(edge_ids?)`, `refresh_traffic_data()`

**內部邏輯**:
- `get_current_traffic`: 從 Redis 讀取指定 edge 的即時車速/LOS 等級
- `refresh_traffic_data`: 呼叫 TDX Live API，批次更新 Redis + TimescaleDB

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

1. **A* 路徑規劃引擎**
   - 從 DB 載入路網圖結構
   - 實作 A* with haversine heuristic
   - 支援 top-K 路徑回傳

2. **TDX Live 資料整合**
   - 定時從 TDX Live API 拉取車流資料
   - 寫入 Redis（即時查詢）+ TimescaleDB（時序儲存）
   - 動態更新 edge weight

3. **MCP Server 建置**
   - Database MCP Server: `get_current_traffic()`
   - Agent Tools MCP Server: `plan_optimal_route()`, `explain_decision()`

4. **Chat Manager + Gemini 整合**
   - 接入 Gemini API（prompt engineering，不做 fine-tune）
   - MCP Client 掛載所有 Tools
   - 串通 chat.request → Gemini → Tool Calls → chat.response 完整流程

5. **YOLO 用 mock 資料替代**
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

## 7. 未來優化方向（不在本專題範圍）

本專題聚焦於 **即時資料 + Prompt Engineering** 的導航推薦。以下為口試可展示的未來方向：

| 方向 | 說明 |
|------|------|
| **YOLO 影像辨識** | 串接高雄市交通攝影機 CCTV，即時偵測車流/事故，取代 mock 資料 |
| **Rust 重寫** | 將 A* 路徑規劃、即時資料處理等 performance-critical 模組以 Rust 重寫 |
| **Historical API 分析** | 利用 TDX 歷史資料訓練壅塞預測模型（LSTM/GRU），提前預判塞車 |
| **ElasticSearch RAG** | 建立交通知識庫，讓 Gemini 回答更精確的交通問題 |
| **OpenTelemetry** | 分散式鏈路追蹤，精確觀測每個推論環節的延遲 |
