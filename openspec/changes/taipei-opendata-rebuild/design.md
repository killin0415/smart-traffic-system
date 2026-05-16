## Context

### 現狀

- 路網資料來源：TDX `Section + SectionShape` API（`scripts/import_tdx_road_network.py`），bbox 限縮在台北車站周邊 4×4 km，實測只有 81 條路段。產出 `data/taipei_road_sections.json` commit 進 repo，service 啟動時 lifespan 從 JSON seed 進 `traffic_node` / `traffic_edge`。
- 速度估算：`traffic_edge.base_weight = length / RoadClass-推估速限`（沒實測值，TDX Section 不提供 SpeedLimit 欄位）。
- 即時車速：`src/agents/traffic.py` 從 TDX `Live/City/{city}` 拉資料，congestion_factor = `min(speed_limit / current_speed, 10.0)`，呼叫 `RoadGraph.update_weight(edge_id, congestion_factor)` 內部算 `base_weight × factor` 寫回 adjacency。Kaohsiung endpoint 不支援，過去半年 in-prod 都在跑「優雅降級」no-op 分支；切到 Taipei 後該 endpoint 雖然能呼叫，但回的資料品質仍有限。
- 已知問題（`memory/eta_accuracy_followup.md`）：A* free-flow ETA 對 Google Maps 樂觀 3-4 倍。三條 remediation paths 中本次採其中之一（effective-speed factor）並結合真實 VD 資料。

### Codebase 事實對齊（review 後校正）

- DB Docker compose 在 `infra/docker-compose.yml`（**非 repo root**），現用 `timescale/timescaledb:latest-pg14` image、`POSTGRES_DB=traffic_data`、`POSTGRES_USER=admin`、`POSTGRES_PASSWORD=secret`。
- Kafka runtime 在 `src/kafka/runtime.py`（**非 `src/kafka_runtime.py`**），用 module-level globals 模式：`set_runtime(graph, loop, session_factory, chat_agent)` + `get_graph()` / `get_loop()` / `get_session_factory()` / `get_chat_agent()`，**沒有 `RuntimeContext` class**。本 change 沿用此模式，只新增 `_weight_provider` global 加上對應 setter/getter。
- `RoadGraph.update_weight(edge_id, congestion_factor)` 已存在（`src/agents/routing.py:135`），目前內部算 `base_weight × factor`。本 change 改 signature 為 `update_weight(edge_id, new_weight)` 吃絕對 weight，是內部 API BREAKING。
- VD 動態 endpoint 實測（2026-05-06 WebFetch）回 plain XML、Content-Type 非 gzip，`<?xml version="1.0"...><VDInfoSet>` 開頭，可直接 `ET.fromstring(text)`。

### 限制 / Constraints

- Capstone 2 個月剩餘時間，solo dev，避免大量 infra 變動。
- 現有 stack：TimescaleDB (PG14)、Redis、Kafka、Spring Boot Kotlin (主 API) + Python FastAPI multiagent-service。本 change 只動 multiagent-service + DB infra；主 service 端只需要同步一個 Kafka response DTO 欄位。
- LLM 預算控制（已切換到 DeepSeek paid frugal mode），不影響本 change。
- Python 環境必須走 `uv` (`backend/multiagent-service/CLAUDE.md` 規則)。

### Stakeholders

- 主要：solo dev（你）。次要：未來 demo 評審。
- 下游消費者：Spring Boot main-service 透過 Kafka `route.request/response` 拿路徑、chat agent 透過 Kafka `chat.intent` 觸發路徑規劃。本 change 對 `route.response` 是**向後相容**的擴充（多一個 `parking_suggestions` 欄位 + 改善 `estimated_minutes` 準度），main-service Kotlin DTO 同步新增欄位即可。

## Goals / Non-Goals

**Goals:**
- 把路網覆蓋從 81 sections 擴到 80k+ edges（含巷弄 + 全市 12 區）。
- 消除「ETA 比 Google Maps 樂觀 3-4 倍」的根因（用實測 VD 平均速率取代速限推估）。
- 三層降級的 weight 策略，VD 稀疏地區也有合理估算。
- 替換掉品質不可靠的測速相機資料源；新增終點停車場推薦。
- 預留個人化權重的擴充介面（Phase 2 不實作）。
- A* 在 ~80k edges 上單次 query 仍能 < 200ms（透過 search bbox frontier pruning）。

