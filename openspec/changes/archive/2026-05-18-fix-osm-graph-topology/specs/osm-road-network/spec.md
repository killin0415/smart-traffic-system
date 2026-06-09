## ADDED Requirements

### Requirement: 非 oneway way 雙向 edge
`scripts/build_graph_from_osm.sql` SHALL 對非 oneway 的 OSM way 在相鄰 vertex 之間建立雙向 `traffic_edge`（同時插入 A→B 與 B→A 兩筆 row），共享 `geom`、`length_km`、`road_class`、`max_speed_kmh`。

oneway 偵測規則 SHALL 為：`LOWER(tags->'oneway') IN ('yes', 'true', '1')` 視為正向 oneway（單一 A→B），`'-1'` 視為反向 oneway（單一 B→A），其餘所有值（含 `'no'`、`'false'`、`'0'`、`'reversible'`、空值、缺欄位）視為非 oneway 並產生雙向 edge。

#### Scenario: 一般街道（無 oneway tag）
- **WHEN** 處理一條 OSM way 且 `tags->'oneway'` 為 NULL 或不存在
- **THEN** 該 way 相鄰兩 vertex `v_i` 與 `v_{i+1}` 之間 SHALL 產生兩筆 `traffic_edge`：`source=v_i, target=v_{i+1}` 與 `source=v_{i+1}, target=v_i`
- **AND** 兩筆 edge 的 `length_km`、`road_class`、`max_speed_kmh`、`geom` SHALL 相同
- **AND** 兩筆 edge 的 `oneway` 欄位 SHALL 都為 FALSE

#### Scenario: oneway 正向街道
- **WHEN** 處理一條 OSM way 且 `LOWER(tags->'oneway')` 為 `'yes'`、`'true'` 或 `'1'`
- **THEN** 該 way 相鄰兩 vertex `v_i` 與 `v_{i+1}` 之間 SHALL 只產生一筆 `traffic_edge`：`source=v_i, target=v_{i+1}`
- **AND** 該 edge 的 `oneway` 欄位 SHALL 為 TRUE

#### Scenario: oneway 反向街道
- **WHEN** 處理一條 OSM way 且 `tags->'oneway'` 為 `'-1'`
- **THEN** 該 way 相鄰兩 vertex `v_i` 與 `v_{i+1}` 之間 SHALL 只產生一筆 `traffic_edge`：`source=v_{i+1}, target=v_i`
- **AND** 該 edge 的 `oneway` 欄位 SHALL 為 TRUE

#### Scenario: oneway 值為 `no` 視同非 oneway
- **WHEN** 處理一條 OSM way 且 `LOWER(tags->'oneway')` 為 `'no'`、`'false'` 或 `'0'`
- **THEN** SHALL 比照「一般街道（無 oneway tag）」處理，產生雙向 edge

### Requirement: Degree-2 chain contraction
`scripts/build_graph_from_osm.sql` SHALL 在所有 vertex 拆解、edge 建立完成後，執行一輪 topology contraction：將連續 degree-2 node 鏈合併成單一較長 edge，僅保留鏈頭尾兩端 node。

合併規則 SHALL 為：
- 一條 chain 中所有 edge 必須具有相同的 `road_class`、`max_speed_kmh` 與方向性（同 oneway 或同非 oneway）
- chain 頭尾兩端 node 為 intersection（degree ≥ 3）或 dead-end（degree = 1）
- chain 中所有「degree = 2 且兩條相鄰 edge 滿足合併條件」的中段 node 被刪除
- 合併後 edge 的 `length_km` 為鏈中所有 segment 之和
- 合併後 edge 的 `geom` 為 `ST_LineMerge(ST_Union(...))` 串接結果（保留 LINESTRING 型別）
- 合併後 edge 的 `road_class` 與 `max_speed_kmh` 取自鏈中任一 segment（規則上應一致）

contraction SHALL 對非 oneway 的雙向 edge 對（A→B + B→A）成對合併，合併後仍保留為兩筆雙向 edge。

#### Scenario: 簡單中段點合併
- **WHEN** 三個相鄰 node `A → B → C`，B 為 degree=2，兩條 edge `A→B` 與 `B→C` 具相同 road_class 與 max_speed_kmh
- **THEN** SHALL 刪除 node B 與兩條原 edge，INSERT 一條合併 edge `A→C`
- **AND** 合併 edge 的 `length_km` SHALL 為原兩條之和

#### Scenario: 跨 road_class 邊界處不合併
- **WHEN** 三個相鄰 node `A → B → C`，B 為 degree=2，`A→B` 為 `primary`、`B→C` 為 `secondary`
- **THEN** SHALL 保留 node B 與兩條原 edge，不執行合併
- **AND** B 在最終 graph 中仍為 degree=2 node（這類點不影響本 change 的 intersection ratio 目標）

