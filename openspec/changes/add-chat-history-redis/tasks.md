## 1. Dev 依賴與環境準備

- [x] 1.1 `cd backend/multiagent-service && uv add fakeredis --dev`，確認 `pyproject.toml` 的 `[dependency-groups]` 或 `[project.optional-dependencies]` 出現 `fakeredis`；**禁止**直接 `pip install` 或編輯 `pyproject.toml` 後手動 sync
- [x] 1.2 確認 `backend/multiagent-service/src/cache/__init__.py` 既有 `redis_client` 可用：在 repo root 跑 `docker compose -f infra/docker-compose.yml up -d redis`，再 `cd backend/multiagent-service && uv run python -c "import asyncio; from src.cache import redis_client; print(asyncio.run(redis_client.ping()))"` 預期印 `True`
- [x] 1.3 在 **repo root 的 `.env.example`**（`/Users/potato/code/claudeProject/smart-traffic-system/.env.example`，**不**在 multiagent-service/）新增 `CHAT_HISTORY_MAX_TURNS=10` 與 `CHAT_HISTORY_TTL_SECONDS=1800` 兩行（含註解標示為可選），方便部署時參考

## 2. ChatHistoryStore 實作（chat-history capability）

- [x] 2.1 新增 `backend/multiagent-service/src/agents/chat_history.py`：定義 `ChatHistoryStore` class，建構子 `__init__(self, redis_client, *, max_turns: int, ttl_seconds: int)`；module-level 常數 `KEY_PREFIX = "chat:history:"` 與 `_key(session_id) -> str` helper
- [x] 2.2 在 `chat_history.py` 實作 `async def load(self, session_id: str) -> list[dict]`：`LRANGE key 0 -1` → 對每個 entry `json.loads`；空 key 回 `[]`；`redis.exceptions.RedisError` / `ConnectionError` 用 try/except 抓住 → `logger.warning` + 回 `[]`（不向上拋）
- [x] 2.3 在 `chat_history.py` 實作 `async def append_turn(self, session_id, user_content: str, assistant_content: str) -> None`：用 `self._redis.pipeline()` 串 `RPUSH key user_json` + `RPUSH key assistant_json` + `LTRIM key -(max_turns*2) -1` + `EXPIRE key ttl_seconds` + `pipe.execute()`；失敗時 `logger.warning` 但**不丟 exception**（避免本輪 chat 反應因歷史寫入失敗而 fail）
- [x] 2.4 在 `chat_history.py` 加 `build_chat_history_store_from_env() -> ChatHistoryStore`：從 `os.getenv("CHAT_HISTORY_MAX_TURNS", "10")` 與 `os.getenv("CHAT_HISTORY_TTL_SECONDS", "1800")` 解析（用 int()，無效值 fallback 預設並 log warning）；從 `src.cache.redis_client` 取 client；回傳 `ChatHistoryStore(redis_client, max_turns=..., ttl_seconds=...)`
- [x] 2.5 派 test engineer sub-agent：在 `backend/multiagent-service/tests/test_chat_history.py` 用 `fakeredis.aioredis.FakeRedis` 寫 unit tests，覆蓋以下 scenarios（對應 spec）：
  - `load` 空 session 回 `[]`
  - `append_turn` 後 `load` 順序 = 寫入順序（最舊在前）
  - 連寫超過 `max_turns * 2` 則後 `LLEN` = `max_turns * 2` 且保留的是「最新」
  - 每次 `append_turn` 後 `TTL` 在 `(0, ttl_seconds]` 區間
  - 不同 session_id 互不污染
  - `redis_client.pipeline().execute()` 被 monkeypatch 成 raise 時，`append_turn` 不丟 exception 且 `caplog` 有 warning
  - `redis_client.lrange` 被 monkeypatch 成 raise 時，`load` 回 `[]` 且 log warning
  - 執行：`cd backend/multiagent-service && uv run pytest tests/test_chat_history.py -v` 全綠

## 3. ChatAgent 接上歷史（chat-history capability）

