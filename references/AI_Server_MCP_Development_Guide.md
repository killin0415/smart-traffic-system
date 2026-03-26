# 智慧交通導航與對話助理 - Python AI Server 與 MCP 整合開發報告書

## 1. 系統定位與架構概述

基於《System Report》文檔提供的系統全貌，平台分為前端 (Mobile client)、負責業務邏輯的主伺服器 (Spring Boot)，以及負責 AI 計算的**多智能體服務 (Multi-Agent Service)**。

本報告書將專注於 **Python AI Server** 的建設，並引入最新的 **MCP (Model Context Protocol)** 生態規範。MCP 能為 LLM 標準化外部數據源與工具的接入流程，解決過往多智能體（Route, Explainer, Forecast）與資料庫（TimescaleDB, ElasticSearch）之間複雜且難以維護的 API 對接問題，極大化提升系統擴展性。

### 1.1 Python AI Server 的核心職責
* **Chat Manager**: 作為主協調者 (Orchestrator)，維護對話狀態並與 LLM 互動，規劃工具呼叫。
* **Route Agent**: 路徑規劃專家，負責計算時間與距離最佳化的路徑。
* **Forecast Agent**: 流量預測專家，結合 TimescaleDB 中的歷史與即時時序數據進行分析。
* **Explainer Agent**: 解釋專家，將 AI 推論過程轉換為人類易讀的直觀解釋。
* **YOLO Node**: 影像分析節點，負責路口影像辨識，將車輛數量與擁擠度打入 Kafka。

### 1.2 MCP (Model Context Protocol) 整合策略
透過導入 MCP 標準，我們可以將原先耦合的 Python 架構大幅優化解耦：
* **MCP Client**: 內建於 `Chat Manager` 中（或由支援 MCP 的 LLM 直接擔任），負責向外發送理解用戶意圖後的具體工具操作指令。
* **MCP Servers**: 將各個底層業務邏輯（如查詢 TimescaleDB、Route Agent 演算法、搜尋 ES 文本）封裝成標準的 MCP Tools 與 Resources，供 LLM 自動感知與存取。

---

## 2. 系統架構設計圖 (MCP架構增強版)

```mermaid
graph TD
    subgraph Spring Boot 主伺服器叢集
        SB[Spring Boot Gateway]
    end

    subgraph Python AI Server (Multi-Agent Service)
        CM[Chat Manager <br> MCP Client & Coordinator]
        
        subgraph MCP Servers
            DB_MCP[Database MCP Server<br>Tools: query_timescaledb, get_redis_cache]
            RAG_MCP[Knowledge MCP Server<br>Tools: search_elasticsearch]
            Agent_MCP[Agent Tools MCP Server<br>Tools: calculate_route, predict_traffic, explain_decision]
        end
    end

    subgraph Kafka 事件驅動架構
        Kafka[Kafka Broker]
    end

    subgraph 資料層與感知層 (Data & Sensors)
        YOLO[YOLO Nodes]
        TSDB[(TimescaleDB)]
        ES[(ElasticSearch)]
        Redis[(Redis)]
    end

    %% 通訊關聯
    SB -- gRPC (即時對話/推論請求) --> CM
    CM -- MCP Protocol --> DB_MCP
    CM -- MCP Protocol --> RAG_MCP
    CM -- MCP Protocol --> Agent_MCP
    
    YOLO -- 影像辨識結果/時序資料 --> Kafka
    Kafka -- Consumer/Sink --> TSDB & Redis
    
    DB_MCP --- TSDB & Redis
    RAG_MCP --- ES
```

---

## 3. 模組詳細開發指南

### 3.1 基礎框架層技術選型
建議採用以下 Python 標準技術與框架：
* **核心環境**: **Python 3.10+** (高度依賴 `asyncio` 發揮並發效能)。
* **通訊對接**: 
  * `grpcio` & `grpcio-tools`: 建立 gRPC Server 對接 Spring Boot 端點。
  * `confluent-kafka` 或 `aiokafka`: 處理 YOLO 即時資料的非同步訊息對列。
