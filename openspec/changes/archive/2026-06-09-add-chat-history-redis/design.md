## Context

### 現狀
- `backend/multiagent-service/src/agents/chat_agent.py:agenerate(content)` 每次被呼叫都重建 `messages = [system, user]`，**完全無狀態**。即使同一個 chat session 連續傳兩則訊息，LLM 也記不得前一輪。
- Kafka `chat.request` payload **早已帶 `session_id`**（前端 / main-service Kotlin 寫入），`backend/multiagent-service/src/kafka/consumer.py:65` 讀出來只用於 log，沒往下游傳。
- demo 場景被迫每輪訊息都自包含 origin/destination，無法做到「那條路再給我一次」「第二條怎麼走」這種指代。

### 基礎設施已就位
- `infra/docker-compose.yml:26` 已有 `redis:alpine` service + healthcheck + named volume `redis_data`，**和 multiagent-service 同個 docker network**。
- `backend/multiagent-service/pyproject.toml:17` 已宣告 `redis>=7.1.0`。
- `backend/multiagent-service/src/cache/__init__.py` 已實例化 `redis_client = redis.from_url(REDIS_URL, decode_responses=True)` 並提供 `async get_redis()`。本 change **直接重用**這個 module-level client，不再實例化。
- 既有 capability spec `redis-caching` 只規範 Docker / 連線設定，沒有 chat domain 內容，無衝突。

### Constraints
- Capstone solo dev、demo 取向：避免大量 infra 變動。
- Python 環境必須走 `uv`（`CLAUDE.md` 規則 + `backend/multiagent-service/CLAUDE.md`）。
- 既有測試（`tests/test_chat_agent.py` 若存在 / 其他依賴 `ChatAgent.agenerate` 的測試）**不能因為這個 change 而紅**。
- DeepSeek `max_tokens=500`、`max_tool_loops=3` 已經是 budget 控制，本 change 不改這些 knob。

### Stakeholders
- 主要：solo dev（你）+ 未來 demo 評審。
- 下游：前端 `ChatPanel.tsx`（已可帶 `session_id`，無需改）、main-service Kotlin Kafka producer（已寫 `session_id` 到 `chat.request`，無需改）。

## Goals / Non-Goals

**Goals:**
- `ChatAgent` 可以根據 `session_id` 載入並 append 對話歷史，做到多輪指代。
- 歷史長度有上限（最近 N 輪）+ TTL 過期，避免 Redis 無限長大。
- `session_id` 缺省時行為與現在完全一致，舊 caller / 測試不破。
- single round-trip 寫入（pipeline `RPUSH` + `LTRIM` + `EXPIRE`），避免每輪 latency 多一次往返。
- 不引入新的 runtime 依賴、不動 docker-compose、不動 DB schema。

**Non-Goals:**
- 不持久化上一輪的 `route_payload`（使用者明確未勾選此範圍；要查同一條路就讓 LLM 自己重 call `plan_route`）。
- 不做 summary / 壓縮（10 輪窗對 capstone demo 足夠；未來再來談）。
- 不做使用者偏好 profile（家/公司地址、常去地點）—— 是另一個 change。
- 不做跨 session 的長期記憶。
- 不動 frontend（`session_id` 在 wire schema 早就有）。
- 不引入 vector store / embedding。

## Decisions

### D1: 儲存層用 Redis LIST + `LTRIM` 限長

**選項：**
| 選項 | 內容 | 優 | 劣 |
|---|---|---|---|
| A | 進程內 dict + TTL eviction | 0 dep、最快 | 重啟掉、橫向擴展不行、demo 時 reload 就斷 |
| B | Redis LIST + `LTRIM` + `EXPIRE` ★ | 既有 client、原生 TTL、pipeline 一個 round-trip、容量自然受限 | 多一個外部依賴（但本就在 stack） |
| C | Redis Stream + MAXLEN | 有 ack semantics 適合 event log | 對「載入最近 N 則」overkill，本 use case 不需要 consumer group |
| D | Postgres + Timescale hypertable | 永久保存、SQL 可查 | 多一張表 + ORM + migration；過於重量 |