**Non-Goals:**
- 不做 Time-of-day weight profile（尖峰 / 離峰）—— vd_reading 留 30 天歷史給未來分析用。
- 不收集使用者 GPS 軌跡、不做 map matching、不實作個人化 weight（只留 Protocol stub）。
- 不接 frontend，本 change 純後端。
- 不接即時道路事件 / 道挖（data.taipei 沒有，TDX News API 不在範圍）。
- 不支援多城市 — 寫死 Taipei，新北延伸留待未來 change。
- 不做 Yen's K-shortest-paths — 維持現有 penalty-based top-K（penalty=3.0）。
- **不跨 PG 大版號**：留 PG14（見 D1）。
- **不納入行人/自行車路網**：OSM 篩 highway 時排除 `pedestrian`、`footway`、`cycleway`、`track`、`steps` 等非汽車可行道路，避免 graph 膨脹且 routing 結果不適用。

## Decisions

### D1: 路網結構用 OSM PBF + osm2pgsql；DB image 留 PG14 換 timescaledb-ha pg14-all

**選項評估：**
| 選項 | 工具 | 優 | 劣 |
|---|---|---|---|
| A | `osmnx` (Python) | API 簡單、自帶 simplify | 背後打 Overpass，全台北會 timeout / rate limit |
| B | PBF + `pyrosm` | 一次下載永久用 | Python 解析 250MB PBF 慢（~2 min），需自己處理 way-to-edge 拆分 |
| **C** ★ | **PBF + `osm2pgsql` → PostGIS** | 業界標準、PostGIS GIST index 自動建、未來空間 query 直接用 | osm2pgsql 是 C++ CLI 需 Docker 跑、要學 PostGIS |

**選 C 的理由：**
1. **資料同源**：使用者明確要求「資料都存在資料庫裡」，PostGIS 把 raw OSM tags 都留在 DB（`planet_osm_*`），未來想做「找路徑沿途的便利商店」這類查詢直接 SQL（**注意：本 change 沒有任何 query 用到 raw `planet_osm_*` 表，純 graph build 中介層；Phase 2 才真正用到**，列為 D2 的明確 trade-off）。
2. **空間查詢**：`vd_static.snapped_road_class` 計算（VD snap 到最近 OSM edge）用 `ST_DWithin` 一句 SQL 完成，比 Python 端跑 100k × 667 距離計算快幾個量級。
3. **TimescaleDB 共存**：用 `timescale/timescaledb-ha:pg14-all` image，**留 PG14 不跨大版號**。原本 review 指出該 image 寫成 pg16 會引入 PG14→PG16 跨版升級成本（資料 dump/restore + extension 重建 + ORM 相容），對 capstone 規模不值得。pg14-all 同時帶 PostGIS + TimescaleDB + Toolkit。
4. **可重現性**：osm2pgsql 是 C++ CLI 跑 Docker，避免開發者本機 Windows 上裝失敗（osm2pgsql Windows 安裝實測痛）。

### D2: 保留現有 `traffic_node` / `traffic_edge` schema（不全 PostGIS-native）

**選項：**
| 選項 | 內容 | 優 | 劣 |
|---|---|---|---|
| A | A* 直接 query `planet_osm_line` + PostGIS `ST_*` | flexible | 重寫 `RoadGraph.from_db()` + adjacency 邏輯，影響大 |
| **B** ★ | **`build_graph_from_osm.sql` 把 OSM transform 成現有 schema** | A* 程式幾乎不動，只加 bbox 跟 WeightProvider | raw OSM 跟 graph 兩份資料 |

選 B：A* 介面穩定、程式改動最小，風險可控。

**誠實補充**：本 change 內部沒有任何 SQL 查詢直接用 `planet_osm_*` raw 表——它純粹是 graph build SQL 的中介資料來源。Phase 2 若要做「沿路 POI 查詢」「沿路便利商店」這類功能，raw 表才會被真正用到。如果未來決定不做這類功能，可以簡化為 osm2pgsql 後立即 build graph + drop raw 表釋放空間（~500MB-1GB）。

### D3: WeightProvider 三層 fallback（VD spatial → class avg → maxspeed × calibration）

