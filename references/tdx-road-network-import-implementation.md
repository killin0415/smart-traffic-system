# tdx-road-network-import-impl

> 這份文件記錄了 `tdx-road-network-import` 這個 change 的實作細節，
> 目的是幫助理解「為什麼這樣做」以及「每個元件怎麼串起來的」。

---

## 1. 為什麼要做這個 Change？

Route Agent 需要高雄市的路網圖資才能執行 A* 路徑規劃。當時 TimescaleDB 已就緒（`infra/init-db/01-init-timescaledb.sql` 啟用了 extension），但裡面沒有任何路網資料，也沒有取得外部圖資的機制。

**資料來源**：TDX（運輸資料流通服務），政府開放交通資料平台，提供高雄市路段 API。

**問題**：
- TDX 免費方案有 API 頻率限制，不能每次啟動都即時呼叫
- 原定使用的 `Basic/RoadSection` API 已回傳 404（下架了）
- 路網是靜態資料，不需要即時更新

**決定**：**抓取一次 → 存成 JSON 快照 commit 進 repo → 服務啟動時自動從 JSON seed 進 DB。**

這樣任何人 `docker-compose up` 就能擁有完整路網，完全不依賴 TDX API 可用性。

---

## 2. TDX API 選擇（踩過的坑）

### 2.1 原定方案失敗

原本要用的 endpoint：
```
GET /api/basic/v2/Road/RoadSection/City/Kaohsiung
```
2026-04-06 實測直接回 404，**已下架**。

### 2.2 替代方案：Section + SectionShape

最終用兩支 API 組合取代：

| API | 路徑 | 提供什麼 |
|-----|------|----------|
| **Section** | `/api/basic/v2/Road/Traffic/Section/City/Kaohsiung` | 路段基本資訊：SectionID、路名、路長、起終點座標、RoadClass |
| **SectionShape** | `/api/basic/v2/Road/Traffic/SectionShape/City/Kaohsiung` | 路段完整幾何線（WKT LINESTRING），前端繪製用 |

兩支 API 透過 `SectionID` 可以 join。

### 2.3 為什麼需要兩支 API？

- **Section API** 只給起終點兩個座標 → 足夠 A* 用，但前端畫路線會是直線，品質很差
- **SectionShape API** 給完整的折線點位 → 前端繪製用
- 對 A* 路徑規劃本身，只需要 Section 的起終點座標 + 路長 + 速限

### 2.4 沒有速限怎麼辦？

Section API **不提供速限欄位**（原本的 RoadSection API 有），所以用 `RoadClass` 推估：

```python
def _infer_speed_limit(road_class: int) -> int:
    return {
        0: 110,  # 國道
        1: 70,   # 省道
        2: 80,   # 快速道路
        3: 70,   # 市區快速
        4: 60,   # 縣道
        5: 50,   # 鄉道
        6: 50,   # 市區道路
    }.get(road_class, 40)  # 未知類型預設 40 km/h
```

**為什麼可以接受？** 這只是 base weight，Phase 2 可用 SectionLink 即時車速校正。粗略推估在初期夠用。

---

## 3. 整體資料流

```
[.env]             [TDX OAuth2]             [TDX Section API]      [TDX SectionShape API]
  |                     |                          |                         |
  | CLIENT_ID/SECRET    |                          |                         |
  |-------------------->|                          |                         |
  |                     | access_token             |                         |
  |                     |------------------------->|                         |
  |                     |                          | 路段基本資訊（N筆）       |
  |                     |                          |------------------------>|
  |                     |                          |                         | WKT 幾何線
  |                     |                          |                         |
  v                     v                          v                         v
                   import_tdx_road_network.py
                   ┌──────────────────────────────────────────┐
                   │ 1. OAuth2 認證取得 token                   │
                   │ 2. 分頁抓取 Section（所有高雄路段）          │
                   │ 3. 分頁抓取 SectionShape（幾何線）          │
                   │ 4. Bounding box 過濾                      │
                   │ 5. RoadClass → 速限推估                   │
                   │ 6. SectionID join 合併幾何                 │
                   │ 7. 輸出 JSON                              │
                   └──────────────────────────────────────────┘
                                    |
                                    v
                   data/kaohsiung_road_sections.json（commit 進 repo）
                                    |
                                    v
               ┌─── multiagent-service 啟動 ───┐
               │ lifespan() → seed_road_network()│
               │ 1. 查 traffic_node 是否為空       │
               │ 2. 空 → 讀 JSON → 解析 → 寫 DB   │
               │ 3. 不空 → 跳過                    │
               └──────────────────────────────────┘
                                    |
                                    v
                   TimescaleDB (traffic_node + traffic_edge)
                                    |
                                    v
                   Route Agent A* 路徑規劃（Phase 2 使用）
```

