## MODIFIED Requirements

### Requirement: OSM 號誌節點 snap 到 traffic_node
系統 SHALL 在 graph build **與 degree-2 chain contraction 完成後** 一次性把 OSM `highway=traffic_signals` point snap 到最近 traffic_node 並標 `has_signal=TRUE`。snap 半徑 SHALL 為 **50 公尺**（自 `fix-osm-graph-topology` 的 30m 提高，以涵蓋寬幹道號誌標點偏移）。執行順序 SHALL 在 contraction 之後，以避免中段號誌節點被 contraction 刪除而丟失。

#### Scenario: 用 PostGIS ST_DWithin 標號誌
- **WHEN** `scripts/build_graph_from_osm.sql` 執行到末尾（contraction 已完成）
- **THEN** SHALL 執行 `UPDATE traffic_node tn SET has_signal = TRUE WHERE EXISTS (SELECT 1 FROM planet_osm_point p WHERE p.highway = 'traffic_signals' AND ST_DWithin(ST_Transform(p.way, 4326)::geography, tn.geom::geography, 50))`
- **AND** snap 半徑為 **50 公尺**

#### Scenario: 號誌 50m 內無任何 traffic_node
- **WHEN** 一個 OSM `traffic_signals` point 50 公尺內找不到 traffic_node（contraction 後 intersection 變稀疏，可能發生）
- **THEN** SHALL 忽略該號誌、不影響其他 node 的 has_signal 判定
- **AND** 不視為錯誤（log 不需特別 warning）

#### Scenario: 中段號誌靠 contraction 重定位
- **WHEN** 某 OSM `traffic_signals` point 在 contraction 前最近的是某 chain 中段 node，contraction 後該中段 node 被刪除
- **THEN** SHALL snap 到 chain 端點（intersection node），只要該端點仍在 50 公尺內
- **AND** 若 chain 端點離號誌 > 50m 則該號誌被忽略（同上一 scenario）

#### Scenario: 多個 OSM 號誌 point snap 到同一 traffic_node
- **WHEN** 一個 traffic_node 50m 內有多個 `traffic_signals` point
- **THEN** `has_signal` SHALL 仍為 TRUE（不重複標記）
- **AND** A* 後續對該 node 仍只加一次 SIGNAL_PENALTY（per-node 不是 per-signal）

#### Scenario: has_signal 覆蓋率不應驟降或暴增
- **WHEN** build SQL 執行完成後檢查 `COUNT(*) FILTER (WHERE has_signal) / COUNT(*)`
- **THEN** 覆蓋率 SHALL 落在 10%-30% 區間（半徑 30m 約 13%、50m 預估 18-22%）
- **AND** 若落在區間外 SHALL 用 `RAISE NOTICE` 印警告（informational，不阻斷 commit）

## ADDED Requirements

### Requirement: Per-edge intersection_count 欄位
`traffic_edge` SHALL 新增 `intersection_count INTEGER NOT NULL DEFAULT 0` 欄位，存「edge 幾何 15m buffer 內的 OSM `highway=traffic_signals` point 數量」，由 `scripts/build_graph_from_osm.sql` 在 build 末段填值。

#### Scenario: schema 包含 intersection_count
- **WHEN** 檢查 `traffic_edge` schema
- **THEN** SHALL 包含 `intersection_count INTEGER NOT NULL DEFAULT 0`
- **AND** 該欄位 SHALL 由 `infra/init-db/02-road-network-tables.sql` 的 `CREATE TABLE IF NOT EXISTS` 與 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 兩種寫法皆能無錯誤建立（向後相容既有部署）

#### Scenario: build SQL 計算每條 edge 的 intersection_count
- **WHEN** `scripts/build_graph_from_osm.sql` 執行到末段（contraction、signal snap 都已完成、健康度 metrics 之前）
- **THEN** SHALL 為每條 `traffic_edge` 用 `ST_DWithin(ST_Transform(p.way, 4326)::geography, te.geom::geography, 15)` 數出 `highway='traffic_signals'` 的 point 數量並 UPDATE
- **AND** 計數 SHALL 使用 `COUNT(p.osm_id)`（或 `COUNT(*) FILTER (WHERE p.osm_id IS NOT NULL)`）而非 `COUNT(*)`，避免 LEFT JOIN 時將「無匹配」誤算為 1
- **AND** 沒有 traffic_signals 在 15m 內的 edge SHALL 保持 `intersection_count = 0`（DEFAULT）

#### Scenario: 15m buffer 限制
- **WHEN** 計算 intersection_count 用 ST_DWithin
- **THEN** 半徑 SHALL 為 **15 公尺**（避免把平行小巷的號誌算進主幹道 edge）

#### Scenario: contraction 後的長 edge 也能正確計數
- **WHEN** 一條 contraction 後的 edge（geom 為跨多個原 OSM 中段的 polyline）經過多個號誌路口
- **THEN** `intersection_count` SHALL 等於該 polyline 15m buffer 內所有 `traffic_signals` point 的總數
- **AND** 此計數依靠 edge.geom（contraction 已合併為單一 LineString），不依靠任何單一 node

#### Scenario: 重新 build 時清零並重算
- **WHEN** `scripts/build_graph_from_osm.sql` 再次執行
- **THEN** TRUNCATE 階段 SHALL 移除所有 traffic_edge（含 intersection_count），重 build 與重新填值
- **AND** 不需要額外的 reset 邏輯