* **MCP 框架**: `mcp` (Anthropic 官方 Python SDK)，快速建構 Server 與 Client。
* **儲存對接**: `asyncpg` (TimescaleDB Postgres 擴充), `elasticsearch-py`, `redis.asyncio`。

### 3.2 Chat Manager 開發 (核心 Orchestrator)
**職責**：將外部 gRPC 請求轉換為 LLM 對話，並聯動 MCP Server 完成動態決策。
**實作建議**：
1. **建立 gRPC Server**：實現對外介面（接收 `session_id` 和 `content`）。
2. **初始化 MCP Client**：設定連線至內部的 `DB_MCP`, `RAG_MCP`, `Agent_MCP` Servers，於啟動時獲取可用的 Tools (例如 `plan_route_tool`)。
3. **LLM 對話運算流程**：
   - 將用戶輸入發送給 LLM 平台（OpenAI / Anthropic）。
   - 若 LLM 選擇呼叫工具 (Tool Call)，Chat Manager 即刻發送請求給對應的 MCP Server。
   - 收到 MCP Server 的工具執行結果後，再次提供給 LLM，直到生成最終針對用戶的回應，最終包裝成回傳結構與 `suggested_actions` 交還給 Spring Boot。

### 3.3 多智能體 (Agents) 轉型為 MCP Tools
在 MCP 原則下，傳統的 Agent 可視為專門的 `Tools` 或工作流。

#### A. Forecast Agent (流量預測)
* **封裝為 MCP Tool**: `predict_traffic(intersection_id, future_time_mins)`
* **內部實作邏輯**:
  - 利用 Redis 直接讀取當前物件與塞車路況（A~F 等級）。
  - 加載儲存於 TimescaleDB 的時序資料做 LSTM 或 GRU 模型推論，預測壅塞趨勢。

#### B. Route Agent (路徑規劃)
* **封裝為 MCP Tool**: `plan_optimal_route(origin_lat_lng, dest_lat_lng, preferences)`
* **內部實作邏輯**:
  - 根據外部地圖靜態資料載入圖結構。
  - 使用 Forecast Tool 的預測結果動態調整有向圖的「時間權重」（Cost）。
  - 利用 A* 或 Dijkstra 計算路徑，並回傳 `GEOMETRY_STR` 及 `route_id`。

#### C. Explainer Agent (推論解釋)
* **封裝為 MCP Tool**: `explain_routing_decision(route_id)`
* **內部實作邏輯**: 組合 Forecast 數據與 Route 的決策分歧點，生成「為何繞過中山路？因為偵測到壅塞等級 E」這樣直觀的理由。

### 3.4 YOLO 節點感知與資料 Sink
1. **影像推論**: 採用 YOLOv8 分析 CCTV RTSP 串流，產出車輛總數與信心指數。
2. **非同步寫入 Kafka**:
   - 資料格式範例: `{"id": "UUID", "vehicle_count": 45, "level": "E", "timestamp": "...", "confidence": 0.85}`
3. **Data Ingestion Worker**: 獨立的 Python Kafka Consumer 負責將流量寫入 TimescaleDB `hypertable`，並使用 **Redis 分散式鎖 (Distributed Locks)** 處理重複資料重算的情境，避免資料庫在高峰期阻塞。

---

## 4. 知識檢索與 RAG 實踐

對付 AI 幻覺 (Hallucination) 並提升交通情境理解，RAG 不可或缺。
* **運作機制**: 將交通常識、路網靜態解釋，以及過往的高品質規劃對話轉換為 Vector Embeddings。
* **Knowledge MCP Server 實作**:
  * 暴露 Tool: `search_traffic_knowledge_base(query)`
  * 當 LLM 被問到特定區域的交通邏輯或遇到未知道路資訊時，能自動呼叫此 MCP Tool 向 ElasticSearch 獲取背景上下文。

---

## 5. 漸進式專案開發 Roadmap

