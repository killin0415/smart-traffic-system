# multiagent-service

FastAPI + Kafka multi-agent service for the smart-traffic-system capstone.
Owns A* route planning, TDX live traffic ingestion, and the Gemini chat
agent that fronts user queries.

## Run

```bash
# from this directory
uv sync
uv run python main.py
```

The service exposes:
- `GET /health` — health check
- Kafka consumer thread subscribing to `chat.request` + `route.request` (configurable, see below)

## Required environment variables

| Var | Purpose | Notes |
| --- | --- | --- |
| `TDX_CLIENT_ID` / `TDX_CLIENT_SECRET` | TDX OAuth2 (road network seed + live polling) | Required for any traffic data |
| `DEEPSEEK_API_KEY` | DeepSeek API key for the chat agent | Optional — missing key triggers fallback stub reply |
| `DATABASE_URL` | PostgreSQL/TimescaleDB connection | Defaults to `postgresql+asyncpg://admin:secret@localhost:5432/traffic_data` |
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka brokers | Default `localhost:9092` |
| `KAFKA_CONSUMER_GROUP` | Consumer group ID | Default `multiagent-service-group` |
| `KAFKA_SUBSCRIBE_TOPICS` | Comma-separated topic list | Default `chat.request,route.request`. Empty / whitespace falls back to default |
| `TDX_LIVE_REFRESH_SECONDS` | TDX live polling interval | Default `300` |
| `DEEPSEEK_MODEL` | DeepSeek model id | Default `deepseek-v4-flash` (cheapest tier) |
| `DEEPSEEK_BASE_URL` | DeepSeek API base URL | Default `https://api.deepseek.com` |
| `DEEPSEEK_TIMEOUT_SECONDS` | Per-call timeout | Default `30` |
| `DEEPSEEK_MAX_TOKENS` | Per-call output token cap (cost guard) | Default `500` |
| `DEEPSEEK_MAX_TOOL_LOOPS` | Max LLM round-trips per chat turn (cost guard) | Default `3` |

## Chat agent flow

1. User publishes a chat message to `chat.request` (Kafka).
2. `handle_chat_request` forwards it to `ChatAgent.agenerate(content)`.
3. `ChatAgent` calls DeepSeek (OpenAI-compatible API) with the `plan_route` tool registered.
4. If DeepSeek decides the user expressed a routing intent it calls `plan_route(origin_lat, origin_lng, dest_lat, dest_lng, top_k)`. The result is captured, fed back as a `tool` message, and the model is called again for a final natural-language summary (capped at `DEEPSEEK_MAX_TOOL_LOOPS` rounds).
5. The handler publishes to `chat.response`:
   - `reply` — DeepSeek's natural-language answer
   - `suggested_actions` — static UI prompts
   - `route_payload` — the `plan_route` JSON dict, **only when** the agent actually invoked the tool with a successful result. Omitted otherwise (forward-compatible: downstream may treat as plain text).

When `DEEPSEEK_API_KEY` is missing, the chat agent returns a stub reply and never invokes the tool — the service still starts cleanly.

## Adding a new Kafka-consumer microservice integration

1. Add a handler function `handle_my_topic(key: str, data: dict)` in `src/kafka/consumer.py` (or import from another module).
2. Register it in `TOPIC_HANDLERS["my.topic"] = handle_my_topic`.
3. Make sure `my.topic` appears in `KAFKA_SUBSCRIBE_TOPICS` for the deployment.

The dispatcher tolerates unknown topics, malformed JSON, and handler exceptions without crashing — see `tests/test_consumer_extensibility.py`.
