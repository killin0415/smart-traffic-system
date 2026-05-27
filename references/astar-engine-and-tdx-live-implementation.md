# astar-engine-and-tdx-live-impl

> 這份文件記錄 `astar-engine-and-tdx-live` 這個 change 的實作細節，
> 目的是幫助理解「A\* 引擎 + TDX Live + 測速照相機 + Geocoding 怎麼串起來」，
> 以及實作過程中踩到的坑（特別是 TDX Live City endpoint 不支援高雄）。
>
> 變更範圍：`backend/multiagent-service/` + `infra/init-db/`。

---

## 1. 為什麼要做這個 Change？

前一個 change (`tdx-road-network-import`) 只把靜態路網 seed 進 DB，multiagent-service 的 Kafka handler 收到 `route.request` 仍然回 stub。

這個 change 的目標是：**讓 `route.request` 真的能回出一條路徑**。要達到這件事，需要四個子系統同時到位：

| 子系統 | 負責 | 落點 |
|--------|------|------|
| **A\* 引擎** | 從 DB 載入路網 → 建 in-memory graph → 算 top-K 路徑 | `src/agents/routing.py` |
| **TDX Live 整合** | 即時車速拉進來 → 動態調整 edge weight | `src/agents/traffic.py` |
| **測速照相機** | 靜態資料 seed → 路徑上附帶沿途相機 | `src/db/speed_camera.py` |
| **Geocoding** | 把「夢時代」這種自然語言地名轉成經緯度 | `src/agents/geocoding.py` |

換句話說：做完這個 change，你對 Kafka 丟一個「從 A 經緯度到 B 經緯度」的 request，multiagent 就能吐出帶測速相機資訊的 top-3 路徑；而且路徑規劃用的 edge weight 會隨 TDX 即時車速動態變化。

---

## 2. 整體流程（Request 進來時發生什麼）

```
Kafka route.request ┐
                    │
                    ▼
         plan_optimal_route(session, graph, o_lat, o_lng, d_lat, d_lng)
                    │
   ┌────────────────┼────────────────┐
   ▼                ▼                ▼
 snap_to_graph   snap_to_graph    （背景定時執行）
  (origin)       (destination)     run_periodic_refresh
   │                │                 │
   └───────┬────────┘                 ▼
           ▼                    fetch_live_section_data（TDX）
    find_top_k_routes           → update_redis_cache
    （penalty-based）            → update_timescaledb
           │                    → update_graph_weights（更新 adjacency 裡的 weight）
           ▼                          ▲
    JOIN speed_camera                 │
     （SQL where in edges）           │
           │                          │
           ▼                          │
    包成 JSON 回 Kafka  ◀─────────────┘
```

重點：**A\* 跑的永遠是 in-memory graph**（不查 DB），而 TDX Live 是定期**在背景**把 graph 的 weight 改掉。兩件事是解耦的，route.request 來的那一刻看到的就是「最新一次 refresh 結果」。

---

## 3. DB Schema 變更

路網圖本身已經在前一個 change 建好，這次**加一個欄位 + 兩張新表**。

### 3.1 `traffic_edge` 加 `tdx_section_id`

為什麼要加？TDX Live API 回的資料是以 `SectionID`（例 `L_6190010300020E`）為單位。要把 live 車速對應到我們的 edge，必須保留這個 ID。

```sql
-- infra/init-db/02-road-network-tables.sql
CREATE TABLE IF NOT EXISTS traffic_edge (
    ...
    tdx_section_id  VARCHAR(64)      -- NEW
);
CREATE INDEX IF NOT EXISTS ix_traffic_edge_tdx_section_id
    ON traffic_edge (tdx_section_id);
```

對應的 ORM 欄位在 `src/db/models.py:27`：
```python
tdx_section_id = Column(String(64), nullable=True, index=True)
```

Seed 流程（`src/db/road_network.py` → `ParsedEdge.tdx_section_id` → `src/db/seed.py`）也串起來：解析 JSON 時從 `RoadSectionID` 欄位抓出來，塞進 ORM。

