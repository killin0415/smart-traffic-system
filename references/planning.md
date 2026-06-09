# 智慧交通系統：AI 與演算法實作計畫 (AI & Algorithm Implementation Plan)

本文件聚焦於系統中的「智慧路由引擎 (Intelligent Routing Engine)」，詳細說明 FastAPI、YOLO、LLM 與 A* 演算法的整合架構與實作細節。

## 系統定位與部署架構
* **部署環境：** Kubernetes (K8s) + Docker
* **主後端 (API Gateway)：** Spring Boot (負責處理前端請求、系統排程)
* **AI 運算中樞：** FastAPI + MCP Server (負責高負載 AI 推理、圖論運算，便於在 K8s 中獨立擴展)
* **資料層：** TimescaleDB (時序資料庫)、Kafka (訊息佇列)、Redis (快取)

---

## 模組 1：影像 AI 模組 (YOLO Inference)
負責將非結構化的 CCTV 影像串流轉換為結構化的交通擁塞指標。

### 實作重點
* **非同步與降採樣：** 採用 `asyncio` 處理推理，避免阻塞 API。影像不需逐幀分析，設定每 5~10 秒抽取一幀進行辨識即可。
* **資料管線：** 訂閱 Kafka `raw-cctv-frames` -> YOLO 推理 -> 發佈結果至 Kafka `traffic-metrics`。
* **權重轉換邏輯 (車流量 -> 擁塞指標 C)：**
  * 辨識並計算「加權車輛數」(例如：1 輛大卡車 = 2.5 輛小客車)。
  * `C = max(1.0, min(5.0, (目前加權車輛數 / 常態車輛數) * 基礎系數))`
  * 數值範圍介於 1.0 (順暢) 到 5.0 (嚴重壅塞)。

---

## 模組 2：LLM 事件分析模組 (透過 MCP Server)
利用大型語言模型 (如 Gemini Pro 1.5) 處理難以量化的突發事件描述，將語意轉化為具體的演算法權重參數。

### 實作流程
1. **事件觸發：** Spring Boot 將外部事故通報 (如政府 Open Data、社群文字) 發送給 FastAPI。
2. **Prompt Engineering 結構設計：**
   * **角色設定：** 資深都市交通管制官。
   * **輸入資訊：** 事故文字描述、發生時間 (尖離峰)、受影響路段 ID、當下天氣。
   * **推理任務：** 評估事故對交通時間成本的影響。
3. **輸出格式 (JSON)：**
   ```json
   {
     "affected_segment_id": "Segment_A_200K_South",
     "penalty_multiplier": 3.5, 
     "estimated_duration_minutes": 90,
     "reasoning": "連環車禍佔用主力車道，且適逢尖峰時間，預期造成極度嚴重的回堵。"
   }
   ```
4. **權重更新：** FastAPI 將此 JSON 發佈至 Kafka，由 Consumer 更新 TimescaleDB。

---

## 模組 3：動態路徑規劃引擎 (Dynamic Routing Engine)
處理前端或後端發起的導航請求，結合動態路況進行圖論運算。

### 演算法核心 (A* Search)
* **雙重動態權重 (Double Weighting)：** `Total Cost = Baseline Cost * YOLO Congestion Index * LLM Penalty Multiplier`
* **快取機制 (Redis Caching)：**
  * **Cache Key：** `sha256(origin_node + dest_node + timestamp_window_1minute)`
  * 相同起訖點在短時間內 (權重無顯著變化時)，直接返回 Redis 快取結果，大幅降低 K8s 叢集運算負載。

### 演算法實作範例 (Python / NetworkX)
```python
import networkx as nx
import math

# 1. 建立加權有向圖 (由 TimescaleDB 載入並定期更新)
G = nx.DiGraph()

# 2. 定義 A* 啟發式函數 (歐幾里得直線時間距離)
def heuristic(u, v):
    (x1, y1) = G.nodes[u]['pos']
    (x2, y2) = G.nodes[v]['pos']
    avg_speed_m_per_min = 1000  # 假設平均時速 60km/h
    dist = math.sqrt((x1 - x2)**2 + (y1 - y2)**2)
    return dist / avg_speed_m_per_min

# 3. 動態權重更新函式 (接收來自 YOLO 與 LLM 的數據)
def update_dynamic_weight(edge_id, yolo_idx=1.0, llm_penalty=1.0):
    u, v = edge_id
    base_weight = G[u][v]['base_weight']
    new_weight = base_weight * yolo_idx * llm_penalty
    G[u][v]['weight'] = new_weight

# 4. 執行路徑規劃
try:
    path = nx.astar_path(G, origin, destination, heuristic=heuristic, weight='weight')
    cost = nx.astar_path_length(G, origin, destination, heuristic=heuristic, weight='weight')
except nx.NetworkXNoPath:
    pass # 處理無路徑可達之例外情況
```

---

## 技術挑戰與後續優化重點
1. **記憶體管理：** 全城市級別的 `NetworkX` 圖資極耗記憶體。需精算 FastAPI Pods 的 RAM 需求；若圖資過大，未來需評估改用 C++ 的 Contraction Hierarchies (CH) 函式庫。
2. **時序資料聚合：** 利用 TimescaleDB 的連續聚合 (Continuous Aggregates) 功能，將分鐘級的 YOLO 資料自動聚合為小時級統計，減輕資料庫讀取壓力並供後續分析使用。