**為什麼三層：**
- Tier 1（VD 鄰近反距離加權）：實測車速最準、空間相關性強。`scipy.spatial.cKDTree` rebuild 667 個 VD < 1ms，query 100k edges 全跑 ~1s。
- Tier 2（同 OSM `highway` class 全市 VD 平均）：VD 覆蓋以幹道為主，巷弄沒 VD → 用「同類道路全市實測平均」當代理。
- Tier 3（maxspeed × calibration）：完全沒任何 VD 資料、或 road_class 連 class avg 都沒有時。**calibration 是「資料推得」**：`calibration[primary] = AVG(VD primary 平均速率) / DEFAULT_MAXSPEED[primary]`。例如速限 50、實測平均 25 → 係數 0.5。這直接解決 ETA 樂觀 3-4 倍的問題。

**捨棄的方案：**
- 純 Tier 1（沒 VD 直接 unreachable）：被剪掉太多巷弄，A* 找不到路。
- 純 Tier 3（永遠用 maxspeed × 校正係數）：失去即時資料的最大價值。
- LLM 推估 weight：成本爆炸、不確定性高、無 audit trail。

**`DEFAULT_MAXSPEED_BY_CLASS` 為單一 source of truth：** 統一定義在 `infra/init-db/06-default-maxspeed-fn.sql` 的 `default_maxspeed(highway TEXT) PL/pgSQL function` 裡（DB init 階段就建好，`build_graph_from_osm.sql` 跟 `weight_provider.py` 兩邊都 reference 同一支 function）。Python 端 (`weight_provider.py`) 不再硬編一份 dict 跟 SQL 重複維護，而是在 `WeightProvider.rebuild()` 第一次呼叫時從 DB 一次性 query：`SELECT highway, default_maxspeed(highway) FROM (VALUES ('motorway'),('primary'),...) v(highway)` 載入到 module-level dict，後續沿用。

**`distance_upper_bound` 公差說明：** `cKDTree.query` 的 `distance_upper_bound=0.01` 是**緯度單位**（degree），緯度方向 ≈ 1.11 km、台北經度方向（cos(25°)≈0.906）≈ 1.01 km。spec 寫「1 公里內」是近似描述，實作以 0.01 度為界，誤差 ≤ 11%，對「要不要算進來」的決策無感。不做 cos(lat) 修正以保持實作簡單。

### D4: VD refresher 留在 multiagent-service in-process（不拆獨立 service）

開銷實測：5 min cycle 約 1.3s wall time，平均 0.4% CPU、ms 級 DB write。`WeightProvider.apply_to_graph(graph)` 直接 mutate in-memory adjacency dict——拆出去就要解 graph 一致性、Kafka event 通知、DB round-trip 等等，得不償失。

未來真要拆（不會），介面已乾淨：`run_periodic_vd_refresh` 抽出成獨立 process + 發 Kafka event，multiagent 收到呼叫 `weight_provider.rebuild()`。約 1 天工作量。

### D5: A* search bbox 在 runtime 做 frontier pruning（不在 DB 端做 subgraph）

啟動時 `RoadGraph.from_db()` 一次性把全市 graph (~80k edges, ~50-80MB RAM) 載入。Per-request 在 A* successor function 加一行 `if not search_box.contains(neighbor.lat, neighbor.lng): continue`。

bbox 算法：`bbox = bounding(origin, dest).expand(max(direct_km * 0.3, 2km))`。padding 30% 給 A* 繞路空間；極短距離 fallback 至少 2km。

找不到路時 retry padding 0.6（最多 1 次）。retry 觸發率 > 5% 表示 0.3 padding 太緊，調高常數。

不做 lazy-load subgraph from DB per-request：多一次 DB round-trip + connection pool 壓力，每 request +50-100ms 不值得。

### D6: VD pre-snap road_class 在 build graph 完之後做（一次性、不在 rebuild 重做）

`vd_static.snapped_road_class` 欄位在 OSM graph build 完後用 PostGIS `ST_DWithin(100m)` join `traffic_edge` 取最近 edge 的 `road_class`，一次寫死。

WeightProvider `rebuild()` 每 5 分鐘只查 `vd_static.snapped_road_class`，不重新 snap。OSM 路網更新時（手動觸發）才需要重算。

### D7: 個人化用 Protocol stub 預留，不實作

`WeightProvider` Protocol + `PersonalizedWeightProvider(base, user_id)` pass-through wrapper class。`plan_optimal_route(..., user_id=None)` 簽章預留參數。Phase 1 永遠不傳 `user_id`，Kafka payload 也不傳。Phase 2 接上 GPS trace 收集後再實作真正的 override 邏輯。

不在這次做的理由：個人化需要 GPS trace 收集 + map matching pipeline + per-user storage + cold-start 處理，至少 2 週工作量，超出本 change 範圍。

