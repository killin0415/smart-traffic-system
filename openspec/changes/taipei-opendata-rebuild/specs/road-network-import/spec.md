## REMOVED Requirements

### Requirement: TDX OAuth2 認證
**Reason**: 整個 TDX-based 路網匯入流程被 `osm-road-network` capability 取代。OSM PBF 是公開資料，不需要 OAuth。

**Migration**: 改執行 `bash scripts/import_taipei_osm.sh`，使用 OSM PBF 而非 TDX API。`TDX_CLIENT_ID` / `TDX_CLIENT_SECRET` 環境變數不再為路網匯入所需（仍可保留供其他用途，但本 change 全面停用 TDX）。

### Requirement: TDX Section API 資料抓取
**Reason**: TDX Section API 對台北覆蓋差（4×4 km bbox 只有 81 條路段，巷弄全缺），且 SpeedLimit 欄位缺失需用 RoadClass 推估。被 OSM 取代後路網規模從 81 → 80k+ edges。

**Migration**: 不再呼叫 TDX `Section/City/Taipei` endpoint。改執行 `bash scripts/import_taipei_osm.sh` 從 geofabrik 下載 PBF。

### Requirement: TDX SectionShape API 幾何線抓取
**Reason**: OSM way 自帶完整 geometry（不只起終點），不再需要從 SectionShape API 補幾何。

**Migration**: 不再呼叫 TDX `SectionShape/City/Taipei`。OSM way 的 geometry 透過 osm2pgsql 灌進 `planet_osm_line.way` 欄位。

### Requirement: 速限推估
**Reason**: OSM way 含原生 `maxspeed` tag（雖非全覆蓋）。沒 `maxspeed` 的 way 由 `default_maxspeed(highway)` PL/pgSQL function 套法規預設值（`osm-road-network` capability 規範）。RoadClass 0~6 對應表不再使用。

**Migration**: 移除 Python `_infer_speed_limit(road_class)` function。改用 SQL `default_maxspeed(highway)` function。

### Requirement: 路網 JSON 快照產出
**Reason**: 改用 PostGIS 儲存 raw OSM (`planet_osm_*`) 與 graph (`traffic_node` / `traffic_edge`)。資料直接存 DB，不再有 JSON snapshot in repo。

**Migration**: 不再產出 `data/taipei_road_sections.json`。檔案保留供歷史對照但不被讀取。Repo 不再 commit 大型 JSON。

### Requirement: Node 推導與座標去重
**Reason**: 由 PostGIS `ST_DumpPoints` + `ST_SnapToGrid(0.00005)` 在 SQL 中完成（`build_graph_from_osm.sql`）。比 Python Haversine O(n²) 快數個量級且更精確。

**Migration**: 移除 Python `deduplicate_nodes(candidates, tolerance_m=20)` 函式。`Coord` / `ParsedNode` dataclass 隨 `road_network.py` 一併移除。

### Requirement: Edge 建立與 base_weight 計算
**Reason**: edge `base_weight` 欄位移除，由 `WeightProvider` 動態計算。Edge 建立邏輯改在 SQL 中完成。

**Migration**: 移除 Python `compute_base_weight(length_km, speed_limit_kmh)` 函式。`traffic_edge.base_weight` 欄位 DROP COLUMN。

### Requirement: 啟動時自動 seed 路網資料
**Reason**: lifespan 不再從 JSON 讀取。改成「DB 透過 `bash scripts/import_taipei_osm.sh` + `psql -f scripts/build_graph_from_osm.sql` 一次性 build 好」。lifespan 仍會檢查 `traffic_node` 是否為空，但只記 warning，不再 seed。

**Migration**:
1. 移除 `seed_road_network()` 函式
2. lifespan 改為：發現 `traffic_node` 為空時 log warning 並提示執行 import script
3. 開發環境部署文件更新：明確列出「先跑 import script、再啟動 service」步驟

### Requirement: 路網匯入 unit test
**Reason**: 原本驗 JSON 解析、Haversine 去重、Python base_weight 計算的測試已不適用。改測 SQL transform、PostGIS snap、osm2pgsql 灌入正確性（在 `osm-road-network` capability 下重寫）。

**Migration**: 移除 `tests/test_road_network.py`（或保留但全 skip）。新測試在 `osm-road-network` capability 中規範。

### Requirement: 路網解析保留 TDX Section ID
**Reason**: OSM way 沒有 TDX SectionID 概念。`traffic_edge.tdx_section_id` 欄位移除。VD 對應 edge 改用 PostGIS 空間 snap（`vd-live-traffic` capability）。

**Migration**:
1. `traffic_edge.tdx_section_id` 欄位 DROP COLUMN
2. `ParsedEdge.tdx_section_id` field 移除（連 `ParsedEdge` 類別本身一併移除）

### Requirement: Seed 時寫入 tdx_section_id
**Reason**: `tdx_section_id` 欄位移除，連帶此 seed 寫入需求消失。

**Migration**: 對應 ORM 寫入邏輯移除。
