## Why

目前 `backend/multiagent-service` 的 `ChatAgent.agenerate(content)` 每一輪都重建 `messages = [system, user]` 後丟 DeepSeek，**對話完全無狀態**。使用者沒辦法接續上一輪指代（「那條路再給我一次」「第二條路怎麼走」「換個更快的方案」），所有 demo 場景都被迫一次性把 origin/destination 講完整，體感不像對話、像表單。這次補上以 Redis 為儲存層的多輪對話歷史，讓 chat agent 有 context。

## What Changes

### 多輪對話歷史儲存（Redis-backed）
- 新增 `backend/multiagent-service/src/agents/chat_history.py`：`ChatHistoryStore` class，吃既有 `cache.redis_client`、`max_turns`、`ttl_seconds`。
- Redis key 設計：`chat:history:{session_id}`，型別為 LIST，每筆 entry 是 JSON 化的 `{"role": "user" | "assistant", "content": str}`。
- `load(session_id) -> list[dict]`：`LRANGE 0 -1` + JSON 反序列化。
- `append_turn(session_id, user_msg, assistant_reply)`：用 pipeline 一次做 `RPUSH` ×2 + `LTRIM -(max_turns*2) -1` + `EXPIRE ttl_seconds`，single round-trip。
- **只儲存 user / assistant 純文字訊息**：跳過 tool-call loop 中的 `assistant w/ tool_calls` 與 `tool` result 訊息，避免歷史 token 膨脹；下一輪 LLM 若仍需要路徑會自己重新呼叫 `plan_route`。

### ChatAgent 接上歷史
- `backend/multiagent-service/src/agents/chat_agent.py`:
  - `ChatAgent.__init__` 多接 `history_store: ChatHistoryStore | None = None`。
  - `agenerate(content, session_id: str | None = None)` 多接 `session_id` 參數。
  - 流程：若 `session_id` + `history_store` 都齊備 → 開頭 `load()` 出歷史 prepend 到 system 之後、本輪 user 之前；本輪結束拿到 final reply 後 `append_turn()` 寫回。
  - **`session_id` 缺省時 fall back 到現在的無狀態行為**，保留向後相容（既有測試不破）。
  - `build_chat_agent_from_env()` 從 env 建 `ChatHistoryStore` 並注入。

### Kafka chat handler 把 session_id 傳下去
- `backend/multiagent-service/src/kafka/consumer.py:handle_chat_request` 目前已從 payload 讀 `session_id`（`consumer.py:65`），但只 log；改成傳進 `chat_agent.agenerate(content, session_id=session_id)`。

### 環境變數（新增、皆有預設值，**不 BREAKING**）
- `CHAT_HISTORY_MAX_TURNS`（預設 10 ＝ 20 則訊息）
- `CHAT_HISTORY_TTL_SECONDS`（預設 1800 ＝ 30 分鐘）

## Capabilities

### New Capabilities
- `chat-history`: 以 Redis 為儲存層的 chat session 多輪對話歷史管理：載入歷史、append 新輪、`LTRIM` 限制最近 N 輪、`EXPIRE` TTL；`ChatAgent` 注入 `ChatHistoryStore` 後 per-`session_id` 維護上下文；Kafka `chat.request` handler 把 payload 中的 `session_id` 串通到 agent。

### Modified Capabilities
（無）—— 既有 `redis-caching` capability 只規範 Docker / 連線基礎設施，不涉及 chat domain；本次新邏輯歸新 capability 而非延伸 `redis-caching`。`astar-routing` / `kafka-messaging` 等 capability 的 requirements 不變動。

## Impact

### Affected code
- 新增：`backend/multiagent-service/src/agents/chat_history.py`
- 修改：`backend/multiagent-service/src/agents/chat_agent.py`（`__init__` + `agenerate` 簽章變動，皆向後相容）
- 修改：`backend/multiagent-service/src/kafka/consumer.py`（`handle_chat_request` 多傳 `session_id`）
- 新增測試：`backend/multiagent-service/tests/test_chat_history.py`
- 新增 / 擴充測試：`backend/multiagent-service/tests/test_chat_agent.py`

### APIs / 介面
- 內部：`ChatAgent.__init__(history_store=None)` 多 keyword-only 參數，預設 None → 向後相容
- 內部：`ChatAgent.agenerate(content, session_id=None)` 多 keyword 參數，預設 None → 向後相容
- Kafka wire schema：`chat.request` payload **未新增欄位**（`session_id` 早已存在）；`chat.response` 未變動 → **不 BREAKING**

### Dependencies
- 不新增 runtime 依賴（`redis>=7.1.0` 已在 `pyproject.toml:17`）
- 新增 dev 依賴：`fakeredis`（透過 `uv add fakeredis --dev` 加入，給 `tests/test_chat_history.py` 用）

### Infra / DB
- 不動 `infra/docker-compose.yml`（Redis service `infra/docker-compose.yml:26` 已就位）
- 不動 DB schema

### 不在本次範圍（明確 non-goal）
- 上一輪 `route_payload` 持久化（使用者勾選範圍時未納入）
- 使用者偏好 profile（家/公司地址、常去地點）— 留待未來 change
- 跨 session 的長期記憶 / summary 壓縮（10 輪窗 + TTL 對 capstone demo 已足夠）
- 任何 frontend 改動（`session_id` 是後端 / Kafka 既有欄位，前端工作流不變）
