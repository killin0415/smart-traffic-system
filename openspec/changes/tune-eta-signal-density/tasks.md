## 1. Schema：traffic_edge.intersection_count 欄位

- [ ] 1.1 在 `infra/init-db/02-road-network-tables.sql` 的 `CREATE TABLE IF NOT EXISTS traffic_edge` 加入 `intersection_count INTEGER NOT NULL DEFAULT 0`
- [ ] 1.2 在同一個檔末段加 `ALTER TABLE traffic_edge ADD COLUMN IF NOT EXISTS intersection_count INTEGER NOT NULL DEFAULT 0`（向後相容既有部署，避免要 drop table）
- [ ] 1.3 在 `backend/multiagent-service/src/db/models.py` 的 `TrafficEdge` ORM model 新增 `intersection_count = Column(Integer, nullable=False, default=0, server_default="0")`

## 2. SQL build：signal snap 半徑 + intersection_count UPDATE

- [ ] 2.1 在 `scripts/build_graph_from_osm.sql` 的「6. Signal snap」區塊把 `ST_DWithin(..., 30)` 改成 `ST_DWithin(..., 50)`；同時更新該區塊頂端的註解（目前提到「still within 30 m for typical Taipei block sizes」），把所有 30 m 字樣改 50 m
- [ ] 2.2 在 §6（signal snap）跑完之後、§7（健康度 metrics）之前，新增「6b. Per-edge intersection_count」區塊：
   - 用 `UPDATE traffic_edge te SET intersection_count = sub.cnt FROM (SELECT te2.id, COUNT(p.osm_id) AS cnt FROM traffic_edge te2 LEFT JOIN planet_osm_point p ON p.highway = 'traffic_signals' AND ST_DWithin(ST_Transform(p.way, 4326)::geography, te2.geom::geography, 15) GROUP BY te2.id) sub WHERE te.id = sub.id;`
   - **注意**：必須用 `COUNT(p.osm_id)` 而非 `COUNT(*)` —— LEFT JOIN 下無匹配的 edge 右側為 NULL，`COUNT(*)` 會把該 NULL row 算成 1，造成每條無號誌的 surface edge 也被算 1 個假號誌
   - 加 `\echo` 標籤 + `RAISE INFO` 印出 `SUM(intersection_count)` 與 `AVG(intersection_count) FILTER (WHERE road_class NOT IN ('motorway','trunk','motorway_link','trunk_link'))`
- [ ] 2.3 在 §7（健康度）的 RAISE INFO 行加上 `has_signal_pct = 8877 / 67454` 計算，並在 has_signal_pct 落在 [10%, 30%] 區間外時 RAISE NOTICE（同 spec scenario）

## 3. Python：weight_provider multiplier

- [ ] 3.1 在 `backend/multiagent-service/src/agents/weight_provider.py` 模組頂端新增常數：
   - `INTERSECTION_MULTIPLIER_FACTOR: float = 0.15`
   - `INTERSECTION_MULTIPLIER_EXEMPT_CLASSES: frozenset[str] = frozenset({"motorway", "trunk", "motorway_link", "trunk_link"})`
- [ ] 3.2 改 `apply_to_graph(graph)` 內的 weight 計算：
   - `base = edge.length_km / speed`（不變）
   - `multiplier = 1.0 if edge.road_class in INTERSECTION_MULTIPLIER_EXEMPT_CLASSES else 1.0 + INTERSECTION_MULTIPLIER_FACTOR * edge.intersection_count`
   - `graph.update_weight(edge.id, base * multiplier)`
- [ ] 3.3 確認 `get_speed()` 函數本身不變（multiplier 只在 apply 階段作用），三層 tier 邏輯維持原狀

## 4. Python：GraphEdge intersection_count 欄位

- [ ] 4.1 在 `backend/multiagent-service/src/agents/routing.py` 的 `GraphEdge` dataclass 加 `intersection_count: int = 0` 欄位（放在 `oneway` 之後）
- [ ] 4.2 在 `RoadGraph.from_db()` 載入 edge 時，從 `e.intersection_count` 讀值寫進 GraphEdge：`intersection_count=int(getattr(e, "intersection_count", 0) or 0)`（用 getattr 容忍舊 schema 沒此欄位的 hot reload 場景）

## 5. Python：SIGNAL_PENALTY_SECONDS 預設值

- [ ] 5.1 在 `backend/multiagent-service/src/agents/routing.py` 把 `SIGNAL_PENALTY_SECONDS = float(os.getenv("SIGNAL_PENALTY_SECONDS", "20"))` 的 fallback `"20"` 改為 `"40"`
- [ ] 5.2 更新該行 docstring 註解，反映「Taipei 一般信號週期下的平均停等 30-45 秒」的依據

