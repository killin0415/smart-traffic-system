## ADDED Requirements

### Requirement: Taiwan OSM PBF 一次性下載
系統 SHALL 提供 shell script 下載 Taiwan OSM PBF 檔案，並裁切到台北市 bounding box。

#### Scenario: 從 geofabrik 下載完整 Taiwan PBF
- **WHEN** 執行 `bash scripts/import_taipei_osm.sh`
- **THEN** SHALL 從 `https://download.geofabrik.de/asia/taiwan-latest.osm.pbf` 下載 PBF 檔到 `data/taiwan-latest.osm.pbf`（約 250 MB）
- **AND** 若檔案已存在且 mtime 在 24 小時內，SHALL 跳過下載

#### Scenario: 用 osmconvert 裁切到台北市 bbox
- **WHEN** 完成 PBF 下載
- **THEN** SHALL 使用 `osmconvert` 以 bbox `(121.45, 24.96, 121.67, 25.21)` 裁切，輸出 `data/taipei.pbf`
- **AND** 該 bbox SHALL 涵蓋台北市全部 12 個行政區

#### Scenario: osmconvert 不可用時的錯誤處理
- **WHEN** 系統找不到 `osmconvert` 執行檔
- **THEN** SHALL 輸出明確錯誤訊息（含安裝建議：透過 Docker container 跑），並以非零 exit code 結束

### Requirement: osm2pgsql 灌入 PostGIS
系統 SHALL 用 `osm2pgsql` 把裁切後的 PBF 灌進 PostGIS 的 `planet_osm_*` raw tables。

#### Scenario: 成功灌入 raw tables
- **WHEN** PBF 裁切完成且 PostGIS DB 可連線
- **THEN** SHALL 執行 `osm2pgsql --create --slim --hstore --style scripts/osm2pgsql.style -d traffic_data -U admin ...`
- **AND** 完成後 SHALL 在 DB 中存在 `planet_osm_line`、`planet_osm_point`、`planet_osm_polygon`、`planet_osm_roads` 四張表
- **AND** 每張表 SHALL 有自動建立的 GIST spatial index

#### Scenario: style file 過濾不必要的 tags
- **WHEN** 檢查 `scripts/osm2pgsql.style`
- **THEN** SHALL 只保留 `highway`、`maxspeed`、`oneway`、`name`、`ref`、`lanes` 這些對 routing 有用的 OSM tags
- **AND** 其餘 tag SHALL 不寫入 PostGIS 以節省空間

#### Scenario: 重複執行時冪等
- **WHEN** 第二次執行 `bash scripts/import_taipei_osm.sh`
- **THEN** SHALL 透過 `--create` flag 自動 drop 既有 `planet_osm_*` tables 後重建，無中間狀態殘留

### Requirement: Build Graph SQL transform
系統 SHALL 提供 SQL script 把 `planet_osm_line` raw OSM 資料 transform 成 `traffic_node` / `traffic_edge` schema。

#### Scenario: 篩選 highway tag (汽車可行)
- **WHEN** 執行 `psql -f scripts/build_graph_from_osm.sql`
- **THEN** SHALL 只處理 `planet_osm_line` 中 `highway IN ('motorway','trunk','primary','secondary','tertiary','unclassified','residential','motorway_link','trunk_link','primary_link','secondary_link','tertiary_link','living_street','service')` 的 ways

#### Scenario: 排除非汽車可行的 highway
- **WHEN** 處理 `planet_osm_line` 時遇到 `highway IN ('pedestrian','footway','cycleway','track','steps','path','bridleway','construction','proposed')`
- **THEN** SHALL 排除這些 ways，不寫入 `traffic_edge`
- **AND** 排除原因：本系統路徑規劃針對汽車場景，行人 / 自行車 / 工程中道路不適用

#### Scenario: 從 way vertex 抽出 node
- **WHEN** 處理一條 OSM way
- **THEN** SHALL 用 `ST_DumpPoints(way)` 拆出該 way 的所有 vertex
- **AND** 對所有候選 vertex 用 `ST_SnapToGrid(geom, 0.00005)`（約 5 公尺）做去重後 INSERT 到 `traffic_node`

#### Scenario: 連接相鄰 vertex 成 edge
- **WHEN** 處理一條 OSM way 的相鄰兩個 vertex
- **THEN** SHALL 在這兩個 vertex 對應的 traffic_node 之間建立一條 traffic_edge
- **AND** edge 的 `length_km` SHALL 為 `ST_Length(ST_MakeLine(v1, v2)::geography) / 1000`
- **AND** edge 的 `road_class` SHALL 為該 way 的 `highway` tag 值
- **AND** edge 的 `geom` SHALL 為 `ST_MakeLine(v1, v2)`，型別為 `geometry(LineString, 4326)`

#### Scenario: maxspeed 推估 fallback
- **WHEN** 一條 way 沒有 `maxspeed` tag 或值無法 parse
- **THEN** SHALL 呼叫 `default_maxspeed(highway)` PL/pgSQL function，依 highway class 套預設值（motorway=80, primary=50, residential=30, service=20 等）
- **AND** 寫入 `traffic_edge.max_speed_kmh`

