## 1. Producer Configuration

- [x] 1.1 Update `backend/multiagent-service/src/kafka/producer.py` so Producer config reads `KAFKA_BOOTSTRAP_SERVERS` with `localhost:9092` fallback.
- [x] 1.2 Ensure broker config is resolved when the singleton Producer is created, not hard-coded at module import in a way tests cannot override.
- [x] 1.3 Keep `publish_message()` topic/key/value behavior unchanged.

## 2. Tests

- [x] 2.1 Update `backend/multiagent-service/tests/test_producer.py` default test to expect `localhost:9092` only when env var is absent.
- [x] 2.2 Add a producer test that sets `KAFKA_BOOTSTRAP_SERVERS=kafka:29092` and asserts `Producer({"bootstrap.servers": "kafka:29092"})`.
- [x] 2.3 Add or update tests to reset `_producer` between config cases so singleton caching does not hide config regressions.

## 3. Verification

- [x] 3.1 Run `uv run pytest tests/test_producer.py tests/test_consumer.py` from `backend/multiagent-service`.
- [x] 3.2 Optionally run compose-level manual acceptance: start the stack, issue one chat/route/geocode request, and confirm the HTTP call receives a response before the 30-second timeout.
- [x] 3.3 Check no Kafka topic schema, REST response schema, or Docker Compose service name changed.
