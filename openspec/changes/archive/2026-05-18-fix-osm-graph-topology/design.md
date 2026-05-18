## Context

`taipei-opendata-rebuild` 引入了 OSM 路網 build pipeline，但 `scripts/build_graph_from_osm.sql` 的 transform 邏輯把每個 OSM way vertex 都當作 graph node，且對非 oneway way 只插單向 edge。實測（224,162 node / 248,644 edge）：

| degree | nodes | % |
|---|---|---|
| 0 | 240 | 0.1% |
| 1 | 56,988 | 25.4% |
| 2 | 130,184 | 58.1% |
| 3+ | 36,750 | **16.4%** |

且 forward BFS 從一個一般 node 出發只能到 219,900 個 node（98%），剩餘 2% 因「只能進不能出」或「只能出不能進」的方向性問題形成 sink / source 群。`snap_to_graph` 的 k=3 + max-degree 選擇邏輯在 25% 的 degree ≤ 1 區域很容易選到 stub node。

`taipei-opendata-rebuild` 本身尚未 archive，所以本 change 修的是 in-flight 規格的不足。改完之後預期 intersection 比例 ≥ 50%，user-facing failure 案例（北車 → 信義「no path found」）消失。

## Goals / Non-Goals

**Goals:**
- **G1**：非 oneway way 在 graph 中**雙向可走**。
- **G2**：connected component（undirected）覆蓋率 ≥ 99%；strongly connected component（directed）覆蓋率 ≥ 95%。
- **G3**：intersection ratio（degree ≥ 3）≥ 50%。
- **G4**：`snap_to_graph` 即使遇到 stub-rich 區域也能 fallback 到實際可達的 node，而非 silent fail。
- **G5**：對 `route.request` / `route.response` Kafka wire schema 完全不破。
- **G6**：對 A\* 主迴圈、bbox pruning、top-K penalty rerun、weight provider tier 等既有邏輯零影響。

**Non-Goals:**
- 不重新評估 OSM 來源（仍是 geofabrik taiwan-latest.osm.pbf）。
- 不引入新的 graph library（NetworkX / igraph / OSMnx）— 保持 in-house adjacency dict + 純 SQL build。
- 不變動 traffic_node / traffic_edge schema 欄位（仍是 c8136ba 那組）。
- 不處理 turn restriction（OSM `restriction=*` relation）— 那是另一個更深的議題，可獨立 propose。
- 不嘗試解決 OSM 本身的拓樸錯誤（孤島路、未連接的 service road）— 那些屬 data quality issue，不在本 change 範圍。

## Decisions

### D1：oneway 偵測規則 — `LOWER(tags->'oneway') IN ('yes','true','1','-1')`

OSM `oneway` tag 合法值含 `yes`、`true`、`1`（正向）、`-1`（反向）、`no`、`false`、`0`、`reversible`、空值或缺欄位。

本 change 把前四個值（正反向）都當作 oneway 處理：
- `yes` / `true` / `1` → 沿 way 方向單向 edge（A→B）
- `-1` → 反向單向 edge（B→A）
- 其餘（含 `no`、缺欄位） → 雙向 edge（A→B + B→A）

**Why not 把 `-1` 也當作雙向？** OSM `oneway=-1` 是明確的反向 oneway 標記（少見但合法），如果當雙向會錯誤允許逆向行駛。

**Alternatives 考慮過：**
- 用 osm2pgsql 的 `oneway` 欄位（已 parse 成 boolean）— 但會丟失 `-1` 反向資訊。手動讀 `tags->'oneway'` 雖然多一行 SQL 但語意完整。

### D2：Degree-2 chain contraction 走純 SQL，不寫 Python

OSM build 已經是純 SQL pipeline，contraction 用 recursive CTE 一輪掃完所有 chain，無需 Python 介入。

```
-- 偽碼（欄位名對齊 traffic_edge schema：source_node_id / target_node_id）
WITH RECURSIVE chain AS (
  SELECT source_node_id AS chain_src, target_node_id AS chain_tgt,
         ARRAY[id] AS edge_ids, length_km, road_class, max_speed_kmh, oneway
  FROM traffic_edge WHERE source_node_id IN (degree=2 set)
  UNION ALL
  SELECT chain.chain_src, next.target_node_id,
         chain.edge_ids || next.id, chain.length_km + next.length_km,
         chain.road_class, chain.max_speed_kmh, chain.oneway
  FROM chain
  JOIN traffic_edge next
    ON next.source_node_id = chain.chain_tgt
   AND next.road_class = chain.road_class
   AND next.max_speed_kmh = chain.max_speed_kmh
   AND next.oneway = chain.oneway
   AND <degree(chain.chain_tgt) = 2 condition>
)
-- 取每條 chain 的最長合併、刪除原 edge、插入合併 edge
```

