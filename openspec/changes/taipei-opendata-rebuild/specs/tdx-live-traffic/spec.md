## REMOVED Requirements

### Requirement: TDX Live API 定時拉取
**Reason**: 整個 capability 被新的 `vd-live-traffic` 取代。data.taipei VD 動態 XML 提供同等功能（5 min 即時平均車速、含 lane-level 細節、不需 OAuth）且資料源是交工處原始資料，比 TDX 二手資料延遲低。

**Migration**:
1. 拿掉 `src/agents/traffic.py`，改用 `src/agents/vd_traffic.py`
2. 拿掉 `TDX_CLIENT_ID` / `TDX_CLIENT_SECRET` 環境變數（VD endpoint 不需 OAuth）
3. 環境變數 `TDX_LIVE_REFRESH_SECONDS` 改名 `VD_REFRESH_SECONDS`（預設仍 300）
4. lifespan 中 `run_periodic_refresh(graph, async_session)` 改 `run_periodic_vd_refresh(graph, weight_provider, async_session)`

### Requirement: 即時資料寫入 Redis
**Reason**: data.taipei VD 改用 PostgreSQL 直接查詢 `vd_reading` hypertable 即可，不再經過 Redis cache。Redis cache 在原設計是為了「跨 agent 即時查路況」，但實作後發現只有 routing agent 會用，且 hypertable 查詢本身已夠快（< 10ms）。

**Migration**:
1. 移除 `traffic:section:*` Redis key 寫入邏輯
2. 任何讀 `traffic:section:*` 的 agent 改 query `vd_reading` 表
3. Redis 仍保留給其他用途（chat 對話 cache、token cache 等）

### Requirement: 即時資料寫入 TimescaleDB
**Reason**: 由 `vd-live-traffic` capability 的 `vd_reading` hypertable 取代。schema 不同（VD 有 lane_no、occupancy 維度），新表更精細。

**Migration**:
1. 砍 `traffic_history` hypertable
2. 改用 `vd_reading (ts, vdid, lane_no PK; avg_speed, volume, occupancy)` hypertable
3. 任何讀 `traffic_history` 的分析 query 需重寫成 `vd_reading`

### Requirement: Congestion factor 計算與 edge weight 更新
**Reason**: 由 `weight-provider` capability 的三層 fallback 邏輯取代。congestion_factor (`speed_limit / current_speed`) 模型基於「速限可信」假設；本次重建因為知道速限推估會讓 ETA 樂觀 3-4 倍，改成直接用實測平均速率計算 weight，不再經過 congestion_factor 中介。

**Migration**:
1. 移除 `_congestion_factor()` 函式
2. A* edge weight 改由 WeightProvider 直接算 `length_km / get_speed(edge)`，不再有 base_weight × factor 概念
3. `MAX_CONGESTION_FACTOR` 常數移除

### Requirement: TDX Section ID 與 Edge 的 Mapping
**Reason**: data.taipei VD 沒有 TDX SectionID 概念。改用空間 snap：`vd_static.snapped_road_class` 在 graph build 完後一次性算好（PostGIS `ST_DWithin` 100m）。

**Migration**:
1. `traffic_edge.tdx_section_id` 欄位刪除
2. `RoadGraph.section_to_edge` dict 不再需要
3. VD 對應 edge 的方式由「`section_to_edge[vdid]`」改為「`vd_static.snapped_road_class` + 空間鄰近」
