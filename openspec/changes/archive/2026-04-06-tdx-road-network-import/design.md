## Context

Route Agent 需要高雄市路網圖資來執行 A* 路徑規劃。目前 TimescaleDB 已透過 `infra/init-db/01-init-timescaledb.sql` 初始化，但僅啟用 TimescaleDB extension，尚無路網相關 table。multiagent-service 使用 SQLAlchemy asyncpg 連線，已有 `db/__init__.py` 提供 async session factory。

原定使用 TDX `Basic/RoadSection` API，但實測後發現該 endpoint 已返回 404（疑似下架）。改用 `Section` API 取得路段基本資訊 + `SectionShape` API 取得完整幾何線。

TDX 免費方案有 API 頻率限制，因此採用「抓一次、存 JSON、啟動時 seed」的離線策略，而非即時呼叫 API。

## Goals / Non-Goals

**Goals:**
- 建立 `traffic_node` / `traffic_edge` DB schema，支援未來 A* 路徑規劃查詢
- 提供獨立 Python script 從 TDX Section + SectionShape API 抓取高雄路段資料
- JSON 快照 commit 進 repo，確保 `docker-compose up` 即可取得完整路網
- 儲存路段完整幾何線（WKT LINESTRING），供前端地圖繪製使用
- multiagent-service 啟動時自動偵測並 seed 資料
- 涵蓋 unit test 與 CI 驗證

**Non-Goals:**
- 即時路況 (SectionLink / VD) 整合（Phase 2）
- CCTV / YOLO 影像辨識（未來計畫，搭配 Rust）
- A* 演算法實作（Phase 2）
- 前端路線繪製（Phase 4）
- 支援高雄以外的區域
- LLM fine-tuning（使用 Gemini prompt engineering 取代）

## Decisions

### 1. DB Schema 設計：靜態表而非 hypertable

**選擇**：`traffic_node` 和 `traffic_edge` 使用一般 PostgreSQL table，不使用 TimescaleDB hypertable。

**原因**：路網圖資是靜態空間資料，沒有時間序列特性。Hypertable 的 chunk 機制對這類資料沒有優勢，反而增加查詢開銷。未來即時路況資料（如車速、壅塞度）才適合用 hypertable。

**替代方案**：
- 使用 hypertable → 不適合，靜態資料無時間維度
- 使用 PostGIS geometry 欄位 → 過度設計，目前只需要 lat/lng float 即可滿足 A* 需求；未來如需空間查詢再加

### 2. API 選擇：Section + SectionShape 取代 RoadSection

**選擇**：使用 `Section` API 取得路段基本資訊，搭配 `SectionShape` API 補充完整幾何線。

**原因**：原定的 `Basic/RoadSection` API 已返回 404（2026-04-06 實測）。Section API 提供 SectionID、路名、路長、起終點座標、RoadClass，SectionShape 提供 WKT LINESTRING 完整幾何。兩者的 SectionID 可對應 join。

**替代方案**：
- 只用 Section API（起終點兩點） → A* 夠用，但前端繪製路線品質差
- 使用 OpenStreetMap → 資料更完整，但不在 TDX 生態系內，增加依賴

### 3. 速限推估：從 RoadClass 推導

**選擇**：Section API 不提供速限欄位，改由 `RoadClass` 推估速限。

**對應表**：
| RoadClass | 道路類型 | 推估速限 |
|-----------|----------|----------|
| 0 | 國道 | 110 km/h |
| 1 | 省道 | 70 km/h |
| 2 | 快速道路 | 80 km/h |
| 3 | 市區快速 | 70 km/h |
| 4 | 縣道 | 60 km/h |
| 5 | 鄉道 | 50 km/h |
| 6 | 市區道路 | 50 km/h |

**原因**：原 RoadSection API 直接提供速限，但該 API 已不可用。RoadClass 推估是合理的 fallback，未來可從 SectionLink 即時車速進一步校正。

### 4. Node 推導策略：座標 snap tolerance ~20m

**選擇**：從路段的起終點座標推導 node，相距 20m 內的座標視為同一 node（使用 Haversine 距離）。

**原因**：TDX Section API 只提供路段資料，沒有獨立的 node endpoint。不同路段的端點座標可能有微小偏差但代表同一路口，需要去重合併。20m 容差在都市路網中合理——小於一般路口寬度，大於 GPS 漂移。

**實測結果**：308 個候選座標（154 路段 × 2）在 20m tolerance 下去重為 154 個 node。

**替代方案**：
- 精確座標比對（0 tolerance）→ 會產生大量假 node，路口無法正確連接
- 較大 tolerance（50m+）→ 可能誤合併不同路口

### 5. Edge weight 計算：distance / speed_limit

**選擇**：`base_weight = length_km / speed_limit_kmh`，單位為小時。

**原因**：以通行時間為 weight 最直覺，也最容易在未來疊加即時路況的 multiplier（例如壅塞時 weight × 1.5）。

**替代方案**：
- 純距離 weight → 忽略速限差異，高速公路與巷弄無區別
- 自訂 cost function → 過早優化，先用簡單公式即可

### 6. Seed 策略：啟動時偵測 + JSON 檔案載入

**選擇**：multiagent-service 啟動時查詢 `traffic_node` 是否為空，若空則從 `data/kaohsiung_road_sections.json` 讀取並寫入 DB。

**原因**：簡單且可靠。JSON commit 進 repo 確保任何環境都能 seed，不依賴外部 API。啟動時偵測避免重複寫入。

### 7. 幾何線儲存：JSON 內嵌 WKT，DB 暫不存

**選擇**：SectionShape 的 WKT LINESTRING 存在 JSON 快照的 `geometry_wkt` 欄位中。目前 DB 的 `traffic_edge` 表不新增 geometry 欄位，前端需要時直接從 JSON 或新 API 讀取。

**原因**：DB schema 變動影響已完成的 migration 和 seed 邏輯。幾何線主要供前端繪製，A* 路徑規劃只需要 node 座標和 edge weight，不需要完整 polyline。Phase 4 做前端時再決定是否入庫。

## Risks / Trade-offs

**~~TDX 帳號審核延遲~~** → 已通過（2026-04-06）。

**Basic/RoadSection API 已下架** → 已改用 Section + SectionShape 替代方案。功能完全覆蓋，但速限需從 RoadClass 推估。

**3km × 3km → 6km × 7km 範圍調整** → 實測後擴大 bounding box 以取得足夠路段。實際取得 154 筆，合理範圍。

**Node snap tolerance 不精確** → 20m 下 154 nodes = 154 edges，表示路網交叉口合併不足。Phase 2 實作 A* 時需確認連通性，可能需微調 tolerance。

**速限推估不精確** → RoadClass 推估是粗略值，同類道路速限可能不同。Phase 2 可用 SectionLink 即時車速校正。

**JSON 快照與 TDX 資料不同步** → 接受此 trade-off。路網結構短期內不會大幅變動，需要更新時重跑 script 即可。

## Open Questions

- ~~TDX RoadSection API 回傳的座標格式是 WGS84 還是 TWD97？~~ → Section API 使用 WGS84，無需轉換。
- Phase 2 實作 A* 時需確認 20m tolerance 下的路網連通性是否足夠。
- Phase 4 前端繪製時，WKT geometry 是否需要入庫，還是前端直接讀 JSON？