實作細節：
- 鏈中所有 edge 必須同 `road_class` + 同 `max_speed_kmh` + 同 `oneway` 方向（避免合併兩段速限不同的路）。
- 鏈頭尾兩端 node 保留；中間 node DELETE（CASCADE 會帶走原 edge，再 INSERT 合併版）。
- 合併 edge 的 `geom` 用 `ST_LineMerge(ST_Union(...))` 串接。
- A* heuristic 不變：仍是 `haversine / max_speed_kmh`，邊變長 hop 變少。

**Alternatives 考慮過：**
- 在 Python `RoadGraph.from_db()` 端做 contraction — 把複雜度推到 multiagent-service，違背 build-time 邏輯歸 SQL 的既有結構。

### D3：Contraction 後再 snap 號誌，不是相反順序

OSM 一條 way 的 vertex 序列可能在某中段點貼到 `highway=traffic_signals` point（30m 內）。若先 snap 號誌再 contraction，這個中段 node 會被合併刪除，`has_signal` 標記就丟了。

決定：
1. Build raw node + edge（含所有 vertex）
2. **Contraction**（刪中段 node）
3. **Snap 號誌**（在剩下的 intersection node 上，用 30m 半徑找號誌）
4. **VD snap road_class**（在 contraction 後的 edge 上）

效果：原本貼在中段的號誌會 snap 到 chain 的某個端點（最近的一端）。30m 內仍然合理。

**Trade-off**：少數號誌可能被誤合到較遠的 intersection node（chain 中段距某號誌 5m、但 chain 端點距該號誌 25m）— 仍在 30m 半徑內，A* 仍會 penalty。可接受。

### D4：snap-with-reachability fallback 放在 `plan_optimal_route`，不放在 `snap_to_graph`

`snap_to_graph` 維持純幾何 + degree 排序，不知道 graph 連通性。`plan_optimal_route` 才有完整 RoadGraph 與「主圖」概念，是 reachability 檢查的合理位置。

實作：
```python
candidates = snap_to_graph(lat, lng, graph, k=15, return_top_n=5)  # 回前 5 candidate
for start in candidates:
    if has_outgoing_path(start, graph):  # cheap BFS limited to ~1000 hops
        return start
return candidates[0]  # 全失敗就走原本第一名
```

**Reverse BFS 的 adjacency 來源**：destination 端要做 incoming-edge BFS（從 dest 沿反向走出去看能到誰），但 `RoadGraph` 目前只有 forward `adjacency` dict。實作方案：`RoadGraph.from_db()` 末段 SHALL 在 forward adjacency 建好後**一次性掃過所有 edge** 建出 `reverse_adjacency: dict[int, list[tuple[int, int, float]]]`，O(E) one-time cost、記憶體增加 ≈ forward adjacency 同等級。Per-request reverse BFS 直接查這個 dict 即可，不需要每次 O(E) 重算。

**Why not 全 BFS 預建 reachability set？** 224k node BFS 在 lifespan 跑一次 ~2s，但本 change 結束後 node 數降到 ~30–50k，per-request BFS 微秒等級（用上述 reverse_adjacency），不需要進一步快取。

### D5：`snap_to_graph` k 從 3 提高到 15，return_top_n 預設 1

目前 `snap_to_graph` 回單一 int。為了 D4 的 fallback，要新增可選參數 `return_top_n: int = 1`，當 ≥ 2 時回 `list[int]`（依 degree → distance 排序）。

簽名變化：
- 既有 caller `snap_to_graph(lat, lng, g)` 行為不變（回 int）
- 新增 `snap_to_graph(lat, lng, g, k=15, return_top_n=5)` 回 `list[int]`

**Alternatives 考慮過：**
- 不加 return_top_n，直接讓 `plan_optimal_route` 自己重做 k-d tree 查詢 — 但 `snap_to_graph` 已經有 degree 排序邏輯，重做會 duplicate。

