## Context

multiagent-service 已完成 Phase 1 基礎設施：Kafka consumer/producer 骨架、TrafficNode/TrafficEdge DB models、路網 JSON 解析（`road_network.py`）、Redis client。但 Kafka handler 全是 stub，沒有真正的路徑規劃或即時路況能力。

本次變更實作 Phase 2 核心引擎的前半部分：A* 路徑規劃 + TDX Live 即時資料整合 + 測速照相機 + Geocoding。MCP Server 封裝與 Chat Manager 整合屬後續工作。

## Goals / Non-Goals

**Goals:**
- 實作可運作的 A* 路徑規劃引擎，支援 top-K 路徑與即時權重
- 整合 TDX Live API，定時更新 Redis 快取與 in-memory graph weight
- 整合政府開放資料測速照相機，路徑結果附帶沿途相機資訊
- 提供 geocoding 功能，將地名轉換為經緯度
- 替換 Kafka handler stub 為真正的路徑規劃邏輯

**Non-Goals:**
- MCP Server 封裝（後續 change）
- Chat Manager + Gemini 整合（後續 change）
- Explainer Agent 自然語言生成（後續 change）
- 前端整合
- YOLO 影像辨識

## Decisions

### Decision 1: In-memory adjacency dict 作為路網圖結構

**選擇**: 啟動時從 DB 一次載入路網，建構 `dict[int, list[tuple[int, int, float]]]`（node_id → [(neighbor_id, edge_id, weight)]）。

**替代方案**:
- 每次查詢時 query DB → 太慢，A* 需要高頻存取鄰居
- NetworkX graph → 額外依賴，且 A* 實作不如自己寫可控

**理由**: 高雄路網幾百到幾千 node，記憶體佔用極小。Traffic Agent 更新路況時直接 patch weight，不需 reload 整張圖。

### Decision 2: Haversine / max_speed 作為 A* heuristic

**選擇**: `h(n) = haversine_km(n, destination) / max_speed_in_graph`

**替代方案**:
- 加入 congestion 預估到 heuristic → 可能違反 admissibility，且計算成本高
- 用平均速限除 → 可能高估，不 admissible

**理由**: 除以全路網最高速限保證 admissible（永不高估）。Congestion 已反映在 g(n) 的 dynamic_weight 中，heuristic 只需提供方向感。

### Decision 3: Penalty-based re-run 實作 top-K

**選擇**: 每次 A* 找到路徑後，將用過的 edge weight × 3.0，再重跑 A*。K=3。

**替代方案**:
- Yen's K-Shortest Paths → 數學上嚴格，但實作複雜 (~100 行額外演算法)，且結果可能只差一條 edge，使用者感知不到差異

**理由**: 實作簡單（~15 行）、路徑差異大（penalty 逼迫走不同路）、畢專夠用。回傳時用原始 graph 重算真實 cost 排序。

### Decision 4: Snap to node（非 snap to edge）

**選擇**: 找最近的 K=3 個 node，優先選 degree 最高的（交叉路口而非 dead end）。

**替代方案**:
- Snap to edge + 投影點拆 edge → 精度更高但需動態改 graph 結構，複雜度高

**理由**: 路網 node 密度已足夠（每條 TDX Section 的起終點），O(N) 暴力掃在幾百 node 下無效能問題。未來可用 KD-Tree 升級。

### Decision 5: Congestion factor = speed_limit / current_speed

**選擇**:
```
congestion_factor = min(speed_limit / current_speed, MAX_FACTOR=10.0)
  - current_speed ≤ 0 → 10.0
  - 無即時資料 → 1.0（假設暢通）
dynamic_weight = base_weight × congestion_factor
```

**替代方案**:
- 直接用 TDX TravelTime 替換 weight → 失去 base_weight 結構，無法在無資料時 fallback
- LOS 等級對照表 → 離散化太粗糙

**理由**: 物理意義清晰（速限除以即時車速 = 時間膨脹倍率）。MAX_FACTOR=10.0 避免極端值讓 A* 誤判為不可達。

### Decision 6: SpeedCamera 獨立 table + snap to nearest edge

**選擇**: 獨立 `speed_camera` table，每筆相機 snap 到最近的 TrafficEdge。路徑結果透過 edge_id JOIN 查詢。

**替代方案**:
- 欄位加在 TrafficEdge 上 → 一條 edge 可能有多個相機，一對多關係不適合

**理由**: 獨立 table 靈活，支援一對多，未來可擴充（固定桿 vs 移動式、啟用時段）。

### Decision 7: Nominatim 作為 geocoding 後端

**選擇**: 使用 OpenStreetMap Nominatim API，查詢時自動附加「高雄」關鍵字提升精度。

**替代方案**:
- Google Places API → 最準但需 billing
- TGOS → 台灣地址最準但需申請帳號

**理由**: 免費、無需 API key、台灣大型 POI 查詢準確度足夠畢專 demo。不足時再換 Google。

### Decision 8: TDX Live 資料透過 tdx_section_id mapping

**選擇**: `TrafficEdge` 新增 `tdx_section_id` (String) 欄位，seed 時從 JSON 的 `RoadSectionID` 寫入。Traffic Agent 拉到 Live 資料時以此欄位 mapping。

**替代方案**:
- 用路名 + 座標模糊比對 → 不可靠，同路名可能有多段

**理由**: `RoadSectionID` 是 TDX 的唯一識別碼，精確且穩定。

## Risks / Trade-offs

- **[Nominatim rate limit]** → Nominatim 限制 1 req/sec。Mitigation: 加 1 秒間隔，畢專流量極低不會觸發。
- **[TDX Live 不涵蓋所有 edge]** → 無即時資料的 edge fallback 為 congestion_factor=1.0，可能讓 A* 偏好「沒偵測器的路」。Mitigation: 高雄市主要道路覆蓋率高，小路本身車流少，fallback 為暢通合理。
- **[Penalty-based top-K 不保證嚴格第 K 短]** → 可接受，導航場景重視路徑差異性而非數學最優。
- **[In-memory graph 重啟後需重新載入]** → 從 DB 載入幾百 node 耗時 < 1 秒，可接受。
- **[測速照相機 CSV 可能更新]** → 定期手動重新下載即可，不需自動更新機制。
