## 1. DB Schema 建立

- [x] 1.1 在 `infra/init-db/` 新增 SQL 檔案，建立 `traffic_node` 表（id, latitude, longitude），使用 `CREATE TABLE IF NOT EXISTS`
- [x] 1.2 在同一 SQL 檔案建立 `traffic_edge` 表（id, source_node_id, target_node_id, road_name, length_km, speed_limit_kmh, base_weight），含外鍵約束
- [x] 1.3 驗證 `docker-compose up` 後兩張表成功建立

## 2. TDX 抓取 Script

- [x] 2.1 建立 `scripts/import_tdx_road_network.py`，實作 TDX OAuth2 認證（支援 `TDX-CLIENT-ID` 和 `TDX_CLIENT_ID` 兩種格式，自動讀取 `.env`）
- [x] 2.2 實作 Section API 呼叫（`/api/basic/v2/Road/Traffic/Section/City/Kaohsiung`），處理分頁與 rate limit 429 重試
- [x] 2.3 實作 bounding box 過濾：Python 端過濾，西南角 (22.600, 120.270)、東北角 (22.660, 120.340)
- [x] 2.4 實作速限推估：從 RoadClass 推導速限（0→110, 1→70, 2→80, 3→70, 4→60, 5→50, 6→50 km/h）
- [x] 2.5 將抓取結果輸出為 `data/kaohsiung_road_sections.json`，包含 `metadata` 和 `road_sections` 欄位
- [x] 2.6 加入錯誤處理：認證失敗、API 錯誤時輸出明確訊息並以非零 exit code 結束
- [x] 2.7 整合 SectionShape API（`/api/basic/v2/Road/Traffic/SectionShape/City/Kaohsiung`），透過 SectionID 對應，將 WKT LINESTRING 存入每筆路段的 `geometry_wkt` 欄位

## 3. 路網解析邏輯

- [x] 3.1 在 `backend/multiagent-service/src/db/` 新增路網解析模組，實作 JSON 讀取與路段解析
- [x] 3.2 實作 node 推導：從每筆路段取出起終點座標作為候選 node
- [x] 3.3 實作座標去重：使用 Haversine 距離，20m tolerance 內的 node 合併
- [x] 3.4 實作 edge 建立：每筆路段對應一條 edge，`base_weight = length_km / speed_limit_kmh`，速限為 0 或缺失時預設 40 km/h

## 4. 啟動時自動 Seed

- [x] 4.1 在 multiagent-service 啟動流程中加入 seed 偵測邏輯：查詢 `traffic_node` 是否為空
- [x] 4.2 DB 為空時從 `data/kaohsiung_road_sections.json` 讀取並執行完整 seed 流程（解析 → 去重 → 寫入）
- [x] 4.3 DB 已有資料時跳過 seed
- [x] 4.4 JSON 檔案不存在時記錄警告 log 並繼續啟動，不中斷服務

## 5. SQLAlchemy Model 定義

- [x] 5.1 新增 `TrafficNode` SQLAlchemy model（對應 `traffic_node` 表）
- [x] 5.2 新增 `TrafficEdge` SQLAlchemy model（對應 `traffic_edge` 表，含外鍵關聯）

## 6. Unit Test

- [x] 6.1 新增 `backend/multiagent-service/tests/test_road_network.py`
- [x] 6.2 撰寫 JSON 解析 test：驗證 JSON 能正確解析為路段物件
- [x] 6.3 撰寫 node 去重 test：驗證距離 < 20m 的座標合併、>= 20m 的保持獨立
- [x] 6.4 撰寫 base_weight 計算 test：驗證已知長度和速限的計算結果
- [x] 6.5 撰寫速限缺失 test：驗證預設 40 km/h 行為

## 7. CI 整合

- [x] 7.1 修改 `.github/workflows/ci-multiagent-service.yml`，加入路網相關 test 步驟
- [x] 7.2 驗證 CI pipeline 中 test 失敗時 workflow 正確失敗

## 8. 執行 TDX 抓取與資料驗證

- [x] 8.1 執行 `scripts/import_tdx_road_network.py` 抓取實際資料（2026-04-06 完成，154 筆路段）
- [x] 8.2 檢查產出的 JSON 資料筆數和內容合理性（路名正確顯示、座標在高雄範圍內）
- [x] 8.3 將 `data/kaohsiung_road_sections.json` commit 進 repo
- [x] 8.4 端對端驗證：`docker-compose up` 後確認 DB 中有完整路網資料

## 9. 依賴清理

- [x] 9.1 移除 pyproject.toml 中不需要的依賴（ultralytics, opencv, pyautogen, openai, langchain, networkx, psycopg2-binary, elasticsearch）
- [x] 9.2 新增 google-genai SDK 依賴
- [x] 9.3 修復 test_main.py：mock lifespan 外部依賴（DB + Kafka），確保測試不依賴外部服務