---

## 4. TDX 抓取 Script（`scripts/import_tdx_road_network.py`）

這支 script 是獨立執行的，不是服務的一部分。跑完一次就好，產出的 JSON 會 commit 進 repo。

### 4.1 OAuth2 認證

```python
def get_access_token(client_id: str, client_secret: str) -> str:
    response = httpx.post(
        TDX_AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    return response.json()["access_token"]
```

TDX 用標準的 **OAuth2 Client Credentials Flow**：
1. 用 `client_id` + `client_secret` 向 TDX token endpoint 發 POST
2. 拿到 `access_token`，後續 API 呼叫放在 `Authorization: Bearer <token>` header

**環境變數讀取**：同時支援 `TDX_CLIENT_ID`（底線）和 `TDX-CLIENT-ID`（連字號）兩種格式，因為 `.env` 檔案通常用連字號但 shell 不接受。

### 4.2 分頁抓取

```python
def fetch_sections(access_token: str) -> list[dict]:
    all_sections = []
    skip = 0
    page_size = 100

    while True:
        params = {"$top": str(page_size), "$skip": str(skip), "$format": "JSON"}
        time.sleep(REQUEST_DELAY_SEC)  # 尊重 rate limit
        response = httpx.get(TDX_SECTION_URL, headers=headers, params=params, timeout=60)

        if response.status_code == 429:
            time.sleep(10)  # rate limited，等 10 秒重試
            continue

        sections = data.get("Sections", [])
        if not sections:
            break

        all_sections.extend(sections)
        if len(sections) < page_size:
            break
        skip += page_size

    return all_sections
```

**TDX 分頁用的是 OData 風格**：`$top`（每頁幾筆）+ `$skip`（跳過幾筆）。

**Rate limit 處理**：
- 每次請求間隔 2 秒（`REQUEST_DELAY_SEC`）
- 收到 429 就等 10 秒後重試，不直接失敗

SectionShape API 的抓取邏輯完全相同，只是 response 結構不同（`SectionShapes` 陣列），回傳 `{SectionID: WKT}` dict。

### 4.3 Bounding Box 過濾

```python
BBOX_SW = (22.600, 120.270)  # 西南角 (lat, lng)
BBOX_NE = (22.660, 120.340)  # 東北角 (lat, lng)

def in_bbox(lat: float, lon: float) -> bool:
    return BBOX_SW[0] <= lat <= BBOX_NE[0] and BBOX_SW[1] <= lon <= BBOX_NE[1]
```

**為什麼要在 Python 端過濾？** TDX API 不支援伺服器端的空間篩選，只能先抓全部再本地過濾。

**過濾邏輯**：路段的起點或終點**至少一端**在 bounding box 內就保留。這樣邊界上的路段也會被納入。

範圍是高雄車站周邊約 6km × 7km，實測抓到 **154 筆路段**。

### 4.4 JSON 輸出結構

```json
{
  "metadata": {
    "source": "TDX Section + SectionShape API",
    "city": "Kaohsiung",
    "bounding_box": {
      "sw": {"latitude": 22.600, "longitude": 120.270},
      "ne": {"latitude": 22.660, "longitude": 120.340}
    },
    "fetched_at": "2026-04-06T...",
    "count": 154
  },
  "road_sections": [
    {
      "RoadSectionID": "L_6190010300020E",
      "RoadName": "一心一路(民生路(南)~中華路(南))",
      "geometry": [[120.316734, 22.60858], [120.320724, 22.606825]],
      "RoadLength": 454.5,
      "SpeedLimit": 50,
      "geometry_wkt": "LINESTRING(120.316734 22.60858, ...)"
    }
  ]
}
```

**注意**：
- `geometry` 是 `[lng, lat]` 順序（GeoJSON 慣例），`RoadLength` 單位是公尺
- `geometry_wkt` 只有在 SectionShape 有對應資料時才存在
- `SpeedLimit` 是從 RoadClass 推估的，不是 TDX 直接提供的

---

## 5. DB Schema 設計

### 5.1 建表 SQL（`infra/init-db/02-road-network-tables.sql`）