#### Scenario: 雙向鏈成對合併
- **WHEN** 一條非 oneway 的 chain `A ↔ B ↔ C`（每對相鄰 node 都有兩筆雙向 edge）
- **THEN** SHALL 合併成 `A ↔ C` 一對雙向 edge（`A→C` 與 `C→A`）
- **AND** node B 與原 4 筆 edge SHALL 全部被刪除

#### Scenario: oneway 與非 oneway 鏈不互相合併
- **WHEN** 三個相鄰 node `A → B → C`，`A→B` 為 oneway、`B→C` 為非 oneway
- **THEN** SHALL 不合併，保留 node B

#### Scenario: 自環（A→A）由既有 grid-equality 過濾擋下
- **WHEN** 某 OSM way 的相鄰兩 vertex 因 `ST_SnapToGrid(0.00005)` 後座標相同
- **THEN** 既有 SQL 的 `WHERE NOT ST_Equals(s1.pt, s2.pt)` 過濾 SHALL 在 vertex 配對階段（contraction **之前**）就排除這對，因此 traffic_edge 不會產生 source = target 的 self-loop
- **AND** 本 change 不新增 source/target node-id 層級的 self-loop guard；contraction 階段不會收到任何 self-loop 為輸入，因此 SHALL 不需要特別處理 self-loop

### Requirement: Topology 健康度驗收門檻
`scripts/build_graph_from_osm.sql` 執行完成後 SHALL 自動 query `traffic_node` / `traffic_edge` 計算 topology 健康度指標，並在指標未達門檻時 RAISE NOTICE。

#### Scenario: Intersection ratio ≥ 50%
- **WHEN** build SQL 執行完成
- **THEN** SHALL 查詢「degree ≥ 3 的 node 數 / 總 node 數」
- **AND** 若比值 < 0.5 SHALL 用 `RAISE NOTICE` 印警告訊息（建議檢查 contraction 是否有效）
- **AND** 若比值 ≥ 0.5 SHALL 印出 INFO 級訊息

#### Scenario: Undirected connected component 覆蓋率 ≥ 99%
- **WHEN** build SQL 執行完成
- **THEN** SHALL 在 PostGIS 中用 recursive CTE 計算最大 undirected connected component 大小
- **AND** 若該 component 涵蓋 node 數 < 99% 總 node 數 SHALL `RAISE NOTICE` 列出最大 5 個其他 component 的 size + 樣本 node

#### Scenario: 違反門檻不阻斷流程
- **WHEN** 任一健康度指標未達門檻
- **THEN** build SQL SHALL 仍然 COMMIT 完成，不 ROLLBACK（門檻是 informational，不是 hard fail）
- **AND** caller（`scripts/import_taipei_osm.sh`）SHALL 不因 NOTICE 而 exit 非零

## MODIFIED Requirements

### Requirement: OSM 號誌節點 snap 到 traffic_node
系統 SHALL 在 graph build **與 degree-2 chain contraction 完成後** 一次性把 OSM `highway=traffic_signals` point snap 到最近 traffic_node 並標 `has_signal=TRUE`。執行順序 SHALL 在 contraction 之後，以避免中段號誌節點被 contraction 刪除而丟失。

#### Scenario: 用 PostGIS ST_DWithin 標號誌
- **WHEN** `scripts/build_graph_from_osm.sql` 執行到末尾（contraction 已完成）
- **THEN** SHALL 執行 `UPDATE traffic_node tn SET has_signal = TRUE WHERE EXISTS (SELECT 1 FROM planet_osm_point p WHERE p.highway = 'traffic_signals' AND ST_DWithin(p.way::geography, tn.geom::geography, 30))`
- **AND** snap 半徑為 30 公尺

#### Scenario: 號誌 30m 內無任何 traffic_node
- **WHEN** 一個 OSM `traffic_signals` point 30 公尺內找不到 traffic_node（contraction 後 intersection 變稀疏，可能發生）
- **THEN** SHALL 忽略該號誌、不影響其他 node 的 has_signal 判定
- **AND** 不視為錯誤（log 不需特別 warning）

#### Scenario: 中段號誌靠 contraction 重定位
- **WHEN** 某 OSM `traffic_signals` point 在 contraction 前最近的是某 chain 中段 node，contraction 後該中段 node 被刪除
- **THEN** SHALL snap 到 chain 端點（intersection node），只要該端點仍在 30 公尺內
- **AND** 若 chain 端點離號誌 > 30m 則該號誌被忽略（同上一 scenario）

#### Scenario: 多個 OSM 號誌 point snap 到同一 traffic_node
- **WHEN** 一個 traffic_node 30m 內有多個 `traffic_signals` point
- **THEN** `has_signal` SHALL 仍為 TRUE（不重複標記）
- **AND** A* 後續對該 node 仍只加一次 SIGNAL_PENALTY（per-node 不是 per-signal）
