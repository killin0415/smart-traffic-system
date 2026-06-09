## MODIFIED Requirements

### Requirement: 測速照相機資料 Seed
系統 SHALL 在啟動時從 data.taipei「臺北市固定測速照相地點表」CSV 匯入台北市測速照相機資料。

#### Scenario: 首次 seed
- **WHEN** multiagent-service 啟動且 `speed_camera` 表為空
- **THEN** SHALL 從預下載的 `data/taipei_speed_cameras.csv` 讀取資料
- **AND** CSV 欄位固定為 `編號`, `功能`, `設置路段`, `設置地點`, `緯度`, `經度`, `轄區`, `拍攝方向`, `速限`
- **AND** 對每筆相機，SHALL 用 PostGIS `ST_Distance(traffic_edge.geom, ST_MakePoint(lng, lat)::geography) ORDER BY ASC LIMIT 1` 找最近 edge 作為 `nearest_edge_id`
- **AND** 寫入 `speed_camera` 表，含 `latitude=緯度`, `longitude=經度`, `speed_limit=速限`, `direction=拍攝方向`, `address=設置地點`

#### Scenario: 已有資料時跳過
- **WHEN** multiagent-service 啟動且 `speed_camera` 表已有資料
- **THEN** SHALL 跳過 seed 並記錄 info log

#### Scenario: CSV 檔案不存在
- **WHEN** `data/taipei_speed_cameras.csv` 不存在
- **THEN** SHALL 記錄 warning log 並跳過 seed，服務繼續啟動

#### Scenario: snap 用 PostGIS 而非 Python 端點距離
- **WHEN** snap 一筆相機到最近 edge
- **THEN** SHALL 用 PostGIS `ST_Distance` 對 `traffic_edge.geom`（完整 LineString）計算「點到線段」距離
- **AND** SHALL NOT 用 Python 端跑 O(n) 端點 Haversine 距離