#### Scenario: oneway 處理
- **WHEN** 一條 way 有 `oneway='yes'` tag
- **THEN** edge 的 `oneway` 欄位 SHALL 設為 TRUE
- **AND** A* 引擎 SHALL 在載入 graph 時只建立單向 adjacency

#### Scenario: 重新執行時清空舊資料
- **WHEN** 執行 `build_graph_from_osm.sql`
- **THEN** SHALL 先 `TRUNCATE traffic_edge, traffic_node RESTART IDENTITY CASCADE` 再 INSERT，避免殘留資料

### Requirement: PostgreSQL Schema 變更
DB schema SHALL 新增 PostGIS geometry 欄位、road_class、has_signal 等欄位以支援 OSM 資料與紅綠燈停等估算。

#### Scenario: traffic_edge 新增欄位
- **WHEN** 檢查 `traffic_edge` schema
- **THEN** SHALL 包含欄位 `road_class VARCHAR(32)`、`max_speed_kmh INTEGER NOT NULL`、`oneway BOOLEAN NOT NULL DEFAULT FALSE`、`geom geometry(LineString, 4326)`
- **AND** 索引 `ix_traffic_edge_road_class` 和 GIST `ix_traffic_edge_geom` SHALL 存在

#### Scenario: traffic_node 新增 geom 與 has_signal 欄位
- **WHEN** 檢查 `traffic_node` schema
- **THEN** SHALL 包含欄位 `geom geometry(Point, 4326)`、`has_signal BOOLEAN NOT NULL DEFAULT FALSE`
- **AND** GIST 索引 `ix_traffic_node_geom` SHALL 存在
- **AND** partial index `ix_traffic_node_signal ON traffic_node(has_signal) WHERE has_signal` SHALL 存在（加速 A* 載入時 has_signal=TRUE 的快查）

### Requirement: OSM 號誌節點 snap 到 traffic_node
系統 SHALL 在 graph build 完成後一次性把 OSM `highway=traffic_signals` point snap 到最近 traffic_node 並標 `has_signal=TRUE`。

#### Scenario: 用 PostGIS ST_DWithin 標號誌
- **WHEN** `scripts/build_graph_from_osm.sql` 執行到末尾
- **THEN** SHALL 執行 `UPDATE traffic_node tn SET has_signal = TRUE WHERE EXISTS (SELECT 1 FROM planet_osm_point p WHERE p.highway = 'traffic_signals' AND ST_DWithin(p.way::geography, tn.geom::geography, 30))`
- **AND** snap 半徑為 30 公尺

#### Scenario: 號誌 30m 內無任何 traffic_node
- **WHEN** 一個 OSM `traffic_signals` point 30 公尺內找不到 traffic_node（路網太稀疏 / OSM 標錯位）
- **THEN** SHALL 忽略該號誌、不影響其他 node 的 has_signal 判定
- **AND** 不視為錯誤（log 不需特別 warning）

#### Scenario: 多個 OSM 號誌 point snap 到同一 traffic_node
- **WHEN** 一個 traffic_node 30m 內有多個 `traffic_signals` point（複合路口、相近的兩個號誌頭）
- **THEN** `has_signal` SHALL 仍為 TRUE（不重複標記）
- **AND** A* 後續對該 node 仍只加一次 SIGNAL_PENALTY（per-node 不是 per-signal）

### Requirement: VD Pre-snap road_class
系統 SHALL 在 graph build 完成後一次性計算每個 VD snap 到最近 OSM edge 的 road_class，寫入 `vd_static.snapped_road_class`。

#### Scenario: 用 PostGIS ST_DWithin 找最近 edge
- **WHEN** `vd_static` 表已 seed 完成且 `traffic_edge` 表已 build 完成
- **THEN** 系統 SHALL 對每個 VD，用 `ST_DWithin(vd.geom, edge.geom, 100m)` 找出 100 公尺內所有 edges
- **AND** 取距離最近的 edge 的 `road_class` 寫入 `vd_static.snapped_road_class`

#### Scenario: VD 100m 內無任何 edge
- **WHEN** 一個 VD 100 公尺內找不到任何 traffic_edge
- **THEN** `snapped_road_class` SHALL 設為 NULL
- **AND** WeightProvider rebuild 時 SHALL 跳過此 VD 的 class avg 統計

### Requirement: Docker image 含 PostGIS + TimescaleDB
DB infra SHALL 使用同時支援 PostGIS 和 TimescaleDB 的 Docker image。

#### Scenario: docker-compose 使用 timescaledb-ha image
- **WHEN** 檢查 `infra/docker-compose.yml`
- **THEN** `timescaledb` service 的 `image` 欄位 SHALL 為 `timescale/timescaledb-ha:pg14-all` 或同等支援雙 extension 的 image
- **AND** SHALL 留 PG14 不跨大版號（避免 PG14→PG16 跨版資料 + extension 重建成本）

#### Scenario: init-db 啟用 PostGIS extension
- **WHEN** DB container 首次啟動執行 init scripts
- **THEN** SHALL 執行 `CREATE EXTENSION IF NOT EXISTS postgis;` 和 `CREATE EXTENSION IF NOT EXISTS postgis_topology;`
- **AND** SHALL 在 timescaledb extension 之前或之後執行皆可（兩者獨立）
