## Why

code review 發現 `multiagent-service` 的 Kafka consumer 會讀取 `KAFKA_BOOTSTRAP_SERVERS`，但 producer singleton 固定使用 `localhost:9092`。在 Docker Compose 內，response producer 需要連到 `kafka:29092`，否則 main-service 可以送出 request，但 multiagent-service 無法可靠送回 `chat.response`、`route.response`、`geocode.response`，最後 HTTP 端只會等到 30 秒 timeout。

## What Changes

- 修改 `backend/multiagent-service/src/kafka/producer.py`，讓 producer 與 consumer 一樣從 `KAFKA_BOOTSTRAP_SERVERS` 讀取 broker，預設仍保留 `localhost:9092` 供本機開發使用。
- 補強 producer unit tests：覆蓋 env var 設定、未設定時的本機預設值、singleton reset 後會使用最新設定。
- 視需要補強 Kafka request/response 測試，確認所有 response publishing path 使用同一個可設定 producer。
- 不改 Kafka topic schema、不改 REST API、不改 Docker Compose service names。

## Capabilities

### New Capabilities

- 無。

### Modified Capabilities

- `kafka-messaging`: multiagent-service 的 Kafka producer SHALL 使用部署環境設定的 broker address，並與 consumer 使用同一個 `KAFKA_BOOTSTRAP_SERVERS` contract。

## Impact

- Affected code: `backend/multiagent-service/src/kafka/producer.py`
- Affected tests: `backend/multiagent-service/tests/test_producer.py`，可能包含 Kafka handler tests
- Affected systems: Docker Compose demo stack 中的 main-service ↔ multiagent-service Kafka request/response bridge
- No external dependency or API contract changes