```sql
CREATE TABLE IF NOT EXISTS traffic_node (
    id          SERIAL PRIMARY KEY,
    latitude    DOUBLE PRECISION NOT NULL,
    longitude   DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS traffic_edge (
    id              SERIAL PRIMARY KEY,
    source_node_id  INTEGER NOT NULL REFERENCES traffic_node(id),
    target_node_id  INTEGER NOT NULL REFERENCES traffic_node(id),
    road_name       VARCHAR(255),
    length_km       DOUBLE PRECISION NOT NULL,
    speed_limit_kmh INTEGER NOT NULL,
    base_weight     DOUBLE PRECISION NOT NULL
);
```

### 5.2 為什麼用一般 table 而不是 hypertable？

TimescaleDB 的 hypertable 是為**時間序列資料**設計的（依時間 partition 成 chunk）。路網是**靜態空間資料**，沒有時間維度，用 hypertable 反而增加查詢開銷。

未來即時路況資料（車速、壅塞度）才適合 hypertable。

### 5.3 為什麼不用 PostGIS geometry 欄位？

目前 A* 只需要 `latitude`/`longitude` 兩個 float。PostGIS 的 `POINT` 和 `LINESTRING` 型別在這階段過度設計，Phase 4 做前端空間查詢時再考慮。

### 5.4 SQLAlchemy Model（`backend/multiagent-service/src/db/models.py`）

```python
class TrafficNode(Base):
    __tablename__ = "traffic_node"
    id = Column(Integer, primary_key=True, autoincrement=True)
    latitude = Column(Double, nullable=False)
    longitude = Column(Double, nullable=False)

class TrafficEdge(Base):
    __tablename__ = "traffic_edge"
    id = Column(Integer, primary_key=True, autoincrement=True)
    source_node_id = Column(Integer, ForeignKey("traffic_node.id"), nullable=False)
    target_node_id = Column(Integer, ForeignKey("traffic_node.id"), nullable=False)
    road_name = Column(String(255))
    length_km = Column(Double, nullable=False)
    speed_limit_kmh = Column(Integer, nullable=False)
    base_weight = Column(Double, nullable=False)

    source_node = relationship("TrafficNode", foreign_keys=[source_node_id])
    target_node = relationship("TrafficNode", foreign_keys=[target_node_id])
```

**`relationship`** 讓 ORM 可以直接 `edge.source_node.latitude` 存取 node 座標，不需要手動 join。

---

## 6. 路網解析邏輯（`backend/multiagent-service/src/db/road_network.py`）

這是整個 change 的核心演算法部分。把 JSON 裡的路段資料轉換成圖論的 **nodes + edges**。

### 6.1 資料結構

```python
@dataclass
class Coord:
    latitude: float
    longitude: float

@dataclass
class ParsedNode:
    id: int
    latitude: float
    longitude: float

@dataclass
class ParsedEdge:
    source_node_id: int
    target_node_id: int
    road_name: str
    length_km: float
    speed_limit_kmh: int
    base_weight: float
```

用 `@dataclass` 而不是 dict，讓欄位有型別提示，IDE 能自動補全和檢查。

### 6.2 Node 推導與座標去重

**問題**：TDX 只給路段（edge）資料，沒有路口（node）的概念。Node 要自己推導。

**做法**：每筆路段取出起點和終點座標作為候選 node → 用 Haversine 距離去重，20m 內合併為同一 node。

```
路段 A: 起點(22.625, 120.305) ─────────── 終點(22.630, 120.310)
路段 B: 起點(22.625001, 120.305001) ───── 終點(22.635, 120.315)
                ↑
          相距 ~0.14m < 20m → 合併為同一 node
```

```python
def deduplicate_nodes(candidates: list[Coord], tolerance_m: float = 20.0) -> list[ParsedNode]:
    nodes: list[ParsedNode] = []
    for coord in candidates:
        merged = False
        for node in nodes:
            if haversine_m(coord, Coord(node.latitude, node.longitude)) < tolerance_m:
                merged = True
                break
        if not merged:
            nodes.append(ParsedNode(id=len(nodes) + 1, latitude=coord.latitude, longitude=coord.longitude))
    return nodes
```

**演算法**：O(n²) 暴力比對。154 路段 × 2 端點 = 308 個候選，O(n²) 完全可以接受。

**為什麼 20m？**
- 太小（0m 精確比對）→ GPS 漂移就會產生假 node，路口無法連接
- 太大（50m+）→ 可能把不同路口誤合併
- 20m 小於一般路口寬度，大於 GPS 漂移，實測 308 → 154 nodes

### 6.3 Haversine 距離計算

```python
def haversine_m(a: Coord, b: Coord) -> float:
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat = lat2 - lat1
    dlng = math.radians(b.longitude - a.longitude)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h)) * 1000
```

