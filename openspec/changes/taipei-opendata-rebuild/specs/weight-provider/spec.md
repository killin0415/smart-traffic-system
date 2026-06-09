## ADDED Requirements

### Requirement: WeightProvider Protocol 介面
系統 SHALL 定義 `WeightProvider` Protocol 作為 A* 引擎與速度估算邏輯之間的契約。

#### Scenario: Protocol 包含三個必要方法
- **WHEN** 檢查 `src/agents/weight_provider.py`
- **THEN** SHALL 存在 `WeightProvider` Protocol 含三個方法：`async rebuild(session_factory)`、`get_speed(edge: GraphEdge) -> tuple[float, str]`、`apply_to_graph(graph: RoadGraph) -> None`

#### Scenario: get_speed 回傳值含資料來源標籤
- **WHEN** 呼叫 `provider.get_speed(edge)`
- **THEN** SHALL 回傳 `(speed_kmh: float, source: str)` tuple
- **AND** `source` SHALL 為 `'vd_spatial'` / `'class_avg'` / `'fallback'` / `'personal'` 之一

### Requirement: TaipeiWeightProvider 三層 fallback 邏輯
`TaipeiWeightProvider` SHALL 實作 WeightProvider Protocol，使用三層降級策略決定 edge 速度。

#### Scenario: Tier 1 - VD 鄰近反距離加權
- **WHEN** edge midpoint 鄰近存在至少 1 個 VD（透過 `cKDTree.query` 找 K=3 個 + `distance_upper_bound=0.01` 緯度單位，緯度方向約 1.11 km、台北經度方向約 1.01 km，spec 描述以「1 公里」為近似）
- **THEN** SHALL 對找到的 VDs 用 inverse-distance weighted average 計算 speed: `sum(speed_i * 1/dist_i) / sum(1/dist_i)`
- **AND** 回傳 `(speed, 'vd_spatial')`
- **AND** 實作 SHALL 不做 cos(latitude) 經度修正（保持簡單，誤差 ≤ 11% 對「要不要算進來」決策無感）

#### Scenario: Tier 2 - 同 road_class 全市 VD 平均
- **WHEN** Tier 1 沒有可用 VD（1km 內無 VD），但 `class_avg[edge.road_class]` 存在
- **THEN** SHALL 回傳 `(class_avg[edge.road_class], 'class_avg')`

#### Scenario: Tier 3 - maxspeed × 資料推得 calibration
- **WHEN** Tier 1 和 Tier 2 都不可用
- **THEN** SHALL 回傳 `(edge.max_speed_kmh × calibration[edge.road_class], 'fallback')`
- **AND** 若 `road_class` 不在 calibration 表中，SHALL 用 default calibration 0.5

### Requirement: Calibration 係數從 VD 資料推得
系統 SHALL 從 VD 實測資料反推每個 highway class 的 actual/maxspeed 比，作為 fallback 校正係數。`DEFAULT_MAXSPEED_BY_CLASS` 的單一 source of truth 為 SQL function `default_maxspeed(highway TEXT) RETURNS INTEGER`（定義於 `infra/init-db/06-default-maxspeed-fn.sql`），Python 端在 `rebuild()` 第一次呼叫時用 `SELECT highway, default_maxspeed(highway) FROM (VALUES …) v(highway)` 一次性 query 載入到 module-level dict，不再硬編。

#### Scenario: 計算 calibration[road_class]
- **WHEN** `rebuild()` 中已得出 `class_avg[road_class] = X km/h`
- **THEN** SHALL 計算 `calibration[road_class] = X / DEFAULT_MAXSPEED_BY_CLASS[road_class]`
- **AND** 例如 `class_avg['primary']=25, DEFAULT_MAXSPEED['primary']=50` → `calibration['primary']=0.5`

