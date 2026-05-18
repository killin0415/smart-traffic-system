## 1. SQL build：oneway 雙向修正

- [x] 1.1 在 `scripts/build_graph_from_osm.sql` 找出既有相鄰 vertex 插入 `traffic_edge` 的 INSERT 區塊
- [x] 1.2 用 CTE 把 oneway tag 解析成 `direction_kind ∈ {'fwd','rev','both'}`（規則見 design D1）
- [x] 1.3 對 `direction_kind = 'fwd'` 插入單筆 `source=v_i, target=v_{i+1}`；`'rev'` 插入單筆 `source=v_{i+1}, target=v_i`；`'both'` 插入兩筆雙向
- [x] 1.4 保留既有 `WHERE NOT ST_Equals(s1.pt, s2.pt)`（line 121 vertex 層級的 self-loop guard 已足夠，spec scenario「自環由既有 grid-equality 過濾擋下」對應的就是這行）
- [x] 1.5 跑一次 build，psql 跳出總 edge 數，預期比目前 248k 多（因雙向 edge 變多），暫先記下這個數字

## 2. SQL build：degree-2 chain contraction

- [x] 2.1 在 `scripts/build_graph_from_osm.sql` 末段（vertex 與 edge 都建好之後、號誌 snap 之前）新增 contraction 區塊
- [x] 2.2 用 recursive CTE 找出所有 degree=2 node 構成的 chain，鎖在「同 road_class + 同 max_speed_kmh + 同 direction」內
- [x] 2.3 對每條 chain：計算合併 edge 的 `length_km = SUM(segment.length_km)`、`geom = ST_LineMerge(ST_Union(...))`
- [x] 2.4 在交易內 DELETE 中段 node 與原 edge、INSERT 合併 edge（注意 CASCADE 設定避免誤刪鏈頭尾）
- [x] 2.5 雙向 chain 成對處理（看 spec scenario「雙向鏈成對合併」）—合併後仍是兩筆 edge `A→C` 與 `C→A`
- [x] 2.6 跨 road_class / 跨 max_speed / 跨方向邊界 SHALL 自然斷開 chain（CTE 條件已包含）

## 3. SQL build：topology 健康度自驗收

- [x] 3.1 build SQL 末段（contraction 與號誌 snap 都跑完後）`SELECT` 計算 intersection ratio = COUNT(degree≥3) / COUNT(*)
- [x] 3.2 計算最大 undirected connected component size（recursive CTE 從任意 node 起始 BFS）
- [x] 3.3 若 intersection_ratio < 0.5 或 main_component_pct < 0.99，`RAISE NOTICE` 印警告含當前數值
- [x] 3.4 用 INFO 級訊息固定印出 `node_count / edge_count / intersection_ratio / main_component_pct` 作為 build artifact 證據

## 4. SQL build：調整號誌 snap 順序

- [x] 4.1 把既有 `UPDATE traffic_node SET has_signal=TRUE ... ST_DWithin(..., 30)` 區塊**移到** contraction 之後執行
- [x] 4.2 確認 contraction 中沒有任何邏輯把已經有 `has_signal=TRUE` 的 node 視為「可合併中段點」（contraction 在此之前跑，has_signal 此時都還是 FALSE，自然安全）
- [x] 4.3 在 build 結束 SELECT 出 `has_signal=TRUE` 的 node 數量，確認與舊 build 數量級接近（避免號誌大量丟失）

## 5. VD / speed_camera 後置 snap 重跑

- [x] 5.1 確認 `scripts/post_build_snap_vd.sql`（taipei-opendata-rebuild 既定）能在 contraction 後正常產出 `vd_static.snapped_road_class`
- [x] 5.2 跑一次 VD snap 並 sanity check：≥ 90% 的 VD 有非 NULL `snapped_road_class`
- [x] 5.3 確認 `speed_camera.nearest_edge_id` 在 build 後仍指向有效 edge（edge id 經 TRUNCATE 重新分配；既有 seed 流程在 `lifespan` 內，下次重啟自動重 snap，無需手動腳本）

## 6. Python：snap_to_graph 強化

- [x] 6.1 改 `backend/multiagent-service/src/agents/routing.py:snap_to_graph()`：預設 `k` 從 3 提到 15
- [x] 6.2 新增可選參數 `return_top_n: int = 1`；當 ≥ 2 時回 `list[int]`，依 degree → distance 排序
- [x] 6.3 確認既有 caller `snap_to_graph(lat, lng, g)` 行為不變（仍回單一 int）
- [x] 6.4 為 `snap_to_graph` 加 2 個 unit test：(a) `return_top_n=5` 回正確排序；(b) graph 為空時 return_top_n=5 回 `[]`

## 7. Python：plan_optimal_route reachability fallback