### 3.2 新增 `speed_camera` 表

```sql
CREATE TABLE IF NOT EXISTS speed_camera (
    id              SERIAL PRIMARY KEY,
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    direction       VARCHAR(64),
    speed_limit     INTEGER NOT NULL,
    address         VARCHAR(255),
    nearest_edge_id INTEGER REFERENCES traffic_edge(id)  -- snap 到最近 edge
);
```

每個相機會 snap 到最近的 `traffic_edge`，所以路徑結果拿到 edge_id list 之後，一句 `WHERE nearest_edge_id IN (...)` 就能撈出沿途相機。

### 3.3 新增 `traffic_history` hypertable

這是 TDX Live 的時序存檔：

```sql
CREATE TABLE IF NOT EXISTS traffic_history (
    time            TIMESTAMPTZ      NOT NULL,
    tdx_section_id  VARCHAR(64)      NOT NULL,
    travel_speed    DOUBLE PRECISION,
    travel_time     DOUBLE PRECISION,
    PRIMARY KEY (time, tdx_section_id)
);
SELECT create_hypertable('traffic_history', 'time', if_not_exists => TRUE);
```

**為什麼要存時序？** 即時路況是 Redis 在用的（快、10 分鐘 TTL）；hypertable 是為了未來做「過去 24 小時的車速趨勢」、「尖峰非尖峰比較」這類分析。對 A\* 本身沒直接用處，但對未來的 Explainer Agent 很有用。

---

## 4. A\* 引擎（`src/agents/routing.py`）

### 4.1 資料結構

```python
class RoadGraph:
    nodes: dict[int, GraphNode]                               # node_id → GraphNode
    edges: dict[int, GraphEdge]                               # edge_id → GraphEdge
    adjacency: dict[int, list[tuple[int, int, float]]]        # node_id → [(neighbor_id, edge_id, dynamic_weight)]
    section_to_edge: dict[str, int]                           # tdx_section_id → edge_id
    max_speed_kmh: int                                        # 全圖最高速限（A* heuristic 用）
```

為什麼不用 NetworkX？

- 高雄路網規模只有幾千 node，自己寫 dict 記憶體更省、debug 更直觀
- A\* 本身的迴圈只有 ~40 行，沒有必要多一層依賴
- **關鍵**：我們需要「動態 patch 某條 edge 的 weight」（TDX Live 回來時），自己寫的 adjacency list 可以 O(degree) 找到要改的那格；NetworkX 會多層抽象

### 4.2 載入流程（`RoadGraph.from_db`）

```python
@classmethod
async def from_db(cls, session):
    graph = cls()
    # 1. 載入所有 node
    for n in await session.execute(select(TrafficNode)).scalars():
        graph.nodes[n.id] = GraphNode(...)
    # 2. 載入所有 edge，雙向寫入 adjacency（TDX Section 沒有方向資訊，視為雙向）
    for e in await session.execute(select(TrafficEdge)).scalars():
        graph.adjacency[e.source_node_id].append((e.target_node_id, e.id, e.base_weight))
        graph.adjacency[e.target_node_id].append((e.source_node_id, e.id, e.base_weight))
        # 建立 tdx_section_id 反查表
        if e.tdx_section_id:
            graph.section_to_edge[e.tdx_section_id] = e.id
```

這個 method 只在 service 啟動時跑一次（在 `main.py` lifespan），產物存在 lifespan context 裡給 Kafka handler 用。

### 4.3 A\* 本身

```python
def astar(graph, start_id, end_id, weight_overrides=None):
    # Heuristic: 直線距離 / 全圖最高速限
    # → admissible (永遠低估，A* 保證找到最佳解)
    # → Congestion 反映在 g(n)，不放 heuristic 裡
    def h(node_id):
        return haversine_km(n.lat, n.lng, end.lat, end.lng) / max_speed

    # 標準 A* with heapq
    ...
```

兩個設計決定值得記：