**註**：「stub」字面意思是「空殼」，本 change 的 `PersonalizedWeightProvider` 實際是 pass-through wrapper（永遠 delegate 給 base），不是真的空 method。叫 stub 是為了強調 Phase 1 還沒實作個人化邏輯。

### D8: 測試：每個產品 task 派獨立 test engineer sub-agent

`tasks.md` 中每個產品實作 task 後緊接一個「測試 task」，用 `general-purpose` sub-agent 角色設「測試工程師」執行。獨立 agent 寫測試的好處：
- **獨立性**：實作者寫測試會 bias 自己的實作；外人寫更容易抓 edge case。
- **併行**：多個 module 可同時實作 + 測試。
- **明確契約**：產品 task 的 acceptance criteria 直接餵給測試 agent，強迫產品端把 contract 寫清楚。

dispatch 模板統一：「角色測試工程師、為 X module 寫 unit tests、讀產品碼、寫 tests/test_X.py、跑 `uv run pytest tests/test_X.py -v`、回報測試數 + 覆蓋率 + 若發現產品碼 bug 一併標出」。

**Integration / e2e 測試對 testcontainers 的依賴**：本 change 引入三個會啟 timescaledb-ha container 的 test 檔案（`test_infra_extensions.py`、`test_lifespan_integration.py`、`test_e2e_route.py`）。這些測試在 dev 機器要先 `docker pull timescale/timescaledb-ha:pg14-all`（~700MB 一次性下載）；CI 環境短期跳過（用 `pytest -m 'not integration'`），等 capstone 後期再加 GitHub Actions service container。

### D9: VD 靜態資料 seed 改 CLI script，不在 lifespan 自動 seed

**問題：** Review 指出 osm-road-network 的 `post_build_snap_vd.sql` 要 `vd_static` 已 seed 才能跑，但若 `seed_vd_static` 在 lifespan 內，graph build (offline) 跟 seed (online) 順序就矛盾。

**選項：**
| 選項 | 內容 | 優 | 劣 |
|---|---|---|---|
| A | seed_vd_static 在 lifespan、snap 也在 lifespan（每次啟動 re-snap） | 流程單純 | 啟動慢 + 強依賴外部 endpoint 可用 |
| B | seed_vd_static 在 lifespan、snap 在 offline SQL | 矛盾，無法執行 | — |
| **C** ★ | **seed_vd_static 拉成 CLI script `scripts/seed_vd_static.py`，跟 graph build 同屬 offline 流程** | 順序明確、不汙染 lifespan、可重複執行 | 多一個 CLI 步驟要文件化 |

選 C：lifespan 只負責 dynamic-only 的事（refresher 啟動、in-memory graph 載入）；vd_static 是一次性靜態資料屬於 offline build。

**新 offline 流程順序：**
```
1. import_taipei_osm.sh             # download + osm2pgsql
2. build_graph_from_osm.sql         # OSM → traffic_node/edge
3. seed_vd_static.py                # data.taipei VD.xml → vd_static (含 geom)
4. post_build_snap_vd.sql           # vd_static.snapped_road_class via ST_DWithin
5. uv run python main.py            # service 啟動
```

lifespan 仍會檢查 `vd_static` 是否為空，空則 log warning 並提示執行 `seed_vd_static.py`；但**不**自動 fetch。

### D10: 紅綠燈停等用 OSM `traffic_signals` node tag + 固定 penalty（不接 data.taipei 號誌週期 dataset）

**問題：** WeightProvider 用 VD 平均速率算 edge weight，但 VD 通常裝在路段中段量「巡航速度」，**捕捉不到號誌停等時間**。台北車站→101 主幹道粗估 12-15 個號誌路口，每個 20s 停等 = +4-5 min ETA 漏算。

**選項：**
| 選項 | 內容 | 工 | 準度 |
|---|---|---|---|
| A | A* 每 expand 一個 node 都加 +20s（無區分） | 1hr | 安靜路口跟主幹道大號誌同價，過度悲觀 |
| **B** ★ | **OSM `highway=traffic_signals` node tag 才加 penalty** | 半天 | 區分有/無號誌，sweet spot |
| C | data.taipei「臺北市路口交通號誌」週期+綠燈比 → 算期望停等 `(1-green_ratio)×cycle/2` | 2-3 天 + 新 ingest | 最準但 capstone overkill |

**選 B 的理由：**

