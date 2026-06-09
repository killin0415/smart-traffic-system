## ADDED Requirements

### Requirement: ChatHistoryStore 提供 per-session 對話歷史存取
系統 SHALL 提供 `ChatHistoryStore` 元件，封裝對 Redis 的 chat 對話歷史讀寫，並以 `session_id` 作為隔離單位。

#### Scenario: Redis key 設計
- **WHEN** 對任一 `session_id` 進行讀或寫
- **THEN** SHALL 使用 Redis key `chat:history:{session_id}`
- **AND** 該 key 的 Redis 型別 SHALL 為 LIST

#### Scenario: Entry 編碼格式
- **WHEN** 寫入一則訊息
- **THEN** 該 entry SHALL 為 JSON 字串，至少包含 `role` 與 `content` 兩個欄位
- **AND** `role` SHALL 為 `"user"` 或 `"assistant"`

#### Scenario: 不同 session_id 互不污染
- **WHEN** 同時存在 `session_id=A` 與 `session_id=B` 兩筆歷史
- **THEN** `load("A")` SHALL 只回傳 A 的訊息
- **AND** `load("B")` SHALL 只回傳 B 的訊息

### Requirement: ChatHistoryStore.load 依寫入順序回傳完整歷史
系統 SHALL 提供 `await load(session_id) -> list[dict]`，回傳該 session 目前 Redis 上保存的所有訊息，順序與寫入順序一致（最舊在前）。

#### Scenario: 空 session
- **WHEN** 對從未寫過的 `session_id` 呼叫 `load`
- **THEN** SHALL 回傳空 list `[]`
- **AND** SHALL NOT 丟出 exception

#### Scenario: 非空 session 依序回傳
- **WHEN** 對已寫入過 N 則訊息的 `session_id` 呼叫 `load`
- **THEN** SHALL 回傳長度為當下 Redis 內筆數的 list
- **AND** list 元素順序 SHALL 與 RPUSH 寫入順序一致

#### Scenario: Redis 不可用時不爆 chat 主路徑
- **WHEN** `load` 過程中 Redis 連線失敗或丟 exception
- **THEN** SHALL log warning
- **AND** SHALL 回傳空 list（fallback 到無狀態行為），讓上層 `ChatAgent.agenerate` 仍可完成本輪

### Requirement: ChatHistoryStore.append_turn 一次寫入一輪對話並維持容量上限
系統 SHALL 提供 `await append_turn(session_id, user_msg, assistant_reply)`，在一個 Redis pipeline 內完成 user / assistant 兩則訊息的寫入、容量截斷與 TTL 更新。

#### Scenario: 一個 round-trip 完成
- **WHEN** 呼叫 `append_turn`
- **THEN** SHALL 使用 Redis pipeline 依序執行 `RPUSH` user entry、`RPUSH` assistant entry、`LTRIM`、`EXPIRE`
- **AND** SHALL 在單一 `await pipe.execute()` 內送出

#### Scenario: 容量截斷至最近 max_turns 輪
- **WHEN** session 累積訊息超過 `max_turns * 2` 則（一輪兩則）
- **THEN** 寫入後該 key 內 list 長度 SHALL 等於 `max_turns * 2`
- **AND** 保留的訊息 SHALL 為「最新的」`max_turns * 2` 則

#### Scenario: 每次寫入皆刷新 TTL
- **WHEN** 對任一 `session_id` 呼叫 `append_turn`
- **THEN** 該 key 的 TTL SHALL 被設定為 `ttl_seconds` 秒
- **AND** 後續 `await redis.ttl(key)` SHALL 回傳值在 `(0, ttl_seconds]` 區間內

#### Scenario: Redis 失敗不阻斷本輪 chat
- **WHEN** `append_turn` 過程中 Redis 拋 exception
- **THEN** SHALL log warning
- **AND** SHALL NOT 將 exception 往外丟（`ChatAgent.agenerate` 已產生的 reply 仍正常回傳給 caller）

### Requirement: ChatHistoryStore 只接受 user / assistant 純文字訊息
系統 SHALL NOT 將 LLM tool-call loop 中的中間訊息（`assistant` with `tool_calls`、`role: "tool"` 的 result）寫入 Redis。

#### Scenario: 跳過 tool 結果
- **WHEN** `ChatAgent.agenerate` 在一輪內觸發 tool call 並收到 `plan_route` 結果
- **THEN** Redis 內該 session 的 list SHALL NOT 出現 `role: "tool"` 的 entry
- **AND** SHALL NOT 出現包含 `tool_calls` 欄位的 assistant entry

#### Scenario: 只 append 一輪兩則
- **WHEN** 一次 `agenerate` 呼叫完成（不論中間是否有 tool call）
- **THEN** Redis 該 session 的 list 長度 SHALL 至多增加 2（user + final assistant text）

### Requirement: ChatAgent 注入 ChatHistoryStore 並 per-session_id 載入 / append
系統 SHALL 修改 `ChatAgent.__init__` 接受 `history_store: ChatHistoryStore | None`，並修改 `agenerate(content, session_id=None)` 使其在 `session_id` 與 `history_store` 都齊備時使用歷史。

#### Scenario: 歷史 prepend 順序
- **WHEN** `agenerate(content, session_id=S)` 呼叫且 `S` 對應的 Redis 歷史含 prior turns
- **THEN** 送進 LLM 的 `messages` SHALL 為 `[system_instruction, *history, {role:"user", content}]`
- **AND** history 的順序 SHALL 與 Redis LIST 寫入順序一致（最舊在前）