1. **heuristic 用 distance / max_speed 而非 distance / speed_limit**：每條 edge 速限不同，但 heuristic 只看「從當前點到終點還要多久」，所以除以**全圖最高速限**（最樂觀估計）才保證 admissible。
2. **`weight_overrides` 參數**：不是修改 adjacency，而是在 A\* 執行時套用臨時 weight。這是為了 top-K 的 penalty 機制（見下）。

### 4.4 Top-K 怎麼算（重要但簡單的 hack）

```python
def find_top_k_routes(graph, start, end, k=3, penalty=3.0):
    overrides = {}
    for _ in range(k):
        result = astar(graph, start, end, weight_overrides=overrides)
        if result is None: break
        # 用原始 graph 重算真實 cost（overrides 是「假」的）
        real_cost = sum(graph.get_weight(eid) for eid in edges)
        results.append((nodes, edges, real_cost))
        # 懲罰用過的 edge，下一輪 A* 會避開
        for eid in edges:
            overrides[eid] = overrides.get(eid, base_w) * penalty
    return sorted(results, key=lambda r: r[2])
```

**為什麼不用 Yen's K-Shortest Paths？** Yen's 很嚴謹但實作 ~100 行，而且畢專使用者感知不出「嚴格第 2 短」跟「我的 penalty 方法挑的第 2 條」的差別。penalty 方法的好處是**路徑之間差異大**（被逼著走完全不同的路），對使用者來說更有用。

回傳前會用**原始 weight 重新排序**，這樣最佳路徑永遠在最前面——penalty 只是用來「產生候選」，不是用來決定排名。

### 4.5 Snap to graph（GPS → 最近 node）

```python
def snap_to_graph(lat, lng, graph, k=3):
    # 找最近 K 個 node，優先選 degree 高的（交叉路口而非 dead end）
```

**為什麼優先高 degree？** 因為使用者點的地點很少剛好在 dead end。選一個交叉路口當起點，A\* 有更多方向可以選，結果更合理。

---

## 5. TDX Live 整合（`src/agents/traffic.py`）

### 5.1 設計意圖

三件事在同一輪 refresh 裡發生：

| 動作 | 為誰做 |
|------|--------|
| `update_redis_cache` | 給**其他 agent** 讀「當前這條 section 多快」 |
| `update_timescaledb` | 給**未來分析**存歷史 |
| `update_graph_weights` | 給**A\* 引擎**用最新的塞車狀況算路徑 |

這三個動作在 `refresh_traffic_data()` 裡串起來，由 `run_periodic_refresh()` 每 5 分鐘跑一次（間隔由 `TDX_LIVE_REFRESH_SECONDS` env 控制）。

### 5.2 Congestion factor 公式

這是 A\* 引擎看到的「塞車數字」：

```python
def _congestion_factor(speed_limit, current_speed):
    if current_speed is None:
        return 1.0                         # 沒資料 → 當暢通
    if current_speed <= 0:
        return MAX_CONGESTION_FACTOR       # 感測器壞掉 → 當最塞
    if speed_limit <= 0:
        return 1.0
    return min(speed_limit / current_speed, MAX_CONGESTION_FACTOR)  # 10.0
```

然後 `graph.update_weight(edge_id, factor)` 把 `base_weight × factor` 寫回 adjacency。

**物理意義**：speed_limit=50、current_speed=10 → factor=5 → 這條路通過時間是暢通時的 5 倍。**MAX=10** 是為了避免極端值讓 A\* 把這條路當作「不可達」永遠繞開。

### 5.3 ⚠️ 踩到的大坑：TDX Live City endpoint 不支援高雄

**原本以為**：`basic/v2/Road/Traffic/Live/City/Kaohsiung` 會回高雄市所有 section 的即時車速。

**實際**：該 endpoint 回 HTTP 400 + `"City: 'Kaohsiung' is not accepted but YilanCounty, HsinchuCounty..."`。高雄**不在** basic 方案的支援清單裡。

**解決策略**（這個 change 只做到「優雅降級」）：