1. **資料免費且已經要灌**：本 change 已用 osm2pgsql 灌全 PostGIS，`planet_osm_point WHERE highway='traffic_signals'` 直接查；不需要新增 ingest pipeline。
2. **A\* 主迴圈改動最小**：weight 拆成兩個正交 component（edge cruise speed + node signal wait），A* successor 加一行 `g += SIGNAL_PENALTY if node.has_signal else 0`。
3. **Heuristic admissibility 不破**：直線距離 / max_speed 仍 underestimate true cost（包含 signal）；A* 找到的還是最佳路徑。
4. **Calibration 留可擴展空間**：先用固定 20s（台北號誌典型 cycle 60-90s × 綠燈比 ~50% → 期望停等 ~20s），未來可從 VD 在不同號誌時相的 volume 變化反推每個號誌平均 wait（Phase 2）。
5. **C 方案的真正問題**：data.taipei 號誌資料覆蓋未必涵蓋全市、且需要 join 到 OSM 號誌 node、ingest 路徑增加。對 capstone 不值得。

**實作位置：**
- Schema：`traffic_node` 加 `has_signal BOOLEAN NOT NULL DEFAULT FALSE` + partial index
- Build SQL：`scripts/build_graph_from_osm.sql` 末尾 UPDATE `traffic_node.has_signal = TRUE WHERE EXISTS (... ST_DWithin(planet_osm_point, traffic_node, 30m) AND highway='traffic_signals')`
- A*：`SIGNAL_PENALTY_HR = SIGNAL_PENALTY_SECONDS / 3600`（環境變數預設 20）；successor function 加 `g_score += SIGNAL_PENALTY_HR if (node.has_signal and neighbor_id != end_id) else 0`
- 注意：終點 node 不加（要停車本來就要等紅燈無妨）；起點靠 `g_score[start]=0` 自然處理

**Edge cases:**
- 號誌 OSM node 在 traffic_node 30m 內找不到對應（路網太稀疏 / OSM 標錯）：忽略該號誌，不影響其他路徑。
- 一個 traffic_node 對應到多個 OSM 號誌 point（罕見，例如複合路口）：`has_signal = TRUE` 即可，不重複加 penalty（penalty 是 per-node 不是 per-signal）。
- 環島 / 立體交叉：OSM 通常標 `highway=motorway_junction` 而非 `traffic_signals`，自然不被 snap 到，no-op。

## Risks / Trade-offs

- **`timescale/timescaledb-ha:pg14-all` image 跟現有 init script 不相容（低機率 / 中影響）** → 緩解：Migration 第一步先 docker-up 驗證 + `\dx` 確認 PostGIS + TimescaleDB 兩個 extension 都載入；fallback 用 `postgis/postgis:14-3.4` + 手動 `apt install timescaledb-2-postgresql-14`（同 PG14，作業可控）。**留 PG14 大幅降低跨大版風險**。
- **OSM 全台北 graph 載入後 RAM > 2GB（低機率 / 高影響：service OOM）** → 緩解：`build_graph_from_osm.sql` 做 graph simplification（合掉 degree-2 中間節點，等同 osmnx 的 simplify）；首次 import 後 `EXPLAIN (ANALYZE, BUFFERS)` benchmark；超過 500MB in-process 重新評估縮 bbox 或進一步 simplify。
- **VD endpoint URL/format 改變（低機率 / 中影響）** → 緩解：health metric 監控「最近 5 min 有讀數的 VD 數」，低於 threshold 告警；refresher try/except 不 crash service，A* 用過期 weight 仍可跑。
- **ETA 校準後仍跟 Google Maps 偏離（中機率 / 中影響）** → 緩解：acceptance 標準 ±50%（不是 ±10%），剩下差距文件化在 `eta_accuracy_followup.md`；calibration 係數對所有 highway class 都單獨算一份，不用 global magic number。
- **osm2pgsql 在 Windows native 安裝痛（高機率 / 低影響：一次性）** → 緩解：`scripts/import_taipei_osm.sh` 內呼叫 Docker container（`docker run --rm iboates/osm2pgsql` 或 `openmaptiles/openmaptiles-tools`），不要求開發者本機裝。
- **bbox padding 30% 不適合台北實際路徑分布（低機率 / 低影響）** → 緩解：retry-with-wider-bbox 接住；retry rate > 5% 觸發調整。
- **DB migration 等同 wipe & rebuild（高機率 / 低影響：當前環境只有 dev DB）** → 緩解：明確文件化 `docker compose -f infra/docker-compose.yml down -v` 為遷移第一步；現階段 DB 無生產資料、無備份需求。
- **新增 4 張表 + 改 2 張表 + 換 image，integration 測試成本上升** → 緩解：用 `testcontainers` 起隔離的 `timescaledb-ha:pg14-all` container（一次性 pull ~700MB），integration test 跑 e2e（OSM fixture → graph build → route plan）。
- **Kafka `route.response` 多 `parking_suggestions` 欄位，main-service Kotlin DTO 沒同步會 deserialize 失敗（中機率 / 中影響）** → 緩解：tasks §10.8 中明確列出 Kotlin DTO 同步 task；該欄位 default `[]` 確保 backward compat（無 parking 推薦時也是合法值）。