為了能穩健自建這套 AI 系統，並明確各階段需要的開發資源，建議按照以下 5 個里程碑（Phases）進行開發：

### Phase 1: 基礎環境建置與通訊骨架 (Week 1)
**📍 目標**：建立可執行的 Python 專案環境，並打通 Spring Boot 與 AI Server 之間的 gRPC 雙向通訊，以及 Kafka 基礎佇列。
**🛠 技術棧與套件**：
* **環境管理**: `uv` (管理套件與虛擬環境)
* **核心語言**: `Python 3.10+` (全非同步設計)
* **通訊層**: `grpcio`, `grpcio-tools`, `protobuf`
* **訊息佇列**: `confluent-kafka` 或 `aiokafka`
* **基本目錄結構**: 建立 `src/grpc_server/`, `src/mcp_servers/`, `src/agents/` 等模組分類。

**✅ 具體實作步驟**：
1. **專案初始化**：執行 `uv init` 建立結構。
2. **通訊協定定義**：根據 Mobile API 與內部需求，撰寫 `chat.proto` 與 `route.proto`，並透過指令編譯為 Python 檔案 (`_pb2.py`, `_pb2_grpc.py`)。
3. **gRPC Server 實作**：以 `asyncio` 啟動一個非同步 gRPC Server，模擬回應簡單的 "Hello" 訊息，先讓 Spring Boot 成功打通 `POST /api/v1/chat/message` 的後端鏈路。
4. **Kafka 連通性驗證**：建立本機 Docker 的 Kafka Cluster（或連線現有環境），撰寫簡易的 `producer.py` 與 `consumer.py` 驗證訊息能否收發。

### Phase 2: 感知層與資料儲存管線 (Data Pipelines) (Week 2)
**📍 目標**：確保 YOLO 影像辨識的原始資料，能高效率地轉為時序數據與快取，為後續預測與路徑規劃鋪路。
**🛠 技術棧與套件**：
* **資料庫驅動**: `asyncpg` (TimescaleDB Postgres 連線)
* **快取體系**: `redis.asyncio` (非同步 Redis 客戶端)
* **任務排程**: `Celery` 或直接使用 `asyncio.create_task` 綁定 Kafka Consumer
* **時序資料庫機制**: TimescaleDB `Hypertable` 架構
* **分散式鎖**: 利用 `redis` 的 `SETNX` 實作機制

**✅ 具體實作步驟**：
1. **TimescaleDB 設計**：撰寫啟動腳本建立 `TrafficNode_Congestion` 等表，並執行 `SELECT create_hypertable('TrafficNode_Congestion', 'timestamp');`。
2. **Data Ingestion Worker**：撰寫一個長駐的 Consumer，監聽 YOLO 傳過來的 Kafka Topic，收到資料即：
   * 寫入最新的狀況到 Redis (格式：`SET traffic:node_id "{level: E, count: 45}"`)，提供高速查詢。
   * 批次 (Batch) 存入 TimescaleDB，這段將作為 Forecast Agent 訓練的歷史資料庫。
3. **並發防護**：針對「同一時刻多重異常路況」的情境，實作 Redis 分散式鎖，防止寫入衝突。

### Phase 3: 模型對齊與 MCP Servers (工具封裝) (Week 3)
**📍 目標**：將業務邏輯（路徑圖演算法、流量預測模型、Elasticsearch 檢索）轉化為標準的 Model Context Protocol (MCP) Tools，讓 LLM 能夠隨時抽換與存取。
**🛠 技術棧與套件**：
* **MCP 協定**: Anthropic `mcp` 官方 Python SDK
* **圖演算法 (Route)**: `networkx` 或 `osmnx` (載入 OpenStreetMap 與處理圖層結構)
* **預測模型 (Forecast)**: `torch` / `pytorch-lightning` (訓練與執行 LSTM/GRU)
* **向量資料庫 (RAG)**: `elasticsearch-py`
* **資料序列化**: `pydantic` (驗證 MCP 工具的輸入參數格式)