## 6. 測試：weight_provider multiplier

- [ ] 6.1 在 `backend/multiagent-service/tests/test_weight_provider.py` 新增 5 個 test：
   - `test_apply_to_graph_skips_multiplier_for_exempt_classes`：parametrized over `["motorway", "trunk", "motorway_link", "trunk_link"]`，每個 class 建 edge + intersection_count=5，apply 後 weight 等於 base（multiplier=1.0）
   - `test_apply_to_graph_applies_multiplier_to_surface_street`：建一個 secondary edge + intersection_count=4，apply 後 weight = base × 1.6
   - `test_multiplier_factor_monkeypatch`：用 `monkeypatch.setattr(weight_provider, "INTERSECTION_MULTIPLIER_FACTOR", 0.5)`，secondary + intersection_count=4 apply 後 weight = base × 3.0
   - `test_graph_edge_default_intersection_count_zero`：直接構造 `GraphEdge(...)` 不傳 intersection_count，預設值 SHALL 為 0；apply 後 multiplier 退化為 1.0、weight 等於 base（驗證向後相容）
   - `test_exempt_classes_monkeypatch`：把 `INTERSECTION_MULTIPLIER_EXEMPT_CLASSES` 設成 `frozenset()`，trunk edge + intersection_count=4 apply 後 weight = base × 1.6
- [ ] 6.2 在 `tests/test_weight_provider.py` 確認既有 test 仍通過（intersection_count default 0 → multiplier 1.0 → weight 公式向後相容）

## 7. 測試：SQL build intersection_count

- [ ] 7.1 在 `backend/multiagent-service/tests/test_build_graph_sql.py` fixture 加 1 個 signal 點靠近某條 secondary edge（既有 line 25 M2-N2 附近）
- [ ] 7.2 新增 test `test_traffic_edge_intersection_count_populated`：build 後該 secondary edge 的 `intersection_count >= 1`
- [ ] 7.3 新增 test `test_traffic_edge_intersection_count_zero_when_no_signal_near`：line 24 L2-M2 附近沒有 signal，build 後 edge intersection_count = 0
- [ ] 7.4 確認 §1.1 / §1.2 的 schema 變更不會破壞現有 test（intersection_count 欄位有預設值 0）

## 8. 測試：SIGNAL_PENALTY_SECONDS 預設值

- [ ] 8.1 在 `backend/multiagent-service/tests/test_routing.py` 新增 test `test_signal_penalty_seconds_default_is_40s`：確認 `SIGNAL_PENALTY_HR ≈ 40 / 3600 = 1/90` 當 env 未設定時
- [ ] 8.2 確認既有 signal penalty test（`TestSignalPenalty`）仍通過 — 這些 test 用 `SIGNAL_PENALTY_HR` 計算 expected diff，重新匯入應該自動跟上新預設值
- [ ] 8.3 `uv run pytest` 整套通過

## 9. Reimport 實機驗證

- [ ] 9.1 跑 `psql -f infra/init-db/02-road-network-tables.sql`（加 intersection_count 欄位；idempotent）
- [ ] 9.2 跑 `psql -f scripts/build_graph_from_osm.sql`，存下 stdout（含 §2.2 與 §2.3 印的 intersection 統計）
- [ ] 9.3 確認 `has_signal_pct` 落在 [10%, 30%]；確認 `AVG(intersection_count) FILTER (road_class IN ('primary', 'secondary'))` ≥ 1.5
- [ ] 9.4 重啟 multiagent-service，確認 startup log 無新 error
- [ ] 9.5 跑 `backend/multiagent-service/scripts/_acceptance_failing_route.py`：
   - 原失敗 OD pair `(25.0478,121.5170) → (25.0337,121.5645)` 的 route[0] ETA SHALL 落在 **12-16 min** 區間（Google 日間 18 min 的 ±35%）
   - 比較 trunk-only 路徑（如 pair[2]、pair[5]）的 ETA 變動 SHALL ≤ ±5%
- [ ] 9.6 把 §9.5 數據放進 commit message（before/after 對照）

## 10. Memory + 收尾

- [ ] 10.1 更新 memory `routing_algorithm.md`：補上 `INTERSECTION_MULTIPLIER_FACTOR` / exemption set 兩個常數的位置 + 用途；更新 SIGNAL_PENALTY_SECONDS 預設值說明
- [ ] 10.2 更新 memory `eta_accuracy_followup.md`：標註 Option A + C 已實作，補上實機效果（before/after ETA 對照）
- [ ] 10.3 commit SQL / Python 改動為一個 commit；memory 更新另開獨立 commit
- [ ] 10.4 push develop branch
