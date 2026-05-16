## ADDED Requirements

### Requirement: data.taipei VD 動態 XML 定時拉取
系統 SHALL 從 data.taipei VD 動態 XML endpoint 定時拉取台北市即時車速資料。

#### Scenario: 定時輪詢
- **WHEN** multiagent-service 啟動後
- **THEN** SHALL 每 `VD_REFRESH_SECONDS` 秒（預設 300）向 `https://tcgbusfs.blob.core.windows.net/blobtisv/GetVDDATA.xml` 發送 HTTP GET
- **AND** 不需要 API key 或 OAuth token
- **AND** SHALL 用 `httpx.AsyncClient` 並設定 timeout 30 秒

#### Scenario: HTTP 請求失敗不中斷 loop
- **WHEN** XML 抓取因 network error / timeout / non-2xx 失敗
- **THEN** SHALL 記錄 `logger.exception(...)` 但 background task SHALL 繼續執行下一個 cycle
- **AND** 既有的 `vd_reading` 資料和 in-memory weight SHALL 保持不變

### Requirement: VD XML 解析
系統 SHALL 解析 VD 動態 XML 的巢狀結構為扁平的 `VDReading` records。

#### Scenario: 解析 ExchangeTime + VDDevice + LaneData
- **WHEN** 收到 XML response
- **THEN** SHALL 解析 root 的 `<ExchangeTime>` 為 timestamp
- **AND** 對每個 `<VDDevice>` 取出 `DeviceID` 作為 `vdid`
- **AND** 對每個 device 下的每個 `<LaneData>`，產生一筆 `VDReading(ts, vdid, lane_no, avg_speed, volume, occupancy)`

#### Scenario: 缺欄位的 LaneData 用預設值
- **WHEN** `<LaneData>` 中 `AvgSpeed`、`Volume`、`AvgOccupancy` 任一為空字串
- **THEN** SHALL 解析為 `0`（int 或 float），並仍寫入 `vd_reading` 表
- **AND** WeightProvider 後續 SHALL 在 query 時用 `WHERE avg_speed > 0` 過濾掉這些無效讀數

### Requirement: VD 讀數寫入 TimescaleDB hypertable
系統 SHALL 將解析後的 VD readings 寫入 `vd_reading` hypertable，並對重複資料做 upsert 處理。

#### Scenario: 成功寫入 hypertable
- **WHEN** 解析得到 N 筆 `VDReading`
- **THEN** SHALL 用 `INSERT INTO vd_reading (...) VALUES (...) ON CONFLICT DO NOTHING` 一次寫入
- **AND** PRIMARY KEY 為 `(ts, vdid, lane_no)`

#### Scenario: 同一 ExchangeTime 重複抓取
- **WHEN** 同一 5 min cycle 內因 retry 或測試重複呼叫
- **THEN** `ON CONFLICT DO NOTHING` SHALL 確保 PK 衝突的 row 被忽略，不丟 exception

#### Scenario: vd_reading 是 hypertable
- **WHEN** 檢查 `vd_reading` 表
- **THEN** SHALL 為 TimescaleDB hypertable，partition 欄位為 `ts`
- **AND** retention policy SHALL 設為 30 天

### Requirement: VD refresh 觸發 WeightProvider rebuild
每次成功 fetch + insert 後，系統 SHALL 觸發 WeightProvider 重新計算並套用到 in-memory graph。

#### Scenario: 一個 refresh cycle 的完整流程
- **WHEN** `refresh_vd_cycle()` 被呼叫
- **THEN** SHALL 依序執行：(1) `fetch_vd_dynamic()` 抓 XML、(2) INSERT vd_reading with `ON CONFLICT DO NOTHING`、(3) `weight_provider.rebuild(session_factory)`、(4) `weight_provider.apply_to_graph(graph)`
- **AND** 第 3 步失敗時 SHALL log 但不執行第 4 步（避免半套用）

#### Scenario: rebuild 失敗時保留舊 weight
- **WHEN** `weight_provider.rebuild()` raise exception
- **THEN** in-memory graph 的 `dynamic_weight` SHALL 保持上一輪的值
- **AND** A* 仍可正常使用該 weight 算路徑

### Requirement: 環境變數 VD_REFRESH_SECONDS
系統 SHALL 透過環境變數 `VD_REFRESH_SECONDS` 控制 refresh 間隔。

#### Scenario: 預設值
- **WHEN** 環境變數 `VD_REFRESH_SECONDS` 未設定
- **THEN** SHALL 使用預設值 300 (5 分鐘)

#### Scenario: 自訂值
- **WHEN** 環境變數 `VD_REFRESH_SECONDS=60` 設定
- **THEN** background task SHALL 每 60 秒執行一次 refresh cycle

### Requirement: VD 靜態資料 seed (CLI script，非 lifespan)
系統 SHALL 提供獨立 CLI script `scripts/seed_vd_static.py` 把 data.taipei VD 靜態 XML upsert 進 `vd_static`。**不在 lifespan 自動執行**，避免啟動依賴外部 endpoint 可用性，且確保 `post_build_snap_vd.sql` 執行時 `vd_static` 已有資料。

#### Scenario: CLI script 一次性執行
- **WHEN** 在 offline migration 流程中執行 `uv run python scripts/seed_vd_static.py`
- **THEN** SHALL 從 `https://tcgbusfs.blob.core.windows.net/blobtisv/VD.xml` 抓 XML
- **AND** 對每筆 `<VD>` 用 `INSERT ... ON CONFLICT (vdid) DO UPDATE` upsert 進 `vd_static (vdid, link_id, road_name, road_class, bidirectional, bearing, lat, lng, geom)` row
- **AND** `geom` SHALL 為 `ST_MakePoint(lng, lat)`
- **AND** 完成後 SHALL log 寫入 row 數

#### Scenario: 重複執行時 idempotent
- **WHEN** `scripts/seed_vd_static.py` 連跑兩次
- **THEN** 第二次 SHALL 用 ON CONFLICT 路徑更新欄位，不丟錯
- **AND** row 數不增（同一批 vdid）

#### Scenario: VD 靜態 XML 抓取失敗
- **WHEN** CLI script 抓 VD.xml HTTP 失敗
- **THEN** SHALL 印出明確錯誤訊息，以非零 exit code 結束
- **AND** 不寫任何資料到 DB

#### Scenario: lifespan 啟動時 vd_static 為空的處理
- **WHEN** multiagent-service 啟動且 `vd_static` 表為空
- **THEN** SHALL 記錄 warning log 並提示「請先執行 `uv run python scripts/seed_vd_static.py`」
- **AND** 服務 SHALL 繼續啟動（不阻擋）
- **AND** WeightProvider Tier 1/Tier 2 SHALL 因為沒有 VD metadata 而全部 fallback 到 Tier 3
