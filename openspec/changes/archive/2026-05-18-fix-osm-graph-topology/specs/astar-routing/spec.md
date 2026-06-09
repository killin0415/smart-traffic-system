## MODIFIED Requirements

### Requirement: Snap to graph
系統 SHALL 將任意 GPS 座標 (latitude, longitude) 對應到路網圖上最合適的 node。預設取最近 K=15 個 node，並支援可選的 `return_top_n` 參數讓 caller 取得前 N 個排序後候選。

`return_top_n` 的回傳型別由**參數值**而非「是否傳入」決定：`return_top_n=1`（預設）回 `int | None`；`return_top_n ≥ 2` 回 `list[int]`。

#### Scenario: 找到最近的高 degree node
- **WHEN** 給定一個 GPS 座標，呼叫 `snap_to_graph(lat, lng, graph)`（採用 `return_top_n=1` 預設值）
- **THEN** SHALL 找到距離最近的 K=15 個 node，按「degree 由大到小、tie-break 距離由近到遠」排序
- **AND** 回傳排序後第一個 node 的 ID（`int`）

#### Scenario: 取得前 N 個候選 node
- **WHEN** 呼叫 `snap_to_graph(lat, lng, graph, return_top_n=5)`
- **THEN** SHALL 回傳排序後前 5 個 node ID 的 list（`list[int]`），順序與單一回傳之排序規則一致

#### Scenario: graph node 總數小於 N
- **WHEN** graph 中 node 總數 < `return_top_n`（極小圖 / 測試 fixture）
- **THEN** 回傳 list 長度 SHALL 等於實際 node 數，順序仍按相同排序規則

#### Scenario: 路網為空
- **WHEN** 路網圖無任何 node
- **THEN** `return_top_n=1`（預設）回傳值 SHALL 為 `None`
- **AND** `return_top_n ≥ 2` 回傳值 SHALL 為 `[]`

## ADDED Requirements

### Requirement: Snap-with-reachability fallback
`plan_optimal_route()` SHALL 在執行 A* 之前，對 origin 端與 destination 端的 snap 結果做 forward / reverse reachability 檢驗；若主 snap 候選不在 graph 的主要 strongly-connected 區域，SHALL 依序嘗試前 5 個候選。

forward reachability 檢驗 SHALL 使用有限步數 BFS（上限 1000 hops 或 5000 node 訪問，取先到者），目的是排除明顯的孤島型 / oneway-only 死巷 node，而不是完整 SCC 分析。

reachability 通過門檻為「能到達 ≥ `REACHABILITY_MIN_NODES` 個其他 node」，其中 `REACHABILITY_MIN_NODES = max(100, total_node_count // 1000)`。理由：post-contraction graph 預期 30–50k node，固定 100 約為 0.2–0.3% — 真孤島聚落可能 ≥ 100 而被誤判為主圖。用 `total // 1000` scale 後在 30k 圖約 ~30、50k 圖約 ~50、原始 224k 圖約 ~224；但仍以 100 為下限避免極小圖 BFS 樣本不足。

#### Scenario: 主 snap 候選可達就直接用
- **WHEN** origin snap 第一名為 node `S1`，從 `S1` BFS（forward edges）能在步數上限內到達 ≥ `REACHABILITY_MIN_NODES` 個其他 node
- **THEN** SHALL 採用 `S1` 為 start，不再嘗試其他候選

#### Scenario: 主 snap 候選為孤島，fallback 到下一個
- **WHEN** origin snap 第一名 `S1` 從 forward BFS 只能到達 < `REACHABILITY_MIN_NODES` 個 node（疑似孤島或單向死巷）
- **THEN** SHALL 取候選第二名 `S2` 重做 BFS 檢驗，依此類推到第五名
- **AND** SHALL 採用第一個通過檢驗的候選作為 start

#### Scenario: 五個候選全部失敗
- **WHEN** 前 5 個 snap 候選都未通過 reachability 檢驗
- **THEN** SHALL 用 `logger.warning` 記錄這個事件（含 origin 座標與 5 個候選的 node ID + degree）
- **AND** SHALL 仍回傳第一名 `S1` 作為 start（degraded behavior — 不阻斷 A\* 嘗試，畢竟 BFS 是 cheap heuristic 可能誤判）

#### Scenario: destination 端對稱檢驗
- **WHEN** 對 destination 座標 snap
- **THEN** SHALL 用 **reverse BFS**（透過 `RoadGraph.reverse_adjacency` 沿 incoming edges 反向走）替代 forward BFS 做相同檢驗
- **AND** 排序與 fallback 規則與 origin 端相同
- **AND** 通過檢驗的目的：確保至少有路徑「能走進」這個 node

#### Scenario: graph 為空
- **WHEN** `plan_optimal_route` 啟動時 graph 為空
- **THEN** SHALL 直接回傳既有的 `"road network not loaded"` 錯誤，**不**進入 reachability 檢驗流程

#### Scenario: 極小圖（總 node 數 < REACHABILITY_MIN_NODES）跳過檢驗
- **WHEN** `len(graph.nodes) < REACHABILITY_MIN_NODES`（測試 fixture / 極小子圖場景）
- **THEN** SHALL 跳過 reachability 檢驗、直接採用 snap 第一名為 start / end
- **AND** 不視為錯誤、不 log warning（這是有意設計，避免小圖把所有 node 誤判為 stub）

### Requirement: RoadGraph 預建 reverse adjacency
`RoadGraph.from_db()` SHALL 在 forward `adjacency` 建立完成後一次性掃描所有 edge 建立 `reverse_adjacency: dict[int, list[tuple[int, int, float]]]`，提供 reverse BFS 與後續 incoming-edge 查詢使用。

#### Scenario: 載入時建立 reverse adjacency
- **WHEN** `RoadGraph.from_db(session)` 執行完畢
- **THEN** graph 物件 SHALL 同時擁有 `adjacency` 與 `reverse_adjacency` 兩個 dict
- **AND** `reverse_adjacency[v]` 中每筆 `(u, edge_id, weight)` 對應 forward `adjacency[u]` 中存在 `(v, edge_id, weight)`

#### Scenario: 動態 update_weight 同步反向
- **WHEN** 呼叫 `graph.update_weight(edge_id, new_weight)` 更新某 edge weight
- **THEN** forward 與 reverse adjacency 中對應的 entry SHALL 同時更新
