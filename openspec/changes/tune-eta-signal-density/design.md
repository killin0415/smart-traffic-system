## Context

`fix-osm-graph-topology` 上線後 reimport 實機驗證得到：

| 指標 | 值 |
|---|---|
| 節點數 | 67,454 |
| 邊數 | 159,914 |
| 號誌節點數 | 8,877（13.2%） |
| weight_provider Tier 1 / 2 / 3 比例 | 53.9% / 45.8% / 0.3% |
| VD readings / 10 min | 1,611 |
| 原失敗 OD pair `(25.0478,121.5170) → (25.0337,121.5645)` 結果 | 3 routes，最快 7.6 min / 6.05 km |
| Google Maps 同 OD（日間 / 深夜） | 18 min / 8 min |

Route[0] 走「市民大道一段 → 市民大道高架 → 光復南路 → 忠孝東路 → 市府路」，跟 Google 路徑大致一致；速度估算誤差非「VD 沒在用」（VD 確實在用、報的速度也接近真實 free-flow），而是「**VD 量不到停等成本**」：

- VD 偵測器量的是車輛通過當下的瞬時速度，紅燈停的車根本被偵測不到；
- A* 既有 `SIGNAL_PENALTY_SECONDS = 20` 只加在 `has_signal=TRUE` node，且 30m snap 半徑漏掉很多遠端 traffic_signals；
- 即使 signal 都標到，20 秒嚴重低估號誌週期（紅燈 30-60 秒 × 50% 紅燈比例 + 起步加速延遲 ≈ 30-40 秒）。

`memory/eta_accuracy_followup.md` 列了三條修法 A / B / C，本 change 採 A + C 組合。

## Goals / Non-Goals

**Goals:**

- **G1**：原失敗 OD pair 的 ETA 從 7.6 min 提高到 12-16 min（Google 日間 18 min 的 ±35%）；同時深夜（無 VD 變化）仍維持 ~8-10 min（不破壞 free-flow 估計）。
- **G2**：對 trunk / motorway 路徑 ETA 影響限定在 ±5%（高架本來就沒紅綠燈，不該受 multiplier 影響）。
- **G3**：對 surface street 路徑 ETA 上升 30-60%（紅綠燈密度高的市區街道得到對應 penalty）。
- **G4**：weight_provider Tier 1 / 2 / 3 分層、KDTree 半徑、class-average 算法不動，保持既有 fallback 行為。
- **G5**：`route.request` / `route.response` Kafka schema、`plan_optimal_route()` Python signature、`traffic_node`/`traffic_edge` 既有欄位完全不破。

**Non-Goals:**

- 不引入外部 ETA 來源（Google Roads / OSRM）— 屬於 `eta_accuracy_followup.md` 的 Option C，後續另開 change。
- 不改 A* heuristic / bbox pruning / top-K rerun 邏輯。
- 不改 weight_provider 三層 tier 的選擇邏輯（只在 apply 後乘 multiplier）。
- 不改 SQL 中 `default_maxspeed()` 函數（屬於 Option B，與 A+C 正交，本 change 不做）。
- 不嘗試自動偵測「未在 OSM 標記的號誌」（例如把 degree ≥ 3 的 node 都當有號誌）— 假陽性風險高，留作 follow-up。

## Decisions

### D1：signal snap 半徑 30m → 50m

OSM `highway=traffic_signals` point 的座標通常標在交叉口中心或近燈杆位置，但 OSM way 的端點（snap 後變成 traffic_node）落在 carriageway 中心線。30m 半徑在窄街上夠用，但 Taipei 主幹道（忠孝東路、市民大道）車道寬 25-35m、加上號誌點偏移，常超過 30m → 漏掉。

提到 50m 平衡誤抓 / 漏抓：

- **誤抓風險**：50m 內可能涵蓋鄰近平行小巷的號誌，把該號誌算到主幹道 node 身上。但 Taipei 多數主幹道兩側 < 50m 沒有平行小巷號誌（小巷沒交通燈），實際誤抓率預估 < 5%。
- **漏抓改善**：預估 has_signal 覆蓋率從 13.2% 提到 18-22%（粗估 50%-75% 提升）。

**Alternatives 考慮過：**

- 半徑 100m → 誤抓率過高（涵蓋平行小巷號誌、跨街口錯標）。
- 動態半徑（依 road_class 調整）— 增加 SQL 複雜度，邊際收益小。

### D2：SIGNAL_PENALTY_SECONDS 20 → 40

Taipei 一般 signalised junction 平均週期約 90 秒（紅 50 + 綠 40 with all-red transitions），平均等待時間（假設均勻到達）≈ 25 秒 + 起步加速延遲 5-10 秒 ≈ **30-35 秒**。設定 40 秒留 buffer 給尖峰時段（週期延長到 120 秒、等待提到 45 秒）。

**Alternatives 考慮過：**