#### Scenario: 一輪結束後 append
- **WHEN** `agenerate(content, session_id=S)` 成功取得 final assistant text reply
- **THEN** 在 `return` 之前 SHALL 呼叫 `history_store.append_turn(S, user_msg, assistant_msg)`
- **AND** `user_msg.content` SHALL 等於本輪 `content` 參數
- **AND** `assistant_msg.content` SHALL 等於本輪回傳的 `reply`

#### Scenario: session_id 缺省 → 無狀態 fallback
- **WHEN** `agenerate(content)` 不傳 `session_id`（或傳 `None` / 空字串）
- **THEN** SHALL NOT 對 Redis 讀任何 key
- **AND** SHALL NOT 對 Redis 寫任何 key
- **AND** 行為 SHALL 與既有無狀態實作一致

#### Scenario: history_store 缺省 → 無狀態 fallback
- **WHEN** `ChatAgent` 在 `history_store=None` 的情況下被建構
- **THEN** 不論 `session_id` 是否傳入，SHALL NOT 對 Redis 進行任何讀寫
- **AND** 行為 SHALL 與既有無狀態實作一致

#### Scenario: LLM 呼叫失敗時不寫歷史
- **WHEN** `agenerate` 因 timeout / exception 走進 error 分支回傳 `GENERIC_ERROR_REPLY` 或 `FALLBACK_REPLY` 或 `SERVICE_NOT_READY_REPLY`
- **THEN** SHALL NOT 對 Redis append 本輪
- **AND** 下一次 `load(session_id)` 結果 SHALL 不含本次失敗訊息

#### Scenario: LLM 成功路徑但 reply 為 placeholder 字串時不寫歷史
- **WHEN** `agenerate` 走完 LLM 流程但最終 `reply` 是系統 canned placeholder（`"（已收到您的訊息）"` ＝ LLM 回空字串、`"（已執行工具但回覆超過內部上限）"` ＝ `max_tool_loops` 用完）
- **THEN** SHALL NOT 對 Redis append 本輪
- **AND** 下一次 `load(session_id)` 結果 SHALL 不含本次 placeholder 訊息
- **AND** 此判定 SHALL 透過比對 module-level 常數集合實作，不採模糊 string match

### Requirement: Kafka chat handler 將 session_id 傳入 ChatAgent
系統 SHALL 修改 `backend/multiagent-service/src/kafka/consumer.py:handle_chat_request`，把從 `chat.request` payload 取得的 `session_id` 透過 keyword argument 傳入 `chat_agent.agenerate`。

#### Scenario: session_id 透傳
- **WHEN** Kafka 收到 `chat.request` payload 含非空 `session_id`
- **THEN** `handle_chat_request` SHALL 呼叫 `chat_agent.agenerate(content, session_id=<該值>)`

#### Scenario: payload 無 session_id 或為空字串時正規化為 None
- **WHEN** Kafka 收到的 `chat.request` payload 沒有 `session_id` 鍵
- **THEN** `handle_chat_request` SHALL 呼叫 `chat_agent.agenerate(content, session_id=None)`

#### Scenario: payload 帶空字串 session_id
- **WHEN** Kafka 收到的 `chat.request` payload `session_id` 為空字串 `""`
- **THEN** `handle_chat_request` SHALL 在 handler 端先正規化為 `None`，再呼叫 `chat_agent.agenerate(content, session_id=None)`
- **AND** SHALL NOT 將空字串直接傳給 `agenerate`（避免 `agenerate` 內部對「空字串」與「None」做不同判定）

#### Scenario: chat.response 介面不變
- **WHEN** 處理完任一 `chat.request`（含或不含 session_id）
- **THEN** 發到 `chat.response` 的 payload schema SHALL 與現有定義一致（`correlation_id`、`reply`、`suggested_actions`、optional `route_payload`），SHALL NOT 新增欄位

### Requirement: 環境變數設定 CHAT_HISTORY_MAX_TURNS 與 CHAT_HISTORY_TTL_SECONDS
系統 SHALL 透過環境變數 `CHAT_HISTORY_MAX_TURNS` 與 `CHAT_HISTORY_TTL_SECONDS` 設定歷史容量與 TTL，並提供合理預設值。

#### Scenario: CHAT_HISTORY_MAX_TURNS 預設
- **WHEN** 環境變數 `CHAT_HISTORY_MAX_TURNS` 未設定
- **THEN** SHALL 使用預設值 `10`（代表最近 10 輪 = 20 則訊息）

#### Scenario: CHAT_HISTORY_TTL_SECONDS 預設
- **WHEN** 環境變數 `CHAT_HISTORY_TTL_SECONDS` 未設定
- **THEN** SHALL 使用預設值 `1800`（30 分鐘）

#### Scenario: 環境變數覆寫
- **WHEN** 啟動時 `CHAT_HISTORY_MAX_TURNS=5` 且 `CHAT_HISTORY_TTL_SECONDS=600`
- **THEN** `ChatHistoryStore` 實例 SHALL 以 `max_turns=5`、`ttl_seconds=600` 運作
- **AND** Redis LIST 容量上限 SHALL 為 `5 * 2 = 10` 則

#### Scenario: 不引入額外 runtime 依賴或 infra 變動
- **WHEN** 部署本 capability
- **THEN** SHALL NOT 修改 `infra/docker-compose.yml`
- **AND** SHALL NOT 新增 runtime Python 依賴（沿用既有 `redis>=7.1.0` 與 `src/cache/__init__.py` 的 `redis_client`）
- **AND** dev 測試依賴 `fakeredis` SHALL 透過 `uv add fakeredis --dev` 加入 `pyproject.toml` 的 dev group