## Migration Plan

### 一次性遷移步驟（dev 環境，所有指令在 repo root 執行）

```bash
# 1. 停掉舊服務 + 砍 volume
docker compose -f infra/docker-compose.yml down -v

# 2. 啟動新 image (timescaledb-ha:pg14-all)，init-db scripts 自動跑
docker compose -f infra/docker-compose.yml up -d timescaledb
# 驗證: docker compose -f infra/docker-compose.yml exec timescaledb psql -U admin -d traffic_data -c "\dx"
# 預期看到: timescaledb, postgis, postgis_topology

# 3. 灌 OSM 到 PostGIS (5-10 min)
bash scripts/import_taipei_osm.sh

# 4. Build graph (~30s)
docker compose -f infra/docker-compose.yml exec -T timescaledb \
  psql -U admin -d traffic_data -f /scripts/build_graph_from_osm.sql

# 5. Seed VD 靜態 metadata (~5s, 一次性)
#    scripts/seed_vd_static.py 自包含 (用獨立 venv 或 uv run + script-level dep declaration)
uv run --script scripts/seed_vd_static.py

# 6. Snap VD 到最近 edge (~10s)
docker compose -f infra/docker-compose.yml exec -T timescaledb \
  psql -U admin -d traffic_data -f /scripts/post_build_snap_vd.sql

# 7. 啟服務 (lifespan 啟動 VD/parking refresher 背景 task)
cd backend/multiagent-service && uv run python main.py
```

`scripts/` 目錄需在 docker-compose.yml volume mount 中可見（`./../scripts:/scripts:ro`），或者用 `docker cp` 把 SQL 檔案丟進去再跑。

### Rollback

本 change 沒有平滑回退（schema、image 都換）。回退方式：

1. `git revert` 整個 commit
2. `docker compose -f infra/docker-compose.yml down -v` + `up`（會重新 init 舊 schema）
3. `bash scripts/import_taipei_osm.sh` 不需要跑（舊 import_tdx 也已 git revert 回來）
4. `cd backend/multiagent-service && uv run python main.py` lifespan 從舊 JSON snapshot seed

對 dev 環境約 5 min 內可完成。

### 與其他 change 的協調

- 不可與任何修 `traffic_edge` schema 的 change 同時進行
- Spring Boot main-service 端：本 change 對 Kafka `route.response` 多一個 `parking_suggestions` 欄位（default `[]`），DTO 需同步擴充；具體 task 在 tasks.md §10.8 列出

## Open Questions

- **OSM PBF 來源是否改用內政部 TUIC 道路中線？** 目前選 geofabrik 是因為更新快、tag 完整。TUIC 對台灣資料更權威但 tag schema 不同，要重寫 build_graph SQL。本 change 維持 geofabrik，未來若 OSM 巷弄資料品質不夠再 evaluate。
- **Search bbox padding 30% / 2km 的常數**：是否要按 origin/destination 距離自適應（短距離給更大 padding %）？本 change 用固定常數，retry 接住極端 case；retry rate 觀察一週後決定要不要做自適應。
- **VD 動態 XML 5 min 更新 vs 我們 5 min 輪詢的相位差**：可能某些 cycle 抓到上一輪的同一份資料（`ExchangeTime` 相同）。`on_conflict_do_nothing` 處理，但會浪費一次 weight rebuild CPU。可選優化：抓完先比對 `ExchangeTime` 跟前一次，相同就 skip rebuild。本 change 不做（rebuild 才幾秒成本可忽略）。
- **`available_car >= 10` 閾值**：parking_suggestions 過濾條件寫死 10，有些小型停車場永遠湊不到。Phase 2 可改成 `available_car / total_car >= 0.1` 或類似比例條件，本 change 先用簡單常數。