**✅ 具體實作步驟**：
1. **建立 Database MCP Server**：將 Phase 2 對 Redis / Timescale 的操作，包裝成 `get_current_traffic(node_id)` 的 MCP Tool。
2. **實作 AI MCP Server**：
   * 包裝 **Route Tool**：暴露 `plan_optimal_route(origin, dest)` 端點，內部使用 `networkx` 取得最短時間路徑。
   * 包裝 **Forecast Tool**：暴露 `predict_traffic(node_id)`。
   * 包裝 **ElasticSearch Tool**：暴露 `search_knowledge(query)` 提供 RAG 上下文。
3. **本地驗證**：利用 [MCP Inspector (CLI 工具)](https://github.com/modelcontextprotocol/inspector) 啟動這些 Server，以 UI 測試這些 Tools 的 Request / Response Schema 是否正確。

### Phase 4: Chat Manager 與全系統整合 (LLM Orchestration) (Week 4)
**📍 目標**：建構系統大腦，讓 LLM 直接介入 gRPC 連線，聽取用戶自然語言對話，自主判斷該連動哪些 MCP Tools，再回傳決策。
**🛠 技術棧與套件**：
* **LLM 框架**: 建議直接使用帶有 Tool Calling 能耐的原生 SDK (`openai` 或 `anthropic`)，避免過度封裝，或選擇 `langgraph` 作為多智能體狀態機管控。
* **MCP Client**: 結合至上述 LLM Client 內部，負責攔截 Tool Calling 請求並轉發給相對應的 MCP Server。

**✅ 具體實作步驟**：
1. **串接 LLM Client**：在 `Chat Manager` 中設定 OpenAI / Anthropic 的 API 金鑰與推論呼叫邏輯。
2. **掛載 MCP Tools**：在 LLM 初始化時，從 Phase 3 建立好的 MCP Server 拉取所有可用 Tools，並注入到 LLM 的 Prompt 中。
3. **End-to-End 單元測試**：
   * 模擬用戶傳送 `session_id` 和 *"從北車出發，幫我找出最不會塞車的路線"*。
   * **紀錄追蹤**：檢查 LLM 是否順利呼叫了 `Route Tool`。Route Tool 內部又去呼叫了 `Forecast Tool` 取回即時路況。最終 LLM 將結果以文字包裝傳回給 gRPC 對象。
4. **回饋與解釋**：確定 `Explainer Agent` 能順利拿取 Route 產生的物件，並將其生成「因為前方中山路預計壅塞」的中文解釋。

### Phase 5: 生產級別部署、監控與效能調校 (Week 5)
**📍 目標**：將原型投入模擬環境的壓力測試，並設定資源隔離與分散式追蹤，提升系統的 Observability。
**🛠 技術棧與套件**：
* **容器化佈署**: `Docker`, `Docker-Compose` 或 `Kubernetes`
* **可觀測性 (Observability)**: `opentelemetry-api`, `opentelemetry-sdk` (支援分散式鏈路追蹤 Tracing)
* **日誌收集器**: `Logstash` / `Fluentd` / `Prometheus` / `Grafana`
* **非同步網關**: (若必要) `FastAPI` 搭配 gRPC Web 驗證通道

**✅ 具體實作步驟**：
1. **系統容器化**：撰寫 `Dockerfile`，建議採 `multi-stage build` 縮減 Python 影像檔大小。將 gRPC Server、YOLO Consumer 分拆為不同 Container 以限制各自的 CPU/RAM 資源。
2. **鏈路追蹤 (Tracing) 埋點**：在 `Spring Boot`、`gRPC Server` 及 `MCP Tools` 中導入 OpenTelemetry。這可以讓您在 Grafana 長條圖中精確看到每個推論環節耗費了幾毫秒（例如「LLM 生成花費 1.2s，Route 呼叫花費 0.1s」）。
3. **異常告警與日誌**：針對 Kafka Lag (YOLO 數據積壓超過 30 秒) 或 API Timeout 設計 Error Log 並接入 ElasticSearch，並透過 Slack/Discord Webhook 即時提報。
