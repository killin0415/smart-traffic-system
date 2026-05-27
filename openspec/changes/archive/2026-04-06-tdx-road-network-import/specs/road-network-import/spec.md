## ADDED Requirements

### Requirement: TDX OAuth2 認證
TDX 抓取 script SHALL 使用 OAuth2 Client Credentials 向 TDX 認證端點取得 access token。

#### Scenario: 成功取得 token
- **WHEN** 提供有效的 `TDX_CLIENT_ID`（或 `TDX-CLIENT-ID`）和 `TDX_CLIENT_SECRET`（或 `TDX-CLIENT-SECRET`）環境變數，或專案根目錄存在 `.env` 檔案
- **THEN** script SHALL 向 TDX OAuth2 端點發送 POST 請求，取得 access token

#### Scenario: 認證失敗
- **WHEN** `TDX_CLIENT_ID` 或 `TDX_CLIENT_SECRET` 無效或未設定
- **THEN** script SHALL 輸出明確錯誤訊息並以非零 exit code 結束

### Requirement: TDX Section API 資料抓取
script SHALL 從 TDX Section API (`/api/basic/v2/Road/Traffic/Section/City/Kaohsiung`) 抓取高雄路段資料。

> **注意**：原定使用 `Basic/RoadSection` API 已返回 404（2026-04-06），改用 Section API。

#### Scenario: 成功抓取路段資料
- **WHEN** 使用有效 access token 呼叫 Section API
- **THEN** script SHALL 取得高雄市所有路段資料，包含 SectionID、路名、路長、起終點座標、RoadClass

#### Scenario: Bounding box 過濾
- **WHEN** 取得全部路段資料後
- **THEN** script SHALL 在 Python 端過濾，僅保留至少一端座標落在 bounding box 內的路段
- **AND** bounding box 為西南角 (22.600, 120.270)、東北角 (22.660, 120.340)

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
script SHALL 從 TDX SectionShape API (`/api/basic/v2/Road/Traffic/SectionShape/City/Kaohsiung`) 抓取路段完整幾何線。

#### Scenario: 成功抓取幾何資料
- **WHEN** 使用有效 access token 呼叫 SectionShape API
- **THEN** script SHALL 取得每筆路段的 WKT LINESTRING 幾何資料

#### Scenario: 幾何與路段 ID 對應
- **WHEN** 將 SectionShape 資料與 Section 資料合併
- **THEN** SHALL 透過 SectionID 對應，將 WKT Geometry 附加到對應的路段記錄中

### Requirement: 速限推估
Section API 不提供速限欄位，script SHALL 從 RoadClass 推估速限。

#### Scenario: RoadClass 速限對應
- **WHEN** 一筆路段的 RoadClass 為已知值（0~6）
- **THEN** SHALL 依下表推估速限：0→110, 1→70, 2→80, 3→70, 4→60, 5→50, 6→50 km/h

#### Scenario: 未知 RoadClass
- **WHEN** 一筆路段的 RoadClass 不在 0~6 範圍
- **THEN** SHALL 使用預設速限 40 km/h

### Requirement: 路網 JSON 快照產出
script SHALL 將抓取結果儲存為結構化 JSON 檔案。

#### Scenario: 成功產出 JSON 檔案
- **WHEN** TDX 資料抓取完成
- **THEN** script SHALL 將資料寫入 `data/kaohsiung_road_sections.json`，每筆路段包含 `RoadSectionID`、`RoadName`、`geometry`（起終點座標陣列）、`RoadLength`（公尺）、`SpeedLimit`（推估值）
- **AND** 若有對應的 SectionShape，SHALL 包含 `geometry_wkt` 欄位（WKT LINESTRING）

#### Scenario: JSON 檔案格式
- **WHEN** 檢查產出的 JSON 檔案
- **THEN** SHALL 為合法 JSON，使用 UTF-8 編碼，包含 `metadata`（抓取時間、bounding box、資料來源）和 `road_sections` 陣列

### Requirement: Node 推導與座標去重
seed 邏輯 SHALL 從路段座標推導 traffic node，並對相近座標進行去重。

#### Scenario: 從路段端點推導 node
- **WHEN** 解析一筆路段的 geometry
- **THEN** SHALL 取出路段的起點和終點座標作為候選 node

#### Scenario: Snap tolerance 去重
- **WHEN** 兩個候選 node 的 Haversine 距離小於 20 公尺
- **THEN** SHALL 視為同一 node，合併為一筆（取其中一個座標）

#### Scenario: 距離超過 tolerance 的 node 保持獨立
- **WHEN** 兩個候選 node 的 Haversine 距離大於等於 20 公尺
- **THEN** SHALL 視為不同 node，各自建立獨立紀錄

### Requirement: Edge 建立與 base_weight 計算
seed 邏輯 SHALL 從每筆路段建立 traffic edge，並計算 base_weight。

#### Scenario: 從路段建立 edge
- **WHEN** 解析一筆路段
- **THEN** SHALL 建立一條 edge，連接該路段起點和終點對應的 node

#### Scenario: base_weight 計算
- **WHEN** 一筆路段長度為 L km、速限為 S km/h
- **THEN** edge 的 `base_weight` SHALL 為 `L / S`（單位：小時）

#### Scenario: 速限為零或缺失
- **WHEN** 一筆路段的速限為 0 或未提供
- **THEN** SHALL 使用預設速限 40 km/h 計算 base_weight

### Requirement: 啟動時自動 seed 路網資料
multiagent-service 啟動時 SHALL 自動偵測 DB 是否需要 seed，並從 JSON 快照匯入路網資料。

#### Scenario: DB 為空時自動 seed
- **WHEN** multiagent-service 啟動且 `traffic_node` 表為空
- **THEN** SHALL 從 `data/kaohsiung_road_sections.json` 讀取資料，執行 node 推導、去重、edge 建立，並寫入 DB

#### Scenario: DB 已有資料時跳過 seed
- **WHEN** multiagent-service 啟動且 `traffic_node` 表已有資料
- **THEN** SHALL 跳過 seed 流程，不做任何寫入

#### Scenario: JSON 檔案不存在
- **WHEN** multiagent-service 啟動但 `data/kaohsiung_road_sections.json` 不存在
- **THEN** SHALL 記錄警告 log 並繼續啟動，不中斷服務

### Requirement: 路網匯入 unit test
路網匯入相關邏輯 SHALL 有完整的 unit test 覆蓋。

#### Scenario: JSON 解析 test
- **WHEN** 執行 unit test
- **THEN** SHALL 驗證 JSON 檔案能正確解析為路段物件列表

#### Scenario: Node 去重 test
- **WHEN** 執行 unit test，輸入包含距離小於 20m 的重複座標
- **THEN** SHALL 驗證去重後 node 數量正確減少

#### Scenario: base_weight 計算 test
- **WHEN** 執行 unit test，輸入已知長度和速限
- **THEN** SHALL 驗證 base_weight 計算結果正確

#### Scenario: CI workflow 整合
- **WHEN** GitHub Actions CI pipeline 執行
- **THEN** SHALL 包含路網匯入相關 test，且 test 失敗時 pipeline SHALL 失敗