Haversine 公式把地球當球體，算兩點間的大圓距離。在城市尺度（幾公里）誤差 < 0.3%，完全夠用。

### 6.4 Edge 建立與 base_weight

```python
def compute_base_weight(length_km: float, speed_limit_kmh: int) -> float:
    speed = speed_limit_kmh if speed_limit_kmh and speed_limit_kmh > 0 else DEFAULT_SPEED_LIMIT
    return length_km / speed
```

**`base_weight = 路段長度(km) / 速限(km/h)`，單位：小時。**

為什麼用通行時間當 weight？
- 最直覺：A* 找到的是「最快路線」而不是「最短路線」
- 未來疊加即時路況很簡單：壅塞時 `weight × 1.5`，暢通時 `weight × 0.8`
- 如果用純距離，高速公路和巷弄無法區分

**防禦性處理**：速限為 0、None 或負數時，fallback 到預設 40 km/h。

### 6.5 完整解析流程（`parse_road_network`）

```python
def parse_road_network(sections: list[dict]) -> ParsedRoadNetwork:
    # 1. 收集所有候選座標（每路段 2 端點）
    candidates = []
    for section in sections:
        start, end = _get_endpoints(section)
        candidates.append(start)
        candidates.append(end)

    # 2. 去重得到 nodes
    nodes = deduplicate_nodes(candidates)

    # 3. 為每筆路段建立 edge
    edges = []
    for section in sections:
        start, end = _get_endpoints(section)
        src_id = find_node_id(start, nodes)    # 找最近的 node
        tgt_id = find_node_id(end, nodes)      # 找最近的 node
        length_km = section["RoadLength"] / 1000
        speed_limit = section["SpeedLimit"]
        bw = compute_base_weight(length_km, speed_limit)
        edges.append(ParsedEdge(...))

    return ParsedRoadNetwork(nodes=nodes, edges=edges)
```

**`find_node_id()`**：找到與座標最近的 node ID。因為已經做過去重，同一路口的端點一定會指向同一 node。

---

## 7. 啟動時自動 Seed（`backend/multiagent-service/src/db/seed.py`）

### 7.1 觸發時機

在 FastAPI 的 `lifespan` handler 中，服務啟動時呼叫：

```python
# main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed road network if DB is empty
    async for session in get_session():
        await seed_road_network(session)

    # Start Kafka consumer as a background task
    kafka_task = asyncio.create_task(start_kafka_consumer())
    yield
```

**Seed 在 Kafka consumer 啟動之前**，確保路網資料就位後才開始接受請求。

### 7.2 Seed 邏輯

```python
async def seed_road_network(session: AsyncSession) -> None:
    # 1. 查 traffic_node 有幾筆
    result = await session.execute(select(func.count()).select_from(TrafficNode))
    count = result.scalar_one()

    # 2. 已有資料 → 跳過
    if count > 0:
        logger.info("traffic_node 已有 %d 筆資料，跳過 seed", count)
        return

    # 3. JSON 不存在 → 警告但不中斷
    if not DEFAULT_JSON_PATH.exists():
        logger.warning("JSON 快照不存在: %s — 跳過路網 seed，服務繼續啟動", DEFAULT_JSON_PATH)
        return

    # 4. 讀取 → 解析 → 寫 DB
    sections = load_road_sections()
    network = parse_road_network(sections)

    session.add_all([TrafficNode(id=n.id, ...) for n in network.nodes])
    await session.flush()   # 先 flush nodes 讓 edge 的外鍵生效
    session.add_all([TrafficEdge(...) for e in network.edges])
    await session.commit()
```

**三個關鍵設計決策**：

1. **偵測而非強制**：查 `COUNT(*)` 決定是否 seed，不會重複寫入
2. **JSON 不存在時不中斷**：`logger.warning` + `return`，開發環境可能沒有 JSON 但服務仍然能跑
3. **先 flush 再寫 edges**：`traffic_edge` 有外鍵指向 `traffic_node`，必須確保 node 先寫進 DB

---

## 8. 實測結果

### 8.1 TDX 抓取結果（2026-04-06 執行）

```
Section API:      ~1800 筆原始路段
Bounding box 過濾: 154 筆保留
SectionShape:     154 筆中大部分有完整幾何
解析後:           154 nodes, 154 edges
```

### 8.2 端對端驗證