選 B：基礎設施成本 0（Redis 與 client 都已在），`LIST + LTRIM + EXPIRE` 是 Redis 經典「最近 N 筆 + 自然過期」recipe，pipeline 串起來一個 round-trip 即可寫完，latency 影響可忽略。

### D2: 只儲存 `user` / `assistant` 純文字訊息，跳過 tool-call loop 中的中間訊息

DeepSeek chat completion 的 messages 流可能長這樣（一輪內）：
```
system → user → assistant(tool_calls) → tool(result) → assistant(final text)
```

**選項：**
| 選項 | 內容 | 優 | 劣 |
|---|---|---|---|
| A | 整段 messages 都寫回 Redis | 下一輪可以複用工具結果，不用 re-call | tool result 含 `plan_route` 整包 routes JSON（上千 token），10 輪後歷史會非常肥；下游 LLM 拿到舊 routes 容易混淆「這是現在還是上次的路？」 |
| B | 只存 `{role: "user", content}` 跟 `{role: "assistant", content: final_text}` ★ | 歷史 token 開銷小、語意乾淨（只剩自然語言） | 下一輪若使用者問「那條路再走一次」，LLM 只能從自然語言重述推回 origin/dest，可能要重新 call `plan_route` |

選 B：歷史 token 預算可控（10 輪 user+assistant 純文字 < 1k token），語意乾淨。重新 call `plan_route` 的成本對 capstone demo 完全可忽略；route 本身也是 freshness-sensitive 的資料（路況五分鐘變一次），重 call 反而正確。

**實作備註**：在 `chat_agent.py:agenerate` 的 tool-loop 內，不要在每個 message append 時就寫 Redis；改成等整輪結束、`return` 之前才 `await history_store.append_turn(session_id, content, reply)` 把這一輪的 user 訊息 + 最終 assistant 文字寫進去。

### D3: `append_turn` 用 pipeline 做 `RPUSH + LTRIM + EXPIRE` 一個 round-trip

`redis.asyncio.client.Redis.pipeline()` 可以把多個 command 串成一個 round-trip。每輪結束時：
```python
pipe = redis.pipeline()
pipe.rpush(key, json_dumps(user_msg))
pipe.rpush(key, json_dumps(assistant_msg))
pipe.ltrim(key, -max_turns * 2, -1)
pipe.expire(key, ttl_seconds)
await pipe.execute()
```
`LTRIM key -(max_turns*2) -1` 留最近 `max_turns*2` 筆（一輪兩則）。`EXPIRE` 每輪都刷新，使用者只要持續講話 session 就不會過期；30 min 沒講話自然過期清空。

**選項**：用 Lua script 比 pipeline 略快但增加 ops 複雜度。Pipeline 對 demo 已綽綽有餘，不上 Lua。

### D4: `session_id` 缺省時保留無狀態 fallback

`agenerate(content)`（不傳 `session_id`）原本就有大量 caller（測試、直接呼叫）。本 change 把 `session_id` 設計成 keyword-only 參數預設 None：

```python
async def agenerate(self, content: str, session_id: str | None = None) -> dict[str, Any]:
    if session_id and self._history_store:
        history = await self._history_store.load(session_id)
    else:
        history = []
    ...
```

即使 caller 給了 `session_id` 但 agent 是用 `history_store=None` 建出來的（demo 時想關掉歷史），也直接退回無狀態。**任何單一條件不滿足都退回無狀態，不丟 exception**——上下文功能對 chat agent 是「best-effort enhancement」，不該因為 Redis 暫時不可用就讓整個 chat 服務癱掉。

### D5: 用 module-level `cache.redis_client`，不重新 instantiate

`backend/multiagent-service/src/cache/__init__.py:7` 已經建好 `redis_client`。`ChatHistoryStore.__init__(redis_client, ...)` 拿這個 instance 進來即可。