```python
if response.status_code == 400 and "is not accepted" in response.text:
    if not _unsupported_city_logged:
        logger.warning("TDX Live City endpoint does not support Kaohsiung — refresher will no-op.")
        _unsupported_city_logged = True
    return []
```

- 第一次呼叫：log 一條 WARNING
- 後續呼叫：靜默 no-op（避免 log 洗版）
- A\* 繼續用 `base_weight` 跑，沒有即時資料只是沒有 congestion 調整，路徑規劃本身仍正常工作

**真正的解法**已開在另一個 change：`openspec/changes/vd-live-traffic/`，改用 `basic/v2/Road/Traffic/Live/VD/City/Kaohsiung`（這個對高雄有資料），拿 VD 感測器讀數再 snap 到 edge。那個 change 就是替換 `traffic.py` 的 fetch 層。

### 5.4 OAuth2 Token 快取

```python
async def get_access_token(client=None):
    if _token_cache["token"] and now < expires_at - 30:
        return cached
    # ... 呼叫 TDX auth endpoint，快取到 expires_in - 30 秒
```

`_token_cache` 是 module-level dict，同一個 process 內所有 refresh 共用。30 秒緩衝避免「剛好卡在過期瞬間」失敗。env 變數同時支援 `TDX_CLIENT_ID` 和 `TDX-CLIENT-ID`（後者是 `.env` 檔的慣例格式）。

### 5.5 .env 從哪載入？

```python
# main.py 最上面
_env_path = Path(__file__).resolve().parents[2] / ".env"   # repo 根目錄
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        ...os.environ.setdefault(key, value)
```

**為什麼不用 python-dotenv？** 避免多一個依賴，以及 `.env` 在**repo 根目錄**（`E:/smart-traffic-system/.env`）不在 service 目錄，手寫 parser 比 python-dotenv 的預設搜尋路徑直接。`setdefault` 保證不覆蓋已存在的環境變數（Docker 等外部注入優先）。

---

## 6. 測速照相機（`src/db/speed_camera.py`）

### 6.1 資料來源與篩選

- CSV 放在 `data/speed_cameras.csv`（政府開放資料 dataset 6489）
- 篩選條件：`縣市` 欄包含「高雄」**AND** 「測照型式」包含「超速」
- 預期筆數：9 筆（高雄三民區科技執法固定點位，超速 + 闖紅燈兼超速；過濾掉「闖紅燈」「違規左轉」等非速度相關）

### 6.2 CSV 欄位命名的混亂

台灣開放資料同一份 CSV 不同年份欄位可能不一樣，所以用 **alias list**：

```python
_LAT_KEYS = ("Latitude", "緯度", "座標緯度", "PositionLat", "lat", "Y")
_LNG_KEYS = ("Longitude", "經度", "座標經度", "PositionLon", "lng", "lon", "X")
# ... etc
```

`_pick(row, _LAT_KEYS)` 會依序試，找到第一個非空欄位就回傳。新增欄位別名時加在 tuple 裡即可。

### 6.3 Snap camera to edge 的距離計算

```python
def snap_camera_to_edge(cam, edges, node_coords):
    # 每條 edge 取兩端點，用兩端點到相機距離的 min 當作「edge 到相機距離」
```

**為什麼只看端點不做 point-to-segment？** 高雄都市路段平均長度幾百公尺，端點近似跟真正投影點距離差不多，但實作從 ~10 行變 ~3 行。如果 edge 很長（高速公路），這個近似會爛掉——但目前 bounding box 過濾掉了絕大多數高速路段。

### 6.4 Seed 規則：空表才跑

```python
count = (await session.execute(select(func.count()).select_from(SpeedCamera))).scalar_one()
if count > 0:
    logger.info("speed_camera 已有 %d 筆資料，跳過 seed", count)
    return
```

這是跟 road_network seed 一致的慣例：**空表 seed、非空跳過**。要重新 seed 必須手動清表（`TRUNCATE speed_camera`）。

---

## 7. Geocoding（`src/agents/geocoding.py`）

### 7.1 為什麼要有這個？

