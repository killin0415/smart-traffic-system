## Why

TDX `basic/v2/Road/Traffic/Live/City/{City}` 不支援 Kaohsiung（回 HTTP 400 "is not accepted"），astar-engine-and-tdx-live 已把 refresher 降級為 no-op，代表 A* 路由目前完全使用 base weight、沒有即時路況。TDX 對 Kaohsiung 實際可用的即時資料是 `Live/VD/City/Kaohsiung`，回傳逐 Vehicle Detector（VD）的 lane 級即時速度/流量，需要靠空間比對把 VD 對應到既有 `TrafficEdge` 才能驅動動態權重。

## What Changes

- 新增 VD（Vehicle Detector）靜態資料抓取：呼叫 TDX `Road/Traffic/VD/City/Kaohsiung` 取得每個 VD 的座標、`RoadSection`、`LinkID`
- 實作 VD → edge 空間 snap（複用 speed_camera 的 `snap_to_edge` 距離判定），將對應關係寫入新的 `vd_sensor` 表
- 新增 VD Live 資料抓取：呼叫 `Road/Traffic/Live/VD/City/Kaohsiung`、過濾 `Speed=-99 / ErrorType` 錯誤值、每個 edge 取該 edge 上所有健康 VD 的平均速度
- 將 VD-based live 流程接回 `refresh_traffic_data` 的既有三個出口（Redis cache、`traffic_history` hypertable、`graph.update_weight`）
- 移除 `traffic.py` 現有的「Kaohsiung not accepted」降級分支
- 新增 DB migration：`vd_sensor` 表與對應 index

## Capabilities

### New Capabilities
- `vd-live-traffic`: Kaohsiung 的 VD 靜態 metadata 抓取、VD→edge snap、VD live 資料抓取與彙整、把每個 edge 的平均速度餵回 Redis/TimescaleDB/RoadGraph

### Modified Capabilities
<!-- 無：tdx-live-traffic spec 尚未 archive，所以這裡只新增能力不改既有 spec -->

## Impact

- **程式碼**: `backend/multiagent-service/src/agents/traffic.py`（fetch 邏輯替換）、新增 `src/db/vd_sensor.py`（模仿 `speed_camera.py`）、`src/db/models.py`（新增 `VDSensor` model）
- **DB**: 新增 `vd_sensor` 表（id, vdid, latitude, longitude, nearest_edge_id FK, link_id, road_section_id）；`infra/init-db/02-road-network-tables.sql` 追加建表 SQL
- **Seeding**: `main.py` lifespan 增加 `seed_vd_sensors()` 呼叫，service 啟動時若表為空就拉 VD 靜態資料並 snap
- **外部依賴**: TDX 帳號需可打 `basic/v2/Road/Traffic/VD/City/Kaohsiung` 與 `Live/VD/City/Kaohsiung`（目前免費 tier 已確認可用）
- **測試**: 新增 `tests/test_vd.py`，需要 mock VD live payload 形狀（VDLives > LinkFlows > Lanes > Speed）
- **向後相容**: 完全取代現有 TDX Live Section 流程；Redis key 與 traffic_history schema 不變
