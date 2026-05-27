## Why

Route Agent 需要高雄市的路網圖資才能執行 A* 路徑規劃。目前 TimescaleDB 已就緒但沒有任何路網資料，也沒有取得外部圖資的機制。TDX (運輸資料流通服務) 提供高雄市路段相關 API，包含路段座標、長度、幾何線等資訊，正好滿足建圖需求。

由於 TDX 免費方案有 API 頻率限制，策略為**抓取一次、存成 JSON 快照 commit 進 repo**，服務啟動時自動從 JSON 匯入 DB。這樣任何人 `docker-compose up` 就能擁有完整路網，不依賴 TDX API 可用性。

## TDX API 可用性調查結果（2026-04-06 實測）

原定使用的 `Basic/RoadSection` API 已返回 404（疑似下架），改用以下替代 API 組合：

### 可用的 API（高雄市，已驗證 HTTP 200）

| Endpoint | 路徑 | 用途 |
|----------|------|------|
| **Section** | `/api/basic/v2/Road/Traffic/Section/City/Kaohsiung` | 路段基本資訊：SectionID、路名、路長、起終點座標、RoadClass |
| **SectionShape** | `/api/basic/v2/Road/Traffic/SectionShape/City/Kaohsiung` | 路段完整幾何線（WKT LINESTRING），前端繪製路線用 |
| **SectionLink** | `/api/basic/v2/Road/Traffic/SectionLink/City/Kaohsiung` | 即時各 link 車速/佔有率（Phase 2 動態 weight 更新用） |
| **VD** | `/api/basic/v2/Road/Traffic/VD/City/Kaohsiung` | 車輛偵測器即時資料：車流量、車速（Phase 2 用） |
| **CMS** | `/api/basic/v2/Road/Traffic/CMS/City/Kaohsiung` | 電子看板訊息（選用，可供 Gemini 參考） |

### 不可用的 API

| Endpoint | 路徑 | 狀態 |
|----------|------|------|
| Basic RoadSection | `/api/basic/v2/Road/RoadSection/City/Kaohsiung` | 404（已下架） |
| Live News | `/api/basic/v2/Road/Traffic/News/City/Kaohsiung` | 404（高雄無此資料） |

### 關鍵欄位對應

**Section API 回傳結構**:
```json
{
  "SectionID": "L_6190010300020E",
  "SectionName": "一心一路(民生路(南)~中華路(南))",
  "RoadName": "一心一路",
  "RoadClass": 6,
  "SectionLength": 0.4545,
  "SectionStart": { "PositionLat": 22.60858, "PositionLon": 120.316734 },
  "SectionEnd": { "PositionLat": 22.606825, "PositionLon": 120.320724 }
}
```

**SectionShape API 回傳結構**:
```json
{
  "SectionID": "L_1008800101020E",
  "Geometry": "LINESTRING(120.4427 22.59301, 120.44243 22.59304, ...)"
}
```

**速限處理**：Section API 不直接提供速限欄位，改由 `RoadClass` 推估：

| RoadClass | 道路類型 | 推估速限 |
|-----------|----------|----------|
| 0 | 國道 | 110 km/h |
| 1 | 省道 | 70 km/h |
| 2 | 快速道路 | 80 km/h |
| 3 | 市區快速 | 70 km/h |
| 4 | 縣道 | 60 km/h |
| 5 | 鄉道 | 50 km/h |
| 6 | 市區道路 | 50 km/h |

## What Changes

- 新增 Python script 從 TDX Section + SectionShape API 抓取高雄路段資料
- 將抓取結果存為 `data/kaohsiung_road_sections.json`（commit 進 repo，含完整幾何線）
- 在 `infra/init-db/` 新增 SQL 建立 `traffic_node` 和 `traffic_edge` 靜態表
- multiagent-service 啟動時偵測 DB 是否為空，自動從 JSON seed 資料
- 為 seed 邏輯和資料解析撰寫 unit test
- CI/CD pipeline 加入路網匯入相關的測試

## Scope

### Bounding Box（高雄車站周邊 ~6km × 7km）
- 西南角：(22.600, 120.270)
- 東北角：(22.660, 120.340)
- 實際抓取路段數：154 筆（2026-04-06 實測）

### 包含
- TDX OAuth2 認證 + Section API 抓取 + SectionShape 幾何補充
- Node 推導（路段起終點座標去重，snap tolerance ~20m）
- DB schema（traffic_node、traffic_edge）
- 路段幾何線儲存（WKT LINESTRING，供前端地圖繪製）
- 啟動時自動 seed
- Unit test（資料解析、node 去重、base_weight 計算）
- CI workflow 跑測試

### 不包含
- CCTV 資料抓取（YOLO 放未來計畫，搭配 Rust 重寫）
- 即時路況 (SectionLink / VD) 接入（Phase 2）
- 前端路線繪製（Phase 4）
- Route Agent 的 A* 演算法本身（Phase 2）
- LLM fine-tuning（使用 Gemini prompt engineering 取代）

## Capabilities

### New Capabilities
- `road-network-import`：定義 TDX 路網資料抓取、解析、儲存、自動 seed 的需求規範

### Modified Capabilities
- `database-integration`：新增 traffic_node / traffic_edge 表定義

## Impact

- 新增 `scripts/import_tdx_road_network.py`：TDX API 抓取 + 產出 JSON
- 新增 `data/kaohsiung_road_sections.json`：路網快照（含幾何線）
- 修改 `infra/init-db/`：新增建表 SQL
- 修改 `backend/multiagent-service/src/db/`：seed 邏輯
- 新增 `backend/multiagent-service/tests/test_road_network.py`：unit test
- 修改 `.github/workflows/ci-multiagent-service.yml`：加入路網測試

## Blocked

- ~~TDX 帳號審核中~~ → 已通過（2026-04-06）
- ~~Basic RoadSection API~~ → 已 404，改用 Section + SectionShape API
