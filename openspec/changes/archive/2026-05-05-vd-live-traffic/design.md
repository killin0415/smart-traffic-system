## Context

astar-engine-and-tdx-live 期間發現 TDX `basic/v2/Road/Traffic/Live/City/Kaohsiung` 回 HTTP 400（Kaohsiung 不在 basic tier 支援清單：Taipei / Taichung / Tainan / Taoyuan / Keelung 等），目前 `traffic.py` 的 refresher 在偵測到這個錯誤後發一次 WARNING 就 no-op，A* 路由完全跑在 base weight 上。

Kaohsiung 實際拿得到的即時資料是 `Live/VD/City/Kaohsiung`（已實測回 200，payload 為 `{UpdateTime, VDLives: [{VDID, LinkFlows:[{LinkID, Lanes:[{Speed, Vehicles:[...]}]}]}]}`）。缺點是：
- 資料是「感測器點讀」而不是「路段彙整」
- 每個 VD 固定在路上某一點，一條 edge 可能有 0~N 個 VD
- 大量 VD 回傳 `Speed: -99.0 / ErrorType: diag202`（感測器故障或未上線）
- VD 的 `LinkFlows[].LinkID`（e.g. `6196780000010E`）與我們從 Section API 抓到的 `RoadSectionID`（e.g. `L_6190010300020E`，有 `L_` prefix）**格式不同、無法直接 JOIN**

既有 `speed_camera` 已經示範了「外部點位 → 最近 edge」的 snap 流程（`src/db/speed_camera.py:snap_camera_to_edge`），可以複用。

## Goals / Non-Goals

**Goals:**
- 讓 A* 路由在高雄能吃到真實的即時速度資料，夜尖峰差異可以體現在路徑選擇與 top-K 排序上
- Snap 結果持久化到 DB，service 啟動時只做一次，後續 refresher 只做 live 讀 + 平均 + 權重更新
- 保留 astar-engine-and-tdx-live 建好的三個出口：Redis（`traffic:section:{tdx_section_id}`）、`traffic_history` hypertable、`RoadGraph.update_weight()`
- 對感測器雜訊（`-99`、`ErrorType`）做明確的過濾，不讓一顆壞掉的 VD 把整條 edge 的權重拉到 MAX_CONGESTION_FACTOR

**Non-Goals:**
- 不重寫 A* 引擎、不改 Redis key/TTL、不改 `traffic_history` schema
- 不處理 VD 以外的 Live 類型（CMS 號誌、ETag gantry）
- 不為不支援的縣市（Taipei etc.）回退到 Live Section endpoint — 本專案只服務 Kaohsiung
- 不做 real-time streaming（維持 polling，間隔仍是 `TDX_LIVE_REFRESH_SECONDS`）
- 不處理 VD 靜態資料變動（新增/移除 VD 需要手動重跑 seed 或 TRUNCATE `vd_sensor` 表）

## Decisions

### D1. Snap 粒度：edge 而非 section
**選擇**: VD → 最近的 `TrafficEdge`（用 edge 兩端 node 的較近那端算 haversine 距離，複製 `snap_camera_to_edge` 邏輯）。
**理由**:
- 既有 snap 邏輯已通過測試，可重用
- `RoadGraph.update_weight(edge_id, factor)` 本來就是以 edge 為單位
- 跨 edge 對同一個 section 的聚合另一個抽象層，但目前沒有需求

**替代方案**: 用 VD 的 `RoadSection` 欄位（若 TDX 有提供）做邏輯對應。放棄理由：未驗證此欄位是否穩定、且仍要落地成 edge 才能更新權重，不如直接空間對應。

### D2. 一個 edge 上多個 VD 的彙整
**選擇**: 平均（算術平均、過濾 `Speed <= 0` 與 `ErrorType` 非空的讀數）。
**理由**: 簡單、可解釋；夠一個 capstone demo 用。

**替代方案**: 取最低速度（最保守）、中位數（抗離群）。放棄理由：樣本數通常很少（1~3 個 VD），中位數意義不大；取最低會讓單一塞車車道覆蓋整個 edge。若後續發現問題，改成加權平均（依車道類型）或丟棄一定比例的離群值都只是 `_aggregate_vd_speeds()` 內部的事。

### D3. Snap 結果持久化 vs. 啟動時重算
**選擇**: 持久化到新表 `vd_sensor`，schema 參考 `speed_camera`：
```
id (PK), vdid (unique), latitude, longitude,
link_id (nullable), road_section_id (nullable),
nearest_edge_id (FK → traffic_edge.id, nullable)
```
Service 啟動時 seed 一次（若表空），之後 refresher 只用這張表的 `vdid → nearest_edge_id` mapping。