- [x] 7.1 在 `RoadGraph` 新增 `reverse_adjacency: dict[int, list[tuple[int, int, float]]]` 欄位，並在 `from_db()` 末段一次性掃過 `self.adjacency` 建立反向 dict（O(E) one-time）。語意：`reverse_adjacency[v]` 為「指向 v 的所有 edge」之集合，每個 entry 為 `(source_node, edge_id, weight)`
- [x] 7.2 重構 `update_weight(edge_id, new_weight)`：**移除**現有 `for u,v in ((s,t),(t,s))` 雙向迴圈（第二輪本來就是 no-op；雙向街道現在是兩筆獨立 edge_id，各自由各自的 `update_weight` 呼叫處理）。改為直接更新 `forward[source][...]` 與 `reverse[target][...]` 兩個 entry（一個 edge_id 在 forward 出現一次、在 reverse 出現一次）
- [x] 7.3 新增 helper `_has_outgoing_reach(graph, node_id, min_reach) -> bool`：limited BFS forward，上限 1000 hops 或 5000 node 訪問，到達數 ≥ min_reach 就回 True
- [x] 7.4 新增 helper `_has_incoming_reach(graph, node_id, min_reach) -> bool`：用 `graph.reverse_adjacency` 做相同 limited BFS
- [x] 7.5 計算 `REACHABILITY_MIN_NODES = max(100, len(graph.nodes) // 1000)` 於 `plan_optimal_route` 進入時一次
- [x] 7.6 改 `plan_optimal_route` 既有 snap 區塊（`routing.py:506-507` 的 `start_id = snap_to_graph(...)` 與 `end_id = snap_to_graph(...)` 兩行）為：先拿 `return_top_n=5` 候選 list，再依序套用 outgoing / incoming 檢驗找實際 start / end
- [x] 7.7 五個候選都不過時 `logger.warning(...)`、仍用第一名（degraded 行為，spec 已記）
- [x] 7.8 graph 為空時的 `"road network not loaded"` 早退路徑不變（不進入 reachability 流程）

## 8. 測試

- [x] 8.1 在 `tests/test_build_graph_sql.py`（既有）新增 fixture：5 個合成 way 含 oneway / non-oneway / chain / 跨 class 邊界
- [x] 8.2 測 SQL build 後該 fixture 的 edge 方向性正確（非 oneway 雙向、oneway=-1 反向）
- [x] 8.3 測 contraction 後該 fixture 的 chain 確實合併、跨 class 處不合併
- [x] 8.4 測 self-loop 不出現
- [x] 8.5 在 `tests/test_routing.py`（既有）新增 6 個 test：
   - `test_snap_to_graph_return_top_n_value_dispatch`（驗證 `return_top_n=1` 回 int、≥2 回 list；含空圖兩種分流）
   - `test_road_graph_builds_reverse_adjacency`（驗證 from_db 後 reverse_adjacency 正確 mirror forward）
   - `test_road_graph_update_weight_syncs_reverse`（forward + reverse 同步）
   - `test_plan_route_falls_back_on_unreachable_origin_snap`（origin 端 fallback）
   - `test_plan_route_falls_back_on_unreachable_destination_snap`（dest 端 fallback，對稱 reverse BFS）
   - `test_plan_route_skips_reachability_on_tiny_graph`（< REACHABILITY_MIN_NODES 時不跑檢驗）
- [x] 8.6 `uv run pytest` 整套通過（branch-off baseline 199 unit + 新增約 10 個 ≈ 209）

## 9. Reimport 實機驗證

- [x] 9.1 停 multiagent-service uvicorn
- [x] 9.2 跑 `psql -f scripts/build_graph_from_osm.sql`，存下 stdout（含 §3 印的健康度指標）
- [x] 9.3 確認 intersection_ratio ≥ 0.5、main_component_pct ≥ 0.99
- [x] 9.4 跑 `psql -f scripts/post_build_snap_vd.sql`
- [x] 9.5 重啟 multiagent-service，確認 startup log 無新 error
- [x] 9.6 跑 `uv run python scripts/demo_plan_route.py` 對既有 demo 座標確認還能找到路
- [x] 9.7 用本 change proposal 的原失敗座標 `(25.0478,121.5170) → (25.0337,121.5645)` 送 Kafka `route.request` 或直接呼叫 `plan_optimal_route`，**確認回傳含 ≥ 1 條 route**（不再 `no path found`）
- [x] 9.8 量 A* benchmark：跑 10 個典型 OD pair 取平均 latency，與 reimport 前的數字比對；數據放進 commit message

## 10. Memory + 收尾

- [x] 10.1 更新 memory `routing_algorithm.md`：把過期的 `traffic.py` / `road_network.py` file pointer 刪除（這兩個檔 c8136ba 之後就不存在），補上 `vd_traffic.py` / `weight_provider.py` 的真實位置與三層 tier 簡述
- [x] 10.2 更新 memory `eta_accuracy_followup.md`：紀錄 graph topology 修好後可重新 benchmark ETA 與 Google Maps 差距
- [ ] 10.3 commit SQL / Python 改動為一個 commit；§10.1 / §10.2 的 memory 更新另開獨立 commit（避免 revert SQL 時誤動 memory）
- [ ] 10.4 push develop branch
