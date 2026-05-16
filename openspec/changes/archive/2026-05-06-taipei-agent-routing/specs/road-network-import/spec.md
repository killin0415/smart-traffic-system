## MODIFIED Requirements

### Requirement: TDX Section API 資料抓取
script SHALL 從 TDX Section API (`/api/basic/v2/Road/Traffic/Section/City/Taipei`) 抓取 **Taipei** 路段資料。

> **注意**：原定使用 `Basic/RoadSection` API 已返回 404（2026-04-06），改用 Section API。本次（2026-05-05）從 Kaohsiung 切換到 Taipei，因為 `Live/City/Kaohsiung` 不支援、VD 路徑資料品質不可用。

#### Scenario: 成功抓取路段資料
- **WHEN** 使用有效 access token 呼叫 Section API
- **THEN** script SHALL 取得 Taipei 所有路段資料，包含 SectionID、路名、路長、起終點座標、RoadClass

#### Scenario: Bounding box 過濾
- **WHEN** 取得全部路段資料後
- **THEN** script SHALL 在 Python 端過濾，僅保留至少一端座標落在 bounding box 內的路段
- **AND** bounding box SHALL 以台北車站 (25.0478, 121.5170) 為中心、半徑約 2.2km，西南角 (25.0278, 121.4970)、東北角 (25.0678, 121.5370)

#### Scenario: API 回傳分頁資料
- **WHEN** Section API 回傳的資料超過單次上限
- **THEN** script SHALL 處理分頁（$top + $skip），確保取得完整資料集

#### Scenario: API Rate Limit
- **WHEN** TDX API 回傳 HTTP 429 Too Many Requests
- **THEN** script SHALL 等待後重試，而非直接失敗

#### Scenario: API 呼叫失敗
- **WHEN** TDX API 回傳其他 HTTP 錯誤（如 401、500）
- **THEN** script SHALL 輸出錯誤訊息，包含 HTTP status code 和回應內容，並以非零 exit code 結束

### Requirement: TDX SectionShape API 幾何線抓取
script SHALL 從 TDX SectionShape API (`/api/basic/v2/Road/Traffic/SectionShape/City/Taipei`) 抓取路段完整幾何線。

#### Scenario: 成功抓取幾何資料
- **WHEN** 使用有效 access token 呼叫 SectionShape API
- **THEN** script SHALL 取得每筆路段的 WKT LINESTRING 幾何資料

#### Scenario: 幾何與路段 ID 對應
- **WHEN** 將 SectionShape 資料與 Section 資料合併
- **THEN** SHALL 透過 SectionID 對應，將 WKT Geometry 附加到對應的路段記錄中

### Requirement: 路網 JSON 快照產出
script SHALL 將抓取結果儲存為結構化 JSON 檔案。

#### Scenario: 成功產出 JSON 檔案
- **WHEN** TDX 資料抓取完成
- **THEN** script SHALL 將資料寫入 `data/taipei_road_sections.json`，每筆路段包含 `RoadSectionID`、`RoadName`、`geometry`（起終點座標陣列）、`RoadLength`（公尺）、`SpeedLimit`（推估值）
- **AND** 若有對應的 SectionShape，SHALL 包含 `geometry_wkt` 欄位（WKT LINESTRING）

#### Scenario: JSON 檔案格式
- **WHEN** 檢查產出的 JSON 檔案
- **THEN** SHALL 為合法 JSON，使用 UTF-8 編碼，包含 `metadata`（抓取時間、bounding box、city=Taipei、資料來源）和 `road_sections` 陣列

### Requirement: 啟動時自動 seed 路網資料
multiagent-service 啟動時 SHALL 自動偵測 DB 是否需要 seed，並從 JSON 快照匯入路網資料。

#### Scenario: DB 為空時自動 seed
- **WHEN** multiagent-service 啟動且 `traffic_node` 表為空
- **THEN** SHALL 從 `data/taipei_road_sections.json` 讀取資料，執行 node 推導、去重、edge 建立，並寫入 DB

#### Scenario: DB 已有資料時跳過 seed
- **WHEN** multiagent-service 啟動且 `traffic_node` 表已有資料
- **THEN** SHALL 跳過 seed 流程，不做任何寫入

#### Scenario: JSON 檔案不存在
- **WHEN** multiagent-service 啟動但 `data/taipei_road_sections.json` 不存在
- **THEN** SHALL 記錄警告 log 並繼續啟動，不中斷服務