- 50 秒 → 對 surface street penalty 過重，會把所有路線都推向高架，失去多路徑選擇意義。
- 動態 penalty（依時段、依交通量）— 需要 telemetry / time-of-day modeling，超出本 change 範圍。
- Degree-aware penalty（degree=4 路口 40s、degree=3 T 字 25s）— 跟 D3 的 intersection-density multiplier 重複，且 D3 已經透過 multiplier 提供「沿路口越多越貴」的累進效果，degree-aware 邊際收益小。

### D3：Per-edge `intersection_count` 存 INTEGER 欄位，build 階段計算

把 multiplier 移到 weight_provider apply 時計算，**每條 edge 需要知道沿線經過幾個號誌路口**。可選方案：

- **D3a（採用）**：build 階段在 `traffic_edge` 新增 INTEGER 欄位 `intersection_count`，用 `ST_DWithin(edge.geom, traffic_signals_point, 15)` 計數（15m buffer），存進 DB。`weight_provider.apply_to_graph()` 從 `RoadGraph.edges[eid].intersection_count` 讀。
- **D3b（棄）**：runtime 計算 — 每次 weight rebuild 都 PostGIS query 一次 traffic_signals。每 5 分鐘一次 × 160k edges spatial query ≈ 重複浪費。
- **D3c（棄）**：用 `degree(node) ≥ 3` 推測號誌 — 過度推測，會把 dead-end + 三叉小巷都當有燈，假陽性高。

D3a 多一個 INTEGER 欄位（4 byte × 160k = 0.6 MB DB 增長，可忽略），build 時間多 ~30-60 秒（一次 spatial join）。

**15m buffer 而不是更大半徑**：edge geometry 是 contraction 後的長 polyline，可能 500-1000m，幾何上「在 edge 旁 15m 內」的號誌幾乎一定是這條 edge 經過的路口（街道中心線寬 5-15m，加上號誌點偏移 5-10m 都涵蓋）。如果用 30m，會把平行街道的號誌也算進來。

```sql
-- 偽碼。注意 COUNT(p.osm_id) 而非 COUNT(*) —— LEFT JOIN 下無匹配的 edge
-- 會留下 NULL 右側，COUNT(*) 會把該 NULL 算成 1（assigning 每條無號誌的
-- edge 一個假號誌），導致整網 multiplier ≥ 1.15。
UPDATE traffic_edge te
SET intersection_count = sub.cnt
FROM (
    SELECT te2.id, COUNT(p.osm_id) AS cnt
    FROM traffic_edge te2
    LEFT JOIN planet_osm_point p
      ON p.highway = 'traffic_signals'
     AND ST_DWithin(ST_Transform(p.way, 4326)::geography, te2.geom::geography, 15)
    GROUP BY te2.id
) sub
WHERE te.id = sub.id;
```

### D4：multiplier 公式 `(1 + 0.15 × intersection_count)`，trunk 系列排除

公式設計：

- 線性疊加：1 個號誌 +15%、3 個號誌 +45%、5 個號誌 +75%。
- 與 `SIGNAL_PENALTY_HR` 並行運作（penalty 加在 node、multiplier 乘在 edge），沒有 double-count，因為 penalty 只對 `has_signal=TRUE` 的 node 端點加一次、multiplier 反映「edge 經過的路口總數」（含 contraction 吃掉的中段）。
- 對既有 path 結果預期影響：
  - 高架（trunk）：multiplier × 1.0（排除），ETA 不變
  - Surface 主幹道（primary, secondary）：典型 500m 包含 4-6 號誌 → multiplier 1.6-1.9 → ETA 加 60-90%
  - 巷弄（residential, service）：每段 < 100m 通常 0-1 號誌 → multiplier 1.0-1.15

**為什麼 0.15 不是 0.2 或 0.1？**

- 25-30 秒等待 / 60 km/h × 500m edge 0.5 min travel time × multiplier - 0.5 min ≈ 0.4-0.5 min/intersection 額外時間 → multiplier per intersection 約 1.15-1.20
- 取 0.15 偏保守，先看實機效果再決定要不要調到 0.18-0.20

**Alternatives 考慮過：**

- 指數型 `1.2^intersection_count` — 5 個號誌 ×2.5，過度懲罰、不真實
- 飽和型 `1 + 0.5 × intersection_count / (5 + intersection_count)` — 5 個號誌 ×1.25，飽和太早，無法區分主幹道 vs 巷弄
- 用 `road_class` 加權的 multiplier — 過度複雜，邊際收益不確定

### D5：trunk / motorway 排除清單以 set 常數列出

`weight_provider.py` 加：

```python
INTERSECTION_MULTIPLIER_FACTOR = 0.15
INTERSECTION_MULTIPLIER_EXEMPT_CLASSES = frozenset({
    "motorway", "trunk", "motorway_link", "trunk_link",
})
```

兩者宣告為模組常數，方便 monkeypatch 測試與日後 env override。`trunk_link` / `motorway_link` 也排除是因為匝道接在 trunk 後段、不該被視為一般 surface street；雖然匝道實際上有 yield 標誌 / 上下游號誌，但用 SIGNAL_PENALTY 已涵蓋。