#### Scenario: DEFAULT_MAXSPEED_BY_CLASS 從 SQL function 載入
- **WHEN** `TaipeiWeightProvider` 第一次 `rebuild()`
- **THEN** SHALL 從 DB 執行 `SELECT highway, default_maxspeed(highway) FROM (VALUES ('motorway'),('trunk'),('primary'),('secondary'),('tertiary'),('unclassified'),('residential'),('service'),('motorway_link'),('trunk_link'),('primary_link'),('secondary_link'),('tertiary_link'),('living_street')) v(highway)` 載入結果到 module-level cache
- **AND** Python 程式碼中 SHALL NOT 出現硬編的 `{'motorway': 80, 'primary': 50, ...}` dict

#### Scenario: 沒有對應 VD 資料的 class
- **WHEN** 某 highway class 完全沒有 VD 覆蓋（例如 `living_street`）
- **THEN** `calibration[living_street]` SHALL 不存在
- **AND** Tier 3 對該 class edge SHALL 用 default 0.5 作為係數

### Requirement: KDTree 空間索引
WeightProvider SHALL 使用 `scipy.spatial.cKDTree` 對 VD 座標建立 in-memory 空間索引以支援 1km 鄰近查詢。

#### Scenario: rebuild 時建 KDTree
- **WHEN** `rebuild()` 完成載入有效 VD 讀數
- **THEN** SHALL 用所有有讀數的 VD 緯經度建立 `cKDTree`
- **AND** 同時記錄 `vd_id_order: list[str]`，index 順序與 KDTree 點順序一致

#### Scenario: 沒有任何 VD 讀數時
- **WHEN** `rebuild()` 後 `vd_speeds` 為空
- **THEN** `kdtree` SHALL 為 None
- **AND** `get_speed()` SHALL 直接跳到 Tier 2 / Tier 3

### Requirement: 套用 weight 到 in-memory graph
WeightProvider SHALL 把計算出的 dynamic weight 寫入 `RoadGraph.adjacency` 中對應 edge 的 weight 槽位。

#### Scenario: apply_to_graph 對所有 edge 更新
- **WHEN** 呼叫 `weight_provider.apply_to_graph(graph)`
- **THEN** SHALL 對 `graph.edges` 中每一條 edge 呼叫 `get_speed(edge)`
- **AND** 計算 `new_weight = edge.length_km / max(speed, 5.0)` (防 div-by-zero)
- **AND** 呼叫 `graph.update_weight(edge_id, new_weight)` 把新 weight 寫進雙向 adjacency

#### Scenario: 計算耗時可接受
- **WHEN** graph 有 100k edges
- **THEN** `apply_to_graph` 整體執行時間 SHALL 不超過 5 秒（在中階開發機 benchmark）

### Requirement: rebuild 從 DB 拉最近 10 分鐘 VD 讀數
`rebuild()` SHALL 從 `vd_reading` hypertable 取每個 vdid 最近 10 分鐘內最新一筆有效讀數。

#### Scenario: 用 DISTINCT ON 取每個 vdid 最新一筆
- **WHEN** `rebuild()` 從 DB 撈讀數
- **THEN** SHALL 執行類似 `SELECT DISTINCT ON (vdid) vdid, AVG(avg_speed) WHERE ts > NOW() - INTERVAL '10 min' AND avg_speed > 0 GROUP BY vdid, ts ORDER BY vdid, ts DESC`
- **AND** 每個 vdid 取得最近一個 ts 內所有車道的平均速度

#### Scenario: 過濾無效讀數
- **WHEN** 一筆 vd_reading 的 `avg_speed <= 0`
- **THEN** SHALL 排除在外，不參與後續計算

### Requirement: PersonalizedWeightProvider stub
系統 SHALL 提供 `PersonalizedWeightProvider` 類別 stub，在 Phase 2 實作個人化權重時無需修改 A* 介面。

#### Scenario: stub class 存在但不實作
- **WHEN** 檢查 `src/agents/weight_provider.py`
- **THEN** SHALL 存在 `PersonalizedWeightProvider` class，constructor 接受 `(base: TaipeiWeightProvider, user_id: str)`
- **AND** 實作 `WeightProvider` Protocol
- **AND** Phase 1 中 `get_speed()` SHALL 直接 delegate 給 `base.get_speed()` (因為 `personal_overrides` 永遠是空)