```bash
# 1. 啟動 DB
docker compose up -d timescaledb

# 2. 確認建表 SQL 執行（init-db mount 到 /docker-entrypoint-initdb.d/）
docker exec -it timescaledb psql -U postgres -d traffic_db -c "\dt"
# → traffic_node, traffic_edge 兩張表

# 3. 啟動 multiagent-service
cd backend/multiagent-service && uv run python main.py
# → 看到 "traffic_node 為空，開始從 JSON seed 路網資料..."
# → 看到 "路網 seed 完成：154 nodes, 154 edges"

# 4. 確認資料
docker exec -it timescaledb psql -U postgres -d traffic_db \
  -c "SELECT COUNT(*) FROM traffic_node; SELECT COUNT(*) FROM traffic_edge;"
# → 154, 154
```

---

## 9. Unit Tests

### 9.1 測試檔案：`backend/multiagent-service/tests/test_road_network.py`

| 測試類別 | 測試數 | 涵蓋內容 |
|----------|--------|----------|
| `TestLoadRoadSections` | 2 | JSON 解析、geometry 保留 |
| `TestDeduplicateNodes` | 4 | 近距離合併、遠距離獨立、多群組、空輸入 |
| `TestComputeBaseWeight` | 3 | 正常計算、不同數值、精度驗證 |
| `TestComputeBaseWeightDefaultSpeed` | 3 | 速限 0/None/負數時 fallback 40 |
| `TestParseRoadNetwork` | 3 | 端對端解析、weight 正確、Haversine 合理 |

**共 15 個測試**。

### 9.2 測試設計重點

**共用路段的 node 合併**：RS001 終點 `(120.305, 22.625)` == RS002 起點 `(120.305, 22.625)`，所以 2 路段 4 端點 → 3 個獨立 node。

```python
SAMPLE_SECTIONS = [
    {"RoadSectionID": "RS001", "geometry": [[120.300, 22.620], [120.305, 22.625]], ...},
    {"RoadSectionID": "RS002", "geometry": [[120.305, 22.625], [120.310, 22.630]], ...},
]

def test_end_to_end_parsing(self):
    network = parse_road_network(SAMPLE_SECTIONS)
    assert len(network.nodes) == 3   # 4 端點去重後 3 個 node
    assert len(network.edges) == 2   # 2 路段 = 2 edges
```

### 9.3 執行方式

```bash
cd backend/multiagent-service && uv run pytest tests/test_road_network.py -v
```

---

## 10. 依賴清理（順便做的）

趁這個 change 清理了 `pyproject.toml` 中一堆沒在用的依賴：

**移除**：`ultralytics`、`opencv`、`pyautogen`、`openai`、`langchain`、`networkx`、`psycopg2-binary`、`elasticsearch`

**新增**：`google-genai`（Gemini SDK，後續 LLM 整合用）

這些是早期探索時加的，現在方向確定了就清掉，減少不必要的安裝時間和潛在衝突。

---

## 11. 重要觀念總結

| 觀念 | 說明 |
|------|------|
| **OAuth2 Client Credentials** | 用 client_id + secret 直接換 token，適合 server-to-server，不需要使用者介入 |
| **OData 分頁** | `$top` + `$skip` 風格，TDX API 採用此模式 |
| **Bounding Box** | 用矩形框住目標區域，過濾不在範圍內的資料 |
| **Haversine 公式** | 球面三角學，算地球上兩點距離。城市尺度夠精準 |
| **Node Snap Tolerance** | 在座標去重時允許的容差距離。太小→假 node，太大→誤合併 |
| **base_weight** | 圖論 edge 的權重，這裡用通行時間（距離/速限），A* 會用它找最快路線 |
| **Seed 策略** | 啟動時偵測 → 空就寫 → 不空就跳過。簡單且冪等 |
| **靜態表 vs Hypertable** | 路網不隨時間變化，不適合時間序列的 hypertable |
| **`session.flush()` vs `commit()`** | flush 只把資料送到 DB（讓外鍵生效）但不提交事務，commit 才真正持久化 |

---

## 12. 已知限制與未來改進

| 限制 | 影響 | 未來改進 |
|------|------|----------|
| 速限是推估值 | 同類道路實際速限可能不同 | Phase 2：SectionLink 即時車速校正 |
| 154 nodes/edges 路網連通性未驗證 | A* 可能找不到某些路徑 | Phase 2：實作 A* 時驗證連通性 |
| 幾何線只存在 JSON 沒入 DB | 前端繪製路線需讀 JSON | Phase 4：決定是否入庫 |
| Node 去重 O(n²) | 大量路段時效能差 | 目前 154 筆不是問題。若需可改用 spatial hashing |
| JSON 快照是靜態的 | 路網變更需手動重跑 script | 路網短期不會大幅變動，可接受 |