### D6：`GraphEdge.intersection_count` dataclass 欄位 + RoadGraph.from_db 載入

對應 ORM `TrafficEdge.intersection_count` 欄位，`RoadGraph.from_db()` 在建 GraphEdge 時填值。

`GraphEdge` 預設值 `intersection_count: int = 0`，向後相容（如果 DB 沒有此欄位仍能載入，weight × 1.0 = 不變）。

### D7：apply_to_graph 公式調整

現行：

```python
def apply_to_graph(self, graph):
    for edge in graph.edges.values():
        speed, _ = self.get_speed(edge)
        speed = max(speed, _MIN_SPEED_KMH)
        weight_hours = edge.length_km / speed
        graph.update_weight(edge.id, weight_hours)
```

修改後：

```python
def apply_to_graph(self, graph):
    for edge in graph.edges.values():
        speed, _ = self.get_speed(edge)
        speed = max(speed, _MIN_SPEED_KMH)
        base = edge.length_km / speed
        if edge.road_class in INTERSECTION_MULTIPLIER_EXEMPT_CLASSES:
            multiplier = 1.0
        else:
            multiplier = 1.0 + INTERSECTION_MULTIPLIER_FACTOR * edge.intersection_count
        graph.update_weight(edge.id, base * multiplier)
```

不改 `get_speed` 內部三層 tier 邏輯。

## Risks / Trade-offs

- **R1（中）：誤抓 OSM 平行小巷號誌**。50m signal snap 半徑可能把鄰近平行街道號誌錯標到主幹道 node 身上。預估 < 5% 誤判率，但個別 case 可能造成主幹道 weight 過高。Mitigation：tasks.md §6.4 加一個 sanity check 比較新舊 has_signal 數量級（不該突然爆增 3 倍），超出範圍 RAISE NOTICE。
- **R2（中）：multiplier 公式低估號誌密度高的 hot spot**。線性 0.15 / intersection 對 8-10 個號誌的 1km long edge 給 multiplier ~2.0-2.2，可能仍不夠（實際塞車比例可達 ×3）。Mitigation：本 change 先用 0.15 取保守值；若實機 benchmark 仍偏低，下一個 change 改 0.18-0.20 或考慮 saturating curve（spec scenario 已留可調空間）。
- **R3（低）：intersection_count 計算與 contraction 順序耦合**。Contraction 跑完才有「最終 edge geom（長 polyline）」，所以 intersection_count 必須在 contraction + signal snap 都跑完之後才算。Mitigation：tasks.md §2.1 明確把 intersection_count UPDATE 放在 build SQL 末段倒數第二步（健康度 metrics 之前）。
- **R4（低）：對 cold-start 沒 VD readings 的場景，multiplier 也會放大 Tier 3 估算的失真**。如果整網 fallback 到 Tier 3（VD ingest 沒跑），weight = length / (max_speed × cal) × multiplier，誤差會疊加。Mitigation：cold-start 屬於非穩態，不在本 change 設計範圍內；reimport 流程已要求先確認 VD readings ≥ 10 min。
- **R5（低）：trunk 排除清單漏掉某些高架 sub-class**。OSM `highway` tag 有 `motorway` / `trunk` / `motorway_link` / `trunk_link` / `primary`（部分高架被誤標為 primary）。Mitigation：以實機觀察為準，發現有 ETA 異常的 primary 高架再加白名單例外（spec scenario 已寫「對 trunk/motorway 系列不套用」，未來再加 class 時 spec 仍適用）。

## Migration Plan

### 部署步驟（dev / capstone 環境）

1. 在 develop branch merge 本 change 的 commit
2. **不需要停 multiagent-service** — 但需要：
   1. 跑 `psql -f infra/init-db/02-road-network-tables.sql`（idempotent，ALTER TABLE ADD COLUMN IF NOT EXISTS 會跳過已存在的欄位，或補上 `intersection_count INTEGER NOT NULL DEFAULT 0`）。
   2. 跑 `psql -f scripts/build_graph_from_osm.sql`（會 TRUNCATE + 重 build，新欄位會被填值；reuse 上次 OSM ingestion）
3. **重啟 multiagent-service** — `RoadGraph.from_db` 會讀新的 intersection_count 欄位、`apply_to_graph` 會套 multiplier
4. Smoke test：跑 `scripts/_acceptance_failing_route.py`，看原失敗 OD pair 的 ETA 是否進入 12-16 min 範圍

### Rollback

1. `git revert <tune-eta-signal-density commit>`
2. 不需重跑 SQL 也不需 ALTER TABLE — intersection_count 欄位留著無害（multiplier 還是 1 + 0.15 × N，但只要 Python 端 multiplier 公式 revert 成沒有 multiplier，weight 計算就回到舊行為）
3. 重啟 multiagent-service

無 schema 不相容、無資料遷移風險。