Chat Manager 收到「我想從夢時代去漢神巨蛋」時，需要把「夢時代」「漢神巨蛋」轉成經緯度才能丟給 `plan_optimal_route`。

### 7.2 選擇 Nominatim（OSM）而非 Google Places

| 選項 | 為什麼 |
|------|--------|
| **Nominatim ✅** | 免費、無 API key、台灣大型 POI 準確度足夠 demo |
| Google Places | 最準但要 billing 設定，畢專不划算 |
| TGOS | 台灣地址最準但要申請帳號，麻煩 |

### 7.3 Rate limit 自我約束

Nominatim 官方政策是 1 req/sec。直接 spam 會被 ban IP。實作上用 asyncio.Lock + 計時器：

```python
async with _rate_lock:
    await _wait_for_rate_limit()   # 等到距上次請求 >= 1 秒
    # ... 發 HTTP request
    _mark_request_time()
```

還有一個小地方：**自動附加「高雄」**。因為 Nominatim 對「夢時代」這種全球模糊字串，有時會 resolve 到別的國家，加上「高雄」大幅提升精度。

### 7.4 User-Agent

```python
USER_AGENT = "smart-traffic-system/0.1 (capstone-project; contact: swordfire1000@gmail.com)"
```

Nominatim 要求自訂 User-Agent（匿名 or 預設 UA 會被 block）。這是服從規則也是讓他們封我們時有辦法聯絡。

---

## 8. 啟動流程（`main.py`）

lifespan 的順序**很重要**：

```python
async def lifespan(app):
    # 1. 載入 .env（最早，因為其他模組啟動時就要讀 env）
    # 2. seed_road_network（空表才跑）
    # 3. seed_speed_cameras（要在 road_network 之後，因為要 snap 到 edge）
    async for session in get_session():
        await seed_road_network(session)
        await seed_speed_cameras(session)

    # 4. 載入 in-memory RoadGraph（要在 seed 之後，否則圖是空的）
    async with async_session() as session:
        graph = await RoadGraph.from_db(session)

    # 5. 把 graph + loop + session_factory 分享給 Kafka consumer
    kafka_runtime.set_runtime(graph=graph, loop=..., session_factory=async_session)

    # 6. 啟動 Kafka consumer (背景 task)
    kafka_task = asyncio.create_task(start_kafka_consumer())

    # 7. 啟動 TDX Live refresher (背景 task，間隔 5 min)
    traffic_task = asyncio.create_task(run_periodic_refresh(graph, async_session))

    yield

    # Shutdown: cancel 兩個背景 task
```

**兩個 task 的 cancel 必須 try/except CancelledError**，否則 shutdown 會卡住。

---

## 9. 如何擴充 / debug

### 9.1 我想改 top-K 數量或 penalty

`plan_optimal_route(..., k=3)` 的 k 參數；penalty 是 `find_top_k_routes(..., penalty=3.0)`。這兩個常數也在 `routing.py` 開頭：`DEFAULT_TOP_K`、`DEFAULT_PENALTY`。

### 9.2 我想看 Redis 現在有什麼 live 資料

```bash
docker exec -it <redis> redis-cli
> KEYS traffic:section:*
> GET traffic:section:L_6190010300020E
# → {"travel_speed": 35.0, "travel_time": 120.0, "updated_at": "..."}
```

（注意 Kaohsiung 目前 fetch 都 no-op，Redis 不會有資料——VD change 合併後才會。）

### 9.3 我想檢查 A\* 真的用了即時 weight

```python
# REPL:
await graph.get_weight(edge_id)   # 回現在的 dynamic_weight
graph.edges[edge_id].base_weight  # 回原始值
```

兩者不同 = congestion factor 有套用。

### 9.4 我想換 TDX Live 資料源（預期：很快要做）

去 `openspec/changes/vd-live-traffic/` 看 proposal + design + tasks。簡單說只需改 `traffic.py` 的 `fetch_live_section_data` 一支 function，下游的 Redis/TimescaleDB/graph weight 流程完全不用動（這正是這個 change 刻意做的介面設計）。