- [x] 3.1 修改 `backend/multiagent-service/src/agents/chat_agent.py:ChatAgent.__init__`：新增 keyword-only 參數 `history_store: "ChatHistoryStore | None" = None`，存到 `self._history_store`；保留現有 client / model / timeout 等參數順序不變，避免 positional caller 破裂
- [x] 3.2 修改 `agenerate` 簽章為 `async def agenerate(self, content: str, session_id: str | None = None) -> dict[str, Any]`；在 `messages` 初始化前後重組為：
  ```python
  history = []
  if session_id and self._history_store:
      history = await self._history_store.load(session_id)
  messages = [
      {"role": "system", "content": SYSTEM_INSTRUCTION},
      *history,
      {"role": "user", "content": content},
  ]
  ```
- [x] 3.3 在 `agenerate` 成功取得 final text reply 的兩個 return point（tool-loop 內 break 出時、迴圈正常結束時）之前，加 append 邏輯：
  ```python
  if session_id and self._history_store and reply and not _is_placeholder_reply(reply):
      try:
          await self._history_store.append_turn(session_id, content, reply)
      except Exception:
          logger.warning("append_turn failed", exc_info=True)
  ```
  其中 `_is_placeholder_reply` 用集合判定 `reply` 是否為下列任一 canned 字串之一，皆**不 append**：
  - `GENERIC_ERROR_REPLY`（timeout / 例外）
  - `FALLBACK_REPLY`（`DEEPSEEK_API_KEY` 未設）
  - `SERVICE_NOT_READY_REPLY`（runtime 未 wire）
  - `chat_agent.py:196` 的 `"（已收到您的訊息）"`（LLM 回空字串時的 placeholder）
  - `chat_agent.py:212` 的 `"（已執行工具但回覆超過內部上限）"`（`max_tool_loops` 用完時的 placeholder）

  後兩個 placeholder **不是真正的 LLM 回覆**，append 進歷史會污染下一輪 LLM 對「我剛才回了什麼」的理解。建議把這兩個字串先抽成 module-level 常數（例如 `EMPTY_CONTENT_REPLY` / `TOOL_LOOP_EXHAUSTED_REPLY`），讓 `_is_placeholder_reply` 與測試可以用常數比對而非 magic string
- [x] 3.4 **不在 tool-loop 中間 append**：在每次 LLM round-trip / 每次 tool result 處理時都不寫 Redis；只在最終 reply 確定後寫一次（spec D2 / Requirement「只 append 一輪兩則」）
- [x] 3.5 修改 `build_chat_agent_from_env()`：呼叫 `build_chat_history_store_from_env()` 取得 store，傳給 `ChatAgent(api_key=..., history_store=store)`；若 `DEEPSEEK_API_KEY` 不存在則保留現有 fallback 行為（agent `is_available == False`，store 仍可建但不會用到）
- [x] 3.6 派 test engineer sub-agent：在 `backend/multiagent-service/tests/test_chat_agent.py`（若已存在則 extend、否則新建）加入下列 unit tests，使用 `unittest.mock.AsyncMock` mock `AsyncOpenAI.chat.completions.create`、用 `fakeredis` 注入真的 store；覆蓋：
  - 給 `session_id` 且 store 有 prior turns → mock 收到的 `messages` 第 0 個是 system、最後一個是本輪 user、中間是 prior history（順序對）
  - 不給 `session_id` → mock 收到的 `messages` 與舊行為一致（`[system, user]`），且 Redis 沒被寫過任何 key
  - `history_store=None` 但傳 `session_id` → 同樣不讀寫 Redis
  - 一輪完成後（含 tool call → final text 路徑），Redis 內 LIST 長度恰好 `+2`，內容 role 為 user + assistant、`content` 為純文字（不含 `tool_calls` 鍵）
  - LLM 走 timeout / exception 分支回傳 `GENERIC_ERROR_REPLY` 時，Redis 內 LIST 長度 **不變**
  - 同一 session 連續 N+1 輪（N=max_turns）後 `LLEN` = `max_turns*2`，最早一輪被截掉
  - 執行：`cd backend/multiagent-service && uv run pytest tests/test_chat_agent.py -v` 全綠
  - 既有的其他測試套件不應因簽章改動而破裂；同時跑 `cd backend/multiagent-service && uv run pytest -v` 整體確認