### D6：Reimport 不引入 migration script

`traffic_node` / `traffic_edge` schema 沒變、欄位完全相容。只是內容（rows）會被重灌。`build_graph_from_osm.sql` 本來就 `TRUNCATE ... RESTART IDENTITY CASCADE`，直接重跑即可。不需要 Alembic migration。

## Risks / Trade-offs

- **R1（中）**：node id 變動 — 既有 `vd_static.snapped_road_class` 引用的 edge id 會被重新分配。Mitigation：tasks.md §6 SHALL 在 contraction 完之後重跑 VD snap（與 taipei-opendata-rebuild 既定的 `scripts/post_build_snap_vd.sql` 相同邏輯）。speed_camera 的 `nearest_edge_id` 同理 SHALL 重算。

- **R2（中）**：contraction 邏輯邊界條件多 — chain 中段點若是「岔路口（degree=3）」絕不能合併、若是「U turn 處（兩條 edge 都連回自己）」要忽略、若是「跨 way 邊界 highway 不同」要斷開。Mitigation：tasks.md 寫 5 個專門的 SQL unit test（含合成 fixture），測 chain 合併行為。

- **R3（低）**：snap fallback 在「整個 5 個候選都不可達」的極端 case 仍會回第一名（degraded 行為） — 但本 change 的 topology 修好後，這 case 機率趨近 0。Mitigation：用 logger.warning 記錄這個 fallback 失效事件，後續可追蹤。

- **R4（低）**：合併 edge 變長後，`weight_provider` Tier-1（VD 鄰近反距離加權）的 midpoint lookup 精度變差 — 一條 800m 的合併 edge 取中點離兩端 400m。Mitigation：`weight_provider` 的 `_KDTREE_RADIUS_DEG = 0.01`（≈ 1km）半徑遠大於這個誤差，可接受。如果未來要更精準，可改成 edge geom 多採樣，但本 change 不做。

- **R5（低）**：reimport 期間 multiagent-service 重啟 / route.request 短暫無法處理 — capstone 期間沒有 SLA，可接受。Mitigation：tasks.md 標明應在低流量時段（半夜）執行。

## Migration Plan

### 部署步驟（dev / capstone 環境）

1. 在 develop branch merge 本 change 的 commit
2. 停 multiagent-service uvicorn process
3. 跑 `psql -f scripts/build_graph_from_osm.sql`（會 TRUNCATE 既有 traffic_node/edge + 重 build + contraction + snap 號誌）
4. 跑 `psql -f scripts/post_build_snap_vd.sql`（重新計算 VD snapped_road_class）
5. 重啟 multiagent-service — `seed_speed_cameras` 會在 lifespan 中用 PostGIS `ST_Distance + ORDER BY LIMIT 1` 自動把測速相機重 snap 到新 edge id（既有 `speed_camera.py` 邏輯，不需要額外腳本）
6. Smoke test：`uv run python scripts/demo_plan_route.py` 跑既有 demo 座標 + 本 change 原失敗座標 `(25.0478,121.5170) → (25.0337,121.5645)`

### Rollback

`scripts/build_graph_from_osm.sql` 是冪等的（每次都 TRUNCATE + 重 build），rollback 只要：

1. `git revert <fix-osm-graph-topology commit>`
2. 重跑 `build_graph_from_osm.sql`（會回到舊邏輯產生的 graph）

無 schema migration、無資料遷移風險。

## Open Questions

- **OQ1**：A\* 預期速度提升幅度 — 在現有 graph 上一次 plan_route ~100ms，contraction 後預計可降到 ~30ms，但這是假設。SHALL 在 tasks.md §9 加上一個 benchmark task 量測前後對比，數據放進 commit message。
- **OQ2**：是否在 main-service 端加 `routes[].path` 為空時的退場 message — 本 change 修完應該不會再有「no path found」case，但若仍出現，user 需要看到友善訊息。**範圍**：超出本 change，不處理；後續若 main-service 那邊有需求另開 issue。

**已關閉的問題**：
- ~~service road class 是否例外於 contraction~~ → **決議**：D2 一律合併，理由是 weight_provider Tier-1 KDtree 半徑 ~1km 遠大於 service road 合併後典型長度（< 200m），影響可忽略；若 demo 中看到 service road 路徑異常再回頭例外。