### 9.5 unit test 長什麼樣

位於 `tests/`：
- `test_routing.py` — 用手建的小圖驗證 A\* 正確性 + top-K 差異性
- `test_speed_camera.py` — CSV 解析 + snap-to-edge + Kaohsiung 篩選
- `test_geocoding.py` — mock Nominatim API 回傳格式
- `test_road_network.py` — 已更新為驗證 ParsedEdge 含 `tdx_section_id`

執行：
```bash
cd backend/multiagent-service
uv run pytest
```

---

## 10. 驗收清單

| 項目 | 怎麼驗 |
|------|--------|
| 路網載入成功 | service 啟動 log 看到 `RoadGraph loaded: N nodes, M edges, max_speed=X km/h` |
| 測速相機 seed | `SELECT COUNT(*) FROM speed_camera` > 0，且都有 `nearest_edge_id` |
| TDX Live 降級正常 | 啟動後只看到一次 `TDX Live City endpoint does not support Kaohsiung` WARNING，之後靜默 |
| A\* 真的跑 | 丟一個 `route.request` 到 Kafka，consumer log 看到 `plan_optimal_route` 被呼叫，回 `routes` array |
| Geocoding 可用 | `geocode_location("夢時代")` 回「前鎮區中華五路」附近座標 |
| 所有 unit test 綠色 | `uv run pytest` 全過 |

---

## 11. 跟前一個 change 的串接點

| 前一個 change 產出 | 這個 change 怎麼用 |
|----|----|
| `traffic_node` / `traffic_edge` 表 | `RoadGraph.from_db` 全表掃進 memory |
| `ParsedEdge` 的 `tdx_section_id` | seed 進 `TrafficEdge.tdx_section_id`；A\* 載入時建 `section_to_edge` 反查表 |
| `data/kaohsiung_road_sections.json` | 不直接讀，但經由 seed 流程最終進 `traffic_edge` |

---

## 12. 跟下一個 change 的交接點（`vd-live-traffic`）

這個 change 留了三個**明確的擴充點**給 VD：

1. **`TrafficEdge.tdx_section_id`** 已存好——VD snap 到 edge 之後拿來當 Redis key
2. **`refresh_traffic_data()` 的三個下游**（Redis / TimescaleDB / graph weight）**介面不變**——VD change 只需改上游的 fetch + aggregate
3. **`_unsupported_city_logged` 降級分支**——VD change 會**刪掉**這段，因為 VD endpoint 對高雄可用

如果你現在要做 VD，重點就是把 `fetch_live_section_data` 換成 `fetch_live_vd_data` + `aggregate_edge_speeds`，其他維持不變。

---

## 13. 踩坑整理（給未來的自己）

| 坑 | 症狀 | 解法 |
|----|------|------|
| TDX Live City 不支援高雄 | HTTP 400 + `"is not accepted"` | 優雅降級（log 一次 → 靜默），VD 改由另一個 change 處理 |
| `.env` 在 repo 根，不在 service 目錄 | `TDX_CLIENT_ID` 讀不到 | `main.py` 手動 parse `parents[2]/.env` |
| env 變數命名不一致 | 有人寫 `TDX_CLIENT_ID`，TDX 官方範例寫 `TDX-CLIENT-ID` | `os.getenv("TDX_CLIENT_ID") or os.getenv("TDX-CLIENT-ID")` |
| Python 直接叫 `.venv/Scripts/python.exe` | 違反 `CLAUDE.md` 規則 | **一律** `uv run python ...` / `uv run pytest` |
| A\* heuristic 用 edge 速限而非全圖最高 | 可能 overestimate → 不 admissible → 找不到最佳解 | heuristic 除以 `max_speed_kmh`（全圖最高）|
| penalty 太低 top-K 都一樣 | penalty=1.5 時三條路幾乎一樣 | 調高到 3.0；且用原始 weight 重排序 |
| Nominatim 被 ban | User-Agent 沒帶 + spam | 1 req/sec lock + 自訂 UA |