**理由**:
- VD 靜態資料變動極低（一年可能增減幾個）
- Snap 要算每個 VD 到所有 edge 的距離，154 edges × 數百 VDs，每次啟動都重算是浪費
- 和 `speed_camera` 流程對稱，維護心智負擔低

**替代方案**: 啟動時全部放進記憶體、不落地。放棄理由：之後要暴露 `/debug/vd` 端點或在前端顯示 VD 位置時，DB 就是單一事實來源。

### D4. 過濾 `-99` 與 `ErrorType`
**選擇**: 在 `_aggregate_vd_speeds()` 中把 `Speed <= 0` **或** `ErrorType` 非空的 lane 讀數丟掉；若某個 edge 上所有 VD 都壞掉 → 跳過此 edge（不更新權重，讓前一輪 Redis 值自然 TTL 過期）。
**理由**: 一個 `-99` 進平均會把整條路誤判為嚴重壅塞。

### D5. 對 `traffic_history` hypertable 的寫入
**選擇**: 每次 refresh 仍以 `(time, tdx_section_id)` 為 primary key 寫入一筆，但 `tdx_section_id` 改放 `RoadSectionID`（從 edge 反查 `TrafficEdge.tdx_section_id`）。一個 section 可能涵蓋多條 edge → 取該 section 下所有 edge 聚合後的平均速度。
**理由**: 維持與 tdx-live-traffic 定義的 schema 相容，避免改 hypertable。
**代價**: 有些 edge 沒有對應的 VD 或 section → 不會寫入歷史資料，屬已知限制。

### D6. 移除降級分支
**選擇**: 既然 VD 路徑對 Kaohsiung 就是可用，原本 `traffic.py` 中的 `_unsupported_city_logged` 與 400 處理都刪掉。若未來跑到其他城市再視需求處理。

## Risks / Trade-offs

- **[VD 覆蓋率不均] 市中心幹道 VD 密，郊區幾乎沒有 → 部分 edge 永遠拿不到即時資料**
  → Mitigation: 這些 edge 停留在 base weight，A* 依然能跑；log 出「n/154 edges 有 live 覆蓋」讓我們掌握覆蓋率。

- **[大量 VD 故障] `ErrorType` 比率 > 50% 時，平均值偏差**
  → Mitigation: `_aggregate_vd_speeds()` 要求每個 edge 至少 1 個健康 VD 才更新；否則跳過。加 metric log「health=X/Y」。

- **[VD 靜態資料漂移] TDX 新增/移除 VD 時 `vd_sensor` 表會過期**
  → Mitigation: 提供 `TRUNCATE vd_sensor` 後重啟即可重新 seed；第一版不做自動同步。

- **[Snap 誤差] VD 在路口時最近端點可能錯邊 → 對應到錯的 edge**
  → Mitigation: 沿用 speed_camera 的簡化版距離（兩端點較近者），足夠 demo；若後續精度不夠再升級到 point-to-segment。

- **[rate limit] TDX 免費 tier 間隔 2 秒；VD 靜態抓取分頁時需遵守**
  → Mitigation: seed 階段每頁 sleep 2s（參考 `scripts/import_tdx_road_network.py`）。Live refresh 間隔預設 300s 不受影響。

## Migration Plan

1. 先 merge 這個 change：加表、加 seed、加 fetch — 不刪除現有 `TDX_LIVE_SECTION_URL` 程式碼，只是不再呼叫
2. Service 重啟 → lifespan 自動 seed `vd_sensor`（會看到「VD seed 完成: N 筆」log）
3. 下一輪 `run_periodic_refresh` 走新路徑，觀察 log「refresh_traffic_data: N sections fetched, M edges updated」
4. 確認 Redis 有 live 值、`traffic_history` 有新行後，刪除舊的 Section URL 常數與 `_unsupported_city_logged` 標記

**Rollback**: `git revert` 這個 change 後重啟，refresher 會自動回到原本的 no-op 狀態。`vd_sensor` 表留著不影響任何讀取路徑。

## Open Questions

- TDX `Road/Traffic/VD/City/Kaohsiung`（靜態）的 response schema 還未實測 — 需在 task 1.1 撥電話確認欄位（VDID、PositionLat/Lon、LinkID、RoadSection 是否都存在）
- `traffic_history` 的 `tdx_section_id` 若一個 section 對多條 edge 平均後寫入，查詢端要不要明確區分？暫定：先就以這個語意寫入，若有分析需求再擴欄位