**為什麼不用 connection pool 各自管**：`redis.asyncio.Redis.from_url(decode_responses=True)` 本身就用 connection pool，多個邏輯模組共用同一個 client 不會搶連線。

### D6: `ChatHistoryStore` 對外是純資料層，不知道 LLM 訊息語意

把 store 設計成只認 `{role, content}` dict，不關心 `role` 必須是 `user` / `assistant`。語意過濾（跳過 `tool` / `assistant with tool_calls`）的責任在 `ChatAgent` 而非 store。好處：

- store 可單元測（fakeredis）獨立驗證 LTRIM / EXPIRE 行為。
- 未來若要存 system override / preset prompt，介面不用動。

## Risks / Trade-offs

- **Redis 暫時不可用 → 整個 chat 卡住**（低機率 / 高影響） → 緩解：`ChatHistoryStore.load` / `append_turn` 用 try/except 包起來，失敗時 log warning + 退回無狀態行為，**不讓 chat 整體 fail**。`ChatAgent.agenerate` 的 happy path 不依賴 store 成功。
- **`session_id` 重複（不同使用者拿到同個 ID）→ 對話混淆**（低機率 / 高影響） → 緩解：本 change 信任上游（main-service Kotlin / 前端）給的 ID 是 unique；ID 生成策略不在 multiagent-service 範圍。spec 也明確寫「per session_id 隔離」當作前提。
- **歷史 prepend 後 LLM token 超過 context window**（低機率 / 中影響） → 緩解：`CHAT_HISTORY_MAX_TURNS=10` × 2 則 × 平均 50 字 ≈ 1k token，遠低於 DeepSeek context limit。未來若放寬上限要加 token-aware truncation（本 change 不做）。
- **tool 訊息不存 → 下一輪複問「同一條路」會重 call `plan_route`，多花一次 API**（高機率 / 低影響） → 緩解：可接受，且 route 本就 freshness-sensitive。文件化在 D2。
- **TTL 30 min 是否合理**（中機率 / 低影響） → 緩解：env 可調。capstone demo 場景 30 min 足夠涵蓋一次完整體驗；長對話會在 30 min idle 後重來。
- **舊測試破掉（agenerate 簽章變動）**（低機率 / 中影響） → 緩解：D4 已保證向後相容（keyword-only 新參數 + 預設 None）；CI / 本地測試套件全綠才視為 done。

## Migration Plan

本 change 是純 additive，不需要 schema migration。實機上 first-time 流程：

```bash
# 1. 確認 Redis container 跑著（infra/docker-compose.yml 已有）
docker compose -f infra/docker-compose.yml up -d redis

# 2. 加 dev 依賴
cd backend/multiagent-service
uv add fakeredis --dev

# 3. （可選）在 .env 或 docker-compose 環境覆寫預設值
# CHAT_HISTORY_MAX_TURNS=10
# CHAT_HISTORY_TTL_SECONDS=1800

# 4. 重啟 multiagent-service
cd backend/multiagent-service && uv run python main.py
```

### Rollback

`git revert` 整個 commit。本 change 不動 Redis schema（key namespace 是新的 `chat:history:*`），revert 後舊 key 自然過期、不影響任何既有功能；保險起見可在 redis-cli 跑 `redis-cli --scan --pattern 'chat:history:*' | xargs -r redis-cli del` 清乾淨。

## Open Questions

- **是否要把 system 訊息也版本化進 Redis？** 目前 system prompt 是程式碼常數（`SYSTEM_INSTRUCTION`）。若日後 system prompt 想跟 session 綁（per-session A/B test），就要把 system 也存進歷史。本 change 不做。
- **`available_car`-style 條件式截斷**（例如 token 超量時保留最近 + 第一輪）：本 change 用簡單固定 `LTRIM`。未來若 prompt 變長再加複雜度。
- **Tool result 之後想存怎麼存**：未來真的要支援「複用上次路徑」時，建議另設一個 Redis HASH `chat:last_route:{session_id}` 存最近一次 `plan_route` 的輸入 / 輸出，跟對話歷史解耦（本 change 不做，列為 follow-up）。