## 4. Kafka chat handler 串通 session_id（chat-history capability）

- [x] 4.1 修改 `backend/multiagent-service/src/kafka/consumer.py:handle_chat_request`：原本 `result = _run_async(chat_agent.agenerate(content))` 改為 `result = _run_async(chat_agent.agenerate(content, session_id=session_id or None))`；確保空字串會被轉成 None（fallback 到無狀態）
- [x] 4.2 確認 `consumer.py` 其他位置 `session_id` 變數命名與既有 logging 一致（line 65 已 log，無需改）；`chat.response` publish 區段（line 84-92）**不變**——`correlation_id` / `reply` / `suggested_actions` / optional `route_payload` 維持不新增欄位
- [x] 4.3 派 test engineer sub-agent：擴充**既有** `backend/multiagent-service/tests/test_consumer.py::TestHandleChatRequest`（**不要**新建 `test_kafka_consumer.py`，該檔已含 4 個 case）。新增 / 修改：
  - 既有 case：若任何 `mock_chat_agent.agenerate` 的 assertion 假設只收 `content`，更新成 `mock_chat_agent.agenerate.assert_called_with(content, session_id=...)` 形式
  - 新增 case：mock `chat_agent.agenerate` 為 `AsyncMock`，呼叫 `handle_chat_request(key, {"correlation_id": "c1", "session_id": "s1", "content": "hi"})`，assert `agenerate.await_args.kwargs["session_id"] == "s1"`
  - 新增 case：payload 無 `session_id` 時 `agenerate` 收到 `session_id=None`
  - 新增 case：payload `session_id=""` 同樣由 handler 正規化成 `None`（對應 spec「payload 無 session_id」scenario）
  - 執行：`cd backend/multiagent-service && uv run pytest tests/test_consumer.py -v` 全綠

## 5. 整合驗證

- [x] 5.1 `cd backend/multiagent-service && uv run pytest -v` 全套 test 綠燈（含 §2.5 / §3.6 / §4.3 新增的測試 + 舊有測試）
- [ ] 5.2 **[需 Docker compose / runtime 實機驗證]** 啟動實機驗證：
  ```bash
  docker compose -f infra/docker-compose.yml up -d redis kafka zookeeper timescaledb
  cd backend/multiagent-service && uv run python main.py
  ```
  等 lifespan 印 `[Multiagent Service] All components started successfully!`
- [ ] 5.3 **[需 Docker compose / LLM runtime 實機驗證]** manual demo flow：用 frontend (`frontend/`) 或直接對 Kafka 丟兩則訊息（同 `session_id`），第二則用代名詞引用第一則內容（例如：第一則「我要從台北車站到 101」、第二則「那條路再講一次細節」），驗證第二則 LLM reply 顯示**有用到第一則的 origin/dest 內容**（不會反問「你要去哪裡」）
- [ ] 5.4 **[需 Docker compose / redis-cli 實機驗證]** 在 redis-cli 驗 key 結構與 TTL：
  ```bash
  docker compose -f infra/docker-compose.yml exec redis redis-cli
  > KEYS chat:history:*
  > LRANGE chat:history:<session_id> 0 -1
  > TTL chat:history:<session_id>
  ```
  預期 key 存在、TTL 介於 0 與 1800 之間、每筆 entry 是合法 JSON `{role, content}`
- [ ] 5.5 **[需 Docker compose / LLM runtime 實機驗證]** manual：跑 11 輪對話（超過 max_turns=10），驗 `LLEN chat:history:<session_id>` = 20（一輪兩則）；最早一輪訊息已被 `LTRIM` 截掉
- [ ] 5.6 **[需 Docker compose / Redis failure 實機驗證]** manual：手動 `docker compose -f infra/docker-compose.yml stop redis`，再對 chat 丟訊息 → 預期 chat 仍能回覆（無狀態 fallback）、log 有 warning、service 不 crash；之後 `start redis` 恢復
- [x] 5.7 `openspec validate add-chat-history-redis --strict` 全綠
